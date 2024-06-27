# -*- coding: utf-8 -*-
from typing import Literal, TypedDict
from apscheduler.executors.base import logging
import yaml


class Podcast(TypedDict):
    name: str
    url: str
    update_period_cron: str
    keep_latest: int
    sort_by: Literal["date", "title"]
    sort_order: Literal["asc", "desc"]


class Config(TypedDict):
    podcasts: list[Podcast]


log = logging.getLogger(__name__)

__configFile = "config.yaml"


def __check_podcast_name_is_unique(podcasts: list[Podcast]):
    return set(podcast["name"] for podcast in podcasts).__len__() == len(podcasts)


def check_config_file():
    with open(__configFile, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not __check_podcast_name_is_unique(config["podcasts"]):
        raise Exception("Podcast name must be unique")


def get_config() -> Config:
    with open(__configFile, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config
