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

        def extract_info(self, url, download):
            assert url == "https://www.youtube.com/playlist?list=PL123"
            assert download is False
            return {
                "title": "Channel Title",
                "description": "Channel description",
                "thumbnail": "https://img.example/channel.jpg",
                "entries": [
                    {
                        "id": "newer",
                        "title": "Newer video",
                        "description": "Newer description",
                        "webpage_url": "https://www.youtube.com/watch?v=newer",
                        "thumbnail": "https://img.example/newer.jpg",
                        "timestamp": 1_700_000_000,
                    },
                    {
                        "id": "older",
                        "title": "Older video",
                        "description": "Older description",
                        "url": "https://www.youtube.com/watch?v=older",
                        "thumbnails": [{"url": "https://img.example/older.jpg"}],
                        "upload_date": "20230102",
                    },
                ],
            }

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
