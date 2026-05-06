"""One-shot parser: turn the wide-format Excel paste into the long-format
CSVs that seed.py expects.

Input:  data/raw-paste.tsv (header row of M/D dates + per-player rows
        with 3 leading stat columns we ignore: Score, Wins, Days)
Output: data/season1.csv   date,player_name,time_seconds
        data/roster.csv    name,display_name,joined_date

Year is hardcoded to 2026: only year in recent context where Feb 16 is
a Monday and the weekly cadence (M-F, weekends skipped) lines up.
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

YEAR = 2026
HERE = Path(__file__).resolve().parent
RAW = HERE / "raw-paste.tsv"
SUBMISSIONS_OUT = HERE / "season1.csv"
ROSTER_OUT = HERE / "roster.csv"


def parse_md(token: str) -> date:
    token = token.strip()
    if "/" not in token:
        # Header truncated "16" → "2/16"
        token = f"2/{token}"
    m, d = token.split("/")
    return date(YEAR, int(m), int(d))


def parse_time(cell: str) -> float | None:
    cell = cell.strip()
    if not cell:
        return None
    if ":" in cell:
        m, s = cell.split(":")
        return int(m) * 60 + float(s)
    return float(cell)


def first_name(full: str) -> str:
    last, _, first = full.partition(",")
    return first.strip() or last.strip()


def main() -> int:
    rows = RAW.read_text().splitlines()
    header_cells = rows[0].split("\t")
    dates = [parse_md(c) for c in header_cells]
    n_dates = len(dates)
    print(f"Header: {n_dates} dates ({dates[0]} → {dates[-1]})")

    submissions: list[tuple[str, str, float]] = []  # (date, name, seconds)
    earliest_per_player: dict[str, date] = {}
    players: list[str] = []

    for raw_row in rows[1:]:
        cells = raw_row.split("\t")
        name = cells[0].strip()
        # Stats live at cells[1:4]; data cells start at index 4.
        # Pad to expected length with empty strings (the paste truncated
        # trailing blanks).
        expected_total = 1 + 3 + n_dates
        if len(cells) < expected_total:
            cells = cells + [""] * (expected_total - len(cells))
        time_cells = cells[4 : 4 + n_dates]
        if len(time_cells) != n_dates:
            print(f"  ! {name}: got {len(time_cells)} cells, expected {n_dates}")
        players.append(name)
        for d, raw in zip(dates, time_cells):
            t = parse_time(raw)
            if t is None:
                continue
            submissions.append((d.isoformat(), name, t))
            cur = earliest_per_player.get(name)
            if cur is None or d < cur:
                earliest_per_player[name] = d

    # Roster: joined_date = each player's first appearance.
    # Players with no submissions get the league start date.
    league_start = dates[0].isoformat()
    with ROSTER_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "display_name", "joined_date"])
        for name in players:
            joined = earliest_per_player.get(name)
            w.writerow(
                [
                    name,
                    first_name(name),
                    (joined or dates[0]).isoformat(),
                ]
            )

    # Submissions, sorted by (date, name) for human readability.
    submissions.sort(key=lambda r: (r[0], r[1]))
    with SUBMISSIONS_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "player_name", "time_seconds"])
        for d, name, secs in submissions:
            w.writerow([d, name, f"{secs:.2f}"])

    print(f"Wrote {ROSTER_OUT.name}: {len(players)} players")
    print(f"Wrote {SUBMISSIONS_OUT.name}: {len(submissions)} submissions")
    print()
    print("Submissions per player:")
    counts: dict[str, int] = {}
    for _d, name, _s in submissions:
        counts[name] = counts.get(name, 0) + 1
    for name in players:
        print(f"  {name:30}  {counts.get(name, 0):>3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
