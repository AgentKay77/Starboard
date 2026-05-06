"""Submission blueprint. Admin-only at launch; gated by ENABLE_USER_SUBMISSIONS.

When the flag is on, an authenticated user with a claimed player can
submit times for that player only. Admins can always submit on behalf of
any active player.
"""
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

from starboard import apr, queries

submit_bp = Blueprint("submit", __name__)


def _can_submit_for(player_id: int) -> bool:
    if not current_user.is_authenticated:
        return False
    if current_user.is_admin:
        return True
    if not current_app.config["ENABLE_USER_SUBMISSIONS"]:
        return False
    return current_user.player_id == player_id


@submit_bp.route("/submit", methods=["GET", "POST"])
@login_required
def submit():
    from starboard.app import get_db

    enabled = current_app.config["ENABLE_USER_SUBMISSIONS"]
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
    else:
        players = conn.execute(
            "SELECT id, name, display_name FROM players WHERE id = ? AND active = 1",
            (current_user.player_id,),
        ).fetchall()

    if request.method == "POST":
        return _handle_post(conn, today, earliest, players)

    return render_template(
        "submit.html",
        players=players,
        default_date=today.isoformat(),
        min_date=earliest.isoformat(),
        max_date=today.isoformat(),
    )


def _handle_post(conn, today, earliest, players):
    raw_date = (request.form.get("date") or "").strip()
    raw_pid = request.form.get("player_id")
    raw_time = (request.form.get("time") or "").strip()

    try:
        the_date = date.fromisoformat(raw_date)
    except ValueError:
        flash("Pick a valid date.", "error")
        return redirect(url_for("submit.submit"))

    if not (earliest <= the_date <= today) and not current_user.is_admin:
        flash(
            f"Submissions are only accepted for the last "
            f"{(today - earliest).days} days.",
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

    try:
        seconds = queries.parse_time(raw_time)
    except ValueError as e:
        flash(f"Couldn't read that time: {e}.", "error")
        return redirect(url_for("submit.submit"))
    if seconds <= 0:
        flash("Time must be positive.", "error")
        return redirect(url_for("submit.submit"))

    # Make sure puzzle_day exists (auto-create)
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

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM submissions WHERE player_id = ? AND day_id = ?",
        (pid, day_id),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE submissions SET time_seconds = ?, submitted_at = ?,
               submitted_by_user_id = ? WHERE id = ?""",
            (seconds, now, current_user.id, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO submissions
                   (player_id, day_id, time_seconds, submitted_at, submitted_by_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (pid, day_id, seconds, now, current_user.id),
        )
    conn.commit()
    apr.recompute_all_ratings(conn)
    flash(
        f"Recorded {queries.format_time(seconds)} for "
        f"{the_date.isoformat()}.",
        "success",
    )
    return redirect(url_for("public.player_view", player_id=pid))
