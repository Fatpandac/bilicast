# -*- coding: utf-8 -*-
from apscheduler.executors.base import logging
from src.database import get_all_podcasts

log = logging.getLogger(__name__)


def download(url: str) -> None:
    log.info(f"Downloading {url}")


def run_downloader() -> None:
    all_podcast_name = get_all_podcasts()
    for podcast in all_podcast_name:
        download(podcast)
