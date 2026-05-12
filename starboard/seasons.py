"""Seasons: start-fresh button + archive browsing.

A season is a contiguous range of puzzle days. Every puzzle_day and every
weekly_award carries a `season_id`; submissions / rating_history / pauses
inherit the season transitively through their day_id.

"Start new season" inserts a new `seasons` row, sets `ended_at` on the
prior one, and bumps the `current_season_id` setting. From that moment
on, new submissions create puzzle_days under the new season_id, so APR
recompute (filtered to current season) starts from 1500 for everyone.
Past seasons stay in the DB, browsable read-only via /seasons.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from starboard import settings as settings_mod

CURRENT_SEASON_KEY = "current_season_id"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_current_id(conn: sqlite3.Connection) -> int:
    raw = settings_mod.get(conn, CURRENT_SEASON_KEY)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    # Fall back to the highest seasons.id — useful right after the
    # idempotent bootstrap creates season #1 but before any setting row
    # exists.
    row = conn.execute("SELECT MAX(id) FROM seasons").fetchone()
    return int(row[0] or 1)


def get_current(conn: sqlite3.Connection) -> dict:
    sid = get_current_id(conn)
    row = conn.execute("SELECT * FROM seasons WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else {"id": sid, "number": sid, "started_at": _now(), "ended_at": None}


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM seasons ORDER BY number DESC"
        ).fetchall()
    ]


def start_new(conn: sqlite3.Connection, *, user_id: int) -> int:
    """Close the current season, open the next one. Returns the new id.

    Idempotent in spirit but not literally — calling it twice in a row
    opens two seasons. The admin UI confirms before posting."""
    now = _now()
    cur_id = get_current_id(conn)
    conn.execute(
        "UPDATE seasons SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (now, cur_id),
    )
    next_number = (
        conn.execute("SELECT COALESCE(MAX(number), 0) FROM seasons").fetchone()[0]
        + 1
    )
    cur = conn.execute(
        "INSERT INTO seasons (number, started_at) VALUES (?, ?)",
        (next_number, now),
    )
    new_id = cur.lastrowid
    settings_mod.set_value(
        conn,
        CURRENT_SEASON_KEY,
        str(new_id),
        user_id=user_id,
        action="start_new_season",
        detail=f"season #{next_number}",
    )
    return new_id
