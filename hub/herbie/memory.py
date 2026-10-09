"""Long-term memory: user profile, preferences, calendar and episodic history.

SQLite keeps the prototype a single container with zero setup. The access layer is
small and SQL-only, so moving to Postgres (+ pgvector for semantic recall) later
does not change any caller.

Episodes are the raw material for learning: every suggestion Herbie makes and how
the driver reacted is stored together with the context bucket it happened in.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .sim import REFERENCE_MONDAY

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, sim_time TEXT NOT NULL, kind TEXT NOT NULL,
    subject TEXT, bucket TEXT, outcome TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS idx_ep_subject ON episodes (user_id, subject, bucket);
CREATE TABLE IF NOT EXISTS calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    link TEXT, kind TEXT);
"""


class Memory:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(SCHEMA)
        if not self._q("SELECT 1 FROM users LIMIT 1"):
            seed(self)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    def reset(self) -> None:
        with self._lock:
            self._db.executescript("DELETE FROM users; DELETE FROM preferences; DELETE FROM episodes; DELETE FROM calendar;")
            self._db.commit()
        seed(self)

    # --- profile & preferences ---------------------------------------------
    def get_user(self, user_id: str) -> dict[str, Any]:
        rows = self._q("SELECT id, name FROM users WHERE id = ?", (user_id,))
        return dict(rows[0]) if rows else {"id": user_id, "name": "Driver"}

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._q("SELECT key, value FROM preferences WHERE user_id = ?", (user_id,))}

    def set_preference(self, user_id: str, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO preferences VALUES (?, ?, ?)", (user_id, key, json.dumps(value)))

    # --- episodes -----------------------------------------------------------
    def add_episode(self, user_id: str, sim_time: datetime, kind: str, subject: str | None = None,
                    bucket: str | None = None, outcome: str | None = None, payload: dict | None = None) -> int:
        return self._x(
            "INSERT INTO episodes (user_id, sim_time, kind, subject, bucket, outcome, payload) VALUES (?,?,?,?,?,?,?)",
            (user_id, sim_time.isoformat(), kind, subject, bucket, outcome, json.dumps(payload or {})),
        )

    def outcomes(self, user_id: str, subject: str, bucket: str, limit: int = 10) -> list[str]:
        """Most recent outcomes first."""
        rows = self._q(
            "SELECT outcome FROM episodes WHERE user_id=? AND subject=? AND bucket=? AND outcome IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (user_id, subject, bucket, limit))
        return [r["outcome"] for r in rows]

    def recent_episodes(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM episodes WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in rows]

    # --- calendar -----------------------------------------------------------
    def add_event(self, user_id: str, title: str, start: datetime, end: datetime, link: str | None = None, kind: str = "personal") -> int:
        return self._x("INSERT INTO calendar (user_id, title, start, end, link, kind) VALUES (?,?,?,?,?,?)",
                       (user_id, title, start.isoformat(), end.isoformat(), link, kind))

    def delete_events_between(self, user_id: str, start: datetime, end: datetime) -> None:
        self._x("DELETE FROM calendar WHERE user_id=? AND start >= ? AND start < ?",
                (user_id, start.isoformat(), end.isoformat()))

    def events_between(self, user_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM calendar WHERE user_id=? AND end > ? AND start < ? ORDER BY start",
                       (user_id, start.isoformat(), end.isoformat()))
        return [{**dict(r), "start": datetime.fromisoformat(r["start"]), "end": datetime.fromisoformat(r["end"])} for r in rows]


def seed(mem: Memory) -> None:
    """Demo persona. Clearly synthetic: it stands in for weeks of real driving history."""
    uid = "saad"
    mem._x("INSERT INTO users VALUES (?, ?)", (uid, "Saad"))
    for key, value in {
        "favourite_food": "cheeseburger",
        "food_offers_opt_in": True,
        "music_wind_down": "Calm Evening (lo-fi)",
        "music_night_out": "Saturday Night Drive",
        "comfort_seat_recline": 28,
        "comfort_cabin_temp": 22.5,
        "meeting_platform": "Google Meet",
        "favourite_cafe": "Starbucks Pariser Platz 4A",
        "favourite_coffee": "oat flat white",
    }.items():
        mem.set_preference(uid, key, value)

    # Learned routine history (8 past commutes home, 4 weekend nights out).
    t = REFERENCE_MONDAY - timedelta(weeks=3)
    comfort_bucket = "weekday|evening|work->home|tired|solo"
    for outcome in ["declined"] + ["accepted"] * 7:
        mem.add_episode(uid, t, "suggestion", "comfort_wind_down", comfort_bucket, outcome, {"seeded": True})
    for outcome in ["accepted"] * 4:
        mem.add_episode(uid, t, "suggestion", "sport_night", "weekend|night|other|fresh|group", outcome, {"seeded": True})

    # Two weeks of a typical work calendar.
    for day in range(14):
        d = REFERENCE_MONDAY + timedelta(days=day)
        if d.weekday() >= 5:
            continue
        mem.add_event(uid, "Team stand-up", d.replace(hour=8, minute=30), d.replace(hour=9), "https://meet.google.com/herbie-demo", "meeting")
        mem.add_event(uid, "Work", d.replace(hour=9), d.replace(hour=17, minute=30), None, "work")
