"""Trakt.tv API client using device-code OAuth and direct REST calls."""

import time
import logging
import requests

log = logging.getLogger("plextraktsync.trakt")

TRAKT_API_URL = "https://api.trakt.tv"
REQUEST_TIMEOUT = 30  # seconds


class TraktClient:
    def __init__(self, client_id: str, client_secret: str, access_token: str = None,
                 refresh_token: str = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token

    def _headers(self, auth: bool = True) -> dict:
        h = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }
        if auth and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    # --- OAuth Device Flow ---
    def get_device_code(self) -> dict:
        resp = requests.post(
            f"{TRAKT_API_URL}/oauth/device/code",
            json={"client_id": self.client_id},
            headers=self._headers(auth=False),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def poll_for_token(self, device_code: str, interval: int = 5,
                       expires_in: int = 600) -> dict | None:
        elapsed = 0
        while elapsed < expires_in:
            time.sleep(interval)
            elapsed += interval
            try:
                resp = requests.post(
                    f"{TRAKT_API_URL}/oauth/device/token",
                    json={
                        "code": device_code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers=self._headers(auth=False),
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                log.warning("Token poll request failed: %s", e)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self.access_token = data["access_token"]
                self.refresh_token = data.get("refresh_token")
                return data
            elif resp.status_code == 400:
                continue  # pending
            elif resp.status_code == 429:
                time.sleep(interval)  # slow down
            else:
                return None
        return None

    def refresh_access_token(self) -> dict | None:
        if not self.refresh_token:
            return None
        try:
            resp = requests.post(
                f"{TRAKT_API_URL}/oauth/token",
                json={
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                    "grant_type": "refresh_token",
                },
                headers=self._headers(auth=False),
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            log.error("Token refresh failed: %s", e)
            return None

        if resp.status_code == 200:
            data = resp.json()
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            return data
        return None

    # --- Sync endpoints ---
    def sync_watched_movies(self, movies: list[dict]) -> dict:
        payload = {"movies": []}
        for m in movies:
            entry = {"title": m["title"], "year": m["year"], "ids": {}}
            if m.get("imdb"):
                entry["ids"]["imdb"] = m["imdb"]
            if m.get("tmdb"):
                try:
                    entry["ids"]["tmdb"] = int(m["tmdb"])
                except (ValueError, TypeError):
                    pass
            if m.get("watched_at"):
                entry["watched_at"] = m["watched_at"]
            payload["movies"].append(entry)

        resp = requests.post(
            f"{TRAKT_API_URL}/sync/history",
            json=payload,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def sync_watched_episodes(self, episodes: list[dict]) -> dict:
        shows: dict[str, dict] = {}
        for ep in episodes:
            key = f"{ep['show_title']}_{ep.get('show_year', '')}"
            if key not in shows:
                show_entry = {
                    "title": ep["show_title"],
                    "year": ep.get("show_year"),
                    "ids": {},
                    "seasons": [],
                }
                for id_key in ("imdb", "tmdb", "tvdb"):
                    val = ep.get(id_key)
                    if val:
                        if id_key == "imdb":
                            show_entry["ids"]["imdb"] = val
                        else:
                            try:
                                show_entry["ids"][id_key] = int(val)
                            except (ValueError, TypeError):
                                pass
                shows[key] = show_entry

            show_entry = shows[key]
            season_num = ep.get("season", 1)
            season = next((s for s in show_entry["seasons"] if s["number"] == season_num), None)
            if not season:
                season = {"number": season_num, "episodes": []}
                show_entry["seasons"].append(season)

            ep_entry = {"number": ep.get("episode", 1)}
            if ep.get("watched_at"):
                ep_entry["watched_at"] = ep["watched_at"]
            season["episodes"].append(ep_entry)

        payload = {"shows": list(shows.values())}
        resp = requests.post(
            f"{TRAKT_API_URL}/sync/history",
            json=payload,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get_profile(self) -> dict | None:
        try:
            resp = requests.get(
                f"{TRAKT_API_URL}/users/me",
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException as e:
            log.error("Profile fetch failed: %s", e)
        return None
