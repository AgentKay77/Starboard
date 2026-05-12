"""New-season behaviour: bumping seasons resets standings to empty."""
from __future__ import annotations

from starboard import apr, queries, seasons
from tests.conftest import insert_day, insert_submission


def _add(conn, name):
    cur = conn.execute(
        "INSERT INTO players (name, active, joined_date) VALUES (?, 1, '2026-01-01')",
        (name,),
    )
    conn.commit()
    return cur.lastrowid


def _add_admin_user(conn):
    cur = conn.execute(
        """INSERT INTO users (email, username, password_hash, role, created_at)
           VALUES ('a@x','a','x','admin','2026-01-01T00:00:00')"""
    )
    conn.commit()
    return cur.lastrowid


def test_default_season_is_one(conn):
    assert seasons.get_current_id(conn) == 1
    s = seasons.get_current(conn)
    assert s["number"] == 1


def test_start_new_season_bumps_current(conn):
    user = _add_admin_user(conn)
    new_id = seasons.start_new(conn, user_id=user)
    assert new_id == 2
    assert seasons.get_current_id(conn) == new_id
    # Prior season has ended_at set.
    prior = conn.execute("SELECT * FROM seasons WHERE id = 1").fetchone()
    assert prior["ended_at"] is not None


def test_new_season_resets_visible_standings(conn):
    """Submissions from season 1 stop counting toward standings once
    season 2 opens; the rating column reads INITIAL_RATING for everyone
    until the first season-2 submission comes in."""
    a = _add(conn, "Alice")
    b = _add(conn, "Bob")
    user = _add_admin_user(conn)

    # Season 1 day with two completions: APR should move.
    d = insert_day(conn, "2026-01-05")  # Mon
    insert_submission(conn, a, d, 60.0, "completed")
    insert_submission(conn, b, d, 80.0, "completed")
    apr.recompute_all_ratings(conn)
    s_before = queries.standings(conn)
    assert any(r["rating"] != apr.INITIAL_RATING for r in s_before)

    # Open season 2.
    seasons.start_new(conn, user_id=user)
    apr.recompute_all_ratings(conn)
    s_after = queries.standings(conn)
    # Standings now reflect season 2 (empty), so everyone's rating is 1500.
    for r in s_after:
        assert r["rating"] == apr.INITIAL_RATING

    # The archive view of season 1 still shows the old ratings.
    s1 = queries.standings(conn, season_id=1)
    assert any(r["rating"] != apr.INITIAL_RATING for r in s1)


def test_new_season_writes_audit_log(conn):
    user = _add_admin_user(conn)
    seasons.start_new(conn, user_id=user)
    rows = conn.execute(
        "SELECT action, detail FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchall()
    assert rows[0]["action"] == "start_new_season"
    assert "season #2" in (rows[0]["detail"] or "")
