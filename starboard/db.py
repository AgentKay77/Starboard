"""SQLite connection helper and schema bootstrap."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    joined_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    player_id INTEGER REFERENCES players(id),
    created_at TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS puzzle_days (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    day_id INTEGER NOT NULL REFERENCES puzzle_days(id) ON DELETE CASCADE,
    time_seconds REAL,                                       -- NULL when status='dnf'
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('completed', 'dnf')),
    submitted_at TEXT NOT NULL,
    submitted_by_user_id INTEGER REFERENCES users(id),
    UNIQUE(player_id, day_id)
);

CREATE TABLE IF NOT EXISTS rating_history (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    day_id INTEGER NOT NULL REFERENCES puzzle_days(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'completed'
        CHECK (kind IN ('completed', 'dnf', 'absent')),
    rating_before REAL NOT NULL,
    rating_after REAL NOT NULL,
    actual_z REAL NOT NULL,
    expected_z REAL NOT NULL,
    delta REAL NOT NULL,
    UNIQUE(player_id, day_id)
);

CREATE TABLE IF NOT EXISTS weekly_awards (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    award TEXT NOT NULL,
    player_id INTEGER REFERENCES players(id),
    metric_value REAL,
    metric_detail TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE(week_start, award)
);

CREATE INDEX IF NOT EXISTS idx_submissions_day ON submissions(day_id);
CREATE INDEX IF NOT EXISTS idx_submissions_player ON submissions(player_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_rating_history_player ON rating_history(player_id);
CREATE INDEX IF NOT EXISTS idx_rating_history_day ON rating_history(day_id);
CREATE INDEX IF NOT EXISTS idx_rating_history_kind ON rating_history(kind);
CREATE INDEX IF NOT EXISTS idx_weekly_awards_player ON weekly_awards(player_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(timestamp DESC);

CREATE TABLE IF NOT EXISTS puzzle_boards (
    id INTEGER PRIMARY KEY,
    day_id INTEGER NOT NULL UNIQUE
        REFERENCES puzzle_days(id) ON DELETE CASCADE,
    size INTEGER NOT NULL CHECK (size BETWEEN 7 AND 10),
    regions_json TEXT NOT NULL,           -- NxN list[list[int]] of region IDs
    stars_json   TEXT NOT NULL,           -- sorted [[r,c], ...], length 2*size
    created_by_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solve_theories (
    id INTEGER PRIMARY KEY,
    day_id  INTEGER NOT NULL REFERENCES puzzle_days(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    pick_order_json TEXT NOT NULL,        -- subset of board.stars, ≥1 entry
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, day_id)
);

CREATE INDEX IF NOT EXISTS idx_solve_theories_day ON solve_theories(day_id);

CREATE TABLE IF NOT EXISTS player_pauses (
    id INTEGER PRIMARY KEY,
    player_id  INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,   -- inclusive
    end_date   TEXT NOT NULL,   -- inclusive
    reason     TEXT,
    created_at TEXT NOT NULL,
    created_by_user_id INTEGER REFERENCES users(id),
    CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS idx_player_pauses_player ON player_pauses(player_id);
CREATE INDEX IF NOT EXISTS idx_player_pauses_dates  ON player_pauses(start_date, end_date);

CREATE TABLE IF NOT EXISTS seasons (
    id         INTEGER PRIMARY KEY,
    number     INTEGER NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    ended_at   TEXT             -- NULL while active
);
-- Seed a season #1 row so the DEFAULT 1 on the new season_id columns is
-- a real foreign key target. INSERT OR IGNORE keeps the bootstrap idempotent.
INSERT OR IGNORE INTO seasons (id, number, started_at)
    VALUES (1, 1, '2026-01-01T00:00:00');
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL is a no-op on :memory: but helps real files; tolerate either.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    # Column-level migrations layered on top of the CREATE-IF-NOT-EXISTS
    # baseline. SQLite's `ALTER TABLE ADD COLUMN` is idempotent only via a
    # PRAGMA-guarded helper, since it errors on a re-run.
    _ensure_column(
        conn,
        "submissions",
        "assisted",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "puzzle_days",
        "season_id",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _ensure_column(
        conn,
        "weekly_awards",
        "season_id",
        "INTEGER NOT NULL DEFAULT 1",
    )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
