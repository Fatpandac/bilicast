# -*- coding: utf-8 -*-
import logging

from src.config import get_config
from src.downloader import run_downloader
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)


_scheduler: AsyncIOScheduler | None = None


def start_cron_jobs():
    config = get_config()
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = AsyncIOScheduler()
    for podcast in config["podcasts"]:
        log.info(f"Creating job for {podcast['name']}")
        scheduler.add_job(
            run_downloader,
            CronTrigger.from_crontab(podcast["update_period_cron"]),
            args=[podcast],
            id=podcast["name"],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_cron_jobs() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
