"""Flask app factory + the public blueprint."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, Flask, abort, current_app, g, render_template
from flask_login import current_user

from starboard import db as db_module
from starboard import queries
from starboard.config import Config
from starboard.extensions import limiter, login_manager

public_bp = Blueprint("public", __name__)


def get_db():
    if "db" not in g:
        g.db = db_module.connect(current_app.config["DATABASE_PATH"])
    return g.db


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["DATABASE_PATH"] = cfg.database_path
    app.config["ADMIN_EMAIL"] = cfg.admin_email
    app.config["ENABLE_USER_SUBMISSIONS"] = cfg.enable_user_submissions
    app.config["WEEK_TIMEZONE"] = cfg.week_timezone
    app.config["SUBMISSION_LOOKBACK_DAYS"] = cfg.submission_lookback_days
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"
    limiter.init_app(app)

    from starboard.admin import admin_bp
    from starboard.auth import auth_bp
    from starboard.submit import submit_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(submit_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    @app.teardown_appcontext
    def _close_db(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("403.html"), 403

    @app.errorhandler(404)
    def _notfound(_e):
        return render_template("404.html"), 404

    @app.context_processor
    def _inject_globals():
        return {
            "site_name": "Starboard",
            "submissions_enabled": app.config["ENABLE_USER_SUBMISSIONS"],
            "current_year": date.today().year,
        }

    @app.template_filter("rating")
    def _fmt_rating(value):
        if value is None:
            return "—"
        return f"{value:.0f}"

    @app.template_filter("delta")
    def _fmt_delta(value):
        if value is None:
            return "—"
        sign = "+" if value >= 0 else ""
        return f"{sign}{value:.1f}"

    @app.template_filter("pct")
    def _fmt_pct(value):
        if value is None:
            return "—"
        return f"{value * 100:.0f}%"

    @app.template_filter("time")
    def _fmt_time(value):
        return queries.format_time(value)

    @app.template_filter("zscore")
    def _fmt_z(value):
        if value is None:
            return "—"
        sign = "+" if value >= 0 else ""
        return f"{sign}{value:.2f}σ"

    return app


# ---------- public routes ----------


@public_bp.route("/")
def home():
    conn = get_db()
    s = queries.standings(conn)
    return render_template(
        "home.html",
        leader=s[0] if s else None,
        podium=s[:3],
        latest_day=queries.latest_day(conn),
        current_week=queries.current_week_live(conn),
        stats=queries.total_stats(conn),
    )


@public_bp.route("/standings")
def standings_view():
    return render_template("standings.html", standings=queries.standings(get_db()))


@public_bp.route("/weekly")
def weekly_view():
    conn = get_db()
    return render_template(
        "weekly.html",
        weeks=queries.weekly_history(conn),
        current_week=queries.current_week_live(conn),
        career=queries.career_trophies(conn),
    )


@public_bp.route("/players/<int:player_id>")
def player_view(player_id: int):
    profile = queries.player_profile(get_db(), player_id)
    if not profile:
        abort(404)
    is_own = (
        current_user.is_authenticated
        and getattr(current_user, "player_id", None) == player_id
    )
    return render_template("player.html", profile=profile, is_own=is_own)


@public_bp.route("/h2h")
def h2h_view():
    return render_template("h2h.html", matrix=queries.h2h_matrix(get_db()))


@public_bp.route("/records")
def records_view():
    return render_template("records.html", records=queries.records(get_db()))
