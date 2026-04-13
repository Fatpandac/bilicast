# -*- coding: utf-8 -*-
import mimetypes
import logging
import asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from feedgen.feed import FeedGenerator
from bilix.sites.bilibili import api
import httpx
from src.config import check_config_file, get_config
from src.database import get_episodes, init_database
from src.downloader import run_downloader
from src.jobs import start_cron_jobs


log = logging.getLogger(__name__)


async def on_startup() -> None:
    check_config_file()
    init_database()
    config = get_config()
    await asyncio.gather(*[asyncio.to_thread(run_downloader, podcast) for podcast in config["podcasts"]])
    log.debug("Database initialized")
    start_cron_jobs()


app = FastAPI(on_startup=[on_startup], debug=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Hello World"}


def _get_podcasts_from_file():
    return get_config()["podcasts"]


def podcast_exists(name: str):
    return name in list(map(lambda podcast: podcast["name"], _get_podcasts_from_file()))


def _first_non_empty(mapping: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_sid(url: str) -> str | None:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for idx, part in enumerate(path_parts):
        if part == "lists" and idx + 1 < len(path_parts):
            return path_parts[idx + 1]

    qs_sid = parse_qs(parsed.query).get("sid")
    if qs_sid and qs_sid[0].isdigit():
        return qs_sid[0]

    return None


async def _fetch_collect_metadata(client: httpx.AsyncClient, sid: str) -> dict[str, str]:
    res = await client.get("https://api.bilibili.com/x/space/fav/season/list", params={"season_id": sid})
    res.raise_for_status()
    data = res.json()
    payload = data.get("data", {})
    info: dict = payload.get("info", {})
    medias = payload.get("medias", [])
    media = medias[0] if medias else {}
    return {
        "title": _first_non_empty(info, ("title", "name")),
        "description": _first_non_empty(info, ("intro", "description")),
        "image": _first_non_empty(
            {
                **media,
                **info,
            },
            ("cover", "cover_url", "pic", "upper",),
        ),
    }


async def _fetch_list_metadata(client: httpx.AsyncClient, sid: str) -> dict[str, str]:
    res = await client.get(f"https://api.bilibili.com/x/series/series?series_id={sid}")
    res.raise_for_status()
    data = res.json()
    meta = data.get("data", {}).get("meta", {})
    return {
        "title": _first_non_empty(meta, ("name", "title")),
        "description": _first_non_empty(meta, ("description", "intro")),
        "image": _first_non_empty(meta, ("cover", "cover_url")),
    }


async def _get_podcast_metadata(url: str) -> dict[str, str]:
    sid = _extract_sid(url)
    if not sid:
        return {}

    async with httpx.AsyncClient(**api.dft_client_settings) as client:
        try:
            if "collection" in url:
                return await _fetch_collect_metadata(client, sid)
            if "series" in url:
                return await _fetch_list_metadata(client, sid)
        except Exception:
            log.exception("Bilibili 播客元信息获取失败，回退到配置值")
    return {}


def _append_channel_and_episode_images(rss_xml: str, episodes: list[dict], image: str | None = None) -> str:
    if not episodes:
        if not image:
            return rss_xml
        try:
            ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
            root = ET.fromstring(rss_xml.encode("utf-8"))
            channel = root.find("channel")
            if channel is None:
                return rss_xml
            if image:
                image_xml = ET.SubElement(channel, "image")
                ET.SubElement(image_xml, "url").text = image
            itunes_xml = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            itunes_xml.attrib["href"] = image
            return ET.tostring(root, encoding="unicode", xml_declaration=False)
        except Exception:
            log.exception("RSS channel image injection failed")
        return rss_xml

    try:
        ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
        root = ET.fromstring(rss_xml.encode("utf-8"))
        channel = root.find("channel")
        if channel is None:
            return rss_xml

        if image:
            channel_image = channel.find("image")
            if channel_image is None:
                channel_image = ET.SubElement(channel, "image")
            ET.SubElement(channel_image, "url").text = image

            itunes_image = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            if itunes_image is None:
                itunes_image = ET.SubElement(
                    channel,
                    "{http://www.itunes.com/dtds/podcast-1.0.dtd}image",
                )
            itunes_image.attrib["href"] = image

    except Exception:
        log.exception("RSS channel image injection failed")
        return rss_xml

    try:
        root = ET.fromstring(rss_xml.encode("utf-8"))
        channel = root.find("channel")
        if channel is None:
            return rss_xml

        items = channel.findall("item")
        for item, episode in zip(items, episodes):
            cover_image = episode.get("cover_image_url")
            if not cover_image:
                continue
            cover_xml = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            cover_xml.attrib["href"] = cover_image

        return ET.tostring(root, encoding="unicode", xml_declaration=False)
    except Exception:
        log.exception("RSS cover image injection failed")
        return rss_xml


@app.get("/podcasts")
async def podcasts():
    return {"podcasts": _get_podcasts_from_file()}


@app.get("/podcasts/{name}", name="podcast_info")
async def podcasts_with_name(name: str, request: Request):
    if podcast_exists(name):
        config = list(filter(lambda it: it["name"] == name, _get_podcasts_from_file()))[0]
        return {
            "name": name,
            "rss": str(request.url_for("podcast_rss", name=name)),
            "config": config,
        }
    else:
        raise HTTPException(status_code=404, detail=f"Podcast {name} not found")


@app.get("/media/{name}/{file_name}")
async def podcast_media(name: str, file_name: str):
    media_dir = Path("downloads") / name
    media_file = media_dir / Path(file_name).name
    if not media_file.exists():
        raise HTTPException(status_code=404, detail="media not found")
    media_type, _ = mimetypes.guess_type(media_file.name)
    return FileResponse(media_file, media_type=media_type or "application/octet-stream")


@app.get("/rss/{name}", name="podcast_rss")
async def podcast_rss(name: str, request: Request):
    if not podcast_exists(name):
        raise HTTPException(status_code=404, detail=f"Podcast {name} not found")

    config = list(filter(lambda it: it["name"] == name, _get_podcasts_from_file()))[0]
    podcast_metadata = await _get_podcast_metadata(config["url"])
    channel_title = podcast_metadata.get("title") or config["name"]
    channel_description = podcast_metadata.get("description") or f"Podcasts from {name}"
    channel_image = podcast_metadata.get("image")

    episodes = get_episodes(name, limit=config["keep_latest"])
    feed = FeedGenerator()
    feed.title(channel_title)
    feed.link(href=str(request.url_for("podcast_info", name=name)))
    feed.description(channel_description)
    feed.id(str(request.url_for("podcast_rss", name=name)))
    if not channel_image and episodes:
        channel_image = episodes[0].get("cover_image_url") or episodes[0].get("cover")

    for episode in episodes:
        encoded_file_name = quote(episode["file_name"])
        episode_url = request.url_for("podcast_media", name=name, file_name=encoded_file_name)
        media_type, _ = mimetypes.guess_type(episode["file_name"])
        entry = feed.add_entry()
        entry.id(episode["episode_id"])
        entry.title(episode["title"] or episode["file_name"])
        entry.link(href=str(episode_url))
        if episode["description"]:
            entry.description(episode["description"])
        entry.enclosure(str(episode_url), 0, media_type or "audio/mpeg")
        if episode["published_at"] or episode["created_at"]:
            raw_pub_time = episode["published_at"] or episode["created_at"]
            try:
                published_at = datetime.fromisoformat(str(raw_pub_time))
            except ValueError:
                published_at = datetime.strptime(str(raw_pub_time), "%Y-%m-%d %H:%M:%S")
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            entry.published(published_at)

    rss = feed.rss_str(pretty=True)
    if isinstance(rss, bytes):
        rss = rss.decode("utf-8")
    if channel_image:
        feed.image(channel_image)
    rss = _append_channel_and_episode_images(rss, episodes, channel_image)
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")


if __name__ == "__main__":
    main()
