"""Admin blueprint: full CRUD over players, days, submissions, users."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from markupsafe import Markup

from starboard import apr, queries, weekly

admin_bp = Blueprint("admin", __name__)


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)

    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _flash_closed_week_warning(day_date_iso: str) -> None:
    """If the edited day belongs to a previously-closed week, nudge the
    admin to manually recompute that week's locked awards."""
    from starboard.app import get_db

    day_d = date.fromisoformat(day_date_iso)
    week_start = _monday_of(day_d)
    today_monday = _monday_of(date.today())
    if week_start >= today_monday:
        return  # current or future week — awards aren't locked yet
    conn = get_db()
    locked = conn.execute(
        "SELECT 1 FROM weekly_awards WHERE week_start = ? LIMIT 1",
        (week_start.isoformat(),),
    ).fetchone()
    if not locked:
        return
    recompute_url = url_for("admin.recompute_one_week_form", week_start=week_start.isoformat())
    flash(
        Markup(
            f"Heads up — this edit lands inside the closed week of "
            f"<strong>{week_start.isoformat()}</strong>, whose awards are locked. "
            f'<a href="{recompute_url}" class="underline font-medium">Recompute that week</a>.'
        ),
        "info",
    )


# ---------- dashboard ----------


@admin_bp.route("/")
@admin_required
def index():
    from starboard.app import get_db

    conn = get_db()
    counts = {
        "players": conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        "active_players": conn.execute(
            "SELECT COUNT(*) FROM players WHERE active = 1"
        ).fetchone()[0],
        "days": conn.execute("SELECT COUNT(*) FROM puzzle_days").fetchone()[0],
        "submissions": conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0],
        "completed": conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE status = 'completed'"
        ).fetchone()[0],
        "dnfs": conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE status = 'dnf'"
        ).fetchone()[0],
        "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "weeks_locked": conn.execute(
            "SELECT COUNT(DISTINCT week_start) FROM weekly_awards"
        ).fetchone()[0],
    }
    return render_template("admin/index.html", counts=counts)


# ---------- players ----------


@admin_bp.route("/players", methods=["GET", "POST"])
@admin_required
def players():
    from starboard.app import get_db

    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = (request.form.get("name") or "").strip()
            display = (request.form.get("display_name") or "").strip() or None
            joined = (request.form.get("joined_date") or "").strip() or date.today().isoformat()
            if not name:
                flash("Player name is required.", "error")
            else:
                try:
                    conn.execute(
                        "INSERT INTO players (name, display_name, joined_date) VALUES (?, ?, ?)",
                        (name, display, joined),
                    )
                    conn.commit()
                    flash(f"Added {name}.", "success")
                    apr.recompute_all_ratings(conn)
                except Exception as e:
                    flash(f"Couldn't add player: {e}", "error")
        elif action == "edit":
            pid = int(request.form.get("player_id"))
            display = (request.form.get("display_name") or "").strip() or None
            conn.execute(
                "UPDATE players SET display_name = ? WHERE id = ?", (display, pid)
            )
            conn.commit()
            flash("Player updated.", "success")
        elif action == "deactivate":
            pid = int(request.form.get("player_id"))
            conn.execute("UPDATE players SET active = 0 WHERE id = ?", (pid,))
            conn.commit()
            apr.recompute_all_ratings(conn)
            flash("Player deactivated. History preserved; ratings recomputed.", "info")
        elif action == "reactivate":
            pid = int(request.form.get("player_id"))
            conn.execute("UPDATE players SET active = 1 WHERE id = ?", (pid,))
            conn.commit()
            apr.recompute_all_ratings(conn)
            flash("Player reactivated; ratings recomputed.", "success")
        return redirect(url_for("admin.players"))

    rows = conn.execute(
        "SELECT * FROM players ORDER BY active DESC, name ASC"
    ).fetchall()
    return render_template("admin/players.html", players=rows)


# ---------- days ----------


@admin_bp.route("/days", methods=["GET", "POST"])
@admin_required
def days():
    from starboard.app import get_db

    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            d = (request.form.get("date") or "").strip()
            notes = (request.form.get("notes") or "").strip() or None
            try:
                date.fromisoformat(d)
                conn.execute(
                    "INSERT INTO puzzle_days (date, notes) VALUES (?, ?)", (d, notes)
                )
                conn.commit()
                flash(f"Added {d}.", "success")
            except Exception as e:
                flash(f"Couldn't add day: {e}", "error")
        elif action == "delete":
            day_id = int(request.form.get("day_id"))
            row = conn.execute(
                "SELECT date FROM puzzle_days WHERE id = ?", (day_id,)
            ).fetchone()
            day_date = row["date"] if row else None
            conn.execute("DELETE FROM puzzle_days WHERE id = ?", (day_id,))
            conn.commit()
            apr.recompute_all_ratings(conn)
            flash("Day deleted; ratings recomputed.", "info")
            if day_date:
                _flash_closed_week_warning(day_date)
        elif action == "edit_notes":
            day_id = int(request.form.get("day_id"))
            notes = (request.form.get("notes") or "").strip() or None
            conn.execute("UPDATE puzzle_days SET notes = ? WHERE id = ?", (notes, day_id))
            conn.commit()
        return redirect(url_for("admin.days"))

    rows = conn.execute(
        """SELECT pd.*,
                  COUNT(s.id) AS field_size,
                  SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) AS completed_count,
                  SUM(CASE WHEN s.status='dnf' THEN 1 ELSE 0 END) AS dnf_count
           FROM puzzle_days pd
           LEFT JOIN submissions s ON s.day_id = pd.id
           GROUP BY pd.id ORDER BY pd.date DESC"""
    ).fetchall()
    return render_template("admin/days.html", days=rows)


# ---------- submissions for a day ----------


@admin_bp.route("/days/<int:day_id>/submissions", methods=["GET", "POST"])
@admin_required
def submissions(day_id: int):
    from starboard.app import get_db

    conn = get_db()
    day = conn.execute("SELECT * FROM puzzle_days WHERE id = ?", (day_id,)).fetchone()
    if not day:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "upsert":
            pid = int(request.form.get("player_id"))
            status = (request.form.get("status") or "completed").strip()
            raw_time = (request.form.get("time") or "").strip()
            if status not in ("completed", "dnf"):
                flash("Invalid status.", "error")
                return redirect(url_for("admin.submissions", day_id=day_id))
            seconds: float | None
            if status == "dnf":
                seconds = None
            else:
                try:
                    seconds = queries.parse_time(raw_time)
                    assert seconds > 0
                except (ValueError, AssertionError):
                    flash(f"Invalid time: {raw_time!r}", "error")
                    return redirect(url_for("admin.submissions", day_id=day_id))
            existing = conn.execute(
                "SELECT id FROM submissions WHERE player_id = ? AND day_id = ?",
                (pid, day_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE submissions SET time_seconds = ?, status = ?,
                       submitted_at = ?, submitted_by_user_id = ?
                       WHERE id = ?""",
                    (seconds, status, _now(), current_user.id, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO submissions
                           (player_id, day_id, time_seconds, status,
                            submitted_at, submitted_by_user_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (pid, day_id, seconds, status, _now(), current_user.id),
                )
            conn.commit()
            apr.recompute_all_ratings(conn)
            flash("Submission saved; ratings recomputed.", "success")
            _flash_closed_week_warning(day["date"])
        elif action == "delete":
            sid = int(request.form.get("submission_id"))
            conn.execute("DELETE FROM submissions WHERE id = ?", (sid,))
            conn.commit()
            apr.recompute_all_ratings(conn)
            flash("Submission deleted; ratings recomputed.", "info")
            _flash_closed_week_warning(day["date"])
        return redirect(url_for("admin.submissions", day_id=day_id))

    subs = conn.execute(
        """SELECT s.*, p.name, p.display_name FROM submissions s
           JOIN players p ON p.id = s.player_id
           WHERE s.day_id = ?
           ORDER BY (s.status = 'dnf') ASC, s.time_seconds ASC""",
        (day_id,),
    ).fetchall()
    # Annotate with is_late boolean for template.
    subs_with_late = []
    for s in subs:
        d = dict(s)
        d["is_late"] = (
            s["submitted_at"][:10] > day["date"] if s["submitted_at"] else False
        )
        subs_with_late.append(d)

    active_players = conn.execute(
        "SELECT id, name, display_name FROM players WHERE active = 1 ORDER BY name"
    ).fetchall()
    submitted_pids = {s["player_id"] for s in subs}
    available = [p for p in active_players if p["id"] not in submitted_pids]
    return render_template(
        "admin/submissions.html",
        day=day,
        submissions=subs_with_late,
        available_players=available,
        week_of=_monday_of(date.fromisoformat(day["date"])).isoformat(),
    )


# ---------- users ----------


@admin_bp.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    from starboard.app import get_db

    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        uid = int(request.form.get("user_id"))
        if action == "promote":
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
            conn.commit()
            flash("Promoted to admin.", "success")
        elif action == "demote":
            if uid == current_user.id:
                flash("You can't demote yourself.", "error")
            else:
                conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (uid,))
                conn.commit()
                flash("Demoted to user.", "info")
        elif action == "unclaim":
            conn.execute("UPDATE users SET player_id = NULL WHERE id = ?", (uid,))
            conn.commit()
            flash("Player unclaimed.", "info")
        return redirect(url_for("admin.users"))

    rows = conn.execute(
        """SELECT u.*, p.name AS player_name FROM users u
           LEFT JOIN players p ON p.id = u.player_id
           ORDER BY u.created_at DESC"""
    ).fetchall()
    return render_template("admin/users.html", users=rows)


# ---------- recompute actions ----------


@admin_bp.route("/recompute/ratings", methods=["POST"])
@admin_required
def recompute_ratings():
    from starboard.app import get_db

    apr.recompute_all_ratings(get_db())
    flash("APR ratings recomputed from scratch.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/recompute/weeks", methods=["POST"])
@admin_required
def recompute_weeks():
    from starboard.app import get_db

    weekly.recompute_all_weeks(get_db())
    flash("All closed weeks' awards recomputed.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/recompute/week", methods=["POST"])
@admin_required
def recompute_one_week():
    from starboard.app import get_db

    week_start = (request.form.get("week_start") or "").strip()
    if not week_start:
        flash("Pick a week.", "error")
    else:
        try:
            weekly.recompute_week(get_db(), week_start)
            flash(f"Awards for week of {week_start} recomputed.", "success")
        except Exception as e:
            flash(f"Couldn't recompute: {e}", "error")
    return redirect(url_for("admin.index"))


@admin_bp.route("/recompute/week/<week_start>", methods=["GET"])
@admin_required
def recompute_one_week_form(week_start: str):
    """GET handler so closed-week warning flashes can link to a one-click
    confirmation page that POSTs the recompute."""
    return render_template("admin/recompute_week.html", week_start=week_start)
