"""Solve Theory: validation, persistence, gating, admin reset."""
from __future__ import annotations

import json
from datetime import date

import pytest
from argon2 import PasswordHasher

from starboard import clock, theories
from starboard.app import create_app
from starboard.config import Config
from starboard.db import connect, init_schema


# ---------- pure validators ----------


def _valid_8x8():
    """Brute-force a valid 8x8 Stars layout: regions are row-stripes (so
    2-per-region == 2-per-row automatically), then place 2 stars per row
    such that every column has 2 stars and no two stars touch (8-neighbour).
    7x7 with row-stripe regions has no valid solution; 8x8 does."""
    size = 8
    regions = [[r for _ in range(size)] for r in range(size)]

    def search(row, placed, col_counts):
        if row == size:
            return placed if all(n == 2 for n in col_counts) else None
        for c1 in range(size - 1):
            if col_counts[c1] >= 2:
                continue
            for c2 in range(c1 + 2, size):  # >=2 apart so they don't touch
                if col_counts[c2] >= 2:
                    continue
                # 8-adjacency to last row's stars
                if row > 0:
                    prev = placed[-2:]
                    bad = False
                    for (pr, pc) in prev:
                        if abs(pr - row) <= 1 and (
                            abs(pc - c1) <= 1 or abs(pc - c2) <= 1
                        ):
                            bad = True
                            break
                    if bad:
                        continue
                col_counts[c1] += 1
                col_counts[c2] += 1
                placed.append((row, c1))
                placed.append((row, c2))
                out = search(row + 1, placed, col_counts)
                if out is not None:
                    return out
                placed.pop()
                placed.pop()
                col_counts[c1] -= 1
                col_counts[c2] -= 1
        return None

    stars = search(0, [], [0] * size)
    assert stars is not None, "couldn't generate valid 7x7"
    return size, regions, stars


def test_validate_board_accepts_well_formed():
    size, regions, stars = _valid_8x8()
    out_regions, out_stars = theories.validate_board(regions, stars, size)
    assert out_regions == regions
    assert out_stars == sorted(stars)


def test_validate_board_rejects_wrong_size():
    with pytest.raises(theories.TheoryError):
        theories.validate_board([[0, 0], [0, 0]], [(0, 0), (1, 1)], 6)


def test_validate_board_rejects_disconnected_region():
    size = 7
    # Region 0 occupies cells (0,0) and (6,6) — non-contiguous.
    regions = [[(r + c) % size for c in range(size)] for r in range(size)]
    # but build a deliberate disconnected case:
    bad = [[0 if (r, c) in {(0, 0), (6, 6)} else 1 for c in range(size)]
           for r in range(size)]
    # region 0 has 2 cells, region 1 has 47 — counts already wrong, will fire
    # "region X has N cells, expected 7" before connectivity check.
    with pytest.raises(theories.TheoryError):
        theories.validate_board(bad, [], size)


def test_validate_board_rejects_touching_stars():
    size, regions, _ = _valid_8x8()
    bad_stars = [(0, 0), (0, 1)] + [(r, 0) for r in range(2, 8)]  # touching
    with pytest.raises(theories.TheoryError):
        theories.validate_board(regions, bad_stars[:14], size)


def test_validate_board_accepts_partial_stars():
    """Partial placements are intentionally allowed — players don't need
    to mark every star to log a solve theory."""
    size, regions, stars = _valid_8x8()
    out_regions, out_stars = theories.validate_board(regions, stars[:5], size)
    assert len(out_stars) == 5


def test_validate_board_rejects_too_many_stars():
    """Over-placement (more than 2*size) is flatly invalid. The count
    check fires before the no-touching check, so we can hand the
    validator 17 distinct cells without arranging them carefully."""
    size, regions, _ = _valid_8x8()
    too_many = [(r, c) for r in range(size) for c in range(size)][: 2 * size + 1]
    with pytest.raises(theories.TheoryError, match="too many"):
        theories.validate_board(regions, too_many, size)


def test_validate_board_accepts_irregular_region_sizes():
    """Regions can have unequal cell counts — Stars puzzles ship that way.
    The only requirement is that each region holds two non-touching stars."""
    size, regions, stars = _valid_8x8()
    # Reshape: move two cells from region 0's row into region 1's territory,
    # so region 0 has 6 cells and region 1 has 10. Star placement still
    # respects the two-per-row/col/region invariants.
    regions = [row[:] for row in regions]
    regions[0][6] = 1
    regions[0][7] = 1
    out_regions, _ = theories.validate_board(regions, stars, size)
    assert sum(row.count(0) for row in out_regions) == 6
    assert sum(row.count(1) for row in out_regions) == 10


def test_validate_board_rejects_region_with_one_cell():
    """A region with a single cell can't fit two stars."""
    size, regions, stars = _valid_8x8()
    regions = [row[:] for row in regions]
    # Steal one of region 0's cells for region 0 -> region 7,
    # leaving region 0 with 7 cells (still valid), then collapse region 0's
    # remaining cells one by one to trigger the "needs ≥ 2" branch.
    # Easiest: rewrite so region 0 has just one cell.
    for r in range(size):
        for c in range(size):
            if regions[r][c] == 0 and not (r == 0 and c == 0):
                regions[r][c] = 1
    with pytest.raises(theories.TheoryError, match="needs at least 2"):
        theories.validate_board(regions, stars, size)


def test_parse_pick_order_accepts_partial():
    board_stars = [(0, 0), (1, 1), (2, 2), (3, 3)]
    raw = json.dumps([[0, 0], [2, 2]])
    out = theories.parse_pick_order(raw, board_stars)
    assert out == [(0, 0), (2, 2)]


def test_parse_pick_order_rejects_empty():
    with pytest.raises(theories.TheoryError):
        theories.parse_pick_order(json.dumps([]), [(0, 0)])


def test_parse_pick_order_rejects_dupes():
    with pytest.raises(theories.TheoryError):
        theories.parse_pick_order(
            json.dumps([[0, 0], [0, 0]]), [(0, 0), (1, 1)]
        )


def test_parse_pick_order_rejects_non_star_coord():
    with pytest.raises(theories.TheoryError):
        theories.parse_pick_order(
            json.dumps([[0, 0], [9, 9]]), [(0, 0), (1, 1)]
        )


# ---------- end-to-end through Flask ----------


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = tmp_path / "theory.db"
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ENABLE_USER_SUBMISSIONS", "true")
    # Pin tz=UTC so date.today() in tests and clock.local_today() in the
    # server agree, regardless of when the suite runs.
    monkeypatch.setenv("WEEK_TIMEZONE", "UTC")
    cfg = Config.from_env()
    app = create_app(cfg)
    app.config.update(TESTING=True)
    with app.app_context():
        c = connect(db_path)
        init_schema(c)
        ph = PasswordHasher()
        # Players
        c.execute(
            "INSERT INTO players (id, name, display_name, joined_date) VALUES (1, 'Alice X', 'Alice', '2026-01-01')"
        )
        c.execute(
            "INSERT INTO players (id, name, display_name, joined_date) VALUES (2, 'Bob Y',   'Bob',   '2026-01-01')"
        )
        c.execute(
            "INSERT INTO players (id, name, display_name, joined_date) VALUES (3, 'Carol Z', 'Carol', '2026-01-01')"
        )
        # Users — alice claims player 1, bob claims player 2, carol unclaimed.
        for username, pid, role in [
            ("alice", 1, "user"),
            ("bob", 2, "user"),
            ("carol", None, "user"),
            ("admin", None, "admin"),
        ]:
            c.execute(
                """INSERT INTO users (email, username, password_hash, role,
                                      player_id, created_at)
                   VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00')""",
                (f"{username}@x", username, ph.hash("hunter22hunter22"), role, pid),
            )
        c.commit()
        c.close()
    return app


def _login(client, username):
    r = client.post(
        "/login",
        data={"identifier": username, "password": "hunter22hunter22"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.data


def _board_payload(size, regions, stars):
    return {
        "solve_theory": "on",
        "size": str(size),
        "regions_json": json.dumps(regions),
        "stars_json": json.dumps([list(s) for s in stars]),
        "pick_order_json": json.dumps([list(s) for s in stars]),
    }


def test_first_user_creates_canonical_board_with_theory(app, monkeypatch):
    today = date.today().isoformat()
    size, regions, stars = _valid_8x8()
    with app.test_client() as cl:
        _login(cl, "alice")
        data = {
            "date": today,
            "player_id": "1",
            "status": "completed", "solve_method": "clean",
            "time": "1:30",
            **_board_payload(size, regions, stars),
            "notes": "looked at the corners first",
        }
        r = cl.post("/submit", data=data, follow_redirects=False)
        assert r.status_code == 302

    c = connect(app.config["DATABASE_PATH"])
    try:
        day_id = c.execute(
            "SELECT id FROM puzzle_days WHERE date = ?", (today,)
        ).fetchone()["id"]
        board = c.execute(
            "SELECT * FROM puzzle_boards WHERE day_id = ?", (day_id,)
        ).fetchone()
        assert board is not None
        assert board["size"] == size
        theory = c.execute(
            "SELECT * FROM solve_theories WHERE day_id = ?", (day_id,)
        ).fetchone()
        assert theory is not None
        assert theory["notes"] == "looked at the corners first"
    finally:
        c.close()


def test_second_user_uses_existing_board(app):
    today = date.today().isoformat()
    size, regions, stars = _valid_8x8()
    with app.test_client() as cl:
        _login(cl, "alice")
        cl.post("/submit", data={
            "date": today, "player_id": "1", "status": "completed", "solve_method": "clean", "time": "1:30",
            **_board_payload(size, regions, stars),
        })
    with app.test_client() as cl:
        _login(cl, "bob")
        # Bob submits only pick_order — no regions/stars.
        bob_pick = json.dumps([list(stars[0]), list(stars[3])])
        r = cl.post("/submit", data={
            "date": today, "player_id": "2", "status": "completed", "solve_method": "clean", "time": "2:00",
            "solve_theory": "on", "pick_order_json": bob_pick,
            "notes": "spotted the bottom row first",
        })
        assert r.status_code == 302

    c = connect(app.config["DATABASE_PATH"])
    try:
        boards = c.execute("SELECT COUNT(*) FROM puzzle_boards").fetchone()[0]
        assert boards == 1  # Bob did NOT create a second board.
        theories_n = c.execute("SELECT COUNT(*) FROM solve_theories").fetchone()[0]
        assert theories_n == 2
    finally:
        c.close()


def test_invalid_theory_does_not_block_time(app):
    today = date.today().isoformat()
    with app.test_client() as cl:
        _login(cl, "alice")
        r = cl.post("/submit", data={
            "date": today, "player_id": "1", "status": "completed", "solve_method": "clean", "time": "1:30",
            "solve_theory": "on", "size": "7",
            "regions_json": "not json",
            "stars_json": "[]", "pick_order_json": "[]",
        }, follow_redirects=True)
        # Time still saved; warning flash present.
        body = r.data.decode("utf-8")
        assert "rejected" in body or "warning" in body.lower()

    c = connect(app.config["DATABASE_PATH"])
    try:
        s = c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        assert s == 1
        b = c.execute("SELECT COUNT(*) FROM puzzle_boards").fetchone()[0]
        assert b == 0
        t = c.execute("SELECT COUNT(*) FROM solve_theories").fetchone()[0]
        assert t == 0
    finally:
        c.close()


def test_theory_upsert_on_resubmit(app):
    today = date.today().isoformat()
    size, regions, stars = _valid_8x8()
    with app.test_client() as cl:
        _login(cl, "alice")
        cl.post("/submit", data={
            "date": today, "player_id": "1", "status": "completed", "solve_method": "clean", "time": "1:30",
            **_board_payload(size, regions, stars), "notes": "first try",
        })
        # Resubmit with different notes
        cl.post("/submit", data={
            "date": today, "player_id": "1", "status": "completed", "solve_method": "clean", "time": "1:25",
            "solve_theory": "on",
            "pick_order_json": json.dumps([list(stars[0])]),
            "notes": "second try",
        })

    c = connect(app.config["DATABASE_PATH"])
    try:
        rows = c.execute(
            "SELECT notes FROM solve_theories WHERE user_id = (SELECT id FROM users WHERE username='alice')"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["notes"] == "second try"
    finally:
        c.close()


def test_theories_page_gates_users_without_submission(app):
    today = date.today().isoformat()
    size, regions, stars = _valid_8x8()
    with app.test_client() as cl:
        _login(cl, "alice")
        cl.post("/submit", data={
            "date": today, "player_id": "1", "status": "completed", "solve_method": "clean", "time": "1:30",
            **_board_payload(size, regions, stars),
        })

    # Carol has no submission for today → gated.
    with app.test_client() as cl:
        _login(cl, "carol")
        r = cl.get(f"/days/{today}/theories")
        assert r.status_code == 200
        assert b"Submit your time first" in r.data

    # Bob submits a DNF → DNF counts as having engaged → page renders.
    with app.test_client() as cl:
        _login(cl, "bob")
        cl.post("/submit", data={
            "date": today, "player_id": "2", "status": "dnf",
        })
        r = cl.get(f"/days/{today}/theories")
        assert r.status_code == 200
        assert b"Submit your time first" not in r.data
        assert b"Canonical board" in r.data

    # Admin always bypasses.
    with app.test_client() as cl:
        _login(cl, "admin")
        r = cl.get(f"/days/{today}/theories")
        assert r.status_code == 200
        assert b"Submit your time first" not in r.data


def test_admin_reset_board_cascades(app):
    today = date.today().isoformat()
    size, regions, stars = _valid_8x8()
    with app.test_client() as cl:
        _login(cl, "alice")
        cl.post("/submit", data={
            "date": today, "player_id": "1", "status": "completed", "solve_method": "clean", "time": "1:30",
            **_board_payload(size, regions, stars),
        })

    c = connect(app.config["DATABASE_PATH"])
    try:
        day_id = c.execute(
            "SELECT id FROM puzzle_days WHERE date = ?", (today,)
        ).fetchone()["id"]
        # Foreign keys must be enabled for ON DELETE CASCADE to fire.
        c.execute("PRAGMA foreign_keys = ON")
    finally:
        c.close()

    with app.test_client() as cl:
        _login(cl, "admin")
        r = cl.post(f"/admin/days/{day_id}/reset-board", follow_redirects=False)
        assert r.status_code == 302

    c = connect(app.config["DATABASE_PATH"])
    try:
        c.execute("PRAGMA foreign_keys = ON")
        b = c.execute("SELECT COUNT(*) FROM puzzle_boards").fetchone()[0]
        t = c.execute("SELECT COUNT(*) FROM solve_theories").fetchone()[0]
        # ON DELETE CASCADE fires only when foreign_keys is enabled — db.connect
        # enables it on every fresh connection.
        assert b == 0
        assert t == 0
    finally:
        c.close()


# ---------- honor pledge ----------


def test_assisted_solve_is_recorded_as_dnf_with_time_kept(app):
    """Honor-pledge: 'I used checks/hints' converts the time into a DNF row
    while preserving the recorded time so the player can still see it on
    their profile, and the rating system treats it as a DNF."""
    today = date.today().isoformat()
    with app.test_client() as cl:
        _login(cl, "alice")
        r = cl.post(
            "/submit",
            data={
                "date": today, "player_id": "1",
                "status": "completed", "solve_method": "assisted",
                "time": "1:30",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

    db_path = app.config["DATABASE_PATH"]
    c = connect(db_path)
    try:
        row = c.execute(
            "SELECT status, time_seconds, assisted FROM submissions WHERE player_id = 1"
        ).fetchone()
        assert row["status"] == "dnf"
        assert row["assisted"] == 1
        # Time kept on the DNF row so it shows on the player's profile.
        assert row["time_seconds"] is not None
        assert abs(row["time_seconds"] - 90.0) < 0.01
    finally:
        c.close()


def test_completed_without_method_is_rejected(app):
    """A completed-status submission without a method choice flashes an
    error and writes nothing — the user has to consciously pick one."""
    today = date.today().isoformat()
    with app.test_client() as cl:
        _login(cl, "alice")
        r = cl.post(
            "/submit",
            data={
                "date": today, "player_id": "1",
                "status": "completed", "time": "1:30",
                # no solve_method
            },
            follow_redirects=False,
        )
        assert r.status_code == 302  # redirect back to /submit with flash

    c = connect(app.config["DATABASE_PATH"])
    try:
        n = c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        assert n == 0
    finally:
        c.close()
