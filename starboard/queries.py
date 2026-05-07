"""Read-side queries that views use to render pages."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from starboard.apr import INITIAL_RATING
from starboard.weekly import (
    ALL_AWARDS,
    CHAMPION,
    GIANT_SLAYER,
    IRON_MAN,
    LIGHTNING,
    STEADY,
    compute_weekly_awards,
)


# ---------- formatters ----------


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(seconds, 60)
    if s % 1:
        return f"{int(m)}:{s:05.2f}".rstrip("0").rstrip(".")
    return f"{int(m)}:{int(s):02d}"


def parse_time(raw: str) -> float:
    """Forgiving parser: '90', '1:30', '01:30', '1:30.5' → seconds."""
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


def parse_bulk_time(raw: str) -> float:
    """Bulk-entry parser used by the daily-entry page.

    Accepts the three shapes in the bulk-entry spec — `5:23`, `323`, and
    `5.23` — and normalizes all three to 323 seconds. The dot-as-minute
    separator only kicks in when the right side has exactly two digits and
    is < 60, so `45.5` still reads as 45.5 seconds, not 45 minutes."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty time")
    if ":" not in raw and raw.count(".") == 1:
        left, right = raw.split(".")
        if (
            left.isdigit() and right.isdigit()
            and len(right) == 2 and int(right) < 60
        ):
            raw = f"{left}:{right}"
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
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"unrecognized time format: {raw!r}") from None
    if v < 0:
        raise ValueError(f"negative time: {raw!r}")
    return v


def _display(row) -> str | None:
    if row is None:
        return None
    return row["display_name"] or row["name"]


def _is_late(submitted_at: str | None, day_date: str) -> bool:
    """A submission is late if its `submitted_at` calendar date is after
    the puzzle day's date. Submissions made on the puzzle day itself
    aren't late, even if they came in at 23:58."""
    if not submitted_at:
        return False
    return submitted_at[:10] > day_date


# ---------- general stats ----------


def total_stats(conn: sqlite3.Connection) -> dict:
    days = conn.execute("SELECT COUNT(*) FROM puzzle_days").fetchone()[0]
    subs = conn.execute(
        "SELECT COUNT(*) FROM submissions WHERE status = 'completed'"
    ).fetchone()[0]
    fastest = conn.execute(
        """SELECT s.time_seconds, p.name, p.display_name, pd.date, p.id AS pid
           FROM submissions s
           JOIN players p ON p.id = s.player_id
           JOIN puzzle_days pd ON pd.id = s.day_id
           WHERE s.status = 'completed'
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


# ---------- standings ----------


def standings(conn: sqlite3.Connection) -> list[dict]:
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
            """SELECT rh.rating_after FROM rating_history rh
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
        any_subs = conn.execute(
            """SELECT COUNT(*) FROM submissions s
               JOIN puzzle_days pd ON pd.id = s.day_id
               WHERE s.player_id = ? AND pd.date >= ?""",
            (p["id"], p["joined_date"]),
        ).fetchone()[0]
        attendance = (any_subs / days_avail) if days_avail else 0.0

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
                "submissions": any_subs,
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
        """SELECT pd.date, rh.kind, rh.rating_after, rh.delta, rh.actual_z
           FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           WHERE rh.player_id = ?
           ORDER BY pd.date ASC""",
        (player_id,),
    ).fetchall()

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

    # Recent activity: pull from rating_history (covers completed/dnf/absent)
    recent_activity_rows = conn.execute(
        """SELECT pd.date, pd.id AS day_id, rh.kind, rh.actual_z, rh.delta,
                  s.time_seconds, s.submitted_at, s.assisted
           FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           LEFT JOIN submissions s
               ON s.player_id = rh.player_id AND s.day_id = rh.day_id
           WHERE rh.player_id = ?
           ORDER BY pd.date DESC
           LIMIT 14""",
        (player_id,),
    ).fetchall()
    recent = []
    for r in recent_activity_rows:
        rank_that_day = None
        if r["kind"] == "completed" and r["time_seconds"] is not None:
            rank_that_day = conn.execute(
                """SELECT COUNT(*) + 1 FROM submissions s2
                   WHERE s2.day_id = ? AND s2.status = 'completed'
                     AND s2.time_seconds < ?""",
                (r["day_id"], r["time_seconds"]),
            ).fetchone()[0]
        assisted = bool(r["assisted"]) if r["assisted"] is not None else False
        recent.append(
            {
                "date": r["date"],
                "kind": r["kind"],
                "assisted": assisted,
                "time_seconds": r["time_seconds"],
                "time_str": format_time(r["time_seconds"]) if r["time_seconds"] else "—",
                "z": r["actual_z"],
                "delta": r["delta"],
                "rank": rank_that_day,
                "is_late": _is_late(r["submitted_at"], r["date"]),
            }
        )

    # Stats from completed times only
    all_completed = conn.execute(
        """SELECT time_seconds FROM submissions
           WHERE player_id = ? AND status = 'completed'
           ORDER BY time_seconds ASC""",
        (player_id,),
    ).fetchall()
    all_t = [t["time_seconds"] for t in all_completed]
    fastest = all_t[0] if all_t else None
    median = all_t[len(all_t) // 2] if all_t else None
    peak_rating = max(
        (r["rating_after"] for r in rating_history), default=INITIAL_RATING
    )

    # Day-of-week stats from completed only
    dow_rows = conn.execute(
        """SELECT pd.date, s.time_seconds FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           WHERE s.player_id = ? AND s.status = 'completed'""",
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

    # Counts per status
    status_counts = {
        "completed": conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE player_id = ? AND status = 'completed'",
            (player_id,),
        ).fetchone()[0],
        "dnf": conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE player_id = ? AND status = 'dnf'",
            (player_id,),
        ).fetchone()[0],
        "absent": conn.execute(
            "SELECT COUNT(*) FROM rating_history WHERE player_id = ? AND kind = 'absent'",
            (player_id,),
        ).fetchone()[0],
        "late": conn.execute(
            """SELECT COUNT(*) FROM submissions s
               JOIN puzzle_days pd ON pd.id = s.day_id
               WHERE s.player_id = ? AND substr(s.submitted_at, 1, 10) > pd.date""",
            (player_id,),
        ).fetchone()[0],
    }

    days_avail = conn.execute(
        "SELECT COUNT(*) FROM puzzle_days WHERE date >= ?",
        (p["joined_date"],),
    ).fetchone()[0]
    submitted_count = status_counts["completed"] + status_counts["dnf"]
    attendance = (submitted_count / days_avail) if days_avail else 0.0

    h2h = h2h_for_player(conn, player_id)
    top5 = sorted(h2h, key=lambda r: -(r["wins"] + r["losses"]))[:5]

    heatmap = _attendance_heatmap(conn, player_id, p["joined_date"])
    breakdown = rating_breakdown(conn, player_id)

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
        "submissions_count": submitted_count,
        "status_counts": status_counts,
        "rating_history": [dict(r) for r in rating_history],
        "recent_activity": recent,
        "trophy_case": trophy_case,
        "award_breakdown": award_breakdown,
        "h2h_top5": top5,
        "heatmap": heatmap,
        "rating_breakdown": breakdown,
    }


def rating_breakdown(conn: sqlite3.Connection, player_id: int) -> dict:
    """Per-bucket attribution of a player's current APR.

    Each rating_history row carries a `delta` and a `kind`. Summing the
    deltas grouped by kind tells us how many APR points the player gained
    or lost from completed days, DNFs, and absences respectively. The
    three sums add up exactly to (rating − INITIAL_RATING) by
    construction, so the breakdown explains the entire current rating.
    """
    rows = conn.execute(
        """SELECT kind, COUNT(*) AS n, COALESCE(SUM(delta), 0) AS total_delta
           FROM rating_history WHERE player_id = ? GROUP BY kind""",
        (player_id,),
    ).fetchall()
    out = {
        "completed": {"count": 0, "delta": 0.0},
        "dnf":       {"count": 0, "delta": 0.0},
        "absent":    {"count": 0, "delta": 0.0},
    }
    for r in rows:
        out[r["kind"]] = {"count": int(r["n"]), "delta": float(r["total_delta"] or 0.0)}
    out["total_delta"] = sum(out[k]["delta"] for k in ("completed", "dnf", "absent"))
    out["max_abs"] = max(abs(out[k]["delta"]) for k in ("completed", "dnf", "absent")) or 1.0
    return out


def _attendance_heatmap(
    conn: sqlite3.Connection, player_id: int, joined_date: str
) -> list[dict]:
    """One entry per puzzle_day from joined_date forward.
    kind ∈ {'completed','dnf','absent','unscored'}."""
    rh_rows = conn.execute(
        """SELECT pd.date, rh.kind FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           WHERE rh.player_id = ?""",
        (player_id,),
    ).fetchall()
    rh_by_date = {r["date"]: r["kind"] for r in rh_rows}

    sub_rows = conn.execute(
        """SELECT pd.date, s.status FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           WHERE s.player_id = ?""",
        (player_id,),
    ).fetchall()
    sub_by_date = {r["date"]: r["status"] for r in sub_rows}

    days = conn.execute(
        "SELECT date FROM puzzle_days WHERE date >= ? ORDER BY date ASC",
        (joined_date,),
    ).fetchall()
    out = []
    for d in days:
        date_str = d["date"]
        if date_str in rh_by_date:
            out.append({"date": date_str, "kind": rh_by_date[date_str]})
        elif date_str in sub_by_date:
            # Submitted but day was skipped (<2 completed) — no rating effect
            out.append({"date": date_str, "kind": "unscored"})
        else:
            out.append({"date": date_str, "kind": "unscored"})
    return out


# ---------- head-to-head ----------


def h2h_matrix(conn: sqlite3.Connection) -> dict:
    """Wins[a, b] = 'a finished ahead of b' on shared days. Completed only.
    Returned `players` carry their current APR so the constellation graph can
    size nodes by rating without re-querying."""
    players = conn.execute(
        "SELECT id, name, display_name FROM players WHERE active = 1 ORDER BY id"
    ).fetchall()
    pid_list = [p["id"] for p in players]

    latest_ratings: dict[int, float] = {}
    for p in players:
        cur = conn.execute(
            """SELECT rh.rating_after FROM rating_history rh
               JOIN puzzle_days pd ON pd.id = rh.day_id
               WHERE rh.player_id = ? ORDER BY pd.date DESC LIMIT 1""",
            (p["id"],),
        ).fetchone()
        latest_ratings[p["id"]] = cur["rating_after"] if cur else INITIAL_RATING

    wins: dict[tuple[int, int], int] = defaultdict(int)
    rows = conn.execute(
        """SELECT day_id, player_id, time_seconds FROM submissions
           WHERE status = 'completed'
           ORDER BY day_id, time_seconds ASC"""
    ).fetchall()
    by_day: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        by_day[r["day_id"]].append((r["player_id"], r["time_seconds"]))
    for _day_id, subs in by_day.items():
        for i, (a_pid, _at) in enumerate(subs):
            for b_pid, _bt in subs[i + 1 :]:
                wins[(a_pid, b_pid)] += 1
    return {
        "players": [
            dict(p) | {"display_name": _display(p), "rating": latest_ratings[p["id"]]}
            for p in players
        ],
        "wins": dict(wins),
        "pid_list": pid_list,
    }


def h2h_for_player(conn: sqlite3.Connection, player_id: int) -> list[dict]:
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
    if today is None:
        from starboard import clock

        today = clock.local_today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    rows = conn.execute(
        """SELECT pd.date, s.player_id, s.time_seconds, s.status,
                  p.name, p.display_name
           FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           JOIN players p ON p.id = s.player_id
           WHERE pd.date BETWEEN ? AND ?
           ORDER BY pd.date""",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchall()
    submissions_by_day: dict[str, list[tuple[int, float | None]]] = defaultdict(list)
    name_by_pid: dict[int, str] = {}
    for r in rows:
        time_value = None if r["status"] == "dnf" else float(r["time_seconds"])
        submissions_by_day[r["date"]].append((r["player_id"], time_value))
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
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "days_played": len(submissions_by_day),
        "submission_count": sum(len(v) for v in submissions_by_day.values()),
        "awards": awards_by_kind,
        "name_by_pid": name_by_pid,
    }


# ---------- latest day ----------


def latest_day(conn: sqlite3.Connection) -> dict | None:
    """Latest puzzle_day with metadata + every submission for it.

    `entries` is sorted: completed times ascending, then DNFs. Winner is
    the first entry if any completions exist."""
    from starboard import clock

    today_iso = clock.local_today().isoformat()
    row = conn.execute(
        """SELECT pd.id, pd.date,
                  (SELECT COUNT(*) FROM submissions s2 WHERE s2.day_id = pd.id) AS field_size
           FROM puzzle_days pd
           WHERE pd.date <= ?
           ORDER BY pd.date DESC LIMIT 1""",
        (today_iso,),
    ).fetchone()
    if not row:
        return None
    rows = conn.execute(
        """SELECT s.time_seconds, s.status, s.assisted, p.name, p.display_name, p.id AS pid
           FROM submissions s
           JOIN players p ON p.id = s.player_id
           WHERE s.day_id = ?
           ORDER BY (s.status = 'dnf') ASC,
                    s.time_seconds ASC""",
        (row["id"],),
    ).fetchall()
    entries = []
    rank = 0
    for r in rows:
        is_completed = r["status"] == "completed"
        assisted = bool(r["assisted"]) if r["assisted"] is not None else False
        if is_completed:
            rank += 1
        if is_completed:
            label = format_time(r["time_seconds"])
        elif assisted and r["time_seconds"] is not None:
            label = f"DNF · hints ({format_time(r['time_seconds'])})"
        else:
            label = "DNF"
        entries.append(
            {
                "player_id": r["pid"],
                "display_name": _display(r),
                "status": r["status"],
                "assisted": assisted,
                "time_seconds": r["time_seconds"] if is_completed else None,
                "time_str": label,
                "rank": rank if is_completed else None,
            }
        )
    winner = entries[0] if entries and entries[0]["status"] == "completed" else None
    return {
        "date": row["date"],
        "field_size": row["field_size"],
        "entries": entries,
        "winner_name": winner["display_name"] if winner else None,
        "winner_id": winner["player_id"] if winner else None,
        "winner_time": winner["time_seconds"] if winner else None,
        "winner_time_str": winner["time_str"] if winner else "—",
    }


# ---------- records ----------


def records(conn: sqlite3.Connection) -> dict:
    fastest = conn.execute(
        """SELECT * FROM (
             SELECT s.time_seconds, p.name, p.display_name, p.id AS pid, pd.date,
                    ROW_NUMBER() OVER (
                      PARTITION BY p.id ORDER BY s.time_seconds ASC
                    ) AS rn
             FROM submissions s
             JOIN players p ON p.id = s.player_id
             JOIN puzzle_days pd ON pd.id = s.day_id
             WHERE s.status = 'completed'
           )
           WHERE rn = 1
           ORDER BY time_seconds ASC LIMIT 10"""
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
        """SELECT * FROM (
             SELECT rh.rating_after, p.id, p.name, p.display_name, pd.date,
                    ROW_NUMBER() OVER (
                      PARTITION BY rh.player_id ORDER BY rh.rating_after DESC
                    ) AS rn
             FROM rating_history rh
             JOIN players p ON p.id = rh.player_id
             JOIN puzzle_days pd ON pd.id = rh.day_id
           )
           WHERE rn = 1
           ORDER BY rating_after DESC LIMIT 10"""
    ).fetchall()
    biggest_gs = conn.execute(
        """SELECT wa.metric_value, wa.metric_detail, wa.week_start,
                  p.name, p.display_name, p.id AS pid
           FROM weekly_awards wa
           JOIN players p ON p.id = wa.player_id
           WHERE wa.award = 'giant_slayer'
           ORDER BY wa.metric_value DESC LIMIT 1"""
    ).fetchone()

    day_diff = conn.execute(
        """SELECT pd.date, AVG(s.time_seconds) AS mean_t, COUNT(s.id) AS n
           FROM puzzle_days pd
           JOIN submissions s ON s.day_id = pd.id
           WHERE s.status = 'completed'
           GROUP BY pd.id HAVING n >= 2
           ORDER BY mean_t DESC"""
    ).fetchall()
    hardest = day_diff[0] if day_diff else None
    easiest = day_diff[-1] if day_diff else None

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
    """A 'win' = fastest completed time on a day with ≥2 completed submitters.
    Streak resets on any non-win — including DNFs and absences."""
    days = conn.execute(
        "SELECT id, date FROM puzzle_days ORDER BY date ASC"
    ).fetchall()
    winners: dict[str, int | None] = {}
    for d in days:
        first = conn.execute(
            """SELECT s.player_id FROM submissions s
               WHERE s.day_id = ? AND s.status = 'completed'
               ORDER BY s.time_seconds ASC LIMIT 1""",
            (d["id"],),
        ).fetchone()
        completed_count = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE day_id = ? AND status = 'completed'",
            (d["id"],),
        ).fetchone()[0]
        winners[d["date"]] = first["player_id"] if first and completed_count >= 2 else None
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
