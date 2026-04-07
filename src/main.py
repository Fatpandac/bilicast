# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `uv run src/main.py` (script mode) to import project package `src.*`.
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.config import check_config_file, get_config
from src.database import init_database
from src.jobs import start_cron_jobs


log = logging.getLogger(__name__)


async def on_startup() -> None:
    check_config_file()
    init_database()
    log.debug("Database initialized")
    start_cron_jobs()


config = get_config()
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


def podcast_exists(name: str):
    return name in list(map(lambda podcast: podcast["name"], config["podcasts"]))


@app.get("/podcasts/{name}")
async def podcasts(name: str):
    if podcast_exists(name):
        return {"message": f"Hello {name}!"}
    else:
        return {"message": f"Podcast {name} not found"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
