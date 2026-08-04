# -*- coding: utf-8 -*-
from typing import Literal, TypedDict
from apscheduler.executors.base import logging
from pathlib import Path
import yaml


class Podcast(TypedDict):
    name: str
    url: str
    update_period_cron: str
    keep_latest: int | None  # 不填则保留全部已下载剧集，仅首次抓取最新若干集
    sort_by: Literal["date", "title"]
    sort_order: Literal["asc", "desc"]
    yt_dlp_options: dict  # 透传给 yt-dlp 的额外参数，覆盖内置默认值


class ServerConfig(TypedDict, total=False):
    host: str
    port: int


class YouTubeConfig(TypedDict, total=False):
    cookies_file: str
    cookies_from_browser: str


class Config(TypedDict):
    podcasts: list[Podcast]
    server: ServerConfig
    youtube: YouTubeConfig


log = logging.getLogger(__name__)

__configFile = "config.yaml"


def __get_config_file() -> str:
    config_path = Path(__file__).resolve().parents[1] / __configFile
    if not config_path.exists():
        raise FileNotFoundError(
            "Missing config.yaml. 请先复制 config.yaml.example 为 config.yaml 并按需修改后重试。"
        )
    return str(config_path)


def __check_podcast_name_is_unique(podcasts: list[Podcast]):
    return set(podcast["name"] for podcast in podcasts).__len__() == len(podcasts)


def __apply_podcast_defaults(podcast: dict) -> Podcast:
    """补全可选字段：keep_latest 缺省为 None（不清理旧集），排序缺省按最新发布。"""
    keep_latest = podcast.get("keep_latest")
    return {
        **podcast,
        "keep_latest": int(keep_latest) if keep_latest is not None else None,
        "sort_by": podcast.get("sort_by") or "date",
        "sort_order": podcast.get("sort_order") or "desc",
        "yt_dlp_options": podcast.get("yt_dlp_options") or {},
    }  # type: ignore[return-value]


def check_config_file():
    with open(__get_config_file(), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not __check_podcast_name_is_unique(config["podcasts"]):
        raise Exception("Podcast name must be unique")


def get_config() -> Config:
    with open(__get_config_file(), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["podcasts"] = [
        __apply_podcast_defaults(podcast) for podcast in config["podcasts"]
    ]
    return config
