# -*- coding: utf-8 -*-
import asyncio
from pathlib import Path

from src import downloader
from src.config import Podcast


def _youtube_podcast() -> Podcast:
    return {
        "name": "youtube-show",
        "url": "https://www.youtube.com/playlist?list=PL123",
        "update_period_cron": "0 * * * *",
        "keep_latest": 10,
        "sort_by": "date",
        "sort_order": "desc",
    }


def _stub_meta_cache(monkeypatch, cached=None):
    """屏蔽元数据缓存，避免单测碰到真实的 database.db。"""
    monkeypatch.setattr(downloader, "get_cached_episode_meta", lambda ids: cached or {})
    monkeypatch.setattr(downloader, "save_episode_meta", lambda rows: None)


def test_youtube_url_detection_ignores_port():
    assert downloader._is_youtube_url("https://www.youtube.com:443/playlist?list=PL123")


def test_youtube_playlist_entries_are_collected(monkeypatch):
    captured_metadata = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        # 采集分两步：先扁平列表，再逐个取详情（并发）
        FLAT = {
            "title": "Channel Title",
            "description": "Channel description",
            "thumbnail": "https://img.example/channel.jpg",
            "entries": [
                {"id": "newer", "title": "Newer video",
                 "webpage_url": "https://www.youtube.com/watch?v=newer"},
                {"id": "older", "title": "Older video",
                 "url": "https://www.youtube.com/watch?v=older"},
            ],
        }
        DETAILS = {
            "newer": {
                "id": "newer",
                "title": "Newer video",
                "description": "Newer description",
                "webpage_url": "https://www.youtube.com/watch?v=newer",
                "thumbnail": "https://img.example/newer.jpg",
                "timestamp": 1_700_000_000,
            },
            "older": {
                "id": "older",
                "title": "Older video",
                "description": "Older description",
                "url": "https://www.youtube.com/watch?v=older",
                "thumbnails": [{"url": "https://img.example/older.jpg"}],
                "upload_date": "20230102",
            },
        }

        def extract_info(self, url, download):
            assert download is False
            if url == "https://www.youtube.com/playlist?list=PL123":
                assert self.options.get("extract_flat") is True
                return self.FLAT
            assert not self.options.get("extract_flat")
            for episode_id, detail in self.DETAILS.items():
                if url.endswith(episode_id):
                    return detail
            raise AssertionError(f"未预期的 URL: {url}")

    def fake_update_metadata(name, title, description, image):
        captured_metadata.update(
            {
                "name": name,
                "title": title,
                "description": description,
                "image": image,
            }
        )

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL, raising=False)
    monkeypatch.setattr(downloader, "update_podcast_metadata", fake_update_metadata)
    _stub_meta_cache(monkeypatch)

    episodes = asyncio.run(downloader._collect_youtube_episodes(_youtube_podcast()))

    assert [episode["episode_id"] for episode in episodes] == ["newer", "older"]
    assert episodes[0]["title"] == "Newer video"
    assert episodes[0]["description"] == "Newer description"
    assert episodes[0]["source_url"] == "https://www.youtube.com/watch?v=newer"
    assert episodes[0]["cover_image_url"] == "https://img.example/newer.jpg"
    assert episodes[0]["published_at"] == "2023-11-14T22:13:20+00:00"
    assert episodes[1]["published_at"] == "2023-01-02T00:00:00+00:00"
    assert captured_metadata == {
        "name": "youtube-show",
        "title": "Channel Title",
        "description": "Channel description",
        "image": "https://img.example/channel.jpg",
    }


def test_youtube_episode_downloads_audio(monkeypatch, tmp_path):
    target_dir = tmp_path / "downloads"
    target_dir.mkdir()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download):
            assert url == "https://www.youtube.com/watch?v=abc123"
            assert download is True
            assert self.options["format"].startswith("bestaudio[ext=m4a]")
            assert self.options["paths"]["home"] == str(target_dir)
            written = Path(self.options["paths"]["home"], "downloaded-audio.m4a")
            written.write_bytes(b"audio")
            # filepath 由 postprocessor 更新为转码后的路径
            return {"id": "abc123", "requested_downloads": [{"filepath": str(written)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL, raising=False)

    file_name = asyncio.run(
        downloader._download_youtube_episode(
            {
                "episode_id": "abc123",
                "source_url": "https://www.youtube.com/watch?v=abc123",
            },
            target_dir,
        )
    )

    assert file_name == "downloaded-audio.m4a"


def test_youtube_episode_returns_name_when_file_already_exists(monkeypatch, tmp_path):
    """目标文件已存在时也要返回文件名。

    早先的实现比对下载前后的目录差集，文件已存在时差集为空，会把一次成功的
    下载误判为失败，导致该集永不入库、每轮重复下载。
    """
    target_dir = tmp_path / "downloads"
    target_dir.mkdir()
    existing = target_dir / "downloaded-audio.m4a"
    existing.write_bytes(b"old")

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download):
            existing.write_bytes(b"new")  # yt-dlp 覆盖同名文件，目录没有新增项
            return {"id": "abc123", "requested_downloads": [{"filepath": str(existing)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL, raising=False)

    file_name = asyncio.run(
        downloader._download_youtube_episode(
            {
                "episode_id": "abc123",
                "source_url": "https://www.youtube.com/watch?v=abc123",
            },
            target_dir,
        )
    )

    assert file_name == "downloaded-audio.m4a"


def test_youtube_episode_requires_ffmpeg(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            raise AssertionError("yt-dlp should not run without ffmpeg")

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL, raising=False)

    try:
        asyncio.run(
            downloader._download_youtube_episode(
                {
                    "episode_id": "abc123",
                    "source_url": "https://www.youtube.com/watch?v=abc123",
                },
                tmp_path,
            )
        )
    except RuntimeError as exc:
        assert "ffmpeg" in str(exc)
        assert "m4a" in str(exc)
    else:
        raise AssertionError("Expected missing ffmpeg to raise RuntimeError")


def test_youtube_private_videos_are_dropped(monkeypatch):
    """详情取不到的条目（私享/已删除）要丢弃。

    否则它们会带着空发布时间进入待下载列表，每轮重试一次且永远失败。
    """

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download):
            if url == "https://www.youtube.com/playlist?list=PL123":
                return {
                    "title": "Channel",
                    "entries": [
                        {"id": "good", "url": "https://www.youtube.com/watch?v=good"},
                        {"id": "private", "url": "https://www.youtube.com/watch?v=private"},
                    ],
                }
            if url.endswith("private"):
                return {}  # ignoreerrors 下私享视频返回空
            return {"id": "good", "title": "Good video", "timestamp": 1_700_000_000}

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL, raising=False)
    monkeypatch.setattr(downloader, "update_podcast_metadata", lambda *a: None)
    _stub_meta_cache(monkeypatch)

    episodes = asyncio.run(downloader._collect_youtube_episodes(_youtube_podcast()))

    assert [episode["episode_id"] for episode in episodes] == ["good"]


def test_youtube_cached_entries_skip_detail_fetch(monkeypatch):
    """命中缓存的条目不再解析详情，只对没见过的视频发请求。"""
    detail_calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download):
            if url == "https://www.youtube.com/playlist?list=PL123":
                return {
                    "title": "Channel",
                    "entries": [
                        {"id": "known", "url": "https://www.youtube.com/watch?v=known"},
                        {"id": "fresh", "url": "https://www.youtube.com/watch?v=fresh"},
                    ],
                }
            detail_calls.append(url)
            return {"id": "fresh", "title": "Fresh", "timestamp": 1_700_000_000}

    saved = []
    cached = {
        "known": {
            "episode_id": "known",
            "title": "Known",
            "description": "",
            "source_url": "https://www.youtube.com/watch?v=known",
            "cover_image_url": "",
            "published_at": "2020-01-01T00:00:00+00:00",
        }
    }
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL, raising=False)
    monkeypatch.setattr(downloader, "update_podcast_metadata", lambda *a: None)
    monkeypatch.setattr(downloader, "get_cached_episode_meta", lambda ids: cached)
    monkeypatch.setattr(downloader, "save_episode_meta", saved.extend)

    episodes = asyncio.run(downloader._collect_youtube_episodes(_youtube_podcast()))

    assert detail_calls == ["https://www.youtube.com/watch?v=fresh"]
    assert [e["episode_id"] for e in saved] == ["fresh"]
    assert {e["episode_id"] for e in episodes} == {"known", "fresh"}
    # 新的在前（按发布时间倒序）
    assert episodes[0]["episode_id"] == "fresh"
