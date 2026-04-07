# -*- coding: utf-8 -*-
import logging

from src.config import get_config
from src.downloader import run_downloader
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)


def start_cron_jobs():
    config = get_config()
    scheduler = BackgroundScheduler()
    for podcast in config["podcasts"]:
        log.info(f"Creating job for {podcast['name']}")
        scheduler.add_job(
            run_downloader,
            CronTrigger.from_crontab(podcast["update_period_cron"]),
            id=podcast["name"],
        )
    scheduler.start()
