"""Submission blueprint.

Admins can always submit/edit any past or current day. Non-admins can submit
only when `ENABLE_USER_SUBMISSIONS` (env default) or the runtime override in
the `settings` table is on, and only for the current day's puzzle. Past days
must be backfilled by an admin via the bulk-entry page."""
from __future__ import annotations

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

from starboard import apr, queries, settings as settings_mod

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
    today = date.today()
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
        # Non-admins are locked to today.
        min_date = today.isoformat()

    if request.method == "POST":
        return _handle_post(conn, today, earliest, players)

    return render_template(
        "submit.html",
        players=players,
        default_date=today.isoformat(),
        min_date=min_date,
        max_date=today.isoformat(),
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
        # Non-admins: today only.
        if the_date != today:
            flash(
                "Self-submissions are only accepted for today's puzzle. "
                "Past days are backfilled by the league admin.",
                "error",
            )
            return redirect(url_for("submit.submit"))

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

    day_row = conn.execute(
        "SELECT id FROM puzzle_days WHERE date = ?", (the_date.isoformat(),)
    ).fetchone()
    if not day_row:
        cur = conn.execute(
            "INSERT INTO puzzle_days (date) VALUES (?)", (the_date.isoformat(),)
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
               submitted_at = ?, submitted_by_user_id = ? WHERE id = ?""",
            (seconds, raw_status, now, current_user.id, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO submissions
                   (player_id, day_id, time_seconds, status, submitted_at,
                    submitted_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pid, day_id, seconds, raw_status, now, current_user.id),
        )
    conn.commit()
    apr.recompute_all_ratings(conn)

    if raw_status == "dnf":
        flash(f"Logged a DNF for {the_date.isoformat()}.", "info")
    else:
        flash(
            f"Recorded {queries.format_time(seconds)} for {the_date.isoformat()}.",
            "success",
        )
    return redirect(url_for("public.player_view", player_id=pid))
