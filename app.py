"""PlexTraktSync – Web UI for syncing Plex watch history to Trakt.tv."""

import os
import re
import json
import threading
import logging
import time as _time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_wtf.csrf import CSRFProtect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from crypto_utils import load_secrets, save_secrets, safe_secrets_for_template, CONFIG_DIR
from plex_client import (
    connect, get_watched_movies, get_watched_episodes, validate_plex_url,
    get_rated_movies, get_rated_episodes, set_plex_rating,
    get_managed_users, connect_as_user,
)
from trakt_client import TraktClient

app = Flask(__name__)
_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not _secret:
    _secret = os.urandom(32).hex()
    logging.getLogger("plextraktsync").warning(
        "FLASK_SECRET_KEY not set — generated ephemeral key. Sessions will not persist across restarts."
    )
app.secret_key = _secret
csrf = CSRFProtect(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("plextraktsync")

SYNC_LOG_FILE = CONFIG_DIR / "sync_history.json"
STATS_FILE = CONFIG_DIR / "lifetime_stats.json"
MAX_LOG_ENTRIES = 100

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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    if request.endpoint in ("settings", "setup", "setup_plex", "setup_trakt"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Rate limiting ────────────────────────────────────────────────────────────

_rate_limits: dict[str, list[float]] = {}
_RATE_WINDOW = 60
_RATE_MAX = 10


def rate_limit(group: str):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            now = _time.time()
            key = f"{group}:{request.remote_addr}"
            timestamps = _rate_limits.get(key, [])
            timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
            if len(timestamps) >= _RATE_MAX:
                return jsonify({"status": "error", "message": "Rate limit exceeded."}), 429
            timestamps.append(now)
            _rate_limits[key] = timestamps
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
    parts = cron_str.strip().split()
    return len(parts) == 5 and all(_CRON_PART.match(p) for p in parts)


def _validate_daily_time(time_str: str) -> bool:
    if not _TIME_RE.match(time_str):
        return False
    try:
        h, m = time_str.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, IndexError):
        return False


def _sanitize_error(error) -> str:
    msg = str(error)
    msg = re.sub(r"(/[\w./\\-]+)+", "[path]", msg)
    return msg[:200] + "..." if len(msg) > 200 else msg


# ── Sync history & stats ────────────────────────────────────────────────────

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
    _update_lifetime_stats(entry)


def _load_lifetime_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "total_syncs": 0, "successful_syncs": 0, "failed_syncs": 0,
        "total_movies_synced": 0, "total_episodes_synced": 0,
        "total_ratings_synced": 0, "first_sync": None, "last_sync": None,
    }


def _update_lifetime_stats(entry: dict):
    stats = _load_lifetime_stats()
    stats["total_syncs"] += 1
    if entry.get("success"):
        stats["successful_syncs"] += 1
    else:
        stats["failed_syncs"] += 1
    stats["total_movies_synced"] += entry.get("movies_synced", 0)
    stats["total_episodes_synced"] += entry.get("episodes_synced", 0)
    stats["total_ratings_synced"] += entry.get("ratings_synced", 0)
    if not stats["first_sync"]:
        stats["first_sync"] = entry.get("timestamp")
    stats["last_sync"] = entry.get("timestamp")
    STATS_FILE.write_text(json.dumps(stats, indent=2))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_trakt_client() -> TraktClient | None:
    secrets = load_secrets()
    cid = secrets.get("trakt_client_id")
    csec = secrets.get("trakt_client_secret")
    if not cid or not csec:
        return None
    return TraktClient(
        client_id=cid, client_secret=csec,
        access_token=secrets.get("trakt_access_token"),
        refresh_token=secrets.get("trakt_refresh_token"),
    )


def _is_setup_complete() -> bool:
    secrets = load_secrets()
    return bool(secrets.get("plex_url") and secrets.get("plex_token")
                and secrets.get("trakt_client_id") and secrets.get("trakt_access_token"))


# ── Core sync logic ─────────────────────────────────────────────────────────

def run_sync(triggered_by: str = "manual", dry_run: bool = False, sync_ratings: bool = False):
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
    result = {
        "movies": 0, "episodes": 0, "movies_found": 0, "episodes_found": 0,
        "ratings_synced": 0, "ratings_found": 0, "errors": [], "dry_run": dry_run,
    }

    try:
        server = connect(plex_url, plex_token)
        movie_lib = secrets.get("plex_movie_library", "Movies")
        tv_lib = secrets.get("plex_tv_library", "TV Shows")

        # Watch history
        sync_status["progress"] = f"Scanning '{movie_lib}'..."
        movies = get_watched_movies(server, movie_lib)
        result["movies_found"] = len(movies)

        sync_status["progress"] = f"Scanning '{tv_lib}'..."
        episodes = get_watched_episodes(server, tv_lib)
        result["episodes_found"] = len(episodes)

        if not dry_run:
            if movies:
                sync_status["progress"] = f"Syncing {len(movies)} movies..."
                try:
                    resp = trakt.sync_watched_movies(movies)
                    result["movies"] = resp.get("added", {}).get("movies", len(movies))
                except Exception as e:
                    result["errors"].append(f"Movies: {_sanitize_error(e)}")
            if episodes:
                sync_status["progress"] = f"Syncing {len(episodes)} episodes (batched)..."
                try:
                    resp = trakt.sync_watched_episodes(episodes)
                    result["episodes"] = resp.get("added", {}).get("episodes", len(episodes))
                except Exception as e:
                    result["errors"].append(f"Episodes: {_sanitize_error(e)}")

        # Ratings sync
        if sync_ratings or secrets.get("sync_ratings"):
            sync_status["progress"] = "Scanning Plex ratings..."
            rated_movies = get_rated_movies(server, movie_lib)
            rated_shows = get_rated_episodes(server, tv_lib)
            result["ratings_found"] = len(rated_movies) + len(rated_shows)

            if not dry_run:
                if rated_movies:
                    sync_status["progress"] = f"Syncing {len(rated_movies)} movie ratings..."
                    try:
                        resp = trakt.sync_ratings_movies(rated_movies)
                        result["ratings_synced"] += resp.get("added", {}).get("movies", 0)
                    except Exception as e:
                        result["errors"].append(f"Movie ratings: {_sanitize_error(e)}")
                if rated_shows:
                    sync_status["progress"] = f"Syncing {len(rated_shows)} show ratings..."
                    try:
                        resp = trakt.sync_ratings_shows(rated_shows)
                        result["ratings_synced"] += resp.get("added", {}).get("shows", 0)
                    except Exception as e:
                        result["errors"].append(f"Show ratings: {_sanitize_error(e)}")

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
            "dry_run": dry_run,
            "duration_seconds": round((finished - started).total_seconds(), 1),
            "movies_found": result["movies_found"],
            "movies_synced": result["movies"],
            "episodes_found": result["episodes_found"],
            "episodes_synced": result["episodes"],
            "ratings_found": result["ratings_found"],
            "ratings_synced": result["ratings_synced"],
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
    schedule_info = _get_schedule_info(secrets)
    next_run = None
    job = scheduler.get_job("auto_sync")
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    stats = _load_lifetime_stats()
    return render_template("index.html",
        plex_configured=bool(secrets.get("plex_url") and secrets.get("plex_token")),
        trakt_configured=bool(secrets.get("trakt_access_token")),
        sync_status=sync_status, secrets=safe,
        schedule_info=schedule_info, next_run=next_run, stats=stats,
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
        plex_url = request.form.get("plex_url", "").strip().rstrip("/")
        if plex_url:
            try:
                plex_url = validate_plex_url(plex_url)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("settings"))
        secrets["plex_url"] = plex_url
        plex_token = request.form.get("plex_token", "").strip()
        if plex_token:
            secrets["plex_token"] = plex_token
        secrets["plex_movie_library"] = request.form.get("plex_movie_library", "Movies").strip()[:_LIB_NAME_MAX]
        secrets["plex_tv_library"] = request.form.get("plex_tv_library", "TV Shows").strip()[:_LIB_NAME_MAX]
        client_id = request.form.get("trakt_client_id", "").strip()
        client_secret = request.form.get("trakt_client_secret", "").strip()
        if client_id and len(client_id) <= 128:
            secrets["trakt_client_id"] = client_id
        if client_secret and len(client_secret) <= 128:
            secrets["trakt_client_secret"] = client_secret
        # Feature toggles
        secrets["sync_ratings"] = "sync_ratings" in request.form
        secrets["webhook_enabled"] = "webhook_enabled" in request.form
        # Schedule
        sched_type = request.form.get("schedule_type", "disabled")
        if sched_type not in ("disabled", "interval", "daily", "cron"):
            sched_type = "disabled"
        secrets["schedule_type"] = sched_type
        if sched_type == "interval":
            val = request.form.get("sync_interval", "0").strip()
            secrets["sync_interval_hours"] = max(0, min(int(val) if val.isdigit() else 0, 168))
        elif sched_type == "cron":
            cron_val = request.form.get("sync_cron", "").strip()
            if cron_val and not _validate_cron(cron_val):
                flash("Invalid cron expression.", "error")
                return redirect(url_for("settings"))
            secrets["sync_cron"] = cron_val
        elif sched_type == "daily":
            time_val = request.form.get("sync_daily_time", "03:00").strip()
            if not _validate_daily_time(time_val):
                flash("Invalid time format.", "error")
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
        _device_auth_state.update({
            "device_code": device["device_code"],
            "user_code": device["user_code"],
            "verification_url": device["verification_url"],
            "interval": device.get("interval", 5),
            "expires_in": device.get("expires_in", 600),
        })
        referrer = request.form.get("referrer", "settings")
        if referrer not in ("settings", "setup"):
            referrer = "settings"
        return render_template("trakt_auth.html", device=device, referrer=referrer)
    except Exception as e:
        flash(f"Failed to start Trakt auth: {_sanitize_error(e)}", "error")
        return redirect(url_for("settings"))


@app.route("/trakt/auth/poll", methods=["POST"])
@csrf.exempt
@rate_limit("auth_poll")
def trakt_auth_poll():
    trakt = _get_trakt_client()
    if not trakt or "device_code" not in _device_auth_state:
        return jsonify({"status": "error", "message": "No auth in progress"}), 400
    try:
        token = trakt.poll_for_token(
            _device_auth_state["device_code"],
            interval=_device_auth_state.get("interval", 5), expires_in=30,
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


# ── Sync triggers ────────────────────────────────────────────────────────────

@app.route("/sync", methods=["POST"])
def trigger_sync():
    if sync_status["running"]:
        flash("Sync already in progress.", "warning")
        return redirect(url_for("index"))
    dry_run = "dry_run" in request.form
    thread = threading.Thread(target=run_sync, args=("manual", dry_run), daemon=True)
    thread.start()
    flash("Dry run started." if dry_run else "Sync started.", "info")
    return redirect(url_for("index"))


@app.route("/sync/ratings", methods=["POST"])
def trigger_ratings_sync():
    if sync_status["running"]:
        flash("Sync already in progress.", "warning")
        return redirect(url_for("index"))
    thread = threading.Thread(target=run_sync, args=("manual", False, True), daemon=True)
    thread.start()
    flash("Ratings sync started.", "info")
    return redirect(url_for("index"))


# ── Plex Webhook (real-time sync) ───────────────────────────────────────────

@app.route("/webhook/plex", methods=["POST"])
@csrf.exempt
@rate_limit("webhook")
def plex_webhook():
    """Handle Plex webhook for real-time sync on media.scrobble events."""
    secrets = load_secrets()
    if not secrets.get("webhook_enabled"):
        return jsonify({"status": "ignored", "message": "Webhooks disabled"}), 200

    try:
        # Plex sends multipart form data with a 'payload' field
        payload_str = request.form.get("payload")
        if not payload_str:
            return jsonify({"status": "error", "message": "No payload"}), 400
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, TypeError):
        return jsonify({"status": "error", "message": "Invalid payload"}), 400

    event = payload.get("event")
    if event != "media.scrobble":
        return jsonify({"status": "ignored", "event": event}), 200

    metadata = payload.get("Metadata", {})
    media_type = metadata.get("type")
    trakt = _get_trakt_client()
    if not trakt or not trakt.access_token:
        return jsonify({"status": "error", "message": "Trakt not configured"}), 400

    try:
        if media_type == "movie":
            movie = {
                "title": metadata.get("title", ""),
                "year": metadata.get("year"),
                "imdb": None, "tmdb": None,
                "watched_at": datetime.now().isoformat(),
            }
            for guid in metadata.get("Guid", []):
                gid = guid.get("id", "")
                if "imdb://" in gid:
                    movie["imdb"] = gid.split("imdb://")[-1]
                elif "tmdb://" in gid:
                    movie["tmdb"] = gid.split("tmdb://")[-1]
            trakt.sync_watched_movies([movie])
            log.info("Webhook: synced movie '%s'", movie["title"])
            return jsonify({"status": "ok", "synced": movie["title"]})

        elif media_type == "episode":
            ep = {
                "show_title": metadata.get("grandparentTitle", ""),
                "show_year": metadata.get("parentYear"),
                "season": metadata.get("parentIndex", 1),
                "episode": metadata.get("index", 1),
                "title": metadata.get("title", ""),
                "imdb": None, "tmdb": None, "tvdb": None,
                "watched_at": datetime.now().isoformat(),
            }
            for guid in metadata.get("Guid", []):
                gid = guid.get("id", "")
                for prefix in ("imdb", "tmdb", "tvdb"):
                    if f"{prefix}://" in gid:
                        ep[prefix] = gid.split(f"{prefix}://")[-1]
            trakt.sync_watched_episodes([ep])
            log.info("Webhook: synced episode '%s - %s'", ep["show_title"], ep["title"])
            return jsonify({"status": "ok", "synced": f"{ep['show_title']} - {ep['title']}"})

    except Exception as e:
        log.error("Webhook sync failed: %s", e)
        return jsonify({"status": "error", "message": _sanitize_error(e)}), 500

    return jsonify({"status": "ignored", "type": media_type}), 200


# ── Per-user sync ────────────────────────────────────────────────────────────

@app.route("/users")
def users_page():
    secrets = load_secrets()
    if not secrets.get("plex_token"):
        flash("Configure Plex first.", "error")
        return redirect(url_for("settings"))
    managed_users = get_managed_users(secrets["plex_token"])
    return render_template("users.html", users=managed_users)


# ── Info pages ───────────────────────────────────────────────────────────────

@app.route("/history")
def history():
    entries = _load_sync_history()
    return render_template("history.html", entries=entries)


@app.route("/stats")
def stats_page():
    stats = _load_lifetime_stats()
    history = _load_sync_history()
    # Last 7 days activity
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    recent = [e for e in history if e.get("timestamp", "") >= week_ago]
    recent_movies = sum(e.get("movies_synced", 0) for e in recent)
    recent_episodes = sum(e.get("episodes_synced", 0) for e in recent)
    recent_ratings = sum(e.get("ratings_synced", 0) for e in recent)
    # Daily breakdown for chart
    daily = {}
    for e in recent:
        day = e.get("timestamp", "")[:10]
        if day not in daily:
            daily[day] = {"movies": 0, "episodes": 0, "ratings": 0}
        daily[day]["movies"] += e.get("movies_synced", 0)
        daily[day]["episodes"] += e.get("episodes_synced", 0)
        daily[day]["ratings"] += e.get("ratings_synced", 0)
    # Fill in missing days
    chart_data = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        d = daily.get(day, {"movies": 0, "episodes": 0, "ratings": 0})
        chart_data.append({"date": day, **d})
    return render_template("stats.html", stats=stats,
        recent_movies=recent_movies, recent_episodes=recent_episodes,
        recent_ratings=recent_ratings, chart_data=chart_data,
        recent_syncs=len(recent),
    )


# ── API endpoints ────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/api/status")
def api_status():
    return jsonify(sync_status)

@app.route("/api/history")
def api_history():
    return jsonify(_load_sync_history())

@app.route("/api/stats")
def api_stats():
    return jsonify(_load_lifetime_stats())

@app.route("/test/plex", methods=["POST"])
@rate_limit("test")
def test_plex():
    secrets = load_secrets()
    if not secrets.get("plex_url") or not secrets.get("plex_token"):
        return jsonify({"status": "error", "message": "Plex not configured"}), 400
    try:
        server = connect(secrets["plex_url"], secrets["plex_token"])
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
        return f"Daily at {secrets.get('sync_daily_time', '03:00')}"
    elif stype == "cron":
        c = secrets.get("sync_cron", "")
        return f"Cron: {c}" if c else "Disabled"
    return "Disabled"


def _scheduled_sync():
    run_sync(triggered_by="scheduled")


def _setup_scheduler(secrets: dict):
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
    elif stype == "cron":
        cron_str = secrets.get("sync_cron", "")
        if cron_str and _validate_cron(cron_str):
            parts = cron_str.split()
            scheduler.add_job(_scheduled_sync, CronTrigger(
                minute=parts[0], hour=parts[1], day=parts[2],
                month=parts[3], day_of_week=parts[4],
            ), id="auto_sync", replace_existing=True)


with app.app_context():
    secrets = load_secrets()
    _setup_scheduler(secrets)
    if not scheduler.running:
        scheduler.start()
