"""Player pause / vacation windows.

A paused day is exempt from APR: the player can't gain or lose rating, and
their absence isn't punished. Submissions during a pause still count for
H2H, Weekend Warrior, and the player's own activity log — they just don't
move the rating needle.

Players manage their own pauses on /account; admins can manage any
player's pauses on /admin/players/<id>/pauses. Both player and admin can
add windows in the past or the future."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_for_player(conn: sqlite3.Connection, player_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM player_pauses
           WHERE player_id = ?
           ORDER BY start_date DESC""",
        (player_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def paused_pids_for_day(conn: sqlite3.Connection, day_iso: str) -> set[int]:
    """Player IDs whose pause window covers `day_iso` (inclusive on both
    ends).  Hot path — called once per puzzle_day in the APR recompute."""
    rows = conn.execute(
        """SELECT DISTINCT player_id FROM player_pauses
           WHERE start_date <= ? AND end_date >= ?""",
        (day_iso, day_iso),
    ).fetchall()
    return {int(r["player_id"]) for r in rows}


def paused_dates_for_player(
    conn: sqlite3.Connection, player_id: int
) -> set[str]:
    """Every ISO date covered by any of the player's pause windows. Used
    by the attendance heatmap to paint paused cells blue."""
    pauses = list_for_player(conn, player_id)
    out: set[str] = set()
    for p in pauses:
        start = date.fromisoformat(p["start_date"])
        end = date.fromisoformat(p["end_date"])
        cur = start
        while cur <= end:
            out.add(cur.isoformat())
            cur = date.fromordinal(cur.toordinal() + 1)
    return out


def create(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    start_date: str,
    end_date: str,
    reason: str | None,
    created_by_user_id: int,
) -> int:
    # Normalize: parse to validate and force ISO format.
    s = date.fromisoformat(start_date)
    e = date.fromisoformat(end_date)
    if e < s:
        raise ValueError("end_date must be >= start_date")
    cur = conn.execute(
        """INSERT INTO player_pauses
               (player_id, start_date, end_date, reason,
                created_at, created_by_user_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            player_id, s.isoformat(), e.isoformat(),
            (reason or "").strip() or None,
            _now(), created_by_user_id,
        ),
    )
    conn.commit()
    return cur.lastrowid


def delete(
    conn: sqlite3.Connection,
    *,
    pause_id: int,
    requesting_user_id: int,
    requesting_player_id: int | None,
    is_admin: bool,
) -> bool:
    """Owner (claimed player) or admin can delete. Returns True on delete."""
    row = conn.execute(
        "SELECT player_id FROM player_pauses WHERE id = ?", (pause_id,)
    ).fetchone()
    if not row:
        return False
    if not is_admin and row["player_id"] != requesting_player_id:
        return False
    conn.execute("DELETE FROM player_pauses WHERE id = ?", (pause_id,))
    conn.commit()
    return True
