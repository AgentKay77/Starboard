"""New-season behaviour: bumping seasons resets standings to empty."""
from __future__ import annotations

from datetime import date

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


# ---------- planned_end_date + auto-close ----------


def test_add_months_handles_year_overflow():
    from datetime import date as _d
    assert seasons.add_months(_d(2026, 7, 1), 3) == _d(2026, 10, 1)
    assert seasons.add_months(2026 and _d(2026, 11, 15), 3) == _d(2027, 2, 15)
    # Jan 31 + 1 month → Feb 28 (or 29) clamp.
    assert seasons.add_months(_d(2026, 1, 31), 1) == _d(2026, 2, 28)


def test_start_new_defaults_planned_end_to_3_months_out():
    """Calling start_new() without an explicit planned_end_date should
    set one ~3 months from today."""
    # Need a fresh DB; use a temp connection.
    import tempfile, pathlib
    from datetime import date as _d
    from starboard import db
    p = pathlib.Path(tempfile.mkdtemp()) / "s.db"
    c = db.connect(p)
    try:
        db.init_schema(c)
        # Seed a stub user for the audit_log FK.
        c.execute(
            """INSERT INTO users (email, username, password_hash, role, created_at)
               VALUES ('a@x','a','x','admin','2026-01-01T00:00:00')"""
        )
        c.commit()
        new_id = seasons.start_new(c, user_id=1)
        row = c.execute(
            "SELECT planned_end_date FROM seasons WHERE id = ?", (new_id,)
        ).fetchone()
        assert row["planned_end_date"] is not None
        expected = seasons.add_months(_d.today(), seasons.STANDARD_SEASON_MONTHS)
        assert row["planned_end_date"] == expected.isoformat()
    finally:
        c.close()


def test_auto_close_if_due_only_fires_when_date_reached(conn):
    user = _add_admin_user(conn)
    # Set planned_end_date to today; should fire.
    seasons.set_planned_end_date(conn, 1, "2026-05-15")
    rolled = seasons.auto_close_if_due(conn, date.fromisoformat("2026-05-14"))
    assert rolled is None  # before
    rolled = seasons.auto_close_if_due(conn, date.fromisoformat("2026-05-15"))
    assert rolled is not None  # on the day → fires
    # Next call doesn't double-roll because the new season's planned
    # end is 3 months from today.
    again = seasons.auto_close_if_due(conn, date.fromisoformat("2026-05-15"))
    assert again is None


def test_auto_close_no_planned_date_is_inert(conn):
    """Seasons without a planned end never auto-close — admin must flip."""
    # Default-seeded season 1 has planned_end_date = NULL.
    rolled = seasons.auto_close_if_due(conn, date.fromisoformat("2099-12-31"))
    assert rolled is None
