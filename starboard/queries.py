"""Read-side queries that views use to render pages.

Pure SQL plus light Python shaping. The dataset is tiny (50 days × ~13
players), so we don't bother with caching — every page renders straight
from sqlite.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from starboard.apr import INITIAL_RATING
from starboard.weekly import (
    ALL_AWARDS,
    CHAMPION,
    GIANT_SLAYER,
    IRON_MAN,
    LIGHTNING,
    STEADY,
    compute_weekly_awards,
    week_bounds,
)


# ---------- formatters ----------


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:05.2f}".rstrip("0").rstrip(".") if s % 1 else f"{int(m)}:{int(s):02d}"


def parse_time(raw: str) -> float:
    """Forgiving parser: '90', '1:30', '01:30', '1:30.5'. Returns seconds.
    Raises ValueError on garbage."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty time")
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError(f"unrecognized time format: {raw!r}")
        m_str, s_str = parts
        m = int(m_str)
        s = float(s_str)
        if m < 0 or s < 0 or s >= 60:
            raise ValueError(f"unrecognized time format: {raw!r}")
        return m * 60 + s
    return float(raw)


# ---------- general stats ----------


def total_stats(conn: sqlite3.Connection) -> dict:
    days = conn.execute("SELECT COUNT(*) FROM puzzle_days").fetchone()[0]
    subs = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    fastest = conn.execute(
        """SELECT s.time_seconds, p.name, p.display_name, pd.date, p.id AS pid
           FROM submissions s
           JOIN players p ON p.id = s.player_id
           JOIN puzzle_days pd ON pd.id = s.day_id
           ORDER BY s.time_seconds ASC LIMIT 1"""
    ).fetchone()
    most = conn.execute(
        """SELECT p.id, p.name, p.display_name, COUNT(*) AS ct
           FROM weekly_awards wa
           JOIN players p ON p.id = wa.player_id
           WHERE wa.player_id IS NOT NULL
           GROUP BY wa.player_id
           ORDER BY ct DESC, p.id ASC LIMIT 1"""
    ).fetchone()
    return {
        "total_days": days,
        "total_submissions": subs,
        "fastest_time": fastest["time_seconds"] if fastest else None,
        "fastest_time_str": format_time(fastest["time_seconds"]) if fastest else "—",
        "fastest_player": _display(fastest) if fastest else None,
        "fastest_date": fastest["date"] if fastest else None,
        "most_trophies_player": _display(most) if most else None,
        "most_trophies_count": most["ct"] if most else 0,
    }


def _display(row) -> str | None:
    if row is None:
        return None
    return row["display_name"] or row["name"]


# ---------- standings ----------


def standings(conn: sqlite3.Connection) -> list[dict]:
    """All active players sorted by current APR. New players (no rating
    history yet) are surfaced at the bottom at INITIAL_RATING."""
    latest_date_row = conn.execute("SELECT MAX(date) FROM puzzle_days").fetchone()
    latest_date = latest_date_row[0] if latest_date_row else None

    players = conn.execute(
        """SELECT id, name, display_name, joined_date
           FROM players WHERE active = 1
           ORDER BY id ASC"""
    ).fetchall()
    out = []
    for p in players:
        cur = conn.execute(
            """SELECT rh.rating_after, pd.date FROM rating_history rh
               JOIN puzzle_days pd ON pd.id = rh.day_id
               WHERE rh.player_id = ? ORDER BY pd.date DESC LIMIT 1""",
            (p["id"],),
        ).fetchone()
        rating = cur["rating_after"] if cur else INITIAL_RATING

        latest_delta = None
        if latest_date is not None:
            d = conn.execute(
                """SELECT rh.delta FROM rating_history rh
                   JOIN puzzle_days pd ON pd.id = rh.day_id
                   WHERE rh.player_id = ? AND pd.date = ?""",
                (p["id"], latest_date),
            ).fetchone()
            if d:
                latest_delta = d["delta"]

        days_avail = conn.execute(
            "SELECT COUNT(*) FROM puzzle_days WHERE date >= ?",
            (p["joined_date"],),
        ).fetchone()[0]
        subs = conn.execute(
            """SELECT COUNT(*) FROM submissions s
               JOIN puzzle_days pd ON pd.id = s.day_id
               WHERE s.player_id = ? AND pd.date >= ?""",
            (p["id"], p["joined_date"]),
        ).fetchone()[0]
        attendance = (subs / days_avail) if days_avail else 0.0

        champ_count = conn.execute(
            "SELECT COUNT(*) FROM weekly_awards WHERE player_id = ? AND award = ?",
            (p["id"], CHAMPION),
        ).fetchone()[0]
        trophy_count = conn.execute(
            "SELECT COUNT(*) FROM weekly_awards WHERE player_id = ?",
            (p["id"],),
        ).fetchone()[0]

        out.append(
            {
                "player_id": p["id"],
                "name": p["name"],
                "display_name": _display(p),
                "rating": rating,
                "latest_delta": latest_delta,
                "attendance": attendance,
                "champions": champ_count,
                "trophies": trophy_count,
                "submissions": subs,
            }
        )
    out.sort(key=lambda r: (-r["rating"], r["player_id"]))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


# ---------- player profile ----------


def player_profile(conn: sqlite3.Connection, player_id: int) -> dict | None:
    p = conn.execute(
        "SELECT * FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    if not p:
        return None

    standings_rows = standings(conn)
    rank = None
    rating = INITIAL_RATING
    for r in standings_rows:
        if r["player_id"] == player_id:
            rank = r["rank"]
            rating = r["rating"]
            break

    rating_history = conn.execute(
        """SELECT pd.date, rh.rating_after, rh.delta, rh.actual_z
           FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           WHERE rh.player_id = ?
           ORDER BY pd.date ASC""",
        (player_id,),
    ).fetchall()

    # Weekly awards by category
    award_breakdown = {a: 0 for a in ALL_AWARDS}
    award_rows = conn.execute(
        """SELECT wa.award, wa.week_start, wa.metric_value, wa.metric_detail
           FROM weekly_awards wa
           WHERE wa.player_id = ?
           ORDER BY wa.week_start DESC""",
        (player_id,),
    ).fetchall()
    trophy_case = []
    for a in award_rows:
        award_breakdown[a["award"]] = award_breakdown.get(a["award"], 0) + 1
        trophy_case.append(dict(a))

    # Recent submissions w/ z and rank that day
    recent_subs = conn.execute(
        """SELECT pd.date, s.time_seconds, rh.actual_z, rh.delta, pd.id AS day_id
           FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           LEFT JOIN rating_history rh
               ON rh.day_id = s.day_id AND rh.player_id = s.player_id
           WHERE s.player_id = ?
           ORDER BY pd.date DESC
           LIMIT 14""",
        (player_id,),
    ).fetchall()
    recent = []
    for r in recent_subs:
        rank_that_day = conn.execute(
            """SELECT COUNT(*) + 1 FROM submissions s2
               WHERE s2.day_id = ? AND s2.time_seconds < ?""",
            (r["day_id"], r["time_seconds"]),
        ).fetchone()[0]
        recent.append(
            {
                "date": r["date"],
                "time_seconds": r["time_seconds"],
                "time_str": format_time(r["time_seconds"]),
                "z": r["actual_z"],
                "delta": r["delta"],
                "rank": rank_that_day,
            }
        )

    # Stats
    times = [s["time_seconds"] for s in recent_subs]  # only last 14, but enough
    all_times = conn.execute(
        "SELECT time_seconds FROM submissions WHERE player_id = ? ORDER BY time_seconds ASC",
        (player_id,),
    ).fetchall()
    all_t = [t["time_seconds"] for t in all_times]
    fastest = all_t[0] if all_t else None
    median = all_t[len(all_t) // 2] if all_t else None
    peak_rating = max((r["rating_after"] for r in rating_history), default=INITIAL_RATING)

    # Day-of-week: best/worst average
    dow_rows = conn.execute(
        """SELECT pd.date, s.time_seconds FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           WHERE s.player_id = ?""",
        (player_id,),
    ).fetchall()
    dow_buckets: dict[int, list[float]] = defaultdict(list)
    for r in dow_rows:
        wd = date.fromisoformat(r["date"]).weekday()
        dow_buckets[wd].append(r["time_seconds"])
    dow_avgs = {wd: sum(ts) / len(ts) for wd, ts in dow_buckets.items() if ts}
    best_dow = min(dow_avgs.items(), key=lambda kv: kv[1]) if dow_avgs else None
    worst_dow = max(dow_avgs.items(), key=lambda kv: kv[1]) if dow_avgs else None
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Attendance
    days_avail = conn.execute(
        "SELECT COUNT(*) FROM puzzle_days WHERE date >= ?",
        (p["joined_date"],),
    ).fetchone()[0]
    subs_count = len(all_t)
    attendance = (subs_count / days_avail) if days_avail else 0.0

    # H2H top 5
    h2h = h2h_for_player(conn, player_id)
    top5 = sorted(h2h, key=lambda r: -(r["wins"] + r["losses"]))[:5]

    return {
        "player": dict(p),
        "display_name": _display(p),
        "rank": rank,
        "rating": rating,
        "peak_rating": peak_rating,
        "fastest_time": fastest,
        "fastest_str": format_time(fastest),
        "median_time": median,
        "median_str": format_time(median),
        "best_dow": (dow_names[best_dow[0]], best_dow[1]) if best_dow else None,
        "worst_dow": (dow_names[worst_dow[0]], worst_dow[1]) if worst_dow else None,
        "attendance": attendance,
        "submissions_count": subs_count,
        "rating_history": [dict(r) for r in rating_history],
        "recent_submissions": recent,
        "trophy_case": trophy_case,
        "award_breakdown": award_breakdown,
        "h2h_top5": top5,
    }


# ---------- head-to-head ----------


def h2h_matrix(conn: sqlite3.Connection) -> dict:
    """Returns {players: [...], wins: {(row_pid, col_pid): count}} where
    wins[a, b] is "a finished ahead of b on shared days"."""
    players = conn.execute(
        "SELECT id, name, display_name FROM players WHERE active = 1 ORDER BY id"
    ).fetchall()
    pid_list = [p["id"] for p in players]
    wins: dict[tuple[int, int], int] = defaultdict(int)
    rows = conn.execute(
        """SELECT day_id, player_id, time_seconds FROM submissions
           ORDER BY day_id, time_seconds ASC"""
    ).fetchall()
    by_day: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        by_day[r["day_id"]].append((r["player_id"], r["time_seconds"]))
    for day_id, subs in by_day.items():
        # subs is sorted by time asc → earlier players finished ahead.
        for i, (a_pid, _at) in enumerate(subs):
            for b_pid, _bt in subs[i + 1 :]:
                wins[(a_pid, b_pid)] += 1
    return {
        "players": [dict(p) | {"display_name": _display(p)} for p in players],
        "wins": wins,
        "pid_list": pid_list,
    }


def h2h_for_player(conn: sqlite3.Connection, player_id: int) -> list[dict]:
    """Per-opponent record from the focal player's perspective."""
    matrix = h2h_matrix(conn)
    out = []
    for opp in matrix["players"]:
        if opp["id"] == player_id:
            continue
        wins = matrix["wins"].get((player_id, opp["id"]), 0)
        losses = matrix["wins"].get((opp["id"], player_id), 0)
        out.append(
            {
                "opponent_id": opp["id"],
                "opponent_name": opp["display_name"],
                "wins": wins,
                "losses": losses,
            }
        )
    return out


# ---------- weekly history ----------


def weekly_history(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT wa.*, p.name, p.display_name
           FROM weekly_awards wa
           LEFT JOIN players p ON p.id = wa.player_id
           ORDER BY wa.week_start DESC, wa.award ASC"""
    ).fetchall()
    weeks: dict[str, dict] = {}
    for r in rows:
        ws = r["week_start"]
        if ws not in weeks:
            weeks[ws] = {
                "week_start": ws,
                "week_end": r["week_end"],
                "awards": {},
            }
        weeks[ws]["awards"][r["award"]] = {
            "player_id": r["player_id"],
            "player_name": r["display_name"] or r["name"],
            "metric_value": r["metric_value"],
            "metric_detail": r["metric_detail"],
        }
    return list(weeks.values())


def career_trophies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT p.id, p.name, p.display_name,
                  SUM(CASE WHEN wa.award='champion' THEN 1 ELSE 0 END) AS champion,
                  SUM(CASE WHEN wa.award='iron_man' THEN 1 ELSE 0 END) AS iron_man,
                  SUM(CASE WHEN wa.award='lightning' THEN 1 ELSE 0 END) AS lightning,
                  SUM(CASE WHEN wa.award='steady' THEN 1 ELSE 0 END) AS steady,
                  SUM(CASE WHEN wa.award='giant_slayer' THEN 1 ELSE 0 END) AS giant_slayer,
                  COUNT(wa.id) AS total
           FROM players p
           LEFT JOIN weekly_awards wa ON wa.player_id = p.id
           GROUP BY p.id
           ORDER BY total DESC, p.id ASC"""
    ).fetchall()
    return [dict(r) | {"display_name": _display(r)} for r in rows]


# ---------- live current week ----------


def current_week_live(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Compute the in-progress week's standings without writing anything."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    rows = conn.execute(
        """SELECT pd.date, s.player_id, s.time_seconds, p.name, p.display_name
           FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           JOIN players p ON p.id = s.player_id
           WHERE pd.date BETWEEN ? AND ?
           ORDER BY pd.date""",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchall()
    submissions_by_day: dict[str, list[tuple[int, float]]] = defaultdict(list)
    name_by_pid: dict[int, str] = {}
    for r in rows:
        submissions_by_day[r["date"]].append((r["player_id"], r["time_seconds"]))
        name_by_pid[r["player_id"]] = r["display_name"] or r["name"]

    from starboard.apr import get_ratings_as_of

    apr_ratings = get_ratings_as_of(conn, monday.isoformat())
    awards = compute_weekly_awards(
        monday.isoformat(),
        sunday.isoformat(),
        dict(submissions_by_day),
        apr_ratings,
    )
    awards_by_kind = {a["award"]: a for a in awards}
    days_played = len(submissions_by_day)
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "days_played": days_played,
        "submission_count": sum(len(v) for v in submissions_by_day.values()),
        "awards": awards_by_kind,
        "name_by_pid": name_by_pid,
    }


# ---------- latest day ----------


def latest_day(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """SELECT pd.id, pd.date,
                  (SELECT COUNT(*) FROM submissions s2 WHERE s2.day_id = pd.id) AS field_size
           FROM puzzle_days pd
           ORDER BY pd.date DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    winner = conn.execute(
        """SELECT s.time_seconds, p.name, p.display_name, p.id AS pid
           FROM submissions s
           JOIN players p ON p.id = s.player_id
           WHERE s.day_id = ?
           ORDER BY s.time_seconds ASC LIMIT 1""",
        (row["id"],),
    ).fetchone()
    return {
        "date": row["date"],
        "field_size": row["field_size"],
        "winner_name": _display(winner) if winner else None,
        "winner_id": winner["pid"] if winner else None,
        "winner_time": winner["time_seconds"] if winner else None,
        "winner_time_str": format_time(winner["time_seconds"]) if winner else "—",
    }


# ---------- records ----------


def records(conn: sqlite3.Connection) -> dict:
    fastest = conn.execute(
        """SELECT s.time_seconds, p.name, p.display_name, p.id AS pid, pd.date
           FROM submissions s
           JOIN players p ON p.id = s.player_id
           JOIN puzzle_days pd ON pd.id = s.day_id
           ORDER BY s.time_seconds ASC LIMIT 10"""
    ).fetchall()
    most_trophies = conn.execute(
        """SELECT p.id, p.name, p.display_name, COUNT(*) AS ct
           FROM weekly_awards wa
           JOIN players p ON p.id = wa.player_id
           WHERE wa.player_id IS NOT NULL
           GROUP BY wa.player_id ORDER BY ct DESC, p.id ASC LIMIT 10"""
    ).fetchall()
    most_champions = conn.execute(
        """SELECT p.id, p.name, p.display_name, COUNT(*) AS ct
           FROM weekly_awards wa
           JOIN players p ON p.id = wa.player_id
           WHERE wa.award = 'champion'
           GROUP BY wa.player_id ORDER BY ct DESC, p.id ASC LIMIT 10"""
    ).fetchall()
    peak_apr = conn.execute(
        """SELECT rh.rating_after, p.id, p.name, p.display_name, pd.date
           FROM rating_history rh
           JOIN players p ON p.id = rh.player_id
           JOIN puzzle_days pd ON pd.id = rh.day_id
           ORDER BY rh.rating_after DESC LIMIT 10"""
    ).fetchall()
    biggest_gs = conn.execute(
        """SELECT wa.metric_value, wa.metric_detail, wa.week_start,
                  p.name, p.display_name, p.id AS pid
           FROM weekly_awards wa
           JOIN players p ON p.id = wa.player_id
           WHERE wa.award = 'giant_slayer'
           ORDER BY wa.metric_value DESC LIMIT 1"""
    ).fetchone()

    # Day difficulty
    day_diff = conn.execute(
        """SELECT pd.date, AVG(s.time_seconds) AS mean_t, COUNT(s.id) AS n
           FROM puzzle_days pd
           JOIN submissions s ON s.day_id = pd.id
           GROUP BY pd.id HAVING n >= 2
           ORDER BY mean_t DESC"""
    ).fetchall()
    hardest = day_diff[0] if day_diff else None
    easiest = day_diff[-1] if day_diff else None

    # Win streaks: compute by walking each player's history in date order.
    streaks = _longest_streaks(conn)

    return {
        "fastest": [
            dict(r) | {"time_str": format_time(r["time_seconds"]), "display_name": _display(r)}
            for r in fastest
        ],
        "most_trophies": [dict(r) | {"display_name": _display(r)} for r in most_trophies],
        "most_champions": [dict(r) | {"display_name": _display(r)} for r in most_champions],
        "peak_apr": [dict(r) | {"display_name": _display(r)} for r in peak_apr],
        "biggest_gs": dict(biggest_gs) | {"display_name": _display(biggest_gs)} if biggest_gs else None,
        "hardest_day": dict(hardest) if hardest else None,
        "easiest_day": dict(easiest) if easiest else None,
        "longest_streaks": streaks,
    }


def _longest_streaks(conn: sqlite3.Connection) -> list[dict]:
    """A 'win' = finishing first on a day with ≥2 submitters. Streak is
    consecutive day-wins for the same player among days they entered."""
    days = conn.execute(
        """SELECT id, date FROM puzzle_days ORDER BY date ASC"""
    ).fetchall()
    winners: dict[str, int | None] = {}
    for d in days:
        first = conn.execute(
            """SELECT s.player_id, COUNT(*) OVER () AS field
               FROM submissions s WHERE s.day_id = ?
               ORDER BY s.time_seconds ASC LIMIT 1""",
            (d["id"],),
        ).fetchone()
        field = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE day_id = ?", (d["id"],)
        ).fetchone()[0]
        winners[d["date"]] = first["player_id"] if first and field >= 2 else None
    # Walk: maintain best streak per player.
    best: dict[int, int] = defaultdict(int)
    cur_streak: dict[int, int] = defaultdict(int)
    for d in days:
        winner = winners.get(d["date"])
        for pid in list(cur_streak):
            if pid != winner:
                cur_streak[pid] = 0
        if winner is not None:
            cur_streak[winner] += 1
            if cur_streak[winner] > best[winner]:
                best[winner] = cur_streak[winner]
    out = []
    for pid, streak in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        if streak == 0:
            continue
        p = conn.execute(
            "SELECT name, display_name FROM players WHERE id = ?", (pid,)
        ).fetchone()
        if not p:
            continue
        out.append(
            {
                "player_id": pid,
                "display_name": _display(p),
                "streak": streak,
            }
        )
    return out
