"""Seed the Starboard database from CSV exports.

Usage:
    python seed.py <submissions.csv> --roster <roster.csv>

CSV formats:
  roster.csv:        name,display_name,joined_date
  submissions.csv:   date,player_name,time_seconds

Run with --reset to drop and recreate the schema first (prompts for
confirmation). Without --reset the script merges into the existing DB
(skipping duplicate submissions per (player, day)).

Admin bootstrap: if ADMIN_EMAIL / ADMIN_USERNAME / ADMIN_PASSWORD are
set in the environment, an admin user is created or updated.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

from argon2 import PasswordHasher

from starboard import apr, db, weekly
from starboard.config import Config

ph = PasswordHasher()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time_field(raw: str) -> float:
    raw = raw.strip()
    if ":" in raw:
        m, s = raw.split(":")
        return int(m) * 60 + float(s)
    return float(raw)


def load_roster(conn, path: str) -> dict[str, int]:
    print(f"Loading roster from {path}…")
    name_to_id: dict[str, int] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["name"].strip()
            display = (row.get("display_name") or "").strip() or None
            joined = (row.get("joined_date") or "").strip()
            if not name or not joined:
                continue
            cur = conn.execute("SELECT id FROM players WHERE name = ?", (name,)).fetchone()
            if cur:
                pid = cur["id"]
                conn.execute(
                    "UPDATE players SET display_name = ?, joined_date = ? WHERE id = ?",
                    (display, joined, pid),
                )
            else:
                cur2 = conn.execute(
                    "INSERT INTO players (name, display_name, joined_date) VALUES (?, ?, ?)",
                    (name, display, joined),
                )
                pid = cur2.lastrowid
            name_to_id[name] = pid
    conn.commit()
    print(f"  · {len(name_to_id)} player(s) registered.")
    return name_to_id


def load_submissions(conn, path: str, name_to_id: dict[str, int]) -> int:
    print(f"Loading submissions from {path}…")
    inserted = 0
    skipped = 0
    seen_dates: set[str] = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row["date"].strip()
            name = row["player_name"].strip()
            try:
                seconds = parse_time_field(row["time_seconds"])
            except ValueError:
                skipped += 1
                continue
            if name not in name_to_id:
                # Lazy-add player at the earliest date we see them.
                cur = conn.execute(
                    "INSERT INTO players (name, joined_date) VALUES (?, ?)",
                    (name, d),
                )
                name_to_id[name] = cur.lastrowid
            seen_dates.add(d)

            day = conn.execute(
                "SELECT id FROM puzzle_days WHERE date = ?", (d,)
            ).fetchone()
            if day:
                day_id = day["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO puzzle_days (date) VALUES (?)", (d,)
                )
                day_id = cur.lastrowid

            existing = conn.execute(
                "SELECT id FROM submissions WHERE player_id = ? AND day_id = ?",
                (name_to_id[name], day_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE submissions SET time_seconds = ?, submitted_at = ? WHERE id = ?",
                    (seconds, _now(), existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO submissions
                           (player_id, day_id, time_seconds, submitted_at)
                       VALUES (?, ?, ?, ?)""",
                    (name_to_id[name], day_id, seconds, _now()),
                )
                inserted += 1
    conn.commit()
    print(f"  · {inserted} submission(s) inserted ({skipped} skipped, {len(seen_dates)} day(s)).")
    return inserted


def ensure_admin_user(conn) -> None:
    email = os.environ.get("ADMIN_EMAIL", "").lower().strip()
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not (email and username and password):
        print(
            "Skipping admin bootstrap (set ADMIN_EMAIL, ADMIN_USERNAME, "
            "ADMIN_PASSWORD to create one)."
        )
        return
    existing = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET role = 'admin', password_hash = ?, username = ? WHERE id = ?",
            (ph.hash(password), username, existing["id"]),
        )
        print(f"Updated admin user {username}.")
    else:
        conn.execute(
            """INSERT INTO users (email, username, password_hash, role, created_at)
               VALUES (?, ?, ?, 'admin', ?)""",
            (email, username, ph.hash(password), _now()),
        )
        print(f"Created admin user {username}.")
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submissions", help="Path to submissions CSV (date,player_name,time_seconds)")
    parser.add_argument("--roster", help="Path to roster CSV (name,display_name,joined_date)")
    parser.add_argument("--reset", action="store_true", help="Drop existing schema first (prompts).")
    parser.add_argument("--yes", action="store_true", help="Skip the reset confirmation.")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    print(f"Database: {cfg.database_path}")
    if args.reset:
        if not args.yes:
            confirm = input(f"Drop ALL data in {cfg.database_path}? [y/N] ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Aborted.")
                return 1
        if os.path.exists(cfg.database_path):
            os.remove(cfg.database_path)
            print(f"  · Removed {cfg.database_path}")

    conn = db.connect(cfg.database_path)
    db.init_schema(conn)
    print("Schema ready.")

    name_to_id: dict[str, int] = {}
    if args.roster:
        name_to_id = load_roster(conn, args.roster)
    else:
        # Pre-populate from existing players if any
        for r in conn.execute("SELECT id, name FROM players").fetchall():
            name_to_id[r["name"]] = r["id"]

    load_submissions(conn, args.submissions, name_to_id)

    ensure_admin_user(conn)

    print("Recomputing APR ratings…")
    apr.recompute_all_ratings(conn)
    print("Recomputing closed weeks' awards…")
    weekly.recompute_all_weeks(conn)

    rating_count = conn.execute("SELECT COUNT(*) FROM rating_history").fetchone()[0]
    award_count = conn.execute("SELECT COUNT(*) FROM weekly_awards").fetchone()[0]
    print(f"\nDone. {rating_count} rating row(s), {award_count} award row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
