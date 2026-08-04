# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bilix.sites.bilibili import api
from bilix.sites.bilibili.downloader import DownloaderBilibili
from yt_dlp import YoutubeDL

from src.config import DEFAULT_PAGE_SIZE, Podcast, get_config
from src.database import (
    cleanup_old_episodes,
    get_cached_episode_meta,
    get_latest_published_at,
    get_podcast_by_episode,
    get_podcast,
    save_episode,
    save_episode_meta,
    update_podcast_metadata,
)

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("bilix").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

_cancel_downloads = threading.Event()

DOWNLOADS_DIR = Path("downloads")
# keep_latest 未配置时，首次抓取的集数上限（之后只跟进新发布的剧集）
DEFAULT_FIRST_RUN_LATEST = 10
# 采集播放列表时并发取视频详情的上限
YOUTUBE_DETAIL_CONCURRENCY = 8
_detail_semaphore_instance: asyncio.Semaphore | None = None
AUDIO_EXTENSIONS = {
    ".m4a",
    ".mp3",
    ".flac",
    ".wav",
    ".aac",
    ".ogg",
    ".opus",
    ".mp4",
    ".webm",
}

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def __audio_files_in(dirpath: Path) -> set[Path]:
    return {
        path
        for path in dirpath.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    }


def _audio_snapshot(target_dir: Path) -> dict[Path, float]:
    """下载前记录目录里音频文件的修改时间，供事后比对。"""
    return {path: path.stat().st_mtime for path in __audio_files_in(target_dir)}


def _touched_audio_file(target_dir: Path, before: dict[Path, float]) -> str | None:
    """找出本次新增或被覆盖的音频文件，取不到返回 None。

    只看「目录前后差集」的话，目标文件已存在被覆盖时会得到空集，从而把一次成功
    的下载误判为失败，该集永不入库、每轮重下。

    比对的是 mtime 快照而不是拿 time.time() 当基准：文件系统的时间戳可能比
    time.time() 略早（实测差约 0.3ms），用时间点比较会漏判刚写完的文件。
    """
    touched = [
        path
        for path in __audio_files_in(target_dir)
        if before.get(path) != path.stat().st_mtime
    ]
    if not touched:
        return None
    return max(touched, key=lambda path: path.stat().st_mtime).name


def _pubtime_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _is_youtube_url(url: str) -> bool:
    hostname = urlparse(url).hostname or ""
    return hostname.lower() in YOUTUBE_HOSTS


def _clean_thumbnail_url(url: str) -> str:
    """Strip YouTube signing query params that expire."""
    parsed = urlparse(url)
    return parsed._replace(query="").geturl() if parsed.query else url


def _youtube_thumbnail(info: dict) -> str:
    # Prefer clean per-video thumbnails over signed playlist thumbnails
    thumbnails: list[dict] = info.get("thumbnails") or []
    clean = [t for t in thumbnails if t.get("url") and "?" not in t["url"]]
    if clean:
        best = max(clean, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return str(best["url"])
    thumbnail = info.get("thumbnail")
    if thumbnail:
        return _clean_thumbnail_url(str(thumbnail))
    if thumbnails:
        raw = str((thumbnails[-1] or {}).get("url") or "")
        return _clean_thumbnail_url(raw) if raw else ""
    return ""


def _youtube_published_at(info: dict) -> str | None:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if timestamp:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()

    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8:
        try:
            return (
                datetime.strptime(upload_date, "%Y%m%d")
                .replace(tzinfo=timezone.utc)
                .isoformat()
            )
        except ValueError:
            return None
    return None


def _youtube_source_url(info: dict) -> str:
    url = info.get("webpage_url") or info.get("original_url") or info.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    episode_id = info.get("id")
    if episode_id:
        return f"https://www.youtube.com/watch?v={episode_id}"
    return str(url or "")


def _youtube_base_options() -> dict:
    yt_config = get_config().get("youtube", {})
    opts: dict = {"js_runtimes": {"node": {}}}
    if cookies_file := yt_config.get("cookies_file"):
        opts["cookiefile"] = cookies_file
    elif browser := yt_config.get("cookies_from_browser"):
        opts["cookiesfrombrowser"] = (browser, None, None, None)
    return opts


def _merge_yt_dlp_options(base: dict, extra: dict | None) -> dict:
    """把配置里的 yt_dlp_options 合并进内置默认值。

    extra 覆盖 base；对 extractor_args 这类嵌套字典做一层合并，
    这样用户加 youtube 的参数不会把内置的 youtubetab 设置顶掉。
    """
    if not extra:
        return base

    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = {**current, **value}
        else:
            merged[key] = value
    return merged


@contextlib.contextmanager
def _isolated_cookiefile(options: dict):
    """给并发任务各发一份 cookie 副本。

    yt-dlp 在关闭时会把 cookie 写回 cookiefile，多个实例同时用同一个文件会
    相互覆盖，导致 CookieLoadError，严重时还会写坏用户的 cookie 文件。
    副本用完即弃，本来也不需要把刷新后的 cookie 持久化。
    """
    cookiefile = options.get("cookiefile")
    if not cookiefile:
        yield options
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        replica = Path(tmpdir) / "cookies.txt"
        shutil.copyfile(cookiefile, replica)
        yield {**options, "cookiefile": str(replica)}


def _extract_youtube_info(url: str, download: bool, options: dict) -> dict:
    with _isolated_cookiefile(options) as opts, YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download) or {}



def _detail_semaphore() -> asyncio.Semaphore:
    """全局共用的详情解析并发闸门。

    必须是全局的：asyncio.to_thread 用的是同一个默认线程池（max_workers =
    min(32, cpu+4)），每个播客各持一个信号量的话，多个播客一起跑就会有几十个
    任务挤同一个线程池，既排不上队又把机器压垮。
    """
    global _detail_semaphore_instance
    if _detail_semaphore_instance is None:
        _detail_semaphore_instance = asyncio.Semaphore(YOUTUBE_DETAIL_CONCURRENCY)
    return _detail_semaphore_instance


# 直播还没结束、或刚结束但回放尚未转好。这两种状态下拉到的流是残缺的
# （实测 post_live 时 63.7 分钟的节目只下到 0.8 分钟），要等转码完成。
_UNREADY_LIVE_STATUS = {"is_live", "post_live", "is_upcoming"}


async def _fetch_youtube_details(entries: list[dict], options: dict) -> list[dict]:
    """并发取每个视频的详情，取不到的丢弃。

    扁平列表里没有发布时间和简介（实测 timestamp 100% 为 None），而排序和
    「最新 N 集」都依赖发布时间，所以未缓存的条目只能逐个解析。
    """
    semaphore = _detail_semaphore()

    async def fetch(entry: dict) -> dict | None:
        async with semaphore:
            detail = await asyncio.to_thread(
                _extract_youtube_info, _youtube_source_url(entry), False, options
            )
        # 私享/已删除的视频在 ignoreerrors 下返回空 dict。丢弃而不是退回扁平
        # 条目，否则它们会带着空发布时间进入待下载列表，每轮都失败一次。
        if not detail:
            return None

        # 回放没转好的直接跳过，交给下一次定时任务。这里丢弃后不会进元数据
        # 缓存，下一轮会重新解析，届时状态转为 was_live 就能正常下载。
        live_status = detail.get("live_status")
        if live_status in _UNREADY_LIVE_STATUS:
            log.info(
                f"跳过 {detail.get('title') or _entry_id(entry)}："
                f"直播回放尚未就绪（live_status={live_status}）"
            )
            return None

        return {**entry, **detail}

    details = await asyncio.gather(*[fetch(entry) for entry in entries])
    return [detail for detail in details if detail]


def _entry_id(entry: dict) -> str:
    return str(entry.get("id") or _youtube_source_url(entry))


def _newest_window(flat_entries: list[dict], page_size: int, sort_order: str) -> list[dict]:
    """从播放列表里截出最新的 page_size 条。

    播放列表顺序由 sort_order 描述：desc 表示新在前，从头部切；asc 表示旧在前，
    从尾部切。实测 wangjian / weeknews 是新到旧，sqxx 是旧到新，后者需要在配置
    里写明 sort_order: asc，否则会取到最旧的一段。
    """
    if len(flat_entries) <= page_size:
        return flat_entries
    return flat_entries[:page_size] if sort_order == "desc" else flat_entries[-page_size:]


def _episode_from_entry(entry: dict) -> dict:
    return {
        "episode_id": _entry_id(entry),
        "title": str(entry.get("title") or entry.get("id") or "Untitled"),
        "description": str(entry.get("description") or ""),
        "source_url": _youtube_source_url(entry),
        "cover_image_url": _youtube_thumbnail(entry),
        "published_at": _youtube_published_at(entry),
    }


async def _collect_youtube_episodes(podcast: Podcast) -> list[dict]:
    base_options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
        "extractor_args": {"youtubetab": {"skip": ["authcheck"]}},
        **_youtube_base_options(),
    }
    base_options = _merge_yt_dlp_options(base_options, podcast.get("yt_dlp_options"))

    # 第一步：扁平列表，只拿 id / 标题 / 缩略图，几秒钟就能返回
    info = await asyncio.to_thread(
        _extract_youtube_info, podcast["url"], False, {**base_options, "extract_flat": True}
    )
    flat_entries = [entry for entry in (info.get("entries") or [info]) if entry]

    # 第二步：只取最新的 page_size 条。整个播放列表可能上百条，而每条没缓存的
    # 都要单独解析一次详情。page_size 至少要覆盖本轮可能要下的集数，否则永远
    # 凑不够 keep_latest。
    page_size = max(
        podcast["page_size"], podcast["keep_latest"] or DEFAULT_FIRST_RUN_LATEST
    )
    window = _newest_window(flat_entries, page_size, podcast["sort_order"])
    cached = get_cached_episode_meta([_entry_id(entry) for entry in window])

    # 第三步：命中缓存的直接复用，只对没见过的视频解析详情。
    # 稳态下窗口内几乎全是老视频，这一步基本不发请求。
    missing = [entry for entry in window if _entry_id(entry) not in cached]
    if missing:
        log.info(
            f"{podcast['name']}: 播放列表 {len(flat_entries)} 条，"
            f"取最新 {len(window)} 条，需解析 {len(missing)} 条详情"
        )

    fetched = [
        _episode_from_entry(entry)
        for entry in await _fetch_youtube_details(missing, base_options)
    ]
    save_episode_meta(fetched)

    if info.get("title") or info.get("description") or _youtube_thumbnail(info):
        update_podcast_metadata(
            podcast["name"],
            info.get("title") or None,
            info.get("description") or None,
            _youtube_thumbnail(info) or None,
        )

    episodes = [
        cached[_entry_id(entry)] for entry in window if _entry_id(entry) in cached
    ] + fetched

    desc = podcast["sort_order"] == "desc"
    if podcast["sort_by"] == "title":
        episodes.sort(key=lambda item: item["title"], reverse=desc)
    else:
        episodes.sort(key=lambda item: item.get("published_at") or "", reverse=desc)

    return episodes


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


async def __collect_season_episodes(
    client: httpx.AsyncClient, sid: str
) -> tuple[list[dict], dict]:
    res = await client.get(
        "https://api.bilibili.com/x/space/fav/season/list",
        params={"season_id": sid},
    )
    res.raise_for_status()
    payload = res.json()["data"]
    info: dict = payload.get("info", {})
    medias = payload.get("medias", [])

    channel_meta = {
        "title": info.get("title") or info.get("name") or "",
        "description": info.get("intro") or info.get("description") or "",
        "image": info.get("cover") or info.get("cover_url") or "",
    }

    details = await asyncio.gather(
        *[__fetch_video_detail(client, m["bvid"]) for m in medias]
    )
    episodes = [
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
    return episodes, channel_meta


async def __collect_series_episodes(
    client: httpx.AsyncClient, sid: str
) -> tuple[list[dict], dict]:
    meta_res = await client.get(
        f"https://api.bilibili.com/x/series/series?series_id={sid}"
    )
    meta = meta_res.json()["data"]["meta"]
    mid, total = meta["mid"], meta["total"]

    channel_meta = {
        "title": meta.get("name") or meta.get("title") or "",
        "description": meta.get("description") or meta.get("intro") or "",
        "image": meta.get("cover") or meta.get("cover_url") or "",
    }

    res = await client.get(
        "https://api.bilibili.com/x/series/archives",
        params={"mid": mid, "series_id": sid, "ps": total},
    )
    archives = res.json()["data"]["archives"]

    details = await asyncio.gather(
        *[__fetch_video_detail(client, a["bvid"]) for a in archives]
    )
    episodes = [
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
    return episodes, channel_meta


async def __collect_episodes(podcast: Podcast) -> list[dict]:
    if _is_youtube_url(podcast["url"]):
        return await _collect_youtube_episodes(podcast)

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

    channel_meta: dict = {}
    async with httpx.AsyncClient(**api.dft_client_settings) as client:
        if sid or "collection" in url:
            try:
                episodes, channel_meta = await __collect_season_episodes(
                    client, sid or url
                )
            except Exception:
                if sid:
                    episodes, channel_meta = await __collect_series_episodes(
                        client, sid
                    )
                else:
                    raise ValueError(f"Unsupported bilibili URL: {url}")
        elif "series" in url:
            qs_sid = parse_qs(parsed.query).get("sid", [None])[0]
            if not qs_sid:
                raise ValueError(f"Cannot extract series id from URL: {url}")
            episodes, channel_meta = await __collect_series_episodes(client, qs_sid)
        else:
            raise ValueError(f"Unsupported bilibili URL: {url}")

    if (
        channel_meta.get("title")
        or channel_meta.get("description")
        or channel_meta.get("image")
    ):
        update_podcast_metadata(
            podcast["name"],
            channel_meta.get("title") or None,
            channel_meta.get("description") or None,
            channel_meta.get("image") or None,
        )

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
    # bilix 不返回文件名，只能靠比对目录里音频文件的修改时间。
    before = _audio_snapshot(target_dir)
    await bilix_downloader.get_video(
        episode["source_url"],
        path=target_dir,
        only_audio=True,
    )
    return _touched_audio_file(target_dir, before)


async def __download_episode(episode: dict[str, str], target_dir: Path) -> str | None:
    """Self-contained download coroutine, safe to wrap in an asyncio.Task."""
    async with DownloaderBilibili(hierarchy=False) as downloader:
        return await __download_one(downloader, episode, target_dir)


def _yt_progress_hook(d: dict) -> None:
    status = d.get("status")
    if status == "downloading":
        filename = Path(d.get("filename", "")).name
        downloaded = d.get("_downloaded_bytes_str", "?")
        total = d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str", "?")
        speed = d.get("_speed_str", "?")
        eta = d.get("_eta_str", "?")
        log.info(f"下载中: {filename} {downloaded}/{total} 速度:{speed} 剩余:{eta}")
    elif status == "finished":
        log.info(f"下载完成: {Path(d.get('filename', '')).name}")


def _downloaded_file_name(info: dict) -> str | None:
    """从 yt-dlp 的返回信息里取最终文件名。

    filepath 会被 postprocessor 更新，所以拿到的是转码后的 .m4a 而不是中间的
    .mp4。比对目录前后差集的老做法在目标文件已存在时会得到空集，从而把一次
    成功的下载误判为失败，进而永不入库、每轮重下。
    """
    for entry in info.get("requested_downloads") or []:
        path = entry.get("filepath") or entry.get("_filename") or entry.get("filename")
        if path:
            return Path(path).name
    return None


def _run_youtube_download(
    url: str, target_dir: Path, yt_dlp_options: dict | None = None
) -> str | None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "需要 ffmpeg 将音频转换为 m4a 格式。"
            "请先安装 ffmpeg 并确保它在 PATH 中。"
        )
    options = {
        "format": "bestaudio[ext=m4a]/bestaudio/bestvideo+bestaudio/best",
        "paths": {"home": str(target_dir)},
        "outtmpl": {"default": "%(title).200B [%(id)s].%(ext)s"},
        "no_warnings": True,
        "noplaylist": True,
        # HLS 分片缺失时直接报错。yt-dlp 默认会跳过坏分片并照常收尾，产出的残缺
        # 文件会被当成下载成功写进数据库，之后再也不会重下（实测出现过只有 1 分钟
        # 的 64 分钟节目）。失败不入库，交给下一次定时任务重试。
        "skip_unavailable_fragments": False,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        "progress_hooks": [_yt_progress_hook],
        **_youtube_base_options(),
    }
    options = _merge_yt_dlp_options(options, yt_dlp_options)

    # yt-dlp 没给出 requested_downloads 时（例如目标文件已存在被直接跳过），
    # 与 bilix 那边走同一套兜底，避免把成功的下载记成失败。
    before = _audio_snapshot(target_dir)
    info = _extract_youtube_info(url, True, options)
    return _downloaded_file_name(info) or _touched_audio_file(target_dir, before)


async def _download_youtube_episode(
    episode: dict[str, str], target_dir: Path, yt_dlp_options: dict | None = None
) -> str | None:
    return await asyncio.to_thread(
        _run_youtube_download, episode["source_url"], target_dir, yt_dlp_options
    )


async def _download_episode_for_podcast(
    podcast: Podcast,
    episode: dict[str, str],
    target_dir: Path,
) -> str | None:
    if _is_youtube_url(podcast["url"]):
        return await _download_youtube_episode(
            episode, target_dir, podcast.get("yt_dlp_options")
        )

    return await __download_episode(episode, target_dir)


async def _wait_for_cancel() -> None:
    """Async-friendly cancel signal: polls threading.Event at 0.2 s intervals."""
    while not _cancel_downloads.is_set():
        await asyncio.sleep(0.2)


def _select_episodes_without_keep_latest(
    podcast_name: str, episodes: list[dict]
) -> list[dict]:
    """未配置 keep_latest 时的取集范围。

    首次运行只取最新的 DEFAULT_FIRST_RUN_LATEST 集，避免把整个播放列表拉下来。
    之后取两部分的并集：

    1. 比库中最新一集更晚发布的，不设上限，更新几集就下几集；
    2. 最新 DEFAULT_FIRST_RUN_LATEST 集组成的固定窗口。

    只有第 1 部分的话，下载是按新到旧进行的，最新一集一旦成功，水位线就跳到
    最前面，中间失败或被中断的剧集永远不会被重试。加上第 2 部分即可补齐，
    窗口是固定位置而非「最新 N 个缺失的」，所以不会每轮往回多挖 N 集。

    真正下哪些由调用方按数据库去重，这里只负责圈定范围。
    """
    newest_first = sorted(
        episodes, key=lambda item: item.get("published_at") or "", reverse=True
    )
    latest_known = get_latest_published_at(podcast_name)
    if latest_known is None:
        return newest_first[:DEFAULT_FIRST_RUN_LATEST]

    selected = [
        episode
        for episode in newest_first
        if (episode.get("published_at") or "") > latest_known
    ]
    seen = {episode["episode_id"] for episode in selected}
    selected.extend(
        episode
        for episode in newest_first[:DEFAULT_FIRST_RUN_LATEST]
        if episode["episode_id"] not in seen
    )
    return selected


async def __run(podcast: Podcast):
    podcast_name = podcast["name"]
    target_dir = DOWNLOADS_DIR / podcast_name
    target_dir.mkdir(parents=True, exist_ok=True)

    podcast_conf = get_podcast(podcast_name)
    if podcast_conf:
        removed = cleanup_old_episodes(podcast_name, podcast_conf["keep_latest"])
        if removed:
            for name in removed:
                candidate = target_dir / name
                if candidate.exists():
                    candidate.unlink(missing_ok=True)
            log.info(f"{podcast_name}: 下载前清理旧集 {len(removed)} 条以释放空间")

    episodes = await __collect_episodes(podcast)
    keep_latest = podcast["keep_latest"]
    episodes = (
        episodes[:keep_latest]
        if keep_latest
        else _select_episodes_without_keep_latest(podcast_name, episodes)
    )
    pending_episodes = [
        episode
        for episode in episodes
        if not get_podcast_by_episode(podcast_name, episode["episode_id"])
    ]
    total_to_download = len(pending_episodes)
    log.info(f"{podcast_name}: 需要下载 {total_to_download} 条")

    for index, episode in enumerate(pending_episodes, start=1):
        if _cancel_downloads.is_set():
            log.warning(f"{podcast_name}: 已收到退出信号，停止剩余下载")
            return
        log.info(
            f"{podcast_name}: 当前下载第 {index} / {total_to_download} 条（{episode['episode_id']}）"
        )

        dl_task = asyncio.create_task(
            _download_episode_for_podcast(podcast, episode, target_dir)
        )
        cancel_task = asyncio.create_task(_wait_for_cancel())
        try:
            done, _ = await asyncio.wait(
                {dl_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            # lifespan 被取消（如 Ctrl+C 直接杀进程）
            _cancel_downloads.set()
            dl_task.cancel()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(dl_task, cancel_task, return_exceptions=True)
            raise
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

        if _cancel_downloads.is_set():
            dl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dl_task
            log.warning(f"{podcast_name}: 已收到退出信号，停止剩余下载")
            return

        file_name: str | None
        try:
            file_name = dl_task.result()
        except OSError as e:
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
