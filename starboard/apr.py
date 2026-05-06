"""Adjusted Performance Rating: long-term skill snapshot.

Generalization of FIDE's Tournament Performance Rating to time-based events.
Per-day update is rating-aware (so cherry-picking weak fields can't inflate)
and attendance-blind (missing days never costs rating).
"""
from __future__ import annotations

import math
import sqlite3
from typing import Iterable

INITIAL_RATING = 1500.0
K = 22.0
RATING_PER_SIGMA = 400.0


def compute_apr_update(
    submissions: list[tuple[int, float]],
    current_ratings: dict[int, float],
) -> tuple[dict[int, float], list[dict]]:
    """Compute one day's APR update.

    submissions: [(player_id, time_seconds), ...] for that day's submitters only.
    current_ratings: {player_id: rating} known *before* this day. Submitters
        not in the dict are treated as new and seeded at INITIAL_RATING.

    Returns (new_ratings, history_rows).
        new_ratings: {player_id: rating_after} for *submitters only*.
            Caller is responsible for leaving non-submitters untouched.
        history_rows: list of dicts with keys
            player_id, rating_before, rating_after, actual_z, expected_z, delta.

    If fewer than two players submitted, the day is skipped (returns empty,
    empty) — there is no meaningful field to compare against.
    """
    if len(submissions) < 2:
        return {}, []

    times = [t for _, t in submissions]
    n = len(times)
    mean_time = sum(times) / n
    variance = sum((t - mean_time) ** 2 for t in times) / n
    std_time = math.sqrt(variance) or 1.0  # guard against everyone tying

    ratings_before = [current_ratings.get(pid, INITIAL_RATING) for pid, _ in submissions]
    field_avg_R = sum(ratings_before) / n

    new_ratings: dict[int, float] = {}
    history_rows: list[dict] = []
    for (pid, t), rating_before in zip(submissions, ratings_before):
        actual_z = -(t - mean_time) / std_time
        expected_z = (rating_before - field_avg_R) / RATING_PER_SIGMA
        delta = K * (actual_z - expected_z)
        rating_after = rating_before + delta
        new_ratings[pid] = rating_after
        history_rows.append(
            {
                "player_id": pid,
                "rating_before": rating_before,
                "rating_after": rating_after,
                "actual_z": actual_z,
                "expected_z": expected_z,
                "delta": delta,
            }
        )
    return new_ratings, history_rows


def recompute_all_ratings(conn: sqlite3.Connection) -> None:
    """Wipe rating_history and replay every puzzle_day in date order.

    Cheap at our scale (~50 days × ~13 players); call freely after any admin
    edit to submissions or roster.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM rating_history")

    days = cur.execute(
        "SELECT id, date FROM puzzle_days ORDER BY date ASC, id ASC"
    ).fetchall()

    current: dict[int, float] = {}
    for day_id, _date in days:
        rows = cur.execute(
            "SELECT player_id, time_seconds FROM submissions WHERE day_id = ?",
            (day_id,),
        ).fetchall()
        submissions = [(int(pid), float(t)) for pid, t in rows]
        new_ratings, history_rows = compute_apr_update(submissions, current)
        for h in history_rows:
            cur.execute(
                """
                INSERT INTO rating_history
                    (player_id, day_id, rating_before, rating_after,
                     actual_z, expected_z, delta)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    h["player_id"],
                    day_id,
                    h["rating_before"],
                    h["rating_after"],
                    h["actual_z"],
                    h["expected_z"],
                    h["delta"],
                ),
            )
        current.update(new_ratings)

    conn.commit()


def get_current_ratings(conn: sqlite3.Connection) -> dict[int, float]:
    """Latest rating_after per player. Players with no history are absent;
    callers should treat them as INITIAL_RATING."""
    return _ratings_as_of_date(conn, upper_bound_date=None)


def get_ratings_as_of(conn: sqlite3.Connection, date_str: str) -> dict[int, float]:
    """Most recent rating_after per player from days *strictly before* date_str.
    Used by the Weekly Skirmish to pin Giant Slayer gaps to Monday-morning ratings."""
    return _ratings_as_of_date(conn, upper_bound_date=date_str)


def _ratings_as_of_date(
    conn: sqlite3.Connection, upper_bound_date: str | None
) -> dict[int, float]:
    if upper_bound_date is None:
        rows = conn.execute(
            """
            SELECT rh.player_id, rh.rating_after
            FROM rating_history rh
            JOIN puzzle_days pd ON pd.id = rh.day_id
            JOIN (
                SELECT rh2.player_id, MAX(pd2.date) AS max_date
                FROM rating_history rh2
                JOIN puzzle_days pd2 ON pd2.id = rh2.day_id
                GROUP BY rh2.player_id
            ) latest
              ON latest.player_id = rh.player_id AND latest.max_date = pd.date
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT rh.player_id, rh.rating_after
            FROM rating_history rh
            JOIN puzzle_days pd ON pd.id = rh.day_id
            JOIN (
                SELECT rh2.player_id, MAX(pd2.date) AS max_date
                FROM rating_history rh2
                JOIN puzzle_days pd2 ON pd2.id = rh2.day_id
                WHERE pd2.date < ?
                GROUP BY rh2.player_id
            ) latest
              ON latest.player_id = rh.player_id AND latest.max_date = pd.date
            """,
            (upper_bound_date,),
        ).fetchall()
    return {int(pid): float(r) for pid, r in rows}
