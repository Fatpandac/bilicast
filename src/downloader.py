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


def _pubtime_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


async def __fetch_video_detail(client: httpx.AsyncClient, bvid: str) -> dict:
    """从 /x/web-interface/view 获取单集的 desc 和 pic。"""
    try:
        res = await client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
        )
        data = res.json().get("data") or {}
        return {"desc": data.get("desc") or "", "pic": data.get("pic") or ""}
    except Exception:
        log.debug(f"获取 {bvid} 详情失败，跳过")
        return {"desc": "", "pic": ""}


async def __collect_season_episodes(client: httpx.AsyncClient, sid: str) -> list[dict]:
    res = await client.get(
        "https://api.bilibili.com/x/space/fav/season/list",
        params={"season_id": sid},
    )
    res.raise_for_status()
    medias = res.json()["data"]["medias"]

    details = await asyncio.gather(*[__fetch_video_detail(client, m["bvid"]) for m in medias])
    return [
        {
            "episode_id": m["bvid"],
            "title": m["title"],
            "description": detail["desc"],
            "source_url": f"https://www.bilibili.com/video/{m['bvid']}",
            "cover_image_url": detail["pic"] or m.get("cover") or "",
            "published_at": _pubtime_to_iso(m.get("pubtime")),
        }
        for m, detail in zip(medias, details)
    ]


async def __collect_series_episodes(client: httpx.AsyncClient, sid: str) -> list[dict]:
    meta_res = await client.get(f"https://api.bilibili.com/x/series/series?series_id={sid}")
    meta = meta_res.json()["data"]["meta"]
    mid, total = meta["mid"], meta["total"]
    res = await client.get(
        "https://api.bilibili.com/x/series/archives",
        params={"mid": mid, "series_id": sid, "ps": total},
    )
    archives = res.json()["data"]["archives"]

    details = await asyncio.gather(*[__fetch_video_detail(client, a["bvid"]) for a in archives])
    return [
        {
            "episode_id": a["bvid"],
            "title": a["title"],
            "description": detail["desc"] or a.get("desc") or "",
            "source_url": f"https://www.bilibili.com/video/{a['bvid']}",
            "cover_image_url": detail["pic"] or a.get("pic") or a.get("cover") or "",
            "published_at": _pubtime_to_iso(a.get("pubdate")),
        }
        for a, detail in zip(archives, details)
    ]


async def __collect_episodes(podcast: Podcast) -> list[dict]:
    url = podcast["url"]

    sid = None
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for idx, part in enumerate(path_parts):
        if part == "lists" and idx + 1 < len(path_parts):
            sid = path_parts[idx + 1]
            break
    if not sid:
        qs_sid = parse_qs(parsed.query).get("sid")
        if qs_sid and qs_sid[0].isdigit():
            sid = qs_sid[0]

    async with httpx.AsyncClient(**api.dft_client_settings) as client:
        if sid or "collection" in url:
            try:
                episodes = await __collect_season_episodes(client, sid or url)
            except Exception:
                if sid:
                    episodes = await __collect_series_episodes(client, sid)
                else:
                    raise ValueError(f"Unsupported bilibili URL: {url}")
        elif "series" in url:
            qs_sid = parse_qs(parsed.query).get("sid", [None])[0]
            if not qs_sid:
                raise ValueError(f"Cannot extract series id from URL: {url}")
            episodes = await __collect_series_episodes(client, qs_sid)
        else:
            raise ValueError(f"Unsupported bilibili URL: {url}")

    desc = podcast["sort_order"] == "desc"
    if podcast["sort_by"] == "title":
        episodes.sort(key=lambda item: item["title"], reverse=desc)
    else:  # date
        episodes.sort(key=lambda item: item.get("published_at") or "", reverse=desc)

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

    podcast_conf = get_podcast(podcast_name)
    if podcast_conf:
        removed = cleanup_old_episodes(podcast_name, int(podcast_conf["keep_latest"]))
        if removed:
            for name in removed:
                candidate = target_dir / name
                if candidate.exists():
                    candidate.unlink(missing_ok=True)
            log.info(f"{podcast_name}: 下载前清理旧集 {len(removed)} 条以释放空间")

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
        except OSError as e:
            import errno
            if e.errno == errno.ENOSPC:
                log.error(f"{podcast_name}: 磁盘空间不足，跳过剩余下载")
                return
            log.exception(f"下载失败：{podcast_name} / {episode['episode_id']}: {e}")
            continue
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


async def run_downloader(podcast: Podcast) -> None:
    await __run(podcast)


def request_stop() -> None:
    _cancel_downloads.set()


def request_stop_reset() -> None:
    _cancel_downloads.clear()
