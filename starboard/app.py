"""Flask app factory + the public blueprint."""
from __future__ import annotations

import math
from datetime import date

from flask import Blueprint, Flask, abort, current_app, g, render_template
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

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
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https"

    # Cloudflare tunnel terminates TLS and forwards plain HTTP to localhost.
    # Trust X-Forwarded-Proto/Host so url_for emits https://www.starboard.day.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Idempotent schema bootstrap so deploys that introduce new tables
    # (settings, audit_log, …) don't require a manual sqlite3 shell.
    boot_conn = db_module.connect(cfg.database_path)
    try:
        db_module.init_schema(boot_conn)
    finally:
        boot_conn.close()

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

    @app.after_request
    def _no_cache_dynamic(resp):
        """Cloudflare in front of the tunnel happily caches HTML responses
        that don't say otherwise — that's why the latest-day leaderboard
        was showing yesterday's submissions. Tell the edge (and the
        browser) not to cache anything that isn't a static asset.
        Static files keep Flask's default cacheable headers."""
        from flask import request

        if request.path.startswith("/static/"):
            return resp
        # Cookie-bearing responses (login/signup flows etc.) must already
        # be private; harden the rest of the dynamic surface too.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("403.html"), 403

    @app.errorhandler(404)
    def _notfound(_e):
        return render_template("404.html"), 404

    @app.context_processor
    def _inject_globals():
        from starboard import settings as settings_mod

        try:
            enabled = settings_mod.submissions_enabled(
                get_db(), env_default=app.config["ENABLE_USER_SUBMISSIONS"]
            )
        except Exception:
            # Outside a request (e.g. error handler before db is bound).
            enabled = app.config["ENABLE_USER_SUBMISSIONS"]
        from starboard import clock as _clock

        local_today = _clock.local_today(app.config["WEEK_TIMEZONE"])
        return {
            "site_name": "Starboard",
            "submissions_enabled": enabled,
            "current_year": local_today.year,
            "today_iso": local_today.isoformat(),
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

    # Math filters for SVG layout in templates (constellation graph).
    app.jinja_env.filters["cosf"] = lambda v: math.cos(float(v))
    app.jinja_env.filters["sinf"] = lambda v: math.sin(float(v))

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


@public_bp.route("/seasons")
def seasons_index():
    """List all past + current seasons with a small summary card each."""
    from starboard import seasons as seasons_mod

    conn = get_db()
    all_seasons = seasons_mod.list_all(conn)
    current_id = seasons_mod.get_current_id(conn)
    summaries = []
    for s in all_seasons:
        sid = s["id"]
        days = conn.execute(
            "SELECT COUNT(*) FROM puzzle_days WHERE season_id = ?", (sid,)
        ).fetchone()[0]
        awards = conn.execute(
            "SELECT COUNT(*) FROM weekly_awards WHERE season_id = ?", (sid,)
        ).fetchone()[0]
        summaries.append(
            {**s, "is_current": sid == current_id, "days": days, "awards": awards}
        )
    return render_template("seasons_index.html", seasons=summaries)


@public_bp.route("/seasons/<int:season_id>")
def season_archive(season_id: int):
    """Read-only final standings + career trophies + records for a past
    (or current) season."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM seasons WHERE id = ?", (season_id,)
    ).fetchone()
    if not row:
        abort(404)
    return render_template(
        "season_archive.html",
        season=dict(row),
        standings=queries.standings(conn, season_id=season_id),
        weeks=queries.weekly_history(conn, season_id=season_id),
        career=queries.career_trophies(conn, season_id=season_id),
        records=queries.records(conn, season_id=season_id),
        stats=queries.total_stats(conn, season_id=season_id),
    )


@public_bp.route("/about")
def about():
    """Public-facing rules + APR explainer."""
    from starboard import apr as apr_mod

    return render_template(
        "about.html",
        K=apr_mod.K,
        initial_rating=int(apr_mod.INITIAL_RATING),
        rating_per_sigma=int(apr_mod.RATING_PER_SIGMA),
        dnf_floor=apr_mod.DNF_FLOOR,
        absent_floor=apr_mod.ABSENT_FLOOR,
    )


@public_bp.route("/days/<day_date>/theories")
def day_theories(day_date: str):
    from datetime import date as _date

    from flask_login import current_user

    from starboard import theories as theories_mod

    try:
        the_date = _date.fromisoformat(day_date)
    except ValueError:
        abort(404)

    if not current_user.is_authenticated:
        return render_template(
            "day_theories.html",
            day_date=the_date.isoformat(),
            board=None, theories=[],
            gated=True, gated_reason="signin",
        )

    conn = get_db()
    is_admin = bool(getattr(current_user, "is_admin", False))
    user_player_id = getattr(current_user, "player_id", None)

    can_view = theories_mod.user_can_view_theories(
        conn,
        is_admin=is_admin,
        user_player_id=user_player_id,
        day_date=the_date.isoformat(),
    )
    if not can_view:
        return render_template(
            "day_theories.html",
            day_date=the_date.isoformat(),
            board=None, theories=[],
            gated=True, gated_reason="no_submission",
        )

    day_row = conn.execute(
        "SELECT id FROM puzzle_days WHERE date = ?", (the_date.isoformat(),)
    ).fetchone()
    if not day_row:
        return render_template(
            "day_theories.html",
            day_date=the_date.isoformat(),
            board=None, theories=[], gated=False,
        )

    board = theories_mod.get_board_for_day(conn, day_row["id"])
    theories_list = theories_mod.get_theories_for_day(conn, day_row["id"])
    return render_template(
        "day_theories.html",
        day_date=the_date.isoformat(),
        board=board,
        theories=theories_list,
        gated=False,
    )
