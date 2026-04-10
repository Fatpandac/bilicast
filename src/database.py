# -*- coding: utf-8 -*-
import logging
import sqlite3

from src.config import get_config

log = logging.getLogger(__name__)

__databasePath = "database.db"


def __connect_to_database() -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    conn = sqlite3.connect(__databasePath)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    return conn, c


def init_database():
    conn, c = __connect_to_database()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS podcast (
            name text primary key,
            url text,
            update_period_cron text,
            keep_latest int,
            sort_by text,
            sort_order text
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS episode (
            id integer primary key autoincrement,
            podcast_name text not null,
            episode_id text not null,
            title text,
            description text,
            source_url text,
            file_name text not null,
            published_at text,
            created_at text default (datetime('now', 'localtime')),
            foreign key (podcast_name) references podcast(name),
            unique(podcast_name, episode_id)
        )
        """
    )
    c.execute("PRAGMA table_info(episode)")
    existing_episode_cols = {row[1] for row in c.fetchall()}
    if "cover_image_url" not in existing_episode_cols:
        c.execute("ALTER TABLE episode ADD COLUMN cover_image_url text")
    config = get_config()
    for podcast in config["podcasts"]:
        __upsert_podcast(podcast)
    conn.commit()
    conn.close()


def __upsert_podcast(podcast):
    conn, c = __connect_to_database()
    c.execute(
        "INSERT OR REPLACE INTO podcast VALUES (?, ?, ?, ?, ?, ?)",
        (
            podcast["name"],
            podcast["url"],
            podcast["update_period_cron"],
            podcast["keep_latest"],
            podcast["sort_by"],
            podcast["sort_order"],
        ),
    )
    conn.commit()
    conn.close()


def get_all_podcasts() -> list[dict]:
    _, c = __connect_to_database()
    c.execute("SELECT * FROM podcast")
    podcasts = [dict(row) for row in c.fetchall()]
    c.connection.close()
    return podcasts


def get_podcast(name: str) -> dict | None:
    _, c = __connect_to_database()
    c.execute("SELECT * FROM podcast WHERE name = ?", (name,))
    podcast = c.fetchone()
    c.connection.close()
    return dict(podcast) if podcast else None


def get_podcast_by_episode(name: str, episode_id: str) -> dict | None:
    _, c = __connect_to_database()
    c.execute("SELECT * FROM episode WHERE podcast_name = ? AND episode_id = ?", (name, episode_id))
    episode = c.fetchone()
    c.connection.close()
    return dict(episode) if episode else None


def save_episode(
    podcast_name: str,
    episode_id: str,
    title: str,
    source_url: str,
    file_name: str,
    description: str | None = None,
    published_at: str | None = None,
    cover_image_url: str | None = None,
):
    conn, c = __connect_to_database()
    c.execute(
        """
        INSERT OR IGNORE INTO episode (podcast_name, episode_id, title, description, source_url, file_name, published_at, cover_image_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            podcast_name,
            episode_id,
            title,
            description,
            source_url,
            file_name,
            published_at,
            cover_image_url,
        ),
    )
    conn.commit()
    conn.close()


def get_episodes(podcast_name: str, limit: int | None = None) -> list[dict]:
    conn, c = __connect_to_database()
    if limit:
        c.execute(
            """
            SELECT * FROM episode
            WHERE podcast_name = ?
            ORDER BY COALESCE(published_at, created_at) DESC
            LIMIT ?
            """,
            (podcast_name, limit),
        )
    else:
        c.execute(
            """
            SELECT * FROM episode
            WHERE podcast_name = ?
            ORDER BY COALESCE(published_at, created_at) DESC
            """,
            (podcast_name,),
        )
    episodes = [dict(row) for row in c.fetchall()]
    c.connection.close()
    return episodes


def count_episodes_by_podcast(podcast_name: str) -> int:
    conn, c = __connect_to_database()
    c.execute("SELECT COUNT(*) as c FROM episode WHERE podcast_name = ?", (podcast_name,))
    count = c.fetchone()[0]
    c.connection.close()
    return int(count)


def cleanup_old_episodes(podcast_name: str, keep_latest: int) -> list[str]:
    if keep_latest <= 0:
        return []

    conn, c = __connect_to_database()
    c.execute(
        """
        SELECT id, file_name FROM episode
        WHERE podcast_name = ?
        ORDER BY COALESCE(published_at, created_at) DESC
        """,
        (podcast_name,),
    )
    rows = c.fetchall()
    if len(rows) <= keep_latest:
        conn.close()
        return []

    removed_files: list[str] = []
    removed_ids: list[int] = []
    for row in rows[keep_latest:]:
        removed_ids.append(int(row["id"]))
        removed_files.append(str(row["file_name"]))

    c.executemany("DELETE FROM episode WHERE id = ?", [(i,) for i in removed_ids])
    conn.commit()
    conn.close()
    return removed_files
