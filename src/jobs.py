# -*- coding: utf-8 -*-
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from src.config import get_config

log = logging.getLogger(__name__)


def get_video_info(url: str):
    log.info(url)


def start_cron_jobs():
    config = get_config()
    scheduler = BackgroundScheduler()
    for podcast in config["podcasts"]:
        log.info(f"Creating job for {podcast['name']}")
        scheduler.add_job(
            get_video_info,
            CronTrigger.from_crontab(podcast["update_period_cron"]),
            args=[podcast["url"]],
            id=podcast["name"],
        )
    scheduler.start()
