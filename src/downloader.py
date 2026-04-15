# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bilix.sites.bilibili import api
from bilix.sites.bilibili.downloader import DownloaderBilibili

from src.config import Podcast
from src.database import cleanup_old_episodes, get_podcast_by_episode, get_podcast, save_episode

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_cancel_downloads = threading.Event()

DOWNLOADS_DIR = Path("downloads")
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus", ".mp4"}


def __audio_files_in(dirpath: Path) -> set[Path]:
    return {
        path
        for path in dirpath.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    }


async def __collect_episodes(podcast: Podcast) -> list[dict[str, str]]:
    url = podcast["url"]
    episodes: list[dict[str, str]] = []

    sid_from_path = None
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for idx, part in enumerate(path_parts):
        if part == "lists" and idx + 1 < len(path_parts):
            sid_from_path = path_parts[idx + 1]
            break
    if not sid_from_path:
        qs_sid = parse_qs(parsed.query).get("sid")
        if qs_sid and qs_sid[0].isdigit():
            sid_from_path = qs_sid[0]

    async with httpx.AsyncClient(**api.dft_client_settings) as client:
        if sid_from_path or "collection" in url:
            sid = sid_from_path
            try:
                _, _, bvids = await api.get_collect_info(client, sid if sid else url)
            except Exception:
                if "series" in url:
                    _, _, bvids = await api.get_list_info(client, url)
                elif sid:
                    _, _, bvids = await api.get_list_info(client, sid)
                else:
                    raise ValueError(f"Unsupported bilibili URL: {url}")
        elif "series" in url:
            _, _, bvids = await api.get_list_info(client, url)
        else:
            raise ValueError(f"Unsupported bilibili URL: {url}")

        if podcast["sort_order"] == "asc":
            bvids = list(reversed(bvids))
        for bvid in bvids:
            video_url = f"https://www.bilibili.com/video/{bvid}"
            video_info = await api.get_video_info(client, video_url)

            published_at = None
            try:
                resp = await client.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid},
                )
                vdata = resp.json().get("data") or {}
                if vdata.get("pubdate"):
                    published_at = datetime.fromtimestamp(
                        int(vdata["pubdate"]), tz=timezone.utc
                    ).isoformat()
            except Exception:
                log.debug(f"获取 {bvid} 发布时间失败，跳过")

            episodes.append(
                {
                    "episode_id": video_info.bvid or bvid,
                    "title": video_info.title or bvid,
                    "description": video_info.desc or "",
                    "source_url": video_url,
                    "cover_image_url": video_info.img_url,
                    "published_at": published_at,
                }
            )

    if podcast["sort_by"] == "title":
        episodes.sort(key=lambda item: item["title"], reverse=podcast["sort_order"] == "desc")

    return episodes


async def __download_one(
    bilix_downloader: DownloaderBilibili,
    episode: dict[str, str],
    target_dir: Path,
) -> str | None:
    before_files = __audio_files_in(target_dir)
    await bilix_downloader.get_video(
        episode["source_url"],
        path=target_dir,
        only_audio=True,
    )
    after_files = __audio_files_in(target_dir)
    downloaded = after_files - before_files
    if not downloaded:
        return None

    newest_file = max(downloaded, key=lambda f: f.stat().st_mtime)
    return newest_file.name


async def __run(podcast: Podcast):
    podcast_name = podcast["name"]
    target_dir = DOWNLOADS_DIR / podcast_name
    target_dir.mkdir(parents=True, exist_ok=True)

    episodes = await __collect_episodes(podcast)
    pending_episodes = [
        episode for episode in episodes if not get_podcast_by_episode(podcast_name, episode["episode_id"])
    ]
    total_to_download = len(pending_episodes)
    log.info(f"{podcast_name}: 需要下载 {total_to_download} 条")

    for index, episode in enumerate(pending_episodes, start=1):
        if _cancel_downloads.is_set():
            log.warning(f"{podcast_name}: 已收到退出信号，停止剩余下载")
            return
        log.info(f"{podcast_name}: 当前下载第 {index} / {total_to_download} 条（{episode['episode_id']}）")

        try:
            async with DownloaderBilibili(hierarchy=False) as downloader:
                file_name = await __download_one(downloader, episode, target_dir)
        except Exception as e:
            log.exception(f"下载失败：{podcast_name} / {episode['episode_id']}: {e}")
            continue

        if not file_name:
            log.warning(f"{podcast_name} 下载未产生文件：{episode['episode_id']}")
            continue

        save_episode(
            podcast_name=podcast_name,
            episode_id=episode["episode_id"],
            title=episode["title"],
            description=episode["description"],
            source_url=episode["source_url"],
            file_name=file_name,
            published_at=episode.get("published_at"),
            cover_image_url=episode.get("cover_image_url"),
        )
        log.info(f"{podcast_name} 保存音频：{file_name}")

    _cancel_downloads.clear()

    podcast_conf = get_podcast(podcast_name)
    if podcast_conf:
        removed = cleanup_old_episodes(podcast_name, int(podcast_conf["keep_latest"]))
        if removed:
            for name in removed:
                candidate = target_dir / name
                if candidate.exists():
                    candidate.unlink(missing_ok=True)


async def run_downloader(podcast: Podcast) -> None:
    await __run(podcast)


def run_downloader_sync(podcast: Podcast) -> None:
    asyncio.run(__run(podcast))


def request_stop() -> None:
    _cancel_downloads.set()


def request_stop_reset() -> None:
    _cancel_downloads.clear()
