# -*- coding: utf-8 -*-
from __future__ import annotations

import mimetypes
import logging
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# Allow `uv run src/main.py` (script mode) to import project package `src.*`.
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from feedgen.feed import FeedGenerator
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


@app.get("/podcasts")
async def podcasts():
    return {"podcasts": _get_podcasts_from_file()}


@app.get("/podcasts/{name}", name="podcast_info")
async def podcasts(name: str, request: Request):
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
    episodes = get_episodes(name, limit=config["keep_latest"])
    feed = FeedGenerator()
    feed.title(f"{name} Podcast")
    feed.link(href=str(request.url_for("podcast_info", name=name)))
    feed.description(f"Podcasts from {name}")
    feed.id(str(request.url_for("podcast_rss", name=name)))

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
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
