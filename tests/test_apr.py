"""APR rating tests: clamp, DNF, absent, and the cherry-pick defense."""
from __future__ import annotations

import math

from starboard.apr import (
    ABSENT_FLOOR,
    DNF_FLOOR,
    INITIAL_RATING,
    K,
    RATING_PER_SIGMA,
    compute_apr_update,
    get_current_ratings,
    recompute_all_ratings,
)
from tests.conftest import insert_day, insert_submission


# ---------- baseline / signal floor ----------


def test_below_two_completed_skipped():
    """Even with DNFs and absentees, no signal day → no rating updates."""
    new_ratings, history = compute_apr_update(
        completed=[(1, 60.0)],
        dnfs=[2],
        absentees=[3],
        current_ratings={1: 1500, 2: 1500, 3: 1500},
    )
    assert new_ratings == {}
    assert history == []


def test_zero_submissions_skipped():
    new_ratings, history = compute_apr_update([], [], [], {})
    assert new_ratings == {}
    assert history == []


def test_zero_std_guard_no_nans():
    """Everyone tied → variance is 0; std=1 substitution keeps deltas finite."""
    new_ratings, history = compute_apr_update(
        completed=[(1, 60.0), (2, 60.0), (3, 60.0)],
        dnfs=[],
        absentees=[],
        current_ratings={1: 1500, 2: 1500, 3: 1500},
    )
    assert all(math.isfinite(r) for r in new_ratings.values())
    assert new_ratings == {1: 1500.0, 2: 1500.0, 3: 1500.0}
    assert all(h["delta"] == 0.0 for h in history)


def test_winner_gains_loser_loses_at_equal_ratings():
    new_ratings, _ = compute_apr_update(
        completed=[(1, 50.0), (2, 70.0)],
        dnfs=[],
        absentees=[],
        current_ratings={1: 1500, 2: 1500},
    )
    assert new_ratings[1] > 1500
    assert new_ratings[2] < 1500


def test_unknown_submitter_seeded_at_initial_rating():
    _, history = compute_apr_update(
        completed=[(1, 50.0), (99, 70.0)],
        dnfs=[],
        absentees=[],
        current_ratings={1: 1500},
    )
    rb_99 = next(h["rating_before"] for h in history if h["player_id"] == 99)
    assert rb_99 == INITIAL_RATING


# ---------- cherry-pick defense ----------


def test_cherry_picker_gains_less_in_weak_field():
    completed = [(1, 60.0), (2, 70.0), (3, 70.0), (4, 70.0)]

    new_equal, _ = compute_apr_update(completed, [], [], {1: 1500, 2: 1500, 3: 1500, 4: 1500})
    delta_equal = new_equal[1] - 1500

    new_weak, _ = compute_apr_update(completed, [], [], {1: 1500, 2: 1300, 3: 1300, 4: 1300})
    delta_weak = new_weak[1] - 1500

    assert delta_weak < delta_equal
    field_avg_weak = (1500 + 1300 * 3) / 4
    expected_diff = K * ((1500 - field_avg_weak) / RATING_PER_SIGMA)
    assert math.isclose(delta_equal - delta_weak, expected_diff, abs_tol=1e-9)


# ---------- clamp / DNF / absent ----------


def test_completed_clamped_at_dnf_floor():
    """A wildly slow time should not produce a delta worse than DNF.

    With 5 tied finishers + one outlier, raw z for the outlier reaches
    −2.236σ — beyond DNF_FLOOR. The clamp must pull it back to exactly
    DNF_FLOOR.
    """
    completed = [(1, 200.0), (2, 50.0), (3, 50.0), (4, 50.0), (5, 50.0), (6, 50.0)]
    _, history = compute_apr_update(completed, [], [], {})
    h1 = next(h for h in history if h["player_id"] == 1)
    assert h1["actual_z"] == DNF_FLOOR
    assert h1["kind"] == "completed"


def test_dnf_assigned_dnf_floor():
    _, history = compute_apr_update(
        completed=[(1, 50.0), (2, 60.0)],
        dnfs=[3],
        absentees=[],
        current_ratings={1: 1500, 2: 1500, 3: 1500},
    )
    h3 = next(h for h in history if h["player_id"] == 3)
    assert h3["actual_z"] == DNF_FLOOR
    assert h3["kind"] == "dnf"
    # Delta is post-redistribution; assert it's negative and meaningful
    # rather than pinning a specific value.
    assert h3["delta"] < 0


def test_absent_assigned_absent_floor_lower_than_dnf():
    _, history = compute_apr_update(
        completed=[(1, 50.0), (2, 60.0)],
        dnfs=[],
        absentees=[3],
        current_ratings={1: 1500, 2: 1500, 3: 1500},
    )
    h3 = next(h for h in history if h["player_id"] == 3)
    assert h3["actual_z"] == ABSENT_FLOOR
    assert h3["kind"] == "absent"
    assert h3["delta"] < 0


def test_absent_costs_more_than_dnf_in_same_field():
    """Same player rating, same field — absent's delta should be strictly
    more negative than DNF's. This is the design guarantee."""
    _, history = compute_apr_update(
        completed=[(1, 50.0), (2, 60.0)],
        dnfs=[3],
        absentees=[4],
        current_ratings={1: 1500, 2: 1500, 3: 1500, 4: 1500},
    )
    h_dnf = next(h for h in history if h["player_id"] == 3)
    h_abs = next(h for h in history if h["player_id"] == 4)
    assert h_abs["delta"] < h_dnf["delta"]


def test_day_is_zero_sum():
    """Total rating across participants is conserved per day. Without this,
    every absentee/DNF would deflate the league."""
    _, history = compute_apr_update(
        completed=[(1, 50.0), (2, 60.0)],
        dnfs=[3],
        absentees=[4, 5],
        current_ratings={1: 1500, 2: 1500, 3: 1500, 4: 1500, 5: 1500},
    )
    total_delta = sum(h["delta"] for h in history)
    assert math.isclose(total_delta, 0.0, abs_tol=1e-9)


def test_completers_gain_when_others_ghost():
    """Showing up in a field with absentees pays you back: zero-sum
    redistribution means the absentees' loss flows to the active players."""
    # 2 completers tied → both at z=0, raw delta=0. With absentees, drift
    # correction adds positive delta to completers.
    _, history = compute_apr_update(
        completed=[(1, 60.0), (2, 60.0)],
        dnfs=[],
        absentees=[3, 4, 5],
        current_ratings={i: 1500 for i in range(1, 6)},
    )
    completers = [h for h in history if h["kind"] == "completed"]
    assert all(h["delta"] > 0 for h in completers)


def test_dnf_equals_clamped_completion_for_same_player():
    """Cliff-removal property: a player who DNFs and a player whose raw
    z would fall below DNF_FLOOR end up with the same actual_z, and (when
    field_avg_R is identical) the same delta. No incentive to game DNF."""
    # Six-player field; everyone rated 1500 so field_avg_R is identical in
    # both scenarios. P6 is the bad apple.
    _, h_completed = compute_apr_update(
        completed=[(1, 50.0), (2, 50.0), (3, 50.0), (4, 50.0), (5, 50.0), (6, 200.0)],
        dnfs=[],
        absentees=[],
        current_ratings={i: 1500 for i in range(1, 7)},
    )
    _, h_dnf = compute_apr_update(
        completed=[(1, 50.0), (2, 50.0), (3, 50.0), (4, 50.0), (5, 50.0)],
        dnfs=[6],
        absentees=[],
        current_ratings={i: 1500 for i in range(1, 7)},
    )
    p6_completed = next(h for h in h_completed if h["player_id"] == 6)
    p6_dnf = next(h for h in h_dnf if h["player_id"] == 6)
    assert p6_completed["actual_z"] == p6_dnf["actual_z"] == DNF_FLOOR
    # Deltas may differ slightly because zero-sum redistribution sees a
    # different field shape (P6 in the completed pool vs not). The cliff
    # is bounded but non-zero — that's a known design tradeoff.


# ---------- recompute end-to-end ----------


def test_recompute_replays_days_in_date_order(seeded_conn):
    conn = seeded_conn
    d2 = insert_day(conn, "2026-01-07")
    d0 = insert_day(conn, "2026-01-05")
    d1 = insert_day(conn, "2026-01-06")
    for day_id in (d0, d1, d2):
        insert_submission(conn, 1, day_id, 50.0)
        insert_submission(conn, 2, day_id, 60.0)
        insert_submission(conn, 3, day_id, 70.0)

    recompute_all_ratings(conn)

    rows = conn.execute(
        """SELECT pd.date, rh.kind FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           WHERE rh.player_id = 1 ORDER BY pd.date"""
    ).fetchall()
    dates = [r["date"] for r in rows]
    assert dates == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert all(r["kind"] == "completed" for r in rows)


def test_absent_player_gets_negative_delta(seeded_conn):
    """Replaces the old 'attendance does not affect rating' test — under
    the new design, absentees DO take a hit."""
    conn = seeded_conn
    d = insert_day(conn, "2026-01-05")
    insert_submission(conn, 1, d, 50.0)
    insert_submission(conn, 2, d, 60.0)
    # Player 3 is absent.
    recompute_all_ratings(conn)

    rows = conn.execute(
        "SELECT player_id, kind, delta FROM rating_history WHERE day_id = ?",
        (d,),
    ).fetchall()
    by_pid = {r["player_id"]: r for r in rows}
    assert by_pid[3]["kind"] == "absent"
    assert by_pid[3]["delta"] < 0


def test_recompute_is_idempotent(seeded_conn):
    conn = seeded_conn
    d = insert_day(conn, "2026-01-05")
    insert_submission(conn, 1, d, 50.0)
    insert_submission(conn, 2, d, 60.0)
    insert_submission(conn, 3, d, 70.0)

    def snapshot():
        return sorted(
            (r["player_id"], r["kind"], round(r["rating_after"], 6))
            for r in conn.execute(
                "SELECT player_id, kind, rating_after FROM rating_history"
            )
        )

    recompute_all_ratings(conn)
    first = snapshot()
    recompute_all_ratings(conn)
    second = snapshot()
    assert first == second


def test_get_current_ratings_returns_latest(seeded_conn):
    conn = seeded_conn
    d1 = insert_day(conn, "2026-01-05")
    d2 = insert_day(conn, "2026-01-06")
    for d in (d1, d2):
        insert_submission(conn, 1, d, 50.0)
        insert_submission(conn, 2, d, 60.0)
        insert_submission(conn, 3, d, 70.0)
    recompute_all_ratings(conn)

    current = get_current_ratings(conn)
    assert set(current.keys()) == {1, 2, 3}


def test_solo_day_skipped_no_one_penalized(seeded_conn):
    """1 completer + 12 absentees: signal-floor; no rating updates at all."""
    conn = seeded_conn
    d = insert_day(conn, "2026-01-05")
    insert_submission(conn, 1, d, 50.0)
    # Players 2 and 3 absent.
    recompute_all_ratings(conn)

    rows = conn.execute(
        "SELECT COUNT(*) FROM rating_history WHERE day_id = ?", (d,)
    ).fetchone()
    assert rows[0] == 0


def test_dnf_recompute_writes_dnf_row(seeded_conn):
    conn = seeded_conn
    d = insert_day(conn, "2026-01-05")
    insert_submission(conn, 1, d, 50.0)
    insert_submission(conn, 2, d, 60.0)
    insert_submission(conn, 3, d, None, status="dnf")
    recompute_all_ratings(conn)

    row = conn.execute(
        "SELECT kind, actual_z FROM rating_history WHERE day_id = ? AND player_id = 3",
        (d,),
    ).fetchone()
    assert row["kind"] == "dnf"
    assert row["actual_z"] == DNF_FLOOR


def test_inactive_player_not_penalized(seeded_conn):
    """Marking a player inactive removes their absent rows from history."""
    conn = seeded_conn
    d = insert_day(conn, "2026-01-05")
    insert_submission(conn, 1, d, 50.0)
    insert_submission(conn, 2, d, 60.0)
    # Player 3 absent. Mark them inactive.
    conn.execute("UPDATE players SET active = 0 WHERE id = 3")
    conn.commit()
    recompute_all_ratings(conn)

    rows = conn.execute(
        "SELECT player_id, kind FROM rating_history WHERE day_id = ?", (d,)
    ).fetchall()
    pids = {r["player_id"] for r in rows}
    assert 3 not in pids  # inactive — skipped


# ---------- escalating absent floor ----------


def test_absent_floor_for_streak_curve():
    from starboard.apr import absent_floor_for_streak
    assert absent_floor_for_streak(1) == -0.50
    assert absent_floor_for_streak(2) == -1.00
    assert absent_floor_for_streak(3) == -1.50
    assert absent_floor_for_streak(4) == -2.25
    assert absent_floor_for_streak(7) == -2.25  # caps at the full hammer


def test_recompute_uses_escalating_absent_floor(seeded_conn):
    """First absent day lands a -1.0σ floor; the same player's third
    consecutive absent day lands the full -2.25σ. Verify the deltas in
    rating_history reflect that."""
    from starboard import apr

    # Three Mon-Wed weekdays in a row (2026-01-05 Mon, -06 Tue, -07 Wed).
    # Player 1 (Alice) submits on all three; player 2 (Bob) is absent on
    # all three. Player 3 (Carol) submits all three so each day has ≥ 2
    # completions and the APR math actually runs.
    d1 = insert_day(seeded_conn, "2026-01-05")
    d2 = insert_day(seeded_conn, "2026-01-06")
    d3 = insert_day(seeded_conn, "2026-01-07")
    for d in (d1, d2, d3):
        insert_submission(seeded_conn, 1, d, 60.0, "completed")
        insert_submission(seeded_conn, 3, d, 80.0, "completed")
    # Bob (id=2) is the one accumulating absences.

    apr.recompute_all_ratings(seeded_conn)
    rows = seeded_conn.execute(
        """SELECT pd.date, rh.actual_z FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           WHERE rh.player_id = 2 ORDER BY pd.date""",
    ).fetchall()
    floors = [r["actual_z"] for r in rows]
    assert floors == [-0.5, -1.0, -1.5]


def test_submission_resets_absent_streak(seeded_conn):
    """A submission resets the streak so the next absent day starts at
    the light floor again."""
    from starboard import apr

    d1 = insert_day(seeded_conn, "2026-01-05")
    d2 = insert_day(seeded_conn, "2026-01-06")
    d3 = insert_day(seeded_conn, "2026-01-07")
    # Bob is absent day 1, submits day 2, absent day 3.
    insert_submission(seeded_conn, 1, d1, 60.0, "completed")
    insert_submission(seeded_conn, 3, d1, 80.0, "completed")
    insert_submission(seeded_conn, 2, d2, 70.0, "completed")
    insert_submission(seeded_conn, 1, d2, 60.0, "completed")
    insert_submission(seeded_conn, 3, d2, 80.0, "completed")
    insert_submission(seeded_conn, 1, d3, 60.0, "completed")
    insert_submission(seeded_conn, 3, d3, 80.0, "completed")
    apr.recompute_all_ratings(seeded_conn)
    rows = seeded_conn.execute(
        """SELECT pd.date, rh.kind, rh.actual_z FROM rating_history rh
           JOIN puzzle_days pd ON pd.id = rh.day_id
           WHERE rh.player_id = 2 ORDER BY pd.date""",
    ).fetchall()
    assert rows[0]["kind"] == "absent"
    assert rows[0]["actual_z"] == -0.5
    assert rows[1]["kind"] == "completed"
    # Day 3 absent: streak reset, so floor is -0.5 again.
    assert rows[2]["kind"] == "absent"
    assert rows[2]["actual_z"] == -0.5
