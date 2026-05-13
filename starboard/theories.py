"""Solve Theory: server-side validation + read helpers.

The Stars puzzle: NxN grid divided into N regions; each row, column, and
region contains exactly two stars; no two stars touch (including diagonally).

We don't auto-solve — we validate that what the first user submitted as the
canonical board is *internally consistent*. Subsequent users only submit a
pick-order against that frozen board."""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from datetime import datetime, timezone

MIN_SIZE = 7
MAX_SIZE = 10
NOTES_MAX_LEN = 2000


class TheoryError(ValueError):
    """Raised by validate_board / parse_pick_order on bad input."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- canonical board validation ----------


def validate_board(
    regions: list[list[int]], stars: list[tuple[int, int]], size: int
) -> tuple[list[list[int]], list[tuple[int, int]]]:
    """Return canonicalized (regions, stars) or raise TheoryError.

    Stars are normalized to a sorted tuple list so equality / subset checks
    later become a plain set compare."""
    if not (MIN_SIZE <= size <= MAX_SIZE):
        raise TheoryError(f"size must be {MIN_SIZE}..{MAX_SIZE}, got {size}")

    # Shape: NxN, every cell an int in [0, size).
    if len(regions) != size:
        raise TheoryError(f"regions has {len(regions)} rows, expected {size}")
    for r, row in enumerate(regions):
        if len(row) != size:
            raise TheoryError(
                f"regions[{r}] has {len(row)} cols, expected {size}"
            )
        for c, val in enumerate(row):
            if not isinstance(val, int) or not (0 <= val < size):
                raise TheoryError(
                    f"regions[{r}][{c}] = {val!r}, must be int in [0,{size})"
                )

    # Every region 0..size-1 must be used. Cell counts can vary — Stars
    # puzzles ship with irregular regions, the only requirement is that
    # each region holds exactly two stars (enforced below). A region needs
    # at least 2 cells to fit those two non-touching stars.
    counts: dict[int, int] = {}
    for row in regions:
        for v in row:
            counts[v] = counts.get(v, 0) + 1
    if set(counts.keys()) != set(range(size)):
        raise TheoryError(
            f"regions must use exactly IDs 0..{size - 1}, "
            f"got {sorted(counts.keys())}"
        )
    for rid in range(size):
        if counts[rid] < 2:
            raise TheoryError(
                f"region {rid} has {counts[rid]} cell(s); "
                "needs at least 2 to fit two non-touching stars"
            )

    # 4-connectivity per region (each region is one contiguous blob).
    for rid in range(size):
        cells = [
            (r, c) for r in range(size) for c in range(size) if regions[r][c] == rid
        ]
        seen = {cells[0]}
        q = deque([cells[0]])
        while q:
            r, c = q.popleft()
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nr, nc = r + dr, c + dc
                if (
                    0 <= nr < size and 0 <= nc < size
                    and regions[nr][nc] == rid and (nr, nc) not in seen
                ):
                    seen.add((nr, nc))
                    q.append((nr, nc))
        if len(seen) != counts[rid]:
            raise TheoryError(f"region {rid} is not 4-connected")

    # Stars: list of (r,c), distinct, in bounds. Count is flexible so a
    # user submitting a partial solve theory ("here are the stars I'm
    # confident about") isn't forced to commit to a full 2N placement.
    # The hard puzzle invariants — no touching, at-most-2 per row / column
    # / region — are still enforced so a malformed theory can't poison the
    # canonical board.
    star_set: set[tuple[int, int]] = set()
    for s in stars:
        if (
            not isinstance(s, (list, tuple)) or len(s) != 2
            or not all(isinstance(x, int) for x in s)
        ):
            raise TheoryError(f"bad star coord: {s!r}")
        r, c = int(s[0]), int(s[1])
        if not (0 <= r < size and 0 <= c < size):
            raise TheoryError(f"star ({r},{c}) out of bounds")
        if (r, c) in star_set:
            raise TheoryError(f"duplicate star at ({r},{c})")
        star_set.add((r, c))
    if len(star_set) > 2 * size:
        raise TheoryError(
            f"too many stars: {len(star_set)} (max {2 * size})"
        )

    # 8-neighbour non-adjacency — always enforced; touching stars violate
    # the puzzle rules whether the placement is complete or not.
    for r, c in star_set:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                if (r + dr, c + dc) in star_set:
                    raise TheoryError(
                        f"stars touch at ({r},{c}) and ({r+dr},{c+dc})"
                    )

    # At most two stars per row / column / region. Partial placements can
    # have fewer; over-placements are flatly invalid.
    rows = [0] * size
    cols = [0] * size
    regs = [0] * size
    for r, c in star_set:
        rows[r] += 1
        cols[c] += 1
        regs[regions[r][c]] += 1
    for i, n in enumerate(rows):
        if n > 2:
            raise TheoryError(f"row {i} has {n} stars (max 2)")
    for i, n in enumerate(cols):
        if n > 2:
            raise TheoryError(f"column {i} has {n} stars (max 2)")
    for i, n in enumerate(regs):
        if n > 2:
            raise TheoryError(f"region {i} has {n} stars (max 2)")

    return regions, sorted(star_set)


def parse_board_payload(
    raw_size: str, raw_regions: str, raw_stars: str
) -> tuple[int, list[list[int]], list[tuple[int, int]]]:
    """Parse the form's hidden JSON fields into validated structures."""
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        raise TheoryError(f"bad size: {raw_size!r}") from None
    try:
        regions = json.loads(raw_regions)
    except (TypeError, ValueError) as e:
        raise TheoryError(f"regions JSON: {e}") from None
    try:
        stars_raw = json.loads(raw_stars)
    except (TypeError, ValueError) as e:
        raise TheoryError(f"stars JSON: {e}") from None
    if not isinstance(stars_raw, list):
        raise TheoryError("stars must be a list")
    stars = [tuple(s) for s in stars_raw]
    return (size, *validate_board(regions, stars, size))


def parse_added_stars(
    raw_stars_json: str,
    *,
    existing_stars: list[tuple[int, int]],
    regions: list[list[int]],
    size: int,
) -> list[tuple[int, int]]:
    """Subsequent users may add stars the first author missed.

    Accepts the form's full `stars_json` (existing + new), filters out
    coords already on the canonical board, then re-runs `validate_board`
    on the merged set so additions can't break the puzzle's invariants
    (touching, ≤ 2 per row/column/region, ≤ 2*size total).

    Returns just the *new* stars in sorted order, or [] when the payload
    is empty / unchanged."""
    if not (raw_stars_json or "").strip():
        return []
    try:
        raw = json.loads(raw_stars_json)
    except (TypeError, ValueError) as e:
        raise TheoryError(f"stars JSON: {e}") from None
    if not isinstance(raw, list):
        raise TheoryError("stars must be a list")
    submitted: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for s in raw:
        if not isinstance(s, (list, tuple)) or len(s) != 2:
            raise TheoryError(f"bad star coord: {s!r}")
        coord = (int(s[0]), int(s[1]))
        if coord in seen:
            continue
        seen.add(coord)
        submitted.append(coord)
    existing_set = set(existing_stars)
    added = [c for c in submitted if c not in existing_set]
    if not added:
        return []
    merged = list(existing_stars) + added
    # validate_board enforces all the puzzle rules on the merged set.
    _, normalized = validate_board(regions, merged, size)
    # Return only the *new* contribution, preserving sorted normalization.
    return [c for c in normalized if c not in existing_set]


def update_board_stars(
    conn: sqlite3.Connection, *, day_id: int, stars: list[tuple[int, int]]
) -> None:
    """Replace the stars_json for a board after a subsequent user added to
    it. Regions and size stay locked."""
    conn.execute(
        "UPDATE puzzle_boards SET stars_json = ? WHERE day_id = ?",
        (json.dumps([list(s) for s in stars]), day_id),
    )


def parse_pick_order(
    raw: str, board_stars: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Subset of `board_stars`, distinct, ≥1 entry."""
    try:
        pick = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise TheoryError(f"pick_order JSON: {e}") from None
    if not isinstance(pick, list) or not pick:
        raise TheoryError("pick_order must be a non-empty list")
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    star_set = set(board_stars)
    for p in pick:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise TheoryError(f"bad pick_order entry: {p!r}")
        coord = (int(p[0]), int(p[1]))
        if coord not in star_set:
            raise TheoryError(f"pick {coord} is not a star on the board")
        if coord in seen:
            raise TheoryError(f"duplicate pick {coord}")
        seen.add(coord)
        out.append(coord)
    return out


def clean_notes(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    return s[:NOTES_MAX_LEN]


# ---------- read-side helpers ----------


def get_board_for_day(
    conn: sqlite3.Connection, day_id: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM puzzle_boards WHERE day_id = ?", (day_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["regions"] = json.loads(d.pop("regions_json"))
    d["stars"] = [tuple(s) for s in json.loads(d.pop("stars_json"))]
    return d


def get_theories_for_day(
    conn: sqlite3.Connection, day_id: int
) -> list[dict]:
    rows = conn.execute(
        """SELECT t.*, u.username, p.id AS player_id, p.display_name, p.name
           FROM solve_theories t
           JOIN users u   ON u.id = t.user_id
           LEFT JOIN players p ON p.id = u.player_id
           WHERE t.day_id = ?
           ORDER BY t.created_at ASC""",
        (day_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pick_order"] = [tuple(s) for s in json.loads(d.pop("pick_order_json"))]
        d["author_label"] = (
            d["display_name"] or d["name"] or d["username"]
        )
        out.append(d)
    return out


def board_has_other_theories(
    conn: sqlite3.Connection, day_id: int, exclude_user_id: int
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM solve_theories
           WHERE day_id = ? AND user_id != ? LIMIT 1""",
        (day_id, exclude_user_id),
    ).fetchone()
    return row is not None


def user_can_view_theories(
    conn: sqlite3.Connection,
    *,
    is_admin: bool,
    user_player_id: int | None,
    day_date: str,
) -> bool:
    """Spoiler gate: admins always; others must have a submission row
    (any status) for the day."""
    if is_admin:
        return True
    if user_player_id is None:
        return False
    row = conn.execute(
        """SELECT 1 FROM submissions s
           JOIN puzzle_days pd ON pd.id = s.day_id
           WHERE s.player_id = ? AND pd.date = ?
           LIMIT 1""",
        (user_player_id, day_date),
    ).fetchone()
    return row is not None


# ---------- writes ----------


def insert_board(
    conn: sqlite3.Connection,
    *,
    day_id: int,
    size: int,
    regions: list[list[int]],
    stars: list[tuple[int, int]],
    user_id: int,
) -> int:
    cur = conn.execute(
        """INSERT INTO puzzle_boards
               (day_id, size, regions_json, stars_json,
                created_by_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            day_id, size,
            json.dumps(regions),
            json.dumps([list(s) for s in stars]),
            user_id, _now(),
        ),
    )
    return cur.lastrowid


def upsert_theory(
    conn: sqlite3.Connection,
    *,
    day_id: int,
    user_id: int,
    pick_order: list[tuple[int, int]],
    notes: str | None,
) -> None:
    pick_json = json.dumps([list(s) for s in pick_order])
    now = _now()
    conn.execute(
        """INSERT INTO solve_theories
               (day_id, user_id, pick_order_json, notes, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, day_id) DO UPDATE SET
               pick_order_json = excluded.pick_order_json,
               notes = excluded.notes,
               created_at = excluded.created_at""",
        (day_id, user_id, pick_json, notes, now),
    )


def replace_board_if_self_edit_window(
    conn: sqlite3.Connection,
    *,
    day_id: int,
    user_id: int,
    size: int,
    regions: list[list[int]],
    stars: list[tuple[int, int]],
) -> bool:
    """Author may overwrite their own board until any *other* user has
    submitted a theory. Returns True if replaced."""
    existing = conn.execute(
        "SELECT created_by_user_id FROM puzzle_boards WHERE day_id = ?",
        (day_id,),
    ).fetchone()
    if not existing or existing["created_by_user_id"] != user_id:
        return False
    if board_has_other_theories(conn, day_id, exclude_user_id=user_id):
        return False
    conn.execute(
        """UPDATE puzzle_boards
           SET size = ?, regions_json = ?, stars_json = ?, created_at = ?
           WHERE day_id = ?""",
        (
            size, json.dumps(regions),
            json.dumps([list(s) for s in stars]),
            _now(), day_id,
        ),
    )
    return True


def reset_board(conn: sqlite3.Connection, day_id: int) -> None:
    """Wipe a day's canonical board AND every solve_theory attached to it.

    The `solve_theories` FK references `puzzle_days(id)`, not the board, so
    deleting the board alone wouldn't drop the theories — we do it explicitly
    so callers can rely on a single semantic action."""
    conn.execute("DELETE FROM solve_theories WHERE day_id = ?", (day_id,))
    conn.execute("DELETE FROM puzzle_boards WHERE day_id = ?", (day_id,))
