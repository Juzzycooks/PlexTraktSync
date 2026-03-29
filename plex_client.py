"""Fetch watched history and ratings from a Plex server."""

import re
import logging
from urllib.parse import urlparse

from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount

log = logging.getLogger("plextraktsync.plex")

_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|::1|\[::1\]|metadata\.google|169\.254\.\d+\.\d+)$",
    re.IGNORECASE,
)


def validate_plex_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ValueError("Plex URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Plex URL must use http or https")
    if not parsed.hostname:
        raise ValueError("Plex URL must include a hostname")
    if _BLOCKED_HOSTS.match(parsed.hostname):
        raise ValueError("Plex URL cannot point to localhost or metadata endpoints")
    return url


def connect(plex_url: str, plex_token: str) -> PlexServer:
    url = validate_plex_url(plex_url)
    return PlexServer(url, plex_token, timeout=120)


def _extract_ids(item) -> dict:
    """Extract external IDs (imdb, tmdb, tvdb) from a Plex item's guids."""
    ids = {"imdb": None, "tmdb": None, "tvdb": None}
    for guid in getattr(item, "guids", []):
        gid = guid.id if hasattr(guid, "id") else str(guid)
        for prefix in ("imdb", "tmdb", "tvdb"):
            if f"{prefix}://" in gid:
                ids[prefix] = gid.split(f"{prefix}://")[-1]
    return ids


def get_watched_movies(server: PlexServer, library_name: str = "Movies") -> list[dict]:
    movies = []
    try:
        section = server.library.section(library_name)
    except Exception:
        log.warning("Movie library '%s' not found", library_name)
        return movies
    for item in section.search(unwatched=False):
        if not item.isWatched:
            continue
        ids = _extract_ids(item)
        movies.append({
            "title": item.title,
            "year": item.year,
            "imdb": ids["imdb"],
            "tmdb": ids["tmdb"],
            "watched_at": item.lastViewedAt.isoformat() if item.lastViewedAt else None,
        })
    return movies


def get_watched_episodes(server: PlexServer, library_name: str = "TV Shows") -> list[dict]:
    episodes = []
    try:
        section = server.library.section(library_name)
    except Exception:
        log.warning("TV library '%s' not found", library_name)
        return episodes
    for show in section.all():
        show_ids = _extract_ids(show)
        for episode in show.episodes():
            if not episode.isWatched:
                continue
            episodes.append({
                "show_title": show.title,
                "show_year": show.year,
                "season": episode.parentIndex,
                "episode": episode.index,
                "title": episode.title,
                "imdb": show_ids["imdb"],
                "tmdb": show_ids["tmdb"],
                "tvdb": show_ids["tvdb"],
                "watched_at": episode.lastViewedAt.isoformat() if episode.lastViewedAt else None,
            })
    return episodes


def get_rated_movies(server: PlexServer, library_name: str = "Movies") -> list[dict]:
    """Return movies that have a user rating in Plex."""
    rated = []
    try:
        section = server.library.section(library_name)
    except Exception:
        log.warning("Movie library '%s' not found", library_name)
        return rated
    for item in section.all():
        if item.userRating is None:
            continue
        ids = _extract_ids(item)
        rated.append({
            "title": item.title,
            "year": item.year,
            "imdb": ids["imdb"],
            "tmdb": ids["tmdb"],
            "rating": item.userRating,  # Plex uses 0-10 scale
        })
    return rated


def get_rated_episodes(server: PlexServer, library_name: str = "TV Shows") -> list[dict]:
    """Return shows that have a user rating in Plex."""
    rated = []
    try:
        section = server.library.section(library_name)
    except Exception:
        log.warning("TV library '%s' not found", library_name)
        return rated
    for show in section.all():
        if show.userRating is None:
            continue
        ids = _extract_ids(show)
        rated.append({
            "title": show.title,
            "year": show.year,
            "imdb": ids["imdb"],
            "tmdb": ids["tmdb"],
            "tvdb": ids["tvdb"],
            "rating": show.userRating,
        })
    return rated


def set_plex_rating(server: PlexServer, library_name: str, title: str, year: int,
                    rating: float, media_type: str = "movie") -> bool:
    """Set a rating on a Plex item. Returns True if successful."""
    try:
        section = server.library.section(library_name)
        if media_type == "movie":
            results = section.search(title=title, year=year)
        else:
            results = section.search(title=title)
        if results:
            results[0].rate(rating)
            return True
    except Exception as e:
        log.warning("Failed to set Plex rating for '%s': %s", title, e)
    return False


def get_managed_users(plex_token: str) -> list[dict]:
    """Get managed/home users from Plex account."""
    users = []
    try:
        account = MyPlexAccount(token=plex_token)
        for user in account.users():
            users.append({
                "id": user.id,
                "title": user.title,
                "username": user.username or user.title,
                "thumb": getattr(user, "thumb", ""),
            })
    except Exception as e:
        log.warning("Failed to get managed users: %s", e)
    return users


def connect_as_user(plex_url: str, plex_token: str, user_title: str) -> PlexServer | None:
    """Connect to Plex as a managed/home user."""
    try:
        account = MyPlexAccount(token=plex_token)
        user = account.user(user_title)
        user_token = user.get_token(account.resource(connect(plex_url, plex_token).friendlyName).clientIdentifier)
        return PlexServer(plex_url, user_token, timeout=120)
    except Exception as e:
        log.warning("Failed to connect as user '%s': %s", user_title, e)
        return None
