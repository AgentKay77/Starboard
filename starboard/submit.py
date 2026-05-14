"""Submission blueprint.

Admins can always submit/edit any past or current day. Non-admins can submit
only when `ENABLE_USER_SUBMISSIONS` (env default) or the runtime override in
the `settings` table is on, and only for the current day's puzzle. Past days
must be backfilled by an admin via the bulk-entry page."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from starboard import apr, clock, queries, seasons, settings as settings_mod, theories

submit_bp = Blueprint("submit", __name__)


def _self_submit_enabled() -> bool:
    from starboard.app import get_db

    return settings_mod.submissions_enabled(
        get_db(), env_default=current_app.config["ENABLE_USER_SUBMISSIONS"]
    )


def _can_submit_for(player_id: int) -> bool:
    if not current_user.is_authenticated:
        return False
    if current_user.is_admin:
        return True
    if not _self_submit_enabled():
        return False
    return current_user.player_id == player_id


@submit_bp.route("/submit", methods=["GET", "POST"])
@login_required
def submit():
    from starboard.app import get_db

    enabled = _self_submit_enabled()
    if not (current_user.is_admin or enabled):
        abort(403)
    if not current_user.is_admin and current_user.player_id is None:
        flash("Claim a player profile first to submit times.", "error")
        return redirect(url_for("auth.account"))

    conn = get_db()
    today = clock.local_today(current_app.config["WEEK_TIMEZONE"])
    lookback = current_app.config["SUBMISSION_LOOKBACK_DAYS"]
    earliest = today - timedelta(days=lookback)

    if current_user.is_admin:
        players = conn.execute(
            "SELECT id, name, display_name FROM players WHERE active = 1 ORDER BY name"
        ).fetchall()
        # Admins can backfill within the lookback window.
        min_date = earliest.isoformat()
    else:
        players = conn.execute(
            "SELECT id, name, display_name FROM players WHERE id = ? AND active = 1",
            (current_user.player_id,),
        ).fetchall()
        # Non-admins can backlog any day back to their joined_date — they
        # might have missed submitting at the time. The closed-week guard
        # in _handle_post still blocks edits to weeks whose awards have
        # been locked, so the rating chase stays honest.
        joined = (
            players[0]["joined_date"] if players and "joined_date" in players[0].keys()
            else None
        )
        if not joined:
            joined_row = conn.execute(
                "SELECT joined_date FROM players WHERE id = ?",
                (current_user.player_id,),
            ).fetchone()
            joined = joined_row["joined_date"] if joined_row else today.isoformat()
        min_date = joined

    if request.method == "POST":
        return _handle_post(conn, today, earliest, players)

    # Surface today's existing board so the form can render in pick-order
    # mode rather than ground-truth-create mode.
    today_day = conn.execute(
        "SELECT id FROM puzzle_days WHERE date = ?", (today.isoformat(),)
    ).fetchone()
    existing_board = None
    can_edit_board = False
    if today_day:
        existing_board = theories.get_board_for_day(conn, today_day["id"])
        if existing_board:
            can_edit_board = (
                existing_board["created_by_user_id"] == current_user.id
                and not theories.board_has_other_theories(
                    conn, today_day["id"], exclude_user_id=current_user.id
                )
            )

    return render_template(
        "submit.html",
        players=players,
        default_date=today.isoformat(),
        min_date=min_date,
        max_date=today.isoformat(),
        existing_board=existing_board,
        can_edit_board=can_edit_board,
        theory_size_choices=list(range(theories.MIN_SIZE, theories.MAX_SIZE + 1)),
        notes_max_len=theories.NOTES_MAX_LEN,
    )


def _handle_post(conn, today, earliest, _players):
    raw_date = (request.form.get("date") or "").strip()
    raw_pid = request.form.get("player_id")
    raw_status = (request.form.get("status") or "completed").strip()
    raw_time = (request.form.get("time") or "").strip()

    try:
        the_date = date.fromisoformat(raw_date)
    except ValueError:
        flash("Pick a valid date.", "error")
        return redirect(url_for("submit.submit"))

    if current_user.is_admin:
        if not (earliest <= the_date <= today):
            flash(
                f"Admin lookback window is {(today - earliest).days} days. "
                "Use bulk entry for older days.",
                "error",
            )
            return redirect(url_for("submit.submit"))
    else:
        if the_date > today:
            flash("You can't submit for a future date.", "error")
            return redirect(url_for("submit.submit"))
        joined_row = conn.execute(
            "SELECT joined_date FROM players WHERE id = ?",
            (current_user.player_id,),
        ).fetchone()
        if joined_row and the_date.isoformat() < joined_row["joined_date"]:
            flash(
                "You can't backlog a submission from before you joined the league.",
                "error",
            )
            return redirect(url_for("submit.submit"))
        # Closed weeks are still off-limits — see the awards-locked guard
        # further down; non-admins get a clear flash there.

    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        flash("Pick a player.", "error")
        return redirect(url_for("submit.submit"))

    if not _can_submit_for(pid):
        abort(403)

    if raw_status not in ("completed", "dnf"):
        flash("Invalid submission status.", "error")
        return redirect(url_for("submit.submit"))

    seconds: float | None
    if raw_status == "dnf":
        seconds = None
    else:
        try:
            seconds = queries.parse_time(raw_time)
        except ValueError as e:
            flash(f"Couldn't read that time: {e}.", "error")
            return redirect(url_for("submit.submit"))
        if seconds <= 0:
            flash("Time must be positive.", "error")
            return redirect(url_for("submit.submit"))

    # Honor pledge: when status='completed' the user must affirm whether the
    # solve was clean or assisted (used checks/hints). An assisted solve is
    # converted to a DNF for rating purposes, but we keep the time so the
    # player can see what they hit on their profile.
    raw_method = (request.form.get("solve_method") or "").strip()
    assisted = 0
    if raw_status == "completed":
        if raw_method not in ("clean", "assisted"):
            flash(
                "Pick whether your solve was clean or used checks/hints.",
                "error",
            )
            return redirect(url_for("submit.submit"))
        if raw_method == "assisted":
            raw_status = "dnf"
            assisted = 1
            # `seconds` stays — it's stored on the DNF row for context.

    day_row = conn.execute(
        "SELECT id FROM puzzle_days WHERE date = ?", (the_date.isoformat(),)
    ).fetchone()
    if not day_row:
        cur = conn.execute(
            "INSERT INTO puzzle_days (date, season_id) VALUES (?, ?)",
            (the_date.isoformat(), seasons.get_current_id(conn)),
        )
        day_id = cur.lastrowid
    else:
        day_id = day_row["id"]

    # If the day's week has already had awards locked (closed week), only
    # admins can edit. Non-admins shouldn't ever land here for the current day,
    # but guard anyway in case clocks wander across midnight.
    if not current_user.is_admin:
        from datetime import timedelta as _td

        monday = the_date - _td(days=the_date.weekday())
        locked = conn.execute(
            "SELECT 1 FROM weekly_awards WHERE week_start = ? LIMIT 1",
            (monday.isoformat(),),
        ).fetchone()
        if locked:
            flash(
                f"The week of {monday.isoformat()} is closed. "
                "Ask an admin to backfill this entry.",
                "error",
            )
            return redirect(url_for("submit.submit"))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM submissions WHERE player_id = ? AND day_id = ?",
        (pid, day_id),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE submissions SET time_seconds = ?, status = ?,
               submitted_at = ?, submitted_by_user_id = ?, assisted = ?
               WHERE id = ?""",
            (seconds, raw_status, now, current_user.id, assisted, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO submissions
                   (player_id, day_id, time_seconds, status, submitted_at,
                    submitted_by_user_id, assisted)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pid, day_id, seconds, raw_status, now, current_user.id, assisted),
        )
    conn.commit()
    apr.recompute_all_ratings(conn)

    # Optional Solve Theory payload — never block a valid time on a bad
    # theory; flash a non-fatal warning if the theory parse fails.
    theory_status = _maybe_save_theory(conn, day_id, the_date.isoformat())

    if raw_status == "dnf":
        if assisted:
            flash(
                f"Recorded {queries.format_time(seconds)} as a DNF "
                f"for {the_date.isoformat()} (used checks/hints — honor pledge).",
                "info",
            )
        else:
            flash(f"Logged a DNF for {the_date.isoformat()}.", "info")
    else:
        flash(
            f"Recorded {queries.format_time(seconds)} for {the_date.isoformat()}.",
            "success",
        )
    if theory_status == "saved":
        flash("Solve theory saved.", "success")
        return redirect(
            url_for("public.day_theories", day_date=the_date.isoformat())
        )
    return redirect(url_for("public.player_view", player_id=pid))


def _maybe_save_theory(conn, day_id: int, day_date_iso: str) -> str | None:
    """Returns 'saved' on success, None when the checkbox wasn't checked,
    and 'failed' (after flashing the reason) when the payload was malformed."""
    if (request.form.get("solve_theory") or "").lower() not in ("on", "1", "true"):
        return None

    raw_pick = (request.form.get("pick_order_json") or "").strip()
    raw_notes = request.form.get("notes")

    existing_board = theories.get_board_for_day(conn, day_id)

    try:
        if existing_board is None:
            # Mode A: ground-truth board doesn't exist — this user defines it.
            size, regions, stars = theories.parse_board_payload(
                request.form.get("size") or "",
                request.form.get("regions_json") or "",
                request.form.get("stars_json") or "",
            )
            try:
                theories.insert_board(
                    conn, day_id=day_id, size=size, regions=regions,
                    stars=stars, user_id=current_user.id,
                )
            except sqlite3.IntegrityError:
                # Lost a race with another concurrent author. Re-read and
                # fall through to Mode B against the canonical board.
                conn.rollback()
                existing_board = theories.get_board_for_day(conn, day_id)
                if existing_board is None:
                    raise theories.TheoryError(
                        "another player saved a board, then it disappeared"
                    )
                flash(
                    "Another player saved the day's board first — your pick "
                    "order is recorded against theirs.",
                    "info",
                )
                stars = existing_board["stars"]
            else:
                # Successful Mode A: pick_order is the order they tapped
                # the stars. If the form didn't send pick_order, fall back
                # to the placement order.
                if not raw_pick:
                    raw_pick = json.dumps([list(s) for s in stars])
        else:
            stars = existing_board["stars"]
            # Self-edit window: this is the board author and no other user
            # has submitted a theory yet → allow them to replace the board.
            if (
                existing_board["created_by_user_id"] == current_user.id
                and not theories.board_has_other_theories(
                    conn, day_id, exclude_user_id=current_user.id
                )
                and request.form.get("regions_json")
            ):
                size, regions, new_stars = theories.parse_board_payload(
                    request.form.get("size") or str(existing_board["size"]),
                    request.form.get("regions_json") or "",
                    request.form.get("stars_json") or "",
                )
                theories.replace_board_if_self_edit_window(
                    conn, day_id=day_id, user_id=current_user.id,
                    size=size, regions=regions, stars=new_stars,
                )
                stars = new_stars
            else:
                # Subsequent users can still ADD stars the first user
                # missed. Regions are frozen; the star set grows. We re-run
                # the puzzle validator on the merged set so additions can't
                # violate touching / 2-per-row-col-region.
                added = theories.parse_added_stars(
                    request.form.get("stars_json") or "",
                    existing_stars=stars,
                    regions=existing_board["regions"],
                    size=existing_board["size"],
                )
                if added:
                    merged = list(stars) + added
                    theories.update_board_stars(
                        conn, day_id=day_id, stars=merged,
                    )
                    stars = merged

        pick_order = theories.parse_pick_order(raw_pick, stars)
        the_size = existing_board["size"] if existing_board else size
        xs = theories.parse_xs(
            request.form.get("xs_json") or "", stars, the_size,
        )
        theories.upsert_theory(
            conn,
            day_id=day_id, user_id=current_user.id,
            pick_order=pick_order,
            notes=theories.clean_notes(raw_notes),
            xs=xs,
        )
        conn.commit()
        return "saved"
    except theories.TheoryError as e:
        conn.rollback()
        flash(f"Time saved, but solve theory was rejected: {e}", "warning")
        return "failed"
