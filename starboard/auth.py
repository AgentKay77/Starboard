"""Auth blueprint: signup, login, logout, claim, password change."""
from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)

from starboard import apr, pauses
from starboard.extensions import limiter, login_manager

ph = PasswordHasher()
auth_bp = Blueprint("auth", __name__)


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.email = row["email"]
        self.username = row["username"]
        self.role = row["role"]
        self.player_id = row["player_id"]
        self.password_hash = row["password_hash"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@login_manager.user_loader
def _load_user(uid: str):
    from starboard.app import get_db

    row = get_db().execute("SELECT * FROM users WHERE id = ?", (int(uid),)).fetchone()
    return User(row) if row else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@auth_bp.route("/signup", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def signup():
    from starboard.app import get_db

    if request.method == "GET":
        return render_template("signup.html")

    email = (request.form.get("email") or "").strip().lower()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not email or not username or not password:
        flash("Email, username, and password are all required.", "error")
        return render_template("signup.html"), 400
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return render_template("signup.html"), 400

    conn = get_db()
    if conn.execute(
        "SELECT id FROM users WHERE email = ? OR username = ?", (email, username)
    ).fetchone():
        flash("That email or username is already taken.", "error")
        return render_template("signup.html"), 409

    role = "admin" if email == current_app.config["ADMIN_EMAIL"] else "user"
    cur = conn.execute(
        """INSERT INTO users (email, username, password_hash, role, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (email, username, ph.hash(password), role, _now()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    login_user(User(row))
    flash(f"Welcome, {username}.", "success")
    return redirect(url_for("public.home"))


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def login():
    from starboard.app import get_db

    if request.method == "GET":
        return render_template("login.html")

    identifier = (request.form.get("identifier") or "").strip().lower()
    password = request.form.get("password") or ""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? OR username = ?",
        (identifier, identifier),
    ).fetchone()
    if not row:
        flash("No account matches those credentials.", "error")
        return render_template("login.html"), 401
    try:
        ph.verify(row["password_hash"], password)
    except VerifyMismatchError:
        flash("No account matches those credentials.", "error")
        return render_template("login.html"), 401
    if ph.check_needs_rehash(row["password_hash"]):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (ph.hash(password), row["id"]),
        )
    conn.execute(
        "UPDATE users SET last_login = ? WHERE id = ?", (_now(), row["id"])
    )
    conn.commit()
    login_user(User(row))
    flash("Signed in.", "success")
    return redirect(url_for("public.home"))


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("public.home"))


@auth_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    from starboard.app import get_db

    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "claim_player":
            pid = request.form.get("player_id")
            if pid:
                already = conn.execute(
                    "SELECT id FROM users WHERE player_id = ? AND id != ?",
                    (int(pid), current_user.id),
                ).fetchone()
                if already:
                    flash("That player is already claimed.", "error")
                else:
                    conn.execute(
                        "UPDATE users SET player_id = ? WHERE id = ?",
                        (int(pid), current_user.id),
                    )
                    conn.commit()
                    flash("Player claimed.", "success")
        elif action == "rename":
            new_username = (request.form.get("username") or "").strip()
            new_display = (request.form.get("display_name") or "").strip()
            if not (2 <= len(new_username) <= 40):
                flash("Username must be 2–40 characters.", "error")
            elif new_username != current_user.username and conn.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?",
                (new_username, current_user.id),
            ).fetchone():
                flash("That username is already taken.", "error")
            elif new_display and len(new_display) > 40:
                flash("Display name must be 40 characters or fewer.", "error")
            else:
                conn.execute(
                    "UPDATE users SET username = ? WHERE id = ?",
                    (new_username, current_user.id),
                )
                # Update the claimed player's display_name in the same
                # transaction so the standings and leaderboards refresh
                # without a separate manual action.
                if current_user.player_id and new_display:
                    conn.execute(
                        "UPDATE players SET display_name = ? WHERE id = ?",
                        (new_display, current_user.player_id),
                    )
                elif current_user.player_id and not new_display:
                    # Empty string → clear override, fall back to canonical name.
                    conn.execute(
                        "UPDATE players SET display_name = NULL WHERE id = ?",
                        (current_user.player_id,),
                    )
                conn.commit()
                flash("Profile updated.", "success")
        elif action == "change_password":
            current_pw = request.form.get("current_password") or ""
            new_pw = request.form.get("new_password") or ""
            if len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "error")
            else:
                try:
                    ph.verify(current_user.password_hash, current_pw)
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (ph.hash(new_pw), current_user.id),
                    )
                    conn.commit()
                    flash("Password updated.", "success")
                except VerifyMismatchError:
                    flash("Current password is incorrect.", "error")
        elif action == "add_pause":
            if not current_user.player_id:
                flash("Claim a player profile first.", "error")
            else:
                start = (request.form.get("start_date") or "").strip()
                end = (request.form.get("end_date") or "").strip()
                reason = request.form.get("reason") or ""
                try:
                    pauses.create(
                        conn,
                        player_id=current_user.player_id,
                        start_date=start, end_date=end,
                        reason=reason, created_by_user_id=current_user.id,
                    )
                    apr.recompute_all_ratings(conn)
                    flash("Paused window saved; ratings recomputed.", "success")
                except ValueError as e:
                    flash(f"Couldn't save pause: {e}", "error")
        elif action == "delete_pause":
            try:
                pause_id = int(request.form.get("pause_id"))
            except (TypeError, ValueError):
                flash("Missing pause id.", "error")
            else:
                ok = pauses.delete(
                    conn,
                    pause_id=pause_id,
                    requesting_user_id=current_user.id,
                    requesting_player_id=current_user.player_id,
                    is_admin=current_user.is_admin,
                )
                if ok:
                    apr.recompute_all_ratings(conn)
                    flash("Pause removed; ratings recomputed.", "info")
                else:
                    flash("You can only remove your own pauses.", "error")
        return redirect(url_for("auth.account"))

    claimed_player = None
    if current_user.player_id:
        claimed_player = conn.execute(
            "SELECT * FROM players WHERE id = ?", (current_user.player_id,)
        ).fetchone()
    unclaimed = conn.execute(
        """SELECT p.* FROM players p
           WHERE p.active = 1
             AND p.id NOT IN (SELECT player_id FROM users WHERE player_id IS NOT NULL)
           ORDER BY p.name"""
    ).fetchall()
    player_pauses = (
        pauses.list_for_player(conn, current_user.player_id)
        if current_user.player_id else []
    )
    return render_template(
        "account.html",
        claimed_player=claimed_player,
        unclaimed=unclaimed,
        player_pauses=player_pauses,
    )
