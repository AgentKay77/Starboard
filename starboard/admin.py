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

from starboard import apr, clock, pauses, queries, seasons, settings as settings_mod, theories as theories_mod, weekly

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

    from flask import current_app

    day_d = date.fromisoformat(day_date_iso)
    week_start = _monday_of(day_d)
    today_monday = _monday_of(clock.local_today(current_app.config["WEEK_TIMEZONE"]))
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


@admin_bp.route("/", methods=["GET", "POST"])
@admin_required
def index():
    from flask import current_app
    from starboard.app import get_db

    conn = get_db()
    env_default = current_app.config["ENABLE_USER_SUBMISSIONS"]

    if request.method == "POST":
        action = request.form.get("action")
        if action == "toggle_user_submissions":
            currently_on = settings_mod.submissions_enabled(conn, env_default)
            new_value = not currently_on
            settings_mod.set_submissions_enabled(
                conn, new_value, user_id=current_user.id
            )
            flash(
                f"User submissions are now "
                f"{'ON' if new_value else 'OFF'}.",
                "success",
            )
        elif action == "go_to_bulk":
            d = (request.form.get("date") or "").strip()
            try:
                date.fromisoformat(d)
                return redirect(url_for("admin.bulk_day", day_date=d))
            except ValueError:
                flash("Pick a valid date.", "error")
        elif action == "start_new_season":
            confirm = request.form.get("confirm") or ""
            planned_end = (request.form.get("planned_end_date") or "").strip() or None
            if confirm.strip().upper() != "NEW SEASON":
                flash(
                    'Type "NEW SEASON" exactly to confirm — this resets standings.',
                    "error",
                )
            else:
                try:
                    new_id = seasons.start_new(
                        conn,
                        user_id=current_user.id,
                        planned_end_date=planned_end,
                    )
                    flash(
                        f"Season opened (id={new_id}). Standings now read empty "
                        "until the first submission of the new season lands.",
                        "success",
                    )
                except ValueError as e:
                    flash(f"Couldn't open season: {e}", "error")
        elif action == "set_planned_end":
            planned_end = (request.form.get("planned_end_date") or "").strip() or None
            try:
                seasons.set_planned_end_date(
                    conn, seasons.get_current_id(conn), planned_end
                )
                flash(
                    "Planned end date updated."
                    + (" Auto-close disabled." if planned_end is None else ""),
                    "success",
                )
            except ValueError as e:
                flash(f"Bad date: {e}", "error")
        return redirect(url_for("admin.index"))

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
    return render_template(
        "admin/index.html",
        counts=counts,
        user_submissions_enabled=settings_mod.submissions_enabled(conn, env_default),
        today=clock.local_today(current_app.config["WEEK_TIMEZONE"]).isoformat(),
        current_season=seasons.get_current(conn),
        all_seasons=seasons.list_all(conn),
    )


# ---------- players ----------


@admin_bp.route("/players", methods=["GET", "POST"])
@admin_required
def players():
    from flask import current_app
    from starboard.app import get_db

    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = (request.form.get("name") or "").strip()
            display = (request.form.get("display_name") or "").strip() or None
            joined = (
                (request.form.get("joined_date") or "").strip()
                or clock.local_today(
                    current_app.config["WEEK_TIMEZONE"]
                ).isoformat()
            )
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
                    "INSERT INTO puzzle_days (date, notes, season_id) VALUES (?, ?, ?)",
                    (d, notes, seasons.get_current_id(conn)),
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


# ---------- per-player pause windows ----------


@admin_bp.route("/players/<int:player_id>/pauses", methods=["GET", "POST"])
@admin_required
def player_pauses(player_id: int):
    from starboard.app import get_db

    conn = get_db()
    player = conn.execute(
        "SELECT * FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    if not player:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            start = (request.form.get("start_date") or "").strip()
            end = (request.form.get("end_date") or "").strip()
            reason = request.form.get("reason") or ""
            try:
                pauses.create(
                    conn,
                    player_id=player_id,
                    start_date=start, end_date=end,
                    reason=reason, created_by_user_id=current_user.id,
                )
                apr.recompute_all_ratings(conn)
                flash("Pause window added; ratings recomputed.", "success")
            except ValueError as e:
                flash(f"Couldn't save pause: {e}", "error")
        elif action == "delete":
            try:
                pause_id = int(request.form.get("pause_id"))
            except (TypeError, ValueError):
                flash("Missing pause id.", "error")
            else:
                pauses.delete(
                    conn,
                    pause_id=pause_id,
                    requesting_user_id=current_user.id,
                    requesting_player_id=None,
                    is_admin=True,
                )
                apr.recompute_all_ratings(conn)
                flash("Pause removed; ratings recomputed.", "info")
        return redirect(url_for("admin.player_pauses", player_id=player_id))

    return render_template(
        "admin/player_pauses.html",
        player=player,
        pauses_list=pauses.list_for_player(conn, player_id),
    )


@admin_bp.route("/days/<int:day_id>/reset-board", methods=["POST"])
@admin_required
def reset_board(day_id: int):
    """Wipe a day's solve-theory board. ON DELETE CASCADE drops the
    child theories. Audit-logged."""
    from starboard.app import get_db

    conn = get_db()
    day = conn.execute(
        "SELECT date FROM puzzle_days WHERE id = ?", (day_id,)
    ).fetchone()
    if not day:
        abort(404)
    theories_mod.reset_board(conn, day_id)
    settings_mod.audit_log(
        conn, user_id=current_user.id,
        action="reset_solve_board", detail=day["date"],
    )
    conn.commit()
    flash(f"Solve-theory board for {day['date']} reset.", "info")
    return redirect(url_for("admin.submissions", day_id=day_id))


# ---------- bulk daily entry ----------


def _get_or_create_day(conn, day_date_iso: str) -> int:
    row = conn.execute(
        "SELECT id FROM puzzle_days WHERE date = ?", (day_date_iso,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO puzzle_days (date, season_id) VALUES (?, ?)",
        (day_date_iso, seasons.get_current_id(conn)),
    )
    conn.commit()
    return cur.lastrowid


@admin_bp.route("/days/<day_date>/bulk", methods=["GET", "POST"])
@admin_required
def bulk_day(day_date: str):
    """One-page workflow for the admin to enter every player's result for a
    single day at once. UPSERTs in a single transaction, then runs the same
    APR + weekly-awards recompute that `/submit` triggers."""
    from starboard.app import get_db

    try:
        the_date = date.fromisoformat(day_date)
    except ValueError:
        abort(404)

    conn = get_db()
    day_id = _get_or_create_day(conn, the_date.isoformat())

    # Active players who had joined by this date.
    players = conn.execute(
        """SELECT id, name, display_name FROM players
           WHERE active = 1 AND joined_date <= ?
           ORDER BY display_name COLLATE NOCASE, name COLLATE NOCASE""",
        (the_date.isoformat(),),
    ).fetchall()

    if request.method == "POST":
        return _handle_bulk_post(conn, the_date, day_id, players)

    # Existing submissions, keyed by player_id.
    rows = conn.execute(
        "SELECT player_id, status, time_seconds FROM submissions WHERE day_id = ?",
        (day_id,),
    ).fetchall()
    existing = {
        r["player_id"]: {
            "status": r["status"],
            "time_str": queries.format_time(r["time_seconds"])
            if r["time_seconds"] is not None else "",
        }
        for r in rows
    }
    monday = (the_date - timedelta(days=the_date.weekday())).isoformat()
    return render_template(
        "admin/bulk_day.html",
        day_date=the_date.isoformat(),
        players=players,
        existing=existing,
        week_start=monday,
    )


def _handle_bulk_post(conn, the_date, day_id, players):
    errors: list[tuple[int, str]] = []
    counts = {"completed": 0, "dnf": 0, "absent": 0}
    saved = 0
    now = _now()

    # Phase 1: validate everything before writing anything.
    parsed: list[tuple[int, str, float | None]] = []
    for p in players:
        pid = p["id"]
        status = (request.form.get(f"status_{pid}") or "absent").strip()
        if status not in ("completed", "dnf", "absent"):
            errors.append((pid, f"unknown status {status!r}"))
            continue
        seconds: float | None = None
        if status == "completed":
            raw_time = (request.form.get(f"time_{pid}") or "").strip()
            try:
                seconds = queries.parse_bulk_time(raw_time)
                if seconds <= 0:
                    raise ValueError("non-positive")
            except ValueError as e:
                errors.append((pid, f"invalid time {raw_time!r}: {e}"))
                continue
        parsed.append((pid, status, seconds))

    if errors:
        for pid, msg in errors:
            name = next((p["display_name"] or p["name"] for p in players if p["id"] == pid), pid)
            flash(f"{name}: {msg}", "error")
        return redirect(url_for("admin.bulk_day", day_date=the_date.isoformat()))

    # Phase 2: apply in a single transaction.
    try:
        with conn:
            for pid, status, seconds in parsed:
                existing = conn.execute(
                    "SELECT id FROM submissions WHERE player_id = ? AND day_id = ?",
                    (pid, day_id),
                ).fetchone()
                if status == "absent":
                    if existing:
                        conn.execute(
                            "DELETE FROM submissions WHERE id = ?", (existing["id"],)
                        )
                    counts["absent"] += 1
                elif existing:
                    conn.execute(
                        """UPDATE submissions SET time_seconds = ?, status = ?,
                           submitted_at = ?, submitted_by_user_id = ?
                           WHERE id = ?""",
                        (seconds, status, now, current_user.id, existing["id"]),
                    )
                    counts[status] += 1
                else:
                    conn.execute(
                        """INSERT INTO submissions
                               (player_id, day_id, time_seconds, status,
                                submitted_at, submitted_by_user_id)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (pid, day_id, seconds, status, now, current_user.id),
                    )
                    counts[status] += 1
                saved += 1
    except Exception as e:
        flash(f"Couldn't save day: {e}", "error")
        return redirect(url_for("admin.bulk_day", day_date=the_date.isoformat()))

    # Recompute APR (same path /submit uses) and any locked-week awards.
    apr.recompute_all_ratings(conn)
    _flash_closed_week_warning(the_date.isoformat())

    flash(
        f"{saved} saved · {counts['completed']} completed · "
        f"{counts['dnf']} DNF · {counts['absent']} absent",
        "success",
    )
    return redirect(url_for("admin.bulk_day", day_date=the_date.isoformat()))
