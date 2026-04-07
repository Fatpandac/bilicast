# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bilix.sites.bilibili import api
from bilix.sites.bilibili.downloader import DownloaderBilibili

from src.config import Podcast
from src.database import cleanup_old_episodes, get_podcast_by_episode, get_podcast, save_episode

log = logging.getLogger(__name__)

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
            episodes.append(
                {
                    "episode_id": video_info.bvid or bvid,
                    "title": video_info.title or bvid,
                    "description": video_info.desc or "",
                    "source_url": video_url,
                    "cover_image_url": video_info.img_url,
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
    for episode in episodes:
        exists = get_podcast_by_episode(podcast_name, episode["episode_id"])
        if exists:
            continue

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
            published_at=None,
            cover_image_url=episode.get("cover_image_url"),
        )
        log.info(f"{podcast_name} 保存音频：{file_name}")

    podcast_conf = get_podcast(podcast_name)
    if podcast_conf:
        removed = cleanup_old_episodes(podcast_name, int(podcast_conf["keep_latest"]))
        if removed:
            for name in removed:
                candidate = target_dir / name
                if candidate.exists():
                    candidate.unlink(missing_ok=True)


def run_downloader(podcast: Podcast) -> None:
    asyncio.run(__run(podcast))
