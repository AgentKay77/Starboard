"""Adjusted Performance Rating: long-term skill snapshot.

Three statuses contribute to APR each day, in increasing order of pain:

    completed → actual z, clamped at DNF_FLOOR if abysmal
    dnf       → actual z forced to DNF_FLOOR
    absent    → actual z forced to ABSENT_FLOOR (worse than DNF)

The clamp removes the "should I just DNF?" cliff: any completed time
worse than DNF_FLOOR is treated as DNF for rating purposes. The
absent-vs-DNF gap rewards engagement: DNFing is strictly cheaper than
ghosting.

After per-player deltas are computed, a zero-sum redistribution kicks
in — total rating across all participants on the day is conserved.
Practical consequence: the people who show up gain the rating that the
ghosters and DNFers lose, on top of their normal performance delta.
Without this, any non-trivial absence rate would deflate the league
toward zero over seasons.
"""
from __future__ import annotations

import math
import sqlite3

INITIAL_RATING = 1500.0
K = 22.0
RATING_PER_SIGMA = 400.0

# How bad can a single day get for rating purposes?
DNF_FLOOR = -2.0
ABSENT_FLOOR = -2.25


def compute_apr_update(
    completed: list[tuple[int, float]],
    dnfs: list[int],
    absentees: list[int],
    current_ratings: dict[int, float],
) -> tuple[dict[int, float], list[dict]]:
    """Compute one day's APR update across all three player statuses.

    completed:   [(player_id, time_seconds), ...]
    dnfs:        [player_id, ...] — submitted but didn't finish
    absentees:   [player_id, ...] — active that day, never submitted
    current_ratings: ratings *before* this day. Missing entries default
        to INITIAL_RATING.

    Returns (new_ratings, history_rows). history_rows have a 'kind'
    field of 'completed' / 'dnf' / 'absent'.

    A day with fewer than two completed times has no statistical signal:
    the function returns empty results and no rating updates happen for
    anyone (DNFs/absentees included). This is intentional — penalizing
    twelve people because one person submitted alone would be unfair.
    """
    if len(completed) < 2:
        return {}, []

    times = [t for _, t in completed]
    n = len(times)
    mean_t = sum(times) / n
    std_t = math.sqrt(sum((t - mean_t) ** 2 for t in times) / n) or 1.0

    completed_ratings = [
        current_ratings.get(pid, INITIAL_RATING) for pid, _ in completed
    ]
    field_avg_R = sum(completed_ratings) / n

    new_ratings: dict[int, float] = {}
    history: list[dict] = []

    for (pid, t), rating_before in zip(completed, completed_ratings):
        raw_z = -(t - mean_t) / std_t
        actual_z = max(raw_z, DNF_FLOOR)  # clamp catastrophic times
        expected_z = (rating_before - field_avg_R) / RATING_PER_SIGMA
        delta = K * (actual_z - expected_z)
        rating_after = rating_before + delta
        new_ratings[pid] = rating_after
        history.append(
            {
                "player_id": pid,
                "kind": "completed",
                "rating_before": rating_before,
                "rating_after": rating_after,
                "actual_z": actual_z,
                "expected_z": expected_z,
                "delta": delta,
            }
        )

    for pid in dnfs:
        rating_before = current_ratings.get(pid, INITIAL_RATING)
        expected_z = (rating_before - field_avg_R) / RATING_PER_SIGMA
        delta = K * (DNF_FLOOR - expected_z)
        rating_after = rating_before + delta
        new_ratings[pid] = rating_after
        history.append(
            {
                "player_id": pid,
                "kind": "dnf",
                "rating_before": rating_before,
                "rating_after": rating_after,
                "actual_z": DNF_FLOOR,
                "expected_z": expected_z,
                "delta": delta,
            }
        )

    for pid in absentees:
        rating_before = current_ratings.get(pid, INITIAL_RATING)
        expected_z = (rating_before - field_avg_R) / RATING_PER_SIGMA
        delta = K * (ABSENT_FLOOR - expected_z)
        rating_after = rating_before + delta
        new_ratings[pid] = rating_after
        history.append(
            {
                "player_id": pid,
                "kind": "absent",
                "rating_before": rating_before,
                "rating_after": rating_after,
                "actual_z": ABSENT_FLOOR,
                "expected_z": expected_z,
                "delta": delta,
            }
        )

    # Zero-sum redistribution. Without it, every absentee/DNF bleeds rating
    # out of the system; over a season the league deflates. Subtracting the
    # mean drift from every participant's delta keeps the total constant
    # without disturbing relative differences (or the ranking ordering).
    if history:
        drift = sum(h["delta"] for h in history) / len(history)
        for h in history:
            h["delta"] -= drift
            h["rating_after"] = h["rating_before"] + h["delta"]
            new_ratings[h["player_id"]] = h["rating_after"]

    return new_ratings, history


def recompute_all_ratings(conn: sqlite3.Connection) -> None:
    """Wipe rating_history and replay every puzzle_day in date order.

    Cheap at our scale (~13 active × ~50 days). Re-run whenever a
    submission is inserted, updated, or deleted, or whenever the active
    roster changes.

    Future-dated puzzle_days (relative to the league's local timezone)
    are skipped so a stray "tomorrow" row — e.g. from a pre-fix
    submission that landed at 8 pm CT while the server was already on
    UTC's tomorrow — doesn't penalise everyone as "absent" on a day
    that hasn't happened yet.
    """
    from starboard import clock

    today_iso = clock.local_today().isoformat()

    cur = conn.cursor()
    cur.execute("DELETE FROM rating_history")

    days = cur.execute(
        "SELECT id, date FROM puzzle_days WHERE date <= ? ORDER BY date ASC, id ASC",
        (today_iso,),
    ).fetchall()
    active_players = cur.execute(
        "SELECT id, joined_date FROM players WHERE active = 1"
    ).fetchall()

    current: dict[int, float] = {}
    for day_id, date_str in days:
        # Friday/Saturday/Sunday don't affect APR. Players can still submit
        # — those entries count for H2H and Weekend Warrior — but no rating
        # change is applied, so absences over the weekend are also free.
        if clock.is_weekend_date(date_str):
            continue
        completed_rows = cur.execute(
            """SELECT player_id, time_seconds FROM submissions
               WHERE day_id = ? AND status = 'completed'""",
            (day_id,),
        ).fetchall()
        completed = [(int(pid), float(t)) for pid, t in completed_rows]

        dnf_rows = cur.execute(
            "SELECT player_id FROM submissions WHERE day_id = ? AND status = 'dnf'",
            (day_id,),
        ).fetchall()
        dnfs = [int(r[0]) for r in dnf_rows]

        submitter_ids = {pid for pid, _ in completed} | set(dnfs)
        absentees = [
            p["id"]
            for p in active_players
            if p["joined_date"] <= date_str and p["id"] not in submitter_ids
        ]

        new_ratings, history = compute_apr_update(
            completed, dnfs, absentees, current
        )
        for h in history:
            cur.execute(
                """INSERT INTO rating_history
                       (player_id, day_id, kind, rating_before, rating_after,
                        actual_z, expected_z, delta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    h["player_id"],
                    day_id,
                    h["kind"],
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
    return _ratings_as_of_date(conn, upper_bound_date=None)


def get_ratings_as_of(conn: sqlite3.Connection, date_str: str) -> dict[int, float]:
    """Most recent rating per player from days strictly before date_str."""
    return _ratings_as_of_date(conn, upper_bound_date=date_str)


def _ratings_as_of_date(
    conn: sqlite3.Connection, upper_bound_date: str | None
) -> dict[int, float]:
    if upper_bound_date is None:
        rows = conn.execute(
            """SELECT rh.player_id, rh.rating_after
               FROM rating_history rh
               JOIN puzzle_days pd ON pd.id = rh.day_id
               JOIN (SELECT rh2.player_id, MAX(pd2.date) AS max_date
                     FROM rating_history rh2
                     JOIN puzzle_days pd2 ON pd2.id = rh2.day_id
                     GROUP BY rh2.player_id) latest
                 ON latest.player_id = rh.player_id AND latest.max_date = pd.date"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT rh.player_id, rh.rating_after
               FROM rating_history rh
               JOIN puzzle_days pd ON pd.id = rh.day_id
               JOIN (SELECT rh2.player_id, MAX(pd2.date) AS max_date
                     FROM rating_history rh2
                     JOIN puzzle_days pd2 ON pd2.id = rh2.day_id
                     WHERE pd2.date < ?
                     GROUP BY rh2.player_id) latest
                 ON latest.player_id = rh.player_id AND latest.max_date = pd.date""",
            (upper_bound_date,),
        ).fetchall()
    return {int(pid): float(r) for pid, r in rows}
