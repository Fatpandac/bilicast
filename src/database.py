# -*- coding: utf-8 -*-
import sqlite3

from src.config import get_config

config = get_config()


def __connect_to_database() -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    conn = sqlite3.connect("database.db")
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
            CREATE TABLE IF NOT EXISTS downloaded (
                id integer primary key autoincrement,
                name text,
                url text,
                date text default (datetime('now', 'localtime'))
            )
            """
    )
    for podcast in config["podcasts"]:
        __update_podcast(podcast)
    conn.commit()
    conn.close()


def __update_podcast(podcast):
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


def get_all_podcasts():
    _, c = __connect_to_database()
    c.execute("SELECT * FROM podcast")
    all_podcasts = c.fetchall()
    return all_podcasts
