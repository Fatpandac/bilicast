# -*- coding: utf-8 -*-
from __future__ import annotations

import email.utils
import mimetypes
import logging
import asyncio
import os
from contextlib import asynccontextmanager
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
from bilix.sites.bilibili import api
import httpx

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", _ITUNES_NS)
ET.register_namespace("atom", _ATOM_NS)
from src.config import check_config_file, get_config
from src.database import get_episodes, init_database
from src.downloader import run_downloader, request_stop, request_stop_reset
from src.jobs import start_cron_jobs, stop_cron_jobs


log = logging.getLogger(__name__)
_PODCAST_METADATA_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_PODCAST_METADATA_TTL_SECONDS = 3600.0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_config_file()
    init_database()
    request_stop_reset()
    config = get_config()
    await asyncio.gather(*[run_downloader(podcast) for podcast in config["podcasts"]])
    log.debug("Database initialized")
    start_cron_jobs()
    yield
    request_stop()
    stop_cron_jobs()


app = FastAPI(
    debug=os.getenv("DEBUG", "").lower() in ("1", "true", "yes"),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    podcasts = _get_podcasts_from_file()
    return {"status": "ok", "podcast_count": len(podcasts)}


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


def _build_rss(
    channel_title: str,
    channel_link: str,
    channel_rss_url: str,
    channel_description: str,
    channel_image: str | None,
    episodes: list[dict],
    name: str,
    request: Request,
) -> bytes:
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = channel_title
    ET.SubElement(ch, "link").text = channel_link
    ET.SubElement(ch, "description").text = channel_description
    ET.SubElement(ch, f"{{{_ATOM_NS}}}link", {
        "href": channel_rss_url, "rel": "self", "type": "application/rss+xml",
    })

    if channel_image:
        img_el = ET.SubElement(ch, "image")
        ET.SubElement(img_el, "url").text = channel_image
        ET.SubElement(img_el, "title").text = channel_title
        ET.SubElement(img_el, "link").text = channel_link
        ET.SubElement(ch, f"{{{_ITUNES_NS}}}image", {"href": channel_image})

    for ep in episodes:
        encoded_name = quote(ep["file_name"])
        ep_url = str(request.url_for("podcast_media", name=name, file_name=encoded_name))
        media_type, _ = mimetypes.guess_type(ep["file_name"])
        media_file = Path("downloads") / name / ep["file_name"]
        file_size = media_file.stat().st_size if media_file.exists() else 0

        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = ep["title"] or ep["file_name"]
        ET.SubElement(item, "link").text = ep_url
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep["episode_id"]
        ET.SubElement(item, "enclosure", {
            "url": ep_url,
            "length": str(file_size),
            "type": media_type or "audio/mpeg",
        })
        if ep.get("description"):
            ET.SubElement(item, "description").text = ep["description"]
        if ep.get("cover_image_url"):
            ET.SubElement(item, f"{{{_ITUNES_NS}}}image", {"href": ep["cover_image_url"]})

        raw_pub = ep.get("published_at") or ep.get("created_at")
        if raw_pub:
            try:
                pub_dt = datetime.fromisoformat(str(raw_pub))
            except ValueError:
                pub_dt = datetime.strptime(str(raw_pub), "%Y-%m-%d %H:%M:%S")
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(pub_dt)

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


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

    episodes = get_episodes(
        name,
        limit=config["keep_latest"],
        sort_by=config["sort_by"],
        sort_order=config["sort_order"],
    )
    if not channel_image and episodes:
        channel_image = episodes[0].get("cover_image_url")

    rss = _build_rss(
        channel_title=channel_title,
        channel_link=str(request.url_for("podcast_info", name=name)),
        channel_rss_url=str(request.url_for("podcast_rss", name=name)),
        channel_description=channel_description,
        channel_image=channel_image,
        episodes=episodes,
        name=name,
        request=request,
    )
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
