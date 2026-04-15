# -*- coding: utf-8 -*-
from __future__ import annotations

import mimetypes
import logging
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from feedgen.feed import FeedGenerator
from src.config import check_config_file, get_config
from src.database import get_episodes, get_podcast, init_database
from src.downloader import run_downloader, request_stop, request_stop_reset
from src.jobs import start_cron_jobs, stop_cron_jobs


log = logging.getLogger(__name__)


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
    _BRANDING = "由 bilicast 生成 · https://github.com/Fatpandac/bilicast"
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.id(channel_rss_url)
    fg.title(channel_title)
    fg.link(href=channel_link)
    fg.description(channel_description)
    if channel_image:
        fg.podcast.itunes_image(channel_image)  # type: ignore[attr-defined]

    for ep in episodes:
        encoded_name = quote(ep["file_name"])
        ep_url = str(request.url_for("podcast_media", name=name, file_name=encoded_name))
        media_type, _ = mimetypes.guess_type(ep["file_name"])
        media_file = Path("downloads") / name / ep["file_name"]
        file_size = media_file.stat().st_size if media_file.exists() else 0

        fe = fg.add_entry()
        fe.id(ep["episode_id"])
        fe.title(ep["title"] or ep["file_name"])
        fe.link(href=ep_url)
        fe.enclosure(ep_url, file_size, media_type or "audio/mpeg")
        desc = ep.get("description") or ""
        fe.description(f"{desc}\n\n{_BRANDING}" if desc else _BRANDING)
        if ep.get("cover_image_url"):
            fe.podcast.itunes_image(ep["cover_image_url"])  # type: ignore[attr-defined]

        raw_pub = ep.get("published_at") or ep.get("created_at")
        if raw_pub:
            try:
                pub_dt = datetime.fromisoformat(str(raw_pub))
            except ValueError:
                pub_dt = datetime.strptime(str(raw_pub), "%Y-%m-%d %H:%M:%S")
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            fe.published(pub_dt)

    return fg.rss_str(pretty=True)


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
    podcast_row = get_podcast(name)
    channel_title = (podcast_row and podcast_row.get("channel_title")) or config["name"]
    channel_description = (
        (podcast_row and podcast_row.get("channel_description"))
        or "由 bilicast 生成 · https://github.com/Fatpandac/bilicast"
    )
    channel_image: str | None = podcast_row and podcast_row.get("channel_image") or None

    episodes = get_episodes(
        name,
        limit=config["keep_latest"],
        sort_by=config["sort_by"],
        sort_order=config["sort_order"],
    )
    if not channel_image and episodes:
        channel_image = episodes[0].get("cover_image_url")

    rss_bytes = _build_rss(
        channel_title=channel_title,
        channel_link=str(request.url_for("podcast_info", name=name)),
        channel_rss_url=str(request.url_for("podcast_rss", name=name)),
        channel_description=channel_description,
        channel_image=channel_image,
        episodes=episodes,
        name=name,
        request=request,
    )
    return Response(content=rss_bytes, media_type="application/rss+xml; charset=utf-8")


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
