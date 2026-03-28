"""PlexTraktSync – Web UI for syncing Plex watch history to Trakt.tv."""

import os
import re
import json
import threading
import logging
import time as _time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_wtf.csrf import CSRFProtect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from crypto_utils import load_secrets, save_secrets, safe_secrets_for_template, CONFIG_DIR
from plex_client import connect, get_watched_movies, get_watched_episodes, validate_plex_url
from trakt_client import TraktClient

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)

# Secret key MUST be set via env in production; fail loudly if missing
_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not _secret:
    _secret = os.urandom(32).hex()
    logging.getLogger("plextraktsync").warning(
        "FLASK_SECRET_KEY not set — generated ephemeral key. Sessions will not persist across restarts."
    )
app.secret_key = _secret

# CSRF protection for all POST forms
csrf = CSRFProtect(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("plextraktsync")

SYNC_LOG_FILE = CONFIG_DIR / "sync_history.json"
MAX_LOG_ENTRIES = 50

sync_status = {
    "running": False,
    "last_sync": None,
    "last_result": None,
    "error": None,
    "progress": "",
}
_device_auth_state = {}

scheduler = BackgroundScheduler(daemon=True)


# ── Security middleware ──────────────────────────────────────────────────────

@app.after_request
def set_security_headers(response):
    """Add security headers to every response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: allow inline styles (needed for our UI) but restrict everything else
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    # Prevent caching of pages with sensitive data
    if request.endpoint in ("settings", "setup", "setup_plex", "setup_trakt"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Rate limiting (simple in-memory) ─────────────────────────────────────────

_rate_limits: dict[str, list[float]] = {}
_RATE_WINDOW = 60  # seconds
_RATE_MAX = 10  # max requests per window per endpoint group


def rate_limit(group: str):
    """Simple rate limiter decorator."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            now = _time.time()
            key = f"{group}:{request.remote_addr}"
            timestamps = _rate_limits.get(key, [])
            timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
            if len(timestamps) >= _RATE_MAX:
                return jsonify({"status": "error", "message": "Rate limit exceeded. Try again later."}), 429
            timestamps.append(now)
            _rate_limits[key] = timestamps
            # Periodic cleanup: remove stale keys every 100 requests
            if len(_rate_limits) > 100:
                stale = [k for k, v in _rate_limits.items() if not v or now - v[-1] > _RATE_WINDOW]
                for k in stale:
                    _rate_limits.pop(k, None)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ── Input validation ─────────────────────────────────────────────────────────

_CRON_PART = re.compile(r"^[\d\*,/\-]+$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_LIB_NAME_MAX = 100


def _validate_cron(cron_str: str) -> bool:
    """Validate a 5-part cron expression."""
    parts = cron_str.strip().split()
    if len(parts) != 5:
        return False
    return all(_CRON_PART.match(p) for p in parts)


def _validate_daily_time(time_str: str) -> bool:
    """Validate HH:MM format."""
    if not _TIME_RE.match(time_str):
        return False
    try:
        h, m = time_str.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, IndexError):
        return False


def _sanitize_error(error: Exception) -> str:
    """Sanitize error messages to avoid leaking internal paths or secrets."""
    msg = str(error)
    # Strip file paths
    msg = re.sub(r"(/[\w./\\-]+)+", "[path]", msg)
    # Truncate long messages
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return msg


# ── Sync history log ────────────────────────────────────────────────────────

def _load_sync_history() -> list[dict]:
    if SYNC_LOG_FILE.exists():
        try:
            return json.loads(SYNC_LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_sync_entry(entry: dict):
    history = _load_sync_history()
    history.insert(0, entry)
    history = history[:MAX_LOG_ENTRIES]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SYNC_LOG_FILE.write_text(json.dumps(history, indent=2))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_trakt_client() -> TraktClient | None:
    secrets = load_secrets()
    cid = secrets.get("trakt_client_id")
    csec = secrets.get("trakt_client_secret")
    if not cid or not csec:
        return None
    return TraktClient(
        client_id=cid,
        client_secret=csec,
        access_token=secrets.get("trakt_access_token"),
        refresh_token=secrets.get("trakt_refresh_token"),
    )


def _is_setup_complete() -> bool:
    secrets = load_secrets()
    return bool(
        secrets.get("plex_url")
        and secrets.get("plex_token")
        and secrets.get("trakt_client_id")
        and secrets.get("trakt_access_token")
    )


def run_sync(triggered_by: str = "manual"):
    """Execute a full Plex → Trakt sync."""
    secrets = load_secrets()
    plex_url = secrets.get("plex_url")
    plex_token = secrets.get("plex_token")
    if not plex_url or not plex_token:
        sync_status["error"] = "Plex not configured"
        return

    trakt = _get_trakt_client()
    if not trakt or not trakt.access_token:
        sync_status["error"] = "Trakt not authenticated"
        return

    sync_status["running"] = True
    sync_status["error"] = None
    sync_status["progress"] = "Connecting to Plex..."
    started = datetime.now()
    result = {"movies": 0, "episodes": 0, "movies_found": 0, "episodes_found": 0, "errors": []}

    try:
        server = connect(plex_url, plex_token)

        movie_lib = secrets.get("plex_movie_library", "Movies")
        sync_status["progress"] = f"Scanning '{movie_lib}' library..."
        movies = get_watched_movies(server, movie_lib)
        result["movies_found"] = len(movies)
        if movies:
            sync_status["progress"] = f"Syncing {len(movies)} movies to Trakt..."
            try:
                resp = trakt.sync_watched_movies(movies)
                result["movies"] = resp.get("added", {}).get("movies", len(movies))
            except Exception as e:
                result["errors"].append(f"Movies sync failed: {_sanitize_error(e)}")

        tv_lib = secrets.get("plex_tv_library", "TV Shows")
        sync_status["progress"] = f"Scanning '{tv_lib}' library..."
        episodes = get_watched_episodes(server, tv_lib)
        result["episodes_found"] = len(episodes)
        if episodes:
            sync_status["progress"] = f"Syncing {len(episodes)} episodes to Trakt..."
            try:
                resp = trakt.sync_watched_episodes(episodes)
                result["episodes"] = resp.get("added", {}).get("episodes", len(episodes))
            except Exception as e:
                result["errors"].append(f"Episodes sync failed: {_sanitize_error(e)}")

    except Exception as e:
        sync_status["error"] = _sanitize_error(e)
        result["errors"].append(_sanitize_error(e))
        log.exception("Sync failed")
    finally:
        finished = datetime.now()
        sync_status["running"] = False
        sync_status["last_sync"] = finished.isoformat()
        sync_status["last_result"] = result
        sync_status["progress"] = ""

        if trakt and trakt.access_token:
            secrets["trakt_access_token"] = trakt.access_token
            if trakt.refresh_token:
                secrets["trakt_refresh_token"] = trakt.refresh_token
            save_secrets(secrets)

        _save_sync_entry({
            "timestamp": finished.isoformat(),
            "triggered_by": triggered_by,
            "duration_seconds": round((finished - started).total_seconds(), 1),
            "movies_found": result["movies_found"],
            "movies_synced": result["movies"],
            "episodes_found": result["episodes_found"],
            "episodes_synced": result["episodes"],
            "errors": result["errors"],
            "success": len(result["errors"]) == 0,
        })
        log.info("Sync complete: %s", result)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not _is_setup_complete():
        return redirect(url_for("setup"))
    secrets = load_secrets()
    safe = safe_secrets_for_template(secrets)
    plex_configured = bool(secrets.get("plex_url") and secrets.get("plex_token"))
    trakt_configured = bool(secrets.get("trakt_access_token"))
    schedule_info = _get_schedule_info(secrets)
    next_run = None
    job = scheduler.get_job("auto_sync")
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        "index.html",
        plex_configured=plex_configured,
        trakt_configured=trakt_configured,
        sync_status=sync_status,
        secrets=safe,
        schedule_info=schedule_info,
        next_run=next_run,
    )


@app.route("/setup")
def setup():
    secrets = load_secrets()
    safe = safe_secrets_for_template(secrets)
    step = 1
    if secrets.get("plex_url") and secrets.get("plex_token"):
        step = 2
    if secrets.get("trakt_client_id") and secrets.get("trakt_client_secret"):
        step = 3
    if secrets.get("trakt_access_token"):
        step = 4
    return render_template("setup.html", secrets=safe, step=step)


@app.route("/setup/plex", methods=["POST"])
def setup_plex():
    secrets = load_secrets()
    plex_url = request.form.get("plex_url", "").strip().rstrip("/")
    try:
        plex_url = validate_plex_url(plex_url)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("setup"))

    plex_token = request.form.get("plex_token", "").strip()
    if not plex_token:
        flash("Plex token is required.", "error")
        return redirect(url_for("setup"))

    secrets["plex_url"] = plex_url
    secrets["plex_token"] = plex_token
    secrets["plex_movie_library"] = request.form.get("plex_movie_library", "Movies").strip()[:_LIB_NAME_MAX]
    secrets["plex_tv_library"] = request.form.get("plex_tv_library", "TV Shows").strip()[:_LIB_NAME_MAX]
    save_secrets(secrets)
    flash("Plex settings saved.", "success")
    return redirect(url_for("setup"))


@app.route("/setup/trakt", methods=["POST"])
def setup_trakt():
    secrets = load_secrets()
    client_id = request.form.get("trakt_client_id", "").strip()
    client_secret = request.form.get("trakt_client_secret", "").strip()
    if not client_id or not client_secret:
        flash("Both Client ID and Client Secret are required.", "error")
        return redirect(url_for("setup"))
    # Basic format validation (Trakt IDs are hex strings)
    if len(client_id) > 128 or len(client_secret) > 128:
        flash("Invalid Trakt credentials format.", "error")
        return redirect(url_for("setup"))
    secrets["trakt_client_id"] = client_id
    secrets["trakt_client_secret"] = client_secret
    save_secrets(secrets)
    flash("Trakt app credentials saved.", "success")
    return redirect(url_for("setup"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    secrets = load_secrets()
    if request.method == "POST":
        # Validate Plex URL
        plex_url = request.form.get("plex_url", "").strip().rstrip("/")
        if plex_url:
            try:
                plex_url = validate_plex_url(plex_url)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("settings"))
        secrets["plex_url"] = plex_url
        # Only overwrite secrets if user provided a new value (not blank)
        plex_token = request.form.get("plex_token", "").strip()
        if plex_token:
            secrets["plex_token"] = plex_token
        secrets["plex_movie_library"] = request.form.get("plex_movie_library", "Movies").strip()[:_LIB_NAME_MAX]
        secrets["plex_tv_library"] = request.form.get("plex_tv_library", "TV Shows").strip()[:_LIB_NAME_MAX]

        # Trakt credentials — only overwrite if user provided new values
        client_id = request.form.get("trakt_client_id", "").strip()
        client_secret = request.form.get("trakt_client_secret", "").strip()
        if client_id and len(client_id) <= 128:
            secrets["trakt_client_id"] = client_id
        if client_secret and len(client_secret) <= 128:
            secrets["trakt_client_secret"] = client_secret

        # Schedule with validation
        sched_type = request.form.get("schedule_type", "disabled")
        if sched_type not in ("disabled", "interval", "daily", "cron"):
            sched_type = "disabled"
        secrets["schedule_type"] = sched_type

        if sched_type == "interval":
            val = request.form.get("sync_interval", "0").strip()
            hours = int(val) if val.isdigit() else 0
            secrets["sync_interval_hours"] = max(0, min(hours, 168))
        elif sched_type == "cron":
            cron_val = request.form.get("sync_cron", "").strip()
            if cron_val and not _validate_cron(cron_val):
                flash("Invalid cron expression. Use 5-part format: min hour day month weekday", "error")
                return redirect(url_for("settings"))
            secrets["sync_cron"] = cron_val
        elif sched_type == "daily":
            time_val = request.form.get("sync_daily_time", "03:00").strip()
            if not _validate_daily_time(time_val):
                flash("Invalid time format. Use HH:MM (24-hour).", "error")
                return redirect(url_for("settings"))
            secrets["sync_daily_time"] = time_val

        save_secrets(secrets)
        _setup_scheduler(secrets)
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

    safe = safe_secrets_for_template(secrets)
    schedule_info = _get_schedule_info(secrets)
    return render_template("settings.html", secrets=safe, schedule_info=schedule_info)


@app.route("/trakt/auth/start", methods=["POST"])
def trakt_auth_start():
    trakt = _get_trakt_client()
    if not trakt:
        flash("Set Trakt Client ID and Secret first.", "error")
        return redirect(url_for("settings"))
    try:
        device = trakt.get_device_code()
        _device_auth_state["device_code"] = device["device_code"]
        _device_auth_state["user_code"] = device["user_code"]
        _device_auth_state["verification_url"] = device["verification_url"]
        _device_auth_state["interval"] = device.get("interval", 5)
        _device_auth_state["expires_in"] = device.get("expires_in", 600)
        referrer = request.form.get("referrer", "settings")
        # Whitelist referrer values to prevent open redirect
        if referrer not in ("settings", "setup"):
            referrer = "settings"
        return render_template("trakt_auth.html", device=device, referrer=referrer)
    except Exception as e:
        flash(f"Failed to start Trakt auth: {_sanitize_error(e)}", "error")
        return redirect(url_for("settings"))


@app.route("/trakt/auth/poll", methods=["POST"])
@rate_limit("auth_poll")
def trakt_auth_poll():
    trakt = _get_trakt_client()
    if not trakt or "device_code" not in _device_auth_state:
        return jsonify({"status": "error", "message": "No auth in progress"}), 400
    try:
        token = trakt.poll_for_token(
            _device_auth_state["device_code"],
            interval=_device_auth_state.get("interval", 5),
            expires_in=30,
        )
        if token:
            secrets = load_secrets()
            secrets["trakt_access_token"] = token["access_token"]
            secrets["trakt_refresh_token"] = token.get("refresh_token")
            save_secrets(secrets)
            _device_auth_state.clear()
            return jsonify({"status": "success"})
        return jsonify({"status": "pending"})
    except Exception as e:
        return jsonify({"status": "error", "message": _sanitize_error(e)}), 500


@app.route("/trakt/disconnect", methods=["POST"])
def trakt_disconnect():
    secrets = load_secrets()
    secrets.pop("trakt_access_token", None)
    secrets.pop("trakt_refresh_token", None)
    save_secrets(secrets)
    flash("Trakt disconnected.", "info")
    return redirect(url_for("settings"))


@app.route("/sync", methods=["POST"])
def trigger_sync():
    if sync_status["running"]:
        flash("Sync already in progress.", "warning")
        return redirect(url_for("index"))
    thread = threading.Thread(target=run_sync, args=("manual",), daemon=True)
    thread.start()
    flash("Sync started.", "info")
    return redirect(url_for("index"))


@app.route("/history")
def history():
    entries = _load_sync_history()
    return render_template("history.html", entries=entries)


@app.route("/api/status")
def api_status():
    return jsonify(sync_status)


@app.route("/api/history")
def api_history():
    return jsonify(_load_sync_history())


@app.route("/test/plex", methods=["POST"])
@rate_limit("test")
def test_plex():
    secrets = load_secrets()
    plex_url = secrets.get("plex_url")
    plex_token = secrets.get("plex_token")
    if not plex_url or not plex_token:
        return jsonify({"status": "error", "message": "Plex not configured"}), 400
    try:
        server = connect(plex_url, plex_token)
        libs = [s.title for s in server.library.sections()]
        return jsonify({"status": "ok", "server": server.friendlyName, "libraries": libs})
    except Exception as e:
        return jsonify({"status": "error", "message": _sanitize_error(e)}), 400


@app.route("/test/trakt", methods=["POST"])
@rate_limit("test")
def test_trakt():
    trakt = _get_trakt_client()
    if not trakt or not trakt.access_token:
        return jsonify({"status": "error", "message": "Not authenticated"}), 400
    profile = trakt.get_profile()
    if profile:
        return jsonify({"status": "ok", "username": profile.get("username")})
    return jsonify({"status": "error", "message": "Could not fetch profile"}), 400


# ── Scheduler ────────────────────────────────────────────────────────────────

def _get_schedule_info(secrets: dict) -> str:
    stype = secrets.get("schedule_type", "interval")
    if stype == "interval":
        h = secrets.get("sync_interval_hours", 0)
        return f"Every {h} hour(s)" if h else "Disabled"
    elif stype == "daily":
        t = secrets.get("sync_daily_time", "03:00")
        return f"Daily at {t}"
    elif stype == "cron":
        c = secrets.get("sync_cron", "")
        return f"Cron: {c}" if c else "Disabled"
    return "Disabled"


def _scheduled_sync():
    run_sync(triggered_by="scheduled")


def _setup_scheduler(secrets: dict):
    """Configure auto-sync based on schedule_type."""
    scheduler.remove_all_jobs()
    stype = secrets.get("schedule_type", "interval")

    if stype == "interval":
        hours = secrets.get("sync_interval_hours", 0)
        if hours and hours > 0:
            scheduler.add_job(_scheduled_sync, IntervalTrigger(hours=hours),
                              id="auto_sync", replace_existing=True)
            log.info("Scheduled: every %d hour(s)", hours)
    elif stype == "daily":
        time_str = secrets.get("sync_daily_time", "03:00")
        if _validate_daily_time(time_str):
            h, m = time_str.split(":")
            scheduler.add_job(_scheduled_sync, CronTrigger(hour=int(h), minute=int(m)),
                              id="auto_sync", replace_existing=True)
            log.info("Scheduled: daily at %s", time_str)
    elif stype == "cron":
        cron_str = secrets.get("sync_cron", "")
        if cron_str and _validate_cron(cron_str):
            parts = cron_str.split()
            scheduler.add_job(_scheduled_sync, CronTrigger(
                minute=parts[0], hour=parts[1], day=parts[2],
                month=parts[3], day_of_week=parts[4],
            ), id="auto_sync", replace_existing=True)
            log.info("Scheduled: cron %s", cron_str)


# ── Startup ──────────────────────────────────────────────────────────────────

with app.app_context():
    secrets = load_secrets()
    _setup_scheduler(secrets)
    if not scheduler.running:
        scheduler.start()
