"""Encrypt/decrypt secrets at rest using Fernet (AES-128-CBC)."""

import os
import json
import stat
import logging
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("plextraktsync.crypto")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SECRETS_FILE = CONFIG_DIR / "secrets.enc"

# Keys that are safe to display in templates (non-sensitive)
_SAFE_KEYS = {
    "plex_url", "plex_movie_library", "plex_tv_library",
    "schedule_type", "sync_interval_hours", "sync_daily_time", "sync_cron",
}

# Keys whose presence can be exposed as a boolean, but not their value
_PRESENCE_KEYS = {
    "plex_token", "trakt_client_id", "trakt_client_secret",
    "trakt_access_token", "trakt_refresh_token",
}


def _get_key() -> bytes:
    """Return the Fernet key from env or generate and persist one."""
    env_key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()

    key_file = CONFIG_DIR / ".key"
    if key_file.exists():
        return key_file.read_bytes().strip()

    key = Fernet.generate_key()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    # Owner read/write only
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return key


def _fernet() -> Fernet:
    return Fernet(_get_key())


def load_secrets() -> dict:
    """Load and decrypt the secrets file. Returns empty dict if missing."""
    if not SECRETS_FILE.exists():
        return {}
    try:
        data = SECRETS_FILE.read_bytes()
        decrypted = _fernet().decrypt(data)
        return json.loads(decrypted)
    except (InvalidToken, json.JSONDecodeError) as e:
        log.error("Failed to decrypt secrets: %s", type(e).__name__)
        return {}
    except Exception as e:
        log.error("Unexpected error loading secrets: %s", type(e).__name__)
        return {}


def save_secrets(secrets: dict) -> None:
    """Encrypt and persist the secrets dict."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(secrets).encode()
    encrypted = _fernet().encrypt(raw)
    SECRETS_FILE.write_bytes(encrypted)
    # Restrict file permissions
    SECRETS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def safe_secrets_for_template(secrets: dict) -> dict:
    """Return a sanitized copy of secrets safe for rendering in templates.

    - Safe keys: included as-is (URLs, library names, schedule config)
    - Presence keys: included as boolean (has_plex_token, etc.)
    - Everything else: excluded entirely
    """
    safe = {}
    for k in _SAFE_KEYS:
        if k in secrets:
            safe[k] = secrets[k]
    for k in _PRESENCE_KEYS:
        safe[f"has_{k}"] = bool(secrets.get(k))
    return safe
