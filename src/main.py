# -*- coding: utf-8 -*-
import mimetypes
import logging
import asyncio
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import parse_qs, quote, urlparse
from pathlib import Path
import xml.etree.ElementTree as ET

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
from src.downloader import run_downloader, request_stop, request_stop_reset
from src.jobs import start_cron_jobs, stop_cron_jobs


log = logging.getLogger(__name__)
_PODCAST_METADATA_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_PODCAST_METADATA_TTL_SECONDS = 3600.0

app = FastAPI(debug=True)


async def on_startup() -> None:
    check_config_file()
    init_database()
    request_stop_reset()
    config = get_config()
    await asyncio.gather(*[run_downloader(podcast) for podcast in config["podcasts"]])
    log.debug("Database initialized")
    start_cron_jobs()


async def on_shutdown() -> None:
    request_stop()
    stop_cron_jobs()


app.add_event_handler("startup", on_startup)
app.add_event_handler("shutdown", on_shutdown)
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


def _metadata_cache_key(url: str, sid: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc}|{parsed.path}|{sid}"


def _is_collect_url(url: str) -> bool:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("type", [""])[0] == "season":
        return True
    return False


def _ensure_rss_root_namespace(rss_xml: str) -> str:
    marker = "<rss"
    start = rss_xml.find(marker)
    if start < 0:
        return rss_xml

    end = rss_xml.find(">", start)
    if end < 0:
        return rss_xml

    root_open_tag = rss_xml[start:end+1]
    if 'xmlns:atom="' not in root_open_tag:
        root_open_tag = root_open_tag[:-1] + ' xmlns:atom="http://www.w3.org/2005/Atom">'
    if 'xmlns:itunes="' not in root_open_tag:
        root_open_tag = root_open_tag[:-1] + ' xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
    if ' version="' not in root_open_tag:
        root_open_tag = root_open_tag[:-1] + ' version="2.0">'

    return rss_xml[:start] + root_open_tag + rss_xml[end+1:]


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
            ("cover", "cover_url", "pic"),
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

    now = monotonic()
    cache_key = _metadata_cache_key(url, sid)
    cache_item = _PODCAST_METADATA_CACHE.get(cache_key)
    if cache_item is not None:
        expires_at, cached_data = cache_item
        if now < expires_at:
            return cached_data

    async with httpx.AsyncClient(**api.dft_client_settings) as client:
        try:
            if _is_collect_url(url):
                data = await _fetch_collect_metadata(client, sid)
            elif "series" in url:
                data = await _fetch_list_metadata(client, sid)
            else:
                data = {}
            _PODCAST_METADATA_CACHE[cache_key] = (now + _PODCAST_METADATA_TTL_SECONDS, data)
            return data
        except Exception:
            log.exception("Bilibili 播客元信息获取失败，回退到配置值")
    return {}


def _append_channel_and_episode_images(rss_xml: str, episodes: list[dict], image: str | None = None) -> str:
    try:
        root = ET.fromstring(rss_xml.encode("utf-8"))
        ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
        channel = root.find("channel")
        if channel is None:
            return _ensure_rss_root_namespace(rss_xml)

        if image:
            channel_image = channel.find("image")
            if channel_image is None:
                channel_image = ET.SubElement(channel, "image")
            channel_image_url = channel_image.find("url")
            if channel_image_url is None:
                ET.SubElement(channel_image, "url").text = image
            else:
                channel_image_url.text = image

            itunes_image = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            if itunes_image is None:
                itunes_image = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            itunes_image.attrib["href"] = image

        items = channel.findall("item")
        for item, episode in zip(items, episodes):
            cover_image = episode.get("cover_image_url")
            if not cover_image:
                continue
            cover_xml = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            cover_xml.attrib["href"] = cover_image

        return _ensure_rss_root_namespace(ET.tostring(root, encoding="unicode", xml_declaration=False))
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
        media_file = Path("downloads") / name / episode["file_name"]
        file_size = media_file.stat().st_size if media_file.exists() else 0
        entry.enclosure(str(episode_url), file_size, media_type or "audio/mpeg")
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
    rss = _append_channel_and_episode_images(rss, episodes, channel_image)
    return Response(content=rss.encode("utf-8"), media_type="application/rss+xml; charset=utf-8")


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
