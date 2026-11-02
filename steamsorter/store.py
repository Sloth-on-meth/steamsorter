"""Config file + SQLite cache so ratings/genres are only fetched once."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

STALE_SECONDS = 30 * 24 * 3600  # re-fetch store data after 30 days


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "steamsorter"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    api_key: str = ""
    steam_id: str = ""
    family_token: str = ""

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads((config_dir() / "config.json").read_text("utf-8"))
            return cls(**{k: str(v) for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        path = config_dir() / "config.json"
        path.write_text(json.dumps(self.__dict__, indent=2), "utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


@dataclass
class Game:
    appid: int
    name: str
    source: str = "own"  # "own" or "family"
    owners: list[str] = field(default_factory=list)
    playtime: int = 0  # minutes
    last_played: int = 0
    type: str = ""
    genres: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    release: str = ""
    release_sort: str = ""
    metacritic: int | None = None
    short_desc: str = ""
    header_image: str = ""
    details_ts: int = 0
    positive: int | None = None
    negative: int | None = None
    review_desc: str = ""
    reviews_ts: int = 0

    @property
    def total_reviews(self) -> int | None:
        if self.positive is None or self.negative is None:
            return None
        return self.positive + self.negative

    @property
    def rating(self) -> float | None:
        t = self.total_reviews
        return self.positive / t if t else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    appid INTEGER PRIMARY KEY,
    type TEXT DEFAULT '',
    genres TEXT DEFAULT '[]',
    categories TEXT DEFAULT '[]',
    release TEXT DEFAULT '',
    release_sort TEXT DEFAULT '',
    metacritic INTEGER,
    short_desc TEXT DEFAULT '',
    details_ts INTEGER DEFAULT 0,
    positive INTEGER,
    negative INTEGER,
    review_desc TEXT DEFAULT '',
    reviews_ts INTEGER DEFAULT 0,
    header_image TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS library (
    appid INTEGER PRIMARY KEY,
    name TEXT,
    source TEXT,
    owners TEXT DEFAULT '[]',
    playtime INTEGER DEFAULT 0,
    last_played INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS names (
    steamid TEXT PRIMARY KEY,
    name TEXT
);
"""


class Cache:
    def __init__(self, path: Path | None = None):
        self.db = sqlite3.connect(str(path or config_dir() / "cache.sqlite3"), check_same_thread=False)
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(apps)")}
        if "header_image" not in cols:  # migrate caches from v0.1
            self.db.execute("ALTER TABLE apps ADD COLUMN header_image TEXT DEFAULT ''")

    def close(self) -> None:
        self.db.close()

    # ---- library ---------------------------------------------------------
    def save_library(self, games: list[Game]) -> None:
        with self.db:
            self.db.execute("DELETE FROM library")
            self.db.executemany(
                "INSERT OR REPLACE INTO library VALUES (?,?,?,?,?,?)",
                [
                    (g.appid, g.name, g.source, json.dumps(g.owners), g.playtime, g.last_played)
                    for g in games
                ],
            )

    def load_library(self) -> list[Game]:
        rows = self.db.execute(
            """SELECT l.appid, l.name, l.source, l.owners, l.playtime, l.last_played,
                      a.type, a.genres, a.categories, a.release, a.release_sort, a.metacritic,
                      a.short_desc, a.details_ts, a.positive, a.negative, a.review_desc, a.reviews_ts,
                      a.header_image
               FROM library l LEFT JOIN apps a ON a.appid = l.appid"""
        ).fetchall()
        games = []
        for r in rows:
            games.append(
                Game(
                    appid=r[0], name=r[1], source=r[2], owners=json.loads(r[3] or "[]"),
                    playtime=r[4] or 0, last_played=r[5] or 0, type=r[6] or "",
                    genres=json.loads(r[7] or "[]"), categories=json.loads(r[8] or "[]"),
                    release=r[9] or "", release_sort=r[10] or "", metacritic=r[11],
                    short_desc=r[12] or "", details_ts=r[13] or 0, positive=r[14],
                    negative=r[15], review_desc=r[16] or "", reviews_ts=r[17] or 0,
                    header_image=r[18] or "",
                )
            )
        return games

    # ---- per-app store data ---------------------------------------------
    def save_details(self, appid: int, d: dict | None) -> None:
        d = d or {}
        now = int(time.time())
        self.db.execute("INSERT OR IGNORE INTO apps (appid) VALUES (?)", (appid,))
        self.db.execute(
            """UPDATE apps SET type=?, genres=?, categories=?, release=?, release_sort=?,
                   metacritic=?, short_desc=?, header_image=?, details_ts=? WHERE appid=?""",
            (
                d.get("type", ""), json.dumps(d.get("genres", [])),
                json.dumps(d.get("categories", [])), d.get("release", ""),
                d.get("release_sort", ""), d.get("metacritic"), d.get("short_desc", ""),
                d.get("header_image", ""), now, appid,
            ),
        )

    def save_reviews(self, appid: int, r: dict) -> None:
        now = int(time.time())
        self.db.execute("INSERT OR IGNORE INTO apps (appid) VALUES (?)", (appid,))
        self.db.execute(
            "UPDATE apps SET positive=?, negative=?, review_desc=?, reviews_ts=? WHERE appid=?",
            (r["positive"], r["negative"], r["review_desc"], now, appid),
        )

    def commit(self) -> None:
        self.db.commit()

    # ---- persona names ----------------------------------------------------
    def save_names(self, names: dict[str, str]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO names VALUES (?,?)", names.items())

    def load_names(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT steamid, name FROM names").fetchall())


def is_stale(ts: int) -> bool:
    return not ts or time.time() - ts > STALE_SECONDS
