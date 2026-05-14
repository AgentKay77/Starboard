"""Feedback inbox: anonymous + signed-in POSTs land in the DB, admin
sees them, status flips work."""
from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from starboard.app import create_app
from starboard.config import Config
from starboard.db import connect, init_schema


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = tmp_path / "fb.db"
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ENABLE_USER_SUBMISSIONS", "false")
    monkeypatch.setenv("WEEK_TIMEZONE", "UTC")
    app = create_app(Config.from_env())
    app.config.update(TESTING=True)
    with app.app_context():
        c = connect(db_path)
        init_schema(c)
        ph = PasswordHasher()
        c.execute(
            """INSERT INTO users (email, username, password_hash, role, created_at)
               VALUES ('admin@x', 'admin', ?, 'admin', '2026-01-01T00:00:00')""",
            (ph.hash("hunter22hunter22"),),
        )
        c.commit()
        c.close()
    return app


def _login(client, username, pw="hunter22hunter22"):
    r = client.post("/login", data={"identifier": username, "password": pw})
    assert r.status_code == 302


def test_anonymous_feedback_post_writes_row(app):
    with app.test_client() as c:
        r = c.post(
            "/feedback",
            data={"kind": "bug", "body": "the standings page is empty",
                  "page_url": "/standings"},
            follow_redirects=False,
        )
        assert r.status_code == 302

    conn = connect(app.config["DATABASE_PATH"])
    try:
        rows = conn.execute("SELECT * FROM feedback").fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "bug"
        assert rows[0]["body"] == "the standings page is empty"
        assert rows[0]["user_id"] is None  # anon
        assert rows[0]["status"] == "open"
    finally:
        conn.close()


def test_signed_in_feedback_records_user_id(app):
    with app.test_client() as c:
        _login(c, "admin")
        c.post(
            "/feedback",
            data={"kind": "feature", "body": "weekend warrior glyph is unclear",
                  "page_url": "/weekly"},
        )
    conn = connect(app.config["DATABASE_PATH"])
    try:
        row = conn.execute("SELECT user_id FROM feedback").fetchone()
        assert row["user_id"] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("bad_body", ["", " " * 5, "x" * 4001])
def test_feedback_body_validation(app, bad_body):
    with app.test_client() as c:
        c.post("/feedback", data={"kind": "bug", "body": bad_body})
    conn = connect(app.config["DATABASE_PATH"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    finally:
        conn.close()


def test_feedback_rejects_bad_kind(app):
    with app.test_client() as c:
        c.post("/feedback", data={"kind": "rant", "body": "x"})
    conn = connect(app.config["DATABASE_PATH"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    finally:
        conn.close()


def test_admin_feedback_status_update(app):
    with app.test_client() as c:
        c.post("/feedback", data={"kind": "bug", "body": "x"})
        _login(c, "admin")
        # Find the feedback id.
        conn = connect(app.config["DATABASE_PATH"])
        fid = conn.execute("SELECT id FROM feedback").fetchone()["id"]
        conn.close()
        c.post(
            "/admin/feedback",
            data={"action": "set_status", "feedback_id": str(fid),
                  "status": "triaged"},
            follow_redirects=False,
        )

    conn = connect(app.config["DATABASE_PATH"])
    try:
        row = conn.execute("SELECT status FROM feedback WHERE id = ?", (fid,)).fetchone()
        assert row["status"] == "triaged"
    finally:
        conn.close()
