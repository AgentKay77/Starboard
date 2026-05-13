"""Seasons: start-fresh button + archive browsing.

A season is a contiguous range of puzzle days. Every puzzle_day and every
weekly_award carries a `season_id`; submissions / rating_history / pauses
inherit the season transitively through their day_id.

"Start new season" inserts a new `seasons` row, sets `ended_at` on the
prior one, and bumps the `current_season_id` setting. From that moment
on, new submissions create puzzle_days under the new season_id, so APR
recompute (filtered to current season) starts from 1500 for everyone.
Past seasons stay in the DB, browsable read-only via /seasons.

`planned_end_date` (optional) marks when the current season *should*
close. The weekly-close systemd job checks it on every run and flips the
season automatically once that date has passed — so Hunter doesn't have
to remember the calendar on July 1.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timezone

from starboard import settings as settings_mod

CURRENT_SEASON_KEY = "current_season_id"
STANDARD_SEASON_MONTHS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_months(d: date, months: int) -> date:
    """Calendar-month addition. Used to default a new season's
    planned_end_date 3 months past its start."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def get_current_id(conn: sqlite3.Connection) -> int:
    raw = settings_mod.get(conn, CURRENT_SEASON_KEY)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    row = conn.execute("SELECT MAX(id) FROM seasons").fetchone()
    return int(row[0] or 1)


def get_current(conn: sqlite3.Connection) -> dict:
    sid = get_current_id(conn)
    row = conn.execute("SELECT * FROM seasons WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else {
        "id": sid, "number": sid, "started_at": _now(),
        "ended_at": None, "planned_end_date": None,
    }


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM seasons ORDER BY number DESC"
        ).fetchall()
    ]


def set_planned_end_date(
    conn: sqlite3.Connection, season_id: int, planned_end_date: str | None
) -> None:
    """Set or clear the planned end date for a season. Passing None
    clears it (disables auto-close)."""
    if planned_end_date is not None:
        date.fromisoformat(planned_end_date)  # validate
    conn.execute(
        "UPDATE seasons SET planned_end_date = ? WHERE id = ?",
        (planned_end_date, season_id),
    )
    conn.commit()


def start_new(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    planned_end_date: str | None = None,
) -> int:
    """Close the current season, open the next one. Returns the new id.

    If `planned_end_date` isn't given, defaults to today + 3 months."""
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
    if planned_end_date is None:
        planned_end_date = add_months(
            date.today(), STANDARD_SEASON_MONTHS
        ).isoformat()
    else:
        date.fromisoformat(planned_end_date)
    cur = conn.execute(
        "INSERT INTO seasons (number, started_at, planned_end_date) VALUES (?, ?, ?)",
        (next_number, now, planned_end_date),
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


def auto_close_if_due(
    conn: sqlite3.Connection, today: date, *, user_id: int | None = None
) -> int | None:
    """If the current season has a planned_end_date <= today, close it
    and open the next one with a default 3-month window. Returns the
    new season id, or None if no rollover happened. Called from the
    weekly-close cron so the rollover is automatic.

    user_id=None lands in audit_log as a system event (FK is nullable)."""
    cur = get_current(conn)
    pend = cur.get("planned_end_date")
    if not pend:
        return None
    if date.fromisoformat(pend) > today:
        return None
    return start_new(conn, user_id=user_id, planned_end_date=None)

