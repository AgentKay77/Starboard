"""Weekly Skirmish award tests: eligibility rules and tie-breakers."""
from __future__ import annotations

import json
import math

import pytest

from starboard.weekly import (
    CHAMPION,
    GIANT_SLAYER,
    GIANT_SLAYER_MIN_GAP,
    IRON_MAN,
    LIGHTNING,
    STEADY,
    close_week_and_lock_awards,
    compute_weekly_awards,
    recompute_week,
)
from tests.conftest import insert_day, insert_submission


WEEK_START = "2026-01-05"  # Monday
WEEK_END = "2026-01-11"  # Sunday


def _award(awards, name):
    for a in awards:
        if a["award"] == name:
            return a
    return None


# ---------- compute_weekly_awards (pure) ----------


def test_no_submissions_no_awards():
    awards = compute_weekly_awards(WEEK_START, WEEK_END, {}, {})
    assert awards == []


def test_champion_requires_4_days():
    """3 days of play → no Champion (eligibility threshold is ≥4)."""
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],
        "2026-01-06": [(1, 50.0), (2, 60.0)],
        "2026-01-07": [(1, 50.0), (2, 60.0)],
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    assert _award(awards, CHAMPION) is None


def test_champion_picks_highest_mean_z():
    """Player 1 wins all 4 days → highest mean z → Champion."""
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0), (3, 70.0)],
        "2026-01-06": [(1, 50.0), (2, 60.0), (3, 70.0)],
        "2026-01-07": [(1, 50.0), (2, 60.0), (3, 70.0)],
        "2026-01-08": [(1, 50.0), (2, 60.0), (3, 70.0)],
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    champ = _award(awards, CHAMPION)
    assert champ is not None
    assert champ["player_id"] == 1
    assert champ["metric_value"] > 0
    detail = json.loads(champ["metric_detail"])
    assert detail["days_played"] == 4


def test_champion_tiebreak_days_played():
    """Two players tied on mean z; the one who played more days wins."""
    # Each day is symmetric: only player 1 and 2 play, alternating winners.
    # Player 1 also plays a 5th day alone (won't add a z-score, but adds days).
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],  # P1 wins (z=+1, P2 z=-1)
        "2026-01-06": [(1, 60.0), (2, 50.0)],  # P2 wins
        "2026-01-07": [(1, 50.0), (2, 60.0)],  # P1 wins
        "2026-01-08": [(1, 60.0), (2, 50.0)],  # P2 wins
        # Both have mean z = 0 over 4 days. Now P1 plays a 5th solo day.
        "2026-01-09": [(1, 55.0)],  # solo: no z, but +1 day_played
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    champ = _award(awards, CHAMPION)
    assert champ is not None
    assert champ["player_id"] == 1  # 5 days_played beats 4


def test_champion_tiebreak_player_id_final_fallback():
    """Total tie on every metric → lowest player_id."""
    subs = {
        "2026-01-05": [(1, 60.0), (2, 60.0)],  # both z=0
        "2026-01-06": [(1, 60.0), (2, 60.0)],
        "2026-01-07": [(1, 60.0), (2, 60.0)],
        "2026-01-08": [(1, 60.0), (2, 60.0)],
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    champ = _award(awards, CHAMPION)
    assert champ is not None
    assert champ["player_id"] == 1


def test_iron_man_most_submissions():
    subs = {
        "2026-01-05": [(1, 60.0), (2, 60.0), (3, 60.0)],
        "2026-01-06": [(1, 60.0), (2, 60.0)],
        "2026-01-07": [(1, 60.0)],
        "2026-01-08": [(1, 60.0)],
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    iron = _award(awards, IRON_MAN)
    assert iron is not None
    assert iron["player_id"] == 1
    assert iron["metric_value"] == 4.0


def test_iron_man_tiebreak_uses_mean_z():
    """Tied submission counts → the one with higher mean z wins."""
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],  # P1 z=+1, P2 z=-1
        "2026-01-06": [(1, 50.0), (2, 60.0)],  # P1 z=+1, P2 z=-1
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    iron = _award(awards, IRON_MAN)
    assert iron is not None
    assert iron["player_id"] == 1


def test_lightning_picks_single_day_peak():
    """Lightning is a one-day spike. Player 2 has the highest single z though
    overall mean lower."""
    subs = {
        # Day 1: P1 wins narrowly over P2 → both small magnitude z.
        "2026-01-05": [(1, 59.0), (2, 61.0)],
        # Day 2: P2 dominates → P2 z is much larger.
        "2026-01-06": [(1, 100.0), (2, 30.0), (3, 100.0)],
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    lightning = _award(awards, LIGHTNING)
    assert lightning is not None
    assert lightning["player_id"] == 2
    detail = json.loads(lightning["metric_detail"])
    assert detail["date"] == "2026-01-06"


def test_steady_requires_3_days():
    """≤2 days played: no Steady award even if other awards land."""
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],
        "2026-01-06": [(1, 50.0), (2, 60.0)],
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    assert _award(awards, STEADY) is None


def test_steady_picks_lowest_std():
    """Player with most consistent z-scores wins Steady, not the highest-mean one."""
    # P1 wins every day by the same margin → consistent z each day.
    # P2 swings: dominates one day, last another.
    subs = {
        "2026-01-05": [(1, 55.0), (2, 60.0), (3, 65.0)],  # symmetric
        "2026-01-06": [(1, 55.0), (2, 60.0), (3, 65.0)],
        "2026-01-07": [(1, 55.0), (2, 60.0), (3, 65.0)],
        "2026-01-08": [(1, 55.0), (2, 65.0), (3, 60.0)],  # P2 last, P3 middle
    }
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, {})
    steady = _award(awards, STEADY)
    assert steady is not None
    assert steady["player_id"] == 1  # P1's z is the same every day → std = 0


def test_giant_slayer_picks_largest_upset():
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],  # P1 beats P2
        "2026-01-06": [(3, 50.0), (4, 60.0)],  # P3 beats P4
    }
    ratings = {1: 1300, 2: 1700, 3: 1300, 4: 1600}
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, ratings)
    gs = _award(awards, GIANT_SLAYER)
    assert gs is not None
    assert gs["player_id"] == 1
    assert math.isclose(gs["metric_value"], 400.0, abs_tol=1e-9)
    detail = json.loads(gs["metric_detail"])
    assert detail["giant_id"] == 2
    assert detail["date"] == "2026-01-05"


def test_giant_slayer_threshold_excludes_small_upsets():
    """A 199-point gap doesn't qualify."""
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],
    }
    ratings = {1: 1400, 2: 1599}
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, ratings)
    assert _award(awards, GIANT_SLAYER) is None


def test_giant_slayer_threshold_exactly_200_qualifies():
    subs = {"2026-01-05": [(1, 50.0), (2, 60.0)]}
    ratings = {1: 1400, 2: 1600}
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, ratings)
    gs = _award(awards, GIANT_SLAYER)
    assert gs is not None
    assert math.isclose(gs["metric_value"], GIANT_SLAYER_MIN_GAP, abs_tol=1e-9)


def test_giant_slayer_no_upset_when_higher_rated_wins():
    subs = {"2026-01-05": [(2, 50.0), (1, 60.0)]}
    # P2 (higher rated) wins → no upset.
    ratings = {1: 1300, 2: 1700}
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, ratings)
    assert _award(awards, GIANT_SLAYER) is None


def test_giant_slayer_tiebreak_more_recent_date():
    """Two equal-gap upsets on different days → most recent wins."""
    subs = {
        "2026-01-05": [(1, 50.0), (2, 60.0)],  # earlier
        "2026-01-09": [(3, 50.0), (4, 60.0)],  # later
    }
    ratings = {1: 1300, 2: 1700, 3: 1300, 4: 1700}  # both gaps = 400
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, ratings)
    gs = _award(awards, GIANT_SLAYER)
    assert gs is not None
    detail = json.loads(gs["metric_detail"])
    assert detail["date"] == "2026-01-09"


def test_unknown_player_defaults_to_initial_rating():
    """Players not in the apr_ratings dict are treated as 1500 — so an
    unknown player vs a 1700 player is a 200 gap, qualifies for Giant Slayer."""
    subs = {"2026-01-05": [(99, 50.0), (1, 60.0)]}
    ratings = {1: 1700}
    awards = compute_weekly_awards(WEEK_START, WEEK_END, subs, ratings)
    gs = _award(awards, GIANT_SLAYER)
    assert gs is not None
    assert math.isclose(gs["metric_value"], 200.0, abs_tol=1e-9)


# ---------- close/recompute helpers (DB-touching) ----------


@pytest.fixture
def week_seeded(seeded_conn):
    """Seed a Mon-Sun week of data for the DB-level award helpers."""
    conn = seeded_conn
    days = {}
    for offset, iso in enumerate(
        [
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
        ]
    ):
        days[iso] = insert_day(conn, iso)
        # Player 1 wins every day; 2 second; 3 third.
        insert_submission(conn, 1, days[iso], 50.0)
        insert_submission(conn, 2, days[iso], 60.0)
        insert_submission(conn, 3, days[iso], 70.0)
    return conn


def test_close_week_inserts_awards(week_seeded):
    close_week_and_lock_awards(week_seeded, WEEK_START)
    rows = week_seeded.execute(
        "SELECT award, player_id FROM weekly_awards WHERE week_start = ?",
        (WEEK_START,),
    ).fetchall()
    awards = {r["award"]: r["player_id"] for r in rows}
    # Champion: player 1 wins. Iron Man: tied 4 each → tiebreak by mean z (P1).
    # Lightning: P1 has highest single-day z. Steady: P1 (constant z).
    # Giant Slayer: no rating gaps without prior history → none.
    assert awards.get(CHAMPION) == 1
    assert awards.get(IRON_MAN) == 1
    assert awards.get(LIGHTNING) == 1
    assert awards.get(STEADY) == 1
    assert GIANT_SLAYER not in awards


def test_close_week_is_idempotent(week_seeded):
    """Calling close twice should not duplicate rows nor overwrite."""
    close_week_and_lock_awards(week_seeded, WEEK_START)
    first = week_seeded.execute(
        "SELECT award, player_id, metric_value FROM weekly_awards"
    ).fetchall()
    close_week_and_lock_awards(week_seeded, WEEK_START)
    second = week_seeded.execute(
        "SELECT award, player_id, metric_value FROM weekly_awards"
    ).fetchall()
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_recompute_week_overwrites(week_seeded):
    """Admin edits a submission then recomputes — the locked rows update."""
    close_week_and_lock_awards(week_seeded, WEEK_START)
    # Now flip player 2 to dominate Monday — make them a faster outlier.
    week_seeded.execute(
        """UPDATE submissions SET time_seconds = 10
           WHERE player_id = 2
             AND day_id = (SELECT id FROM puzzle_days WHERE date = ?)""",
        ("2026-01-05",),
    )
    week_seeded.commit()
    recompute_week(week_seeded, WEEK_START)
    lightning = week_seeded.execute(
        "SELECT player_id FROM weekly_awards WHERE week_start = ? AND award = ?",
        (WEEK_START, LIGHTNING),
    ).fetchone()
    assert lightning["player_id"] == 2  # P2's blow-out Monday is now peak


def test_week_start_must_be_monday(week_seeded):
    with pytest.raises(ValueError):
        close_week_and_lock_awards(week_seeded, "2026-01-06")  # Tuesday
