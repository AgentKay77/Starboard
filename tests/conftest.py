"""Shared fixtures: a temp SQLite DB with the live schema applied."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable when pytest is invoked from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starboard import db as db_module  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    c = db_module.connect(path)
    db_module.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def seeded_conn(conn):
    """Three players, no submissions yet."""
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO players (name, joined_date) VALUES (?, ?)",
        [("Alice", "2026-01-01"), ("Bob", "2026-01-01"), ("Carol", "2026-01-01")],
    )
    conn.commit()
    return conn


def insert_day(conn, iso_date: str) -> int:
    cur = conn.execute(
        "INSERT INTO puzzle_days (date) VALUES (?)", (iso_date,)
    )
    conn.commit()
    return cur.lastrowid


def insert_submission(
    conn, player_id: int, day_id: int, time_seconds: float | None,
    status: str = "completed",
) -> None:
    conn.execute(
        """INSERT INTO submissions
               (player_id, day_id, time_seconds, status, submitted_at)
           VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')""",
        (player_id, day_id, time_seconds, status),
    )
    conn.commit()
