"""Bulk daily entry: parser + multi-row POST + idempotent re-POST + the
self-submission gating that lives next to it on the admin dashboard."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher

from starboard import queries, settings as settings_mod
from starboard.app import create_app
from starboard.config import Config
from starboard.db import connect, init_schema


# ---------- parse_bulk_time ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5:23", 323.0),
        ("323", 323.0),
        ("5.23", 323.0),
        ("0:45", 45.0),
        ("0:45.5", 45.5),
        ("45.5", 45.5),  # right side has 1 digit → falls through to float seconds
        ("100", 100.0),
        ("12:00.5", 720.5),
    ],
)
def test_parse_bulk_time_accepts(raw, expected):
    assert queries.parse_bulk_time(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "  ", "abc", "5:60", "1:60", "5:23:11", "-5", "--"])
def test_parse_bulk_time_rejects(raw):
    with pytest.raises(ValueError):
        queries.parse_bulk_time(raw)


# ---------- bulk endpoint ----------


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = tmp_path / "bulk.db"
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ENABLE_USER_SUBMISSIONS", "false")
    # Pin tz=UTC so date.today() in tests and clock.local_today() in the
    # server agree, regardless of when the suite runs.
    monkeypatch.setenv("WEEK_TIMEZONE", "UTC")
    cfg = Config.from_env()
    app = create_app(cfg)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        c = connect(db_path)
        init_schema(c)
        # 13 active players, all joined before the test day.
        ph = PasswordHasher()
        for i in range(1, 14):
            c.execute(
                "INSERT INTO players (name, display_name, joined_date) VALUES (?, ?, ?)",
                (f"Player {i}", f"P{i}", "2026-01-01"),
            )
        # An admin user.
        c.execute(
            """INSERT INTO users (email, username, password_hash, role, created_at)
               VALUES ('admin@x', 'admin', ?, 'admin', '2026-01-01T00:00:00')""",
            (ph.hash("hunter22hunter22"),),
        )
        c.commit()
        c.close()
    return app


def _login_admin(client):
    r = client.post(
        "/login",
        data={"identifier": "admin", "password": "hunter22hunter22"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.data


def _bulk_form(player_ids, *, completed=(), dnf=(), times=None):
    times = times or {}
    data = {}
    for pid in player_ids:
        if pid in completed:
            data[f"status_{pid}"] = "completed"
            data[f"time_{pid}"] = times.get(pid, "1:00")
        elif pid in dnf:
            data[f"status_{pid}"] = "dnf"
        else:
            data[f"status_{pid}"] = "absent"
    return data


def test_bulk_post_creates_13_submissions_and_recomputes(app):
    target_date = "2026-04-20"
    with app.test_client() as client:
        _login_admin(client)

        with patch("starboard.admin.apr.recompute_all_ratings") as mock_recompute:
            r = client.post(
                f"/admin/days/{target_date}/bulk",
                data=_bulk_form(
                    range(1, 14),
                    completed={1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
                    dnf={12},
                    times={i: f"{i}:30" for i in range(1, 12)},
                ),
                follow_redirects=False,
            )
            assert r.status_code == 302
            # Exactly one APR recompute for the whole bulk save.
            assert mock_recompute.call_count == 1

    # Verify counts in the DB directly.
    db_path = app.config["DATABASE_PATH"]
    c = connect(db_path)
    try:
        day = c.execute(
            "SELECT id FROM puzzle_days WHERE date = ?", (target_date,)
        ).fetchone()
        assert day is not None
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM submissions WHERE day_id = ? GROUP BY status",
            (day["id"],),
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        assert by_status.get("completed") == 11
        assert by_status.get("dnf") == 1
        # 13th player was 'absent' → no submissions row.
        total = sum(by_status.values())
        assert total == 12
    finally:
        c.close()


def test_bulk_post_is_idempotent(app):
    """Re-POSTing the same day updates rather than duplicates."""
    target_date = "2026-04-21"
    with app.test_client() as client:
        _login_admin(client)
        first = client.post(
            f"/admin/days/{target_date}/bulk",
            data=_bulk_form(range(1, 14), completed=set(range(1, 14)),
                            times={i: "5:00" for i in range(1, 14)}),
        )
        assert first.status_code == 302

        second = client.post(
            f"/admin/days/{target_date}/bulk",
            data=_bulk_form(range(1, 14), completed=set(range(1, 14)),
                            times={i: "4:30" for i in range(1, 14)}),
        )
        assert second.status_code == 302

    c = connect(app.config["DATABASE_PATH"])
    try:
        day = c.execute(
            "SELECT id FROM puzzle_days WHERE date = ?", (target_date,)
        ).fetchone()
        rows = c.execute(
            "SELECT player_id, time_seconds FROM submissions WHERE day_id = ?",
            (day["id"],),
        ).fetchall()
        # 13 unique (player_id, day_id) pairs, all updated to 4:30 = 270s.
        assert len(rows) == 13
        assert {r["player_id"] for r in rows} == set(range(1, 14))
        for r in rows:
            assert r["time_seconds"] == pytest.approx(270.0)
    finally:
        c.close()


def test_bulk_absent_deletes_existing(app):
    """Switching a row from completed → absent removes the prior submission."""
    target_date = "2026-04-22"
    with app.test_client() as client:
        _login_admin(client)
        client.post(
            f"/admin/days/{target_date}/bulk",
            data=_bulk_form(range(1, 14), completed={1, 2, 3},
                            times={1: "1:00", 2: "1:00", 3: "1:00"}),
        )
        client.post(
            f"/admin/days/{target_date}/bulk",
            data=_bulk_form(range(1, 14), completed={1}, times={1: "1:00"}),
        )

    c = connect(app.config["DATABASE_PATH"])
    try:
        day = c.execute(
            "SELECT id FROM puzzle_days WHERE date = ?", (target_date,)
        ).fetchone()
        pids = [
            r["player_id"]
            for r in c.execute(
                "SELECT player_id FROM submissions WHERE day_id = ?", (day["id"],)
            ).fetchall()
        ]
        assert pids == [1]
    finally:
        c.close()


def test_bulk_invalid_time_aborts_whole_save(app):
    """If any row's time fails to parse, no rows are written."""
    target_date = "2026-04-23"
    with app.test_client() as client:
        _login_admin(client)
        data = _bulk_form(
            range(1, 14),
            completed={1, 2},
            times={1: "1:00", 2: "garbage"},
        )
        r = client.post(f"/admin/days/{target_date}/bulk", data=data)
        assert r.status_code == 302

    c = connect(app.config["DATABASE_PATH"])
    try:
        day = c.execute(
            "SELECT id FROM puzzle_days WHERE date = ?", (target_date,)
        ).fetchone()
        n = c.execute(
            "SELECT COUNT(*) FROM submissions WHERE day_id = ?", (day["id"],)
        ).fetchone()[0]
        assert n == 0
    finally:
        c.close()


# ---------- self-submission gating ----------


def test_settings_toggle_writes_audit_log(app):
    db_path = app.config["DATABASE_PATH"]
    c = connect(db_path)
    try:
        assert settings_mod.submissions_enabled(c, env_default=False) is False
        settings_mod.set_submissions_enabled(c, True, user_id=1)
        assert settings_mod.submissions_enabled(c, env_default=False) is True
        rows = c.execute(
            "SELECT user_id, action, detail FROM audit_log ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "toggle_user_submissions"
        assert rows[0]["detail"] == "enabled"
        assert rows[0]["user_id"] == 1
    finally:
        c.close()


def test_admin_toggle_endpoint_flips_state(app):
    with app.test_client() as client:
        _login_admin(client)
        # Initially OFF (env_default=false, no settings row).
        r = client.post("/admin/", data={"action": "toggle_user_submissions"},
                        follow_redirects=False)
        assert r.status_code == 302

    c = connect(app.config["DATABASE_PATH"])
    try:
        assert settings_mod.submissions_enabled(c, env_default=False) is True
        rows = c.execute("SELECT * FROM audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "toggle_user_submissions"
    finally:
        c.close()
