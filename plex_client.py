"""Fetch watched history from a Plex server."""

import re
import logging
from urllib.parse import urlparse

from plexapi.server import PlexServer

log = logging.getLogger("plextraktsync.plex")

# Only allow http/https schemes and reject obviously internal targets
_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|::1|\[::1\]|metadata\.google|169\.254\.\d+\.\d+)$",
    re.IGNORECASE,
)


def validate_plex_url(url: str) -> str:
    """Validate and normalize a Plex server URL. Raises ValueError if invalid."""
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
    """Return a connected PlexServer instance."""
    url = validate_plex_url(plex_url)
    return PlexServer(url, plex_token, timeout=120)


def get_watched_movies(server: PlexServer, library_name: str = "Movies") -> list[dict]:
    """Return list of watched movies with metadata."""
    movies = []
    try:
        section = server.library.section(library_name)
    except Exception:
        log.warning("Movie library '%s' not found", library_name)
        return movies

    for item in section.search(unwatched=False):
        if not item.isWatched:
            continue
        movie = {
            "title": item.title,
            "year": item.year,
            "imdb": None,
            "tmdb": None,
            "watched_at": item.lastViewedAt.isoformat() if item.lastViewedAt else None,
        }
        for guid in getattr(item, "guids", []):
            gid = guid.id if hasattr(guid, "id") else str(guid)
            if "imdb://" in gid:
                movie["imdb"] = gid.split("imdb://")[-1]
            elif "tmdb://" in gid:
                movie["tmdb"] = gid.split("tmdb://")[-1]
        movies.append(movie)
    return movies


def get_watched_episodes(server: PlexServer, library_name: str = "TV Shows") -> list[dict]:
    """Return list of watched episodes with metadata."""
    episodes = []
    try:
        section = server.library.section(library_name)
    except Exception:
        log.warning("TV library '%s' not found", library_name)
        return episodes

    for show in section.all():
        for episode in show.episodes():
            if not episode.isWatched:
                continue
            ep = {
                "show_title": show.title,
                "show_year": show.year,
                "season": episode.parentIndex,
                "episode": episode.index,
                "title": episode.title,
                "imdb": None,
                "tmdb": None,
                "tvdb": None,
                "watched_at": episode.lastViewedAt.isoformat() if episode.lastViewedAt else None,
            }
            for guid in getattr(show, "guids", []):
                gid = guid.id if hasattr(guid, "id") else str(guid)
                if "imdb://" in gid:
                    ep["imdb"] = gid.split("imdb://")[-1]
                elif "tmdb://" in gid:
                    ep["tmdb"] = gid.split("tmdb://")[-1]
                elif "tvdb://" in gid:
                    ep["tvdb"] = gid.split("tvdb://")[-1]
            episodes.append(ep)
    return episodes
