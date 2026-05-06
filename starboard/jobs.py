"""Scheduled jobs. Run via `python -m starboard.jobs <command>`.

The week-close job is wired up as a systemd timer (see
`deploy/starboard-weekly-close.timer`). It fires Mondays at 00:05 in the
configured timezone and locks in the previous week's awards.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from starboard import db, weekly
from starboard.config import Config


def close_last_week() -> int:
    cfg = Config.from_env()
    tz = ZoneInfo(cfg.week_timezone)
    today = datetime.now(tz).date()
    monday_this_week = today - timedelta(days=today.weekday())
    monday_last_week = monday_this_week - timedelta(days=7)

    conn = db.connect(cfg.database_path)
    try:
        weekly.close_week_and_lock_awards(conn, monday_last_week.isoformat())
        print(f"Locked awards for week of {monday_last_week.isoformat()}")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: python -m starboard.jobs <command>")
        print("commands: close_last_week")
        return 2
    cmd = argv[0]
    if cmd == "close_last_week":
        return close_last_week()
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
