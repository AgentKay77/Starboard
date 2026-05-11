"""Weekly Skirmish: five locked-in awards per closed week.

Pure award computation lives in `compute_weekly_awards`. DB-touching
helpers (`close_week_and_lock_awards`, `recompute_week`,
`recompute_all_weeks`) wrap it.

DNF interaction:
- DNFs *do* count toward Iron Man and toward Champion's ≥4-day eligibility
  (you showed up).
- DNFs are folded into the z-aggregates at z = DNF_FLOOR, so DNFing a hard
  day mathematically equals bombing it on the rating side AND on Champion
  mean. No gaming incentive.
- Giant Slayer compares completed-only pairs (a DNF didn't beat anyone).
- Absent players are excluded from every Skirmish award.

Submission shape passed in: list[tuple[player_id, time_or_None]] where
time=None marks a DNF.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from starboard.apr import DNF_FLOOR, INITIAL_RATING, get_ratings_as_of

CHAMPION = "champion"
IRON_MAN = "iron_man"
LIGHTNING = "lightning"
STEADY = "steady"
GIANT_SLAYER = "giant_slayer"
WEEKEND_WARRIOR = "weekend_warrior"

ALL_AWARDS = (
    CHAMPION, IRON_MAN, LIGHTNING, STEADY, GIANT_SLAYER, WEEKEND_WARRIOR,
)

GIANT_SLAYER_MIN_GAP = 200.0
CHAMPION_MIN_DAYS = 4
STEADY_MIN_DAYS = 3


def _z_scores_per_day(
    submissions_by_day: dict[str, list[tuple[int, float | None]]],
) -> dict[str, dict[int, float]]:
    """Per-day {player_id: z}. DNFs (time=None) get DNF_FLOOR. Days with
    fewer than two completed submitters are skipped entirely."""
    out: dict[str, dict[int, float]] = {}
    for day, subs in submissions_by_day.items():
        completed = [(pid, t) for pid, t in subs if t is not None]
        dnfs = [pid for pid, t in subs if t is None]
        if len(completed) < 2:
            continue
        times = [t for _, t in completed]
        n = len(times)
        mean_t = sum(times) / n
        std_t = math.sqrt(sum((t - mean_t) ** 2 for t in times) / n) or 1.0
        scores: dict[int, float] = {}
        for pid, t in completed:
            scores[pid] = max(-(t - mean_t) / std_t, DNF_FLOOR)
        for pid in dnfs:
            scores[pid] = DNF_FLOOR
        out[day] = scores
    return out


def compute_weekly_awards(
    week_start: str,
    week_end: str,
    submissions_by_day: dict[str, list[tuple[int, float | None]]],
    apr_ratings_at_week_start: dict[int, float],
) -> list[dict]:
    """Return up to 6 award dicts for the week.

    Weekday (Mon-Thu) submissions feed Champion / Iron Man / Lightning /
    Steady / Giant Slayer — the rating-adjacent awards. Friday-Sunday
    submissions feed only the Weekend Warrior trophy."""
    from starboard.clock import is_weekend_date

    weekday_subs = {
        d: subs for d, subs in submissions_by_day.items() if not is_weekend_date(d)
    }
    weekend_subs = {
        d: subs for d, subs in submissions_by_day.items() if is_weekend_date(d)
    }

    z_by_day = _z_scores_per_day(weekday_subs)

    days_played: dict[int, int] = defaultdict(int)
    submissions_count: dict[int, int] = defaultdict(int)
    player_zs: dict[int, list[tuple[str, float]]] = defaultdict(list)

    for day, subs in weekday_subs.items():
        for pid, _t in subs:
            submissions_count[pid] += 1
            days_played[pid] += 1
            if day in z_by_day:
                player_zs[pid].append((day, z_by_day[day][pid]))

    awards: list[dict] = []
    awards.extend(_champion(player_zs, days_played))
    awards.extend(_iron_man(submissions_count, player_zs))
    awards.extend(_lightning(player_zs))
    awards.extend(_steady(player_zs, days_played))
    awards.extend(_giant_slayer(weekday_subs, apr_ratings_at_week_start))
    awards.extend(_weekend_warrior(weekend_subs))
    return awards


def _weekend_warrior(weekend_subs) -> list[dict]:
    """Most days won across Fri/Sat/Sun. Tiebreak: most weekend submissions.

    Wins = days where the player had the fastest completed time among that
    day's completed submitters. A day with zero completions yields no win
    for anyone (but still counts toward submissions for engagement)."""
    wins: dict[int, int] = defaultdict(int)
    subs_count: dict[int, int] = defaultdict(int)
    for day, day_subs in weekend_subs.items():
        for pid, _t in day_subs:
            subs_count[pid] += 1
        completed = [(pid, t) for pid, t in day_subs if t is not None]
        if completed:
            winner_pid = min(completed, key=lambda s: s[1])[0]
            wins[winner_pid] += 1

    pids = set(wins) | set(subs_count)
    if not pids:
        return []
    cands = [(pid, wins.get(pid, 0), subs_count.get(pid, 0)) for pid in pids]
    cands.sort(key=lambda c: (-c[1], -c[2], c[0]))
    pid, w, s = cands[0]
    if w == 0 and s == 0:
        return []
    return [
        {
            "award": WEEKEND_WARRIOR,
            "player_id": pid,
            "metric_value": float(w),
            "metric_detail": json.dumps({"wins": w, "submissions": s}),
        }
    ]


def _champion(player_zs, days_played) -> list[dict]:
    cands = []
    for pid, zs in player_zs.items():
        if days_played[pid] < CHAMPION_MIN_DAYS:
            continue
        zlist = [z for _, z in zs]
        mean_z = sum(zlist) / len(zlist)
        peak_z = max(zlist)
        cands.append((pid, mean_z, days_played[pid], peak_z))
    if not cands:
        return []
    cands.sort(key=lambda c: (-c[1], -c[2], -c[3], c[0]))
    pid, mean_z, days, peak_z = cands[0]
    return [
        {
            "award": CHAMPION,
            "player_id": pid,
            "metric_value": mean_z,
            "metric_detail": json.dumps({"days_played": days, "peak_z": peak_z}),
        }
    ]


def _iron_man(submissions_count, player_zs) -> list[dict]:
    cands = []
    for pid, count in submissions_count.items():
        zs = [z for _, z in player_zs.get(pid, [])]
        mean_z = sum(zs) / len(zs) if zs else 0.0
        cands.append((pid, count, mean_z))
    if not cands:
        return []
    cands.sort(key=lambda c: (-c[1], -c[2], c[0]))
    pid, count, mean_z = cands[0]
    return [
        {
            "award": IRON_MAN,
            "player_id": pid,
            "metric_value": float(count),
            "metric_detail": json.dumps({"submissions": count}),
        }
    ]


def _lightning(player_zs) -> list[dict]:
    cands = []
    for pid, zs in player_zs.items():
        if not zs:
            continue
        peak_date, peak_z = max(zs, key=lambda dz: (dz[1], dz[0]))
        zlist = [z for _, z in zs]
        mean_z = sum(zlist) / len(zlist)
        cands.append((pid, peak_z, mean_z, peak_date))
    if not cands:
        return []
    cands.sort(key=lambda c: (-c[1], -c[2], c[0]))
    pid, peak_z, _mean_z, peak_date = cands[0]
    return [
        {
            "award": LIGHTNING,
            "player_id": pid,
            "metric_value": peak_z,
            "metric_detail": json.dumps({"date": peak_date}),
        }
    ]


def _steady(player_zs, days_played) -> list[dict]:
    cands = []
    for pid, zs in player_zs.items():
        if days_played[pid] < STEADY_MIN_DAYS:
            continue
        zlist = [z for _, z in zs]
        if len(zlist) < 2:
            continue
        std_z = statistics.pstdev(zlist)
        cands.append((pid, std_z, days_played[pid]))
    if not cands:
        return []
    cands.sort(key=lambda c: (c[1], -c[2], c[0]))
    pid, std_z, days = cands[0]
    return [
        {
            "award": STEADY,
            "player_id": pid,
            "metric_value": std_z,
            "metric_detail": json.dumps({"days_played": days}),
        }
    ]


def _giant_slayer(submissions_by_day, apr_ratings) -> list[dict]:
    """Largest rating-gap upset of the week. A DNF never qualifies as a
    winner — they didn't finish ahead of anyone."""
    upsets: list[tuple[float, str, int, int]] = []
    for day, subs in submissions_by_day.items():
        completed = [(pid, t) for pid, t in subs if t is not None]
        if len(completed) < 2:
            continue
        sorted_subs = sorted(completed, key=lambda s: s[1])
        for i, (winner_pid, _wt) in enumerate(sorted_subs):
            winner_r = apr_ratings.get(winner_pid, INITIAL_RATING)
            for loser_pid, _lt in sorted_subs[i + 1 :]:
                loser_r = apr_ratings.get(loser_pid, INITIAL_RATING)
                gap = loser_r - winner_r
                if gap >= GIANT_SLAYER_MIN_GAP:
                    upsets.append((gap, day, winner_pid, loser_pid))
    if not upsets:
        return []
    upsets.sort(key=lambda u: u[2])
    upsets.sort(key=lambda u: u[1], reverse=True)
    upsets.sort(key=lambda u: u[0], reverse=True)
    gap, day, winner_pid, loser_pid = upsets[0]
    return [
        {
            "award": GIANT_SLAYER,
            "player_id": winner_pid,
            "metric_value": gap,
            "metric_detail": json.dumps(
                {"giant_id": loser_pid, "date": day, "gap": gap}
            ),
        }
    ]


def week_bounds(week_start: str) -> tuple[str, str]:
    start = date.fromisoformat(week_start)
    if start.weekday() != 0:
        raise ValueError(f"week_start {week_start} is not a Monday")
    return week_start, (start + timedelta(days=6)).isoformat()


def _load_submissions_for_week(
    conn: sqlite3.Connection, week_start: str, week_end: str
) -> dict[str, list[tuple[int, float | None]]]:
    rows = conn.execute(
        """SELECT pd.date, s.player_id, s.time_seconds, s.status
           FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           WHERE pd.date BETWEEN ? AND ?""",
        (week_start, week_end),
    ).fetchall()
    out: dict[str, list[tuple[int, float | None]]] = defaultdict(list)
    for d, pid, t, status in rows:
        time_value = None if status == "dnf" else float(t)
        out[d].append((int(pid), time_value))
    return dict(out)


def close_week_and_lock_awards(conn: sqlite3.Connection, week_start: str) -> None:
    _, week_end = week_bounds(week_start)
    submissions = _load_submissions_for_week(conn, week_start, week_end)
    apr_ratings = get_ratings_as_of(conn, week_start)
    awards = compute_weekly_awards(week_start, week_end, submissions, apr_ratings)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.cursor()
    for a in awards:
        cur.execute(
            """INSERT OR IGNORE INTO weekly_awards
                   (week_start, week_end, award, player_id,
                    metric_value, metric_detail, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                week_start,
                week_end,
                a["award"],
                a["player_id"],
                a["metric_value"],
                a["metric_detail"],
                now,
            ),
        )
    conn.commit()


def recompute_week(conn: sqlite3.Connection, week_start: str) -> None:
    conn.execute("DELETE FROM weekly_awards WHERE week_start = ?", (week_start,))
    conn.commit()
    close_week_and_lock_awards(conn, week_start)


def recompute_all_weeks(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT MIN(date), MAX(date) FROM puzzle_days").fetchone()
    if not rows or rows[0] is None:
        return
    first = date.fromisoformat(rows[0])
    last = date.fromisoformat(rows[1])
    from starboard import clock

    today = clock.local_today()
    open_week_start = today - timedelta(days=today.weekday())
    cur = first - timedelta(days=first.weekday())
    conn.execute("DELETE FROM weekly_awards")
    conn.commit()
    while cur <= last:
        if cur < open_week_start:
            close_week_and_lock_awards(conn, cur.isoformat())
        cur += timedelta(days=7)
