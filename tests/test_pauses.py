"""Pause / vacation windows: APR exemption, heatmap marking, ownership rules."""
from __future__ import annotations

from datetime import date

import pytest

from starboard import apr, pauses
from tests.conftest import insert_day, insert_submission


def _add_active_player(conn, name, joined):
    cur = conn.execute(
        "INSERT INTO players (name, active, joined_date) VALUES (?, 1, ?)",
        (name, joined),
    )
    conn.commit()
    return cur.lastrowid


def _add_stub_user(conn):
    """player_pauses.created_by_user_id has an FK; tests need a real users row."""
    cur = conn.execute(
        """INSERT INTO users (email, username, password_hash, role, created_at)
           VALUES ('x@x', 'x', 'x', 'admin', '2026-01-01T00:00:00')"""
    )
    conn.commit()
    return cur.lastrowid


def test_pause_create_and_list(conn):
    pid = _add_active_player(conn, "Alice", "2026-01-01")
    pid2 = _add_active_player(conn, "Bob", "2026-01-01")
    pauses.create(
        conn, player_id=pid, start_date="2026-02-01",
        end_date="2026-02-10", reason="vacation", created_by_user_id=_add_stub_user(conn),
    )
    rows = pauses.list_for_player(conn, pid)
    assert len(rows) == 1
    assert rows[0]["start_date"] == "2026-02-01"
    assert rows[0]["reason"] == "vacation"
    # Bob isn't affected.
    assert pauses.list_for_player(conn, pid2) == []


def test_pause_rejects_inverted_window(conn):
    pid = _add_active_player(conn, "Alice", "2026-01-01")
    with pytest.raises(ValueError, match=">= start"):
        pauses.create(
            conn, player_id=pid, start_date="2026-02-10",
            end_date="2026-02-01", reason=None, created_by_user_id=_add_stub_user(conn),
        )


def test_paused_pids_for_day_window_inclusive(conn):
    pid = _add_active_player(conn, "Alice", "2026-01-01")
    pauses.create(
        conn, player_id=pid, start_date="2026-02-01",
        end_date="2026-02-05", reason=None, created_by_user_id=_add_stub_user(conn),
    )
    assert pauses.paused_pids_for_day(conn, "2026-01-31") == set()
    assert pauses.paused_pids_for_day(conn, "2026-02-01") == {pid}
    assert pauses.paused_pids_for_day(conn, "2026-02-03") == {pid}
    assert pauses.paused_pids_for_day(conn, "2026-02-05") == {pid}
    assert pauses.paused_pids_for_day(conn, "2026-02-06") == set()


def test_pause_exempts_player_from_apr(conn):
    """A paused player neither gains nor loses APR while they're paused —
    including being skipped from the absentee list so they're not penalized
    for not submitting."""
    a = _add_active_player(conn, "Alice", "2026-01-01")
    b = _add_active_player(conn, "Bob", "2026-01-01")
    c = _add_active_player(conn, "Carol", "2026-01-01")

    # A non-weekend day so APR actually runs.  Mon 2026-01-05.
    d_id = insert_day(conn, "2026-01-05")
    insert_submission(conn, a, d_id, 60.0, "completed")
    insert_submission(conn, b, d_id, 80.0, "completed")
    # Carol absent. With no pause she'd take the absent penalty.

    pauses.create(
        conn, player_id=c, start_date="2026-01-05",
        end_date="2026-01-05", reason="vacation", created_by_user_id=_add_stub_user(conn),
    )
    apr.recompute_all_ratings(conn)

    rows = {r["player_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM rating_history"
    ).fetchall()}
    assert a in rows and b in rows
    # Carol has NO rating_history row — she was treated as not active.
    assert c not in rows


def test_pause_delete_owner_only(conn):
    a = _add_active_player(conn, "Alice", "2026-01-01")
    pid = pauses.create(
        conn, player_id=a, start_date="2026-02-01",
        end_date="2026-02-02", reason=None, created_by_user_id=_add_stub_user(conn),
    )
    # Wrong player_id → no-op.
    ok = pauses.delete(
        conn, pause_id=pid, requesting_user_id=99,
        requesting_player_id=99, is_admin=False,
    )
    assert ok is False
    assert pauses.list_for_player(conn, a)
    # Admin can delete anyone's.
    ok = pauses.delete(
        conn, pause_id=pid, requesting_user_id=1,
        requesting_player_id=None, is_admin=True,
    )
    assert ok is True
    assert pauses.list_for_player(conn, a) == []


def test_paused_dates_for_player_expands_window(conn):
    a = _add_active_player(conn, "Alice", "2026-01-01")
    pauses.create(
        conn, player_id=a, start_date="2026-02-01",
        end_date="2026-02-03", reason=None, created_by_user_id=_add_stub_user(conn),
    )
    dates = pauses.paused_dates_for_player(conn, a)
    assert dates == {"2026-02-01", "2026-02-02", "2026-02-03"}
