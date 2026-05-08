"""Tiny settings / audit-log helpers backed by SQLite.

A `settings` row overrides the env-derived default for the same key, so the
admin can toggle `enable_user_submissions` at runtime without editing `.env`
and bouncing gunicorn. Every flip writes an `audit_log` row keyed by user."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

ENABLE_USER_SUBMISSIONS_KEY = "enable_user_submissions"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def get_bool(
    conn: sqlite3.Connection, key: str, default: bool = False
) -> bool:
    raw = get(conn, key)
    if raw is None:
        return default
    return raw.lower() in _TRUTHY


def set_value(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    *,
    user_id: int | None,
    action: str,
    detail: str | None = None,
) -> None:
    """Upsert a setting and write an audit_log row in the same transaction."""
    now = _now()
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, value, now),
    )
    conn.execute(
        "INSERT INTO audit_log (timestamp, user_id, action, detail) VALUES (?, ?, ?, ?)",
        (now, user_id, action, detail),
    )
    conn.commit()


def submissions_enabled(conn: sqlite3.Connection, env_default: bool) -> bool:
    """Effective state — DB row wins, env default if no row exists yet."""
    return get_bool(conn, ENABLE_USER_SUBMISSIONS_KEY, default=env_default)


def audit_log(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    action: str,
    detail: str | None = None,
) -> None:
    """Append-only audit row, no settings change."""
    conn.execute(
        "INSERT INTO audit_log (timestamp, user_id, action, detail) VALUES (?, ?, ?, ?)",
        (_now(), user_id, action, detail),
    )
    conn.commit()


def set_submissions_enabled(
    conn: sqlite3.Connection, value: bool, *, user_id: int
) -> None:
    set_value(
        conn,
        ENABLE_USER_SUBMISSIONS_KEY,
        "true" if value else "false",
        user_id=user_id,
        action="toggle_user_submissions",
        detail="enabled" if value else "disabled",
    )
