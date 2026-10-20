"""Strict YouTube URL parsing and normalisation.

This module is deliberately free of any network access. It decides whether a
URL is *allowed*; it never fetches anything.

The rules are:

* only ``http`` and ``https`` are accepted;
* the URL must not carry credentials (``user:pass@``);
* the hostname must be in the configured allowlist, compared **exactly** -- so
  ``youtube.com.attacker.example`` and ``notyoutube.com`` are rejected;
* only the default HTTP/HTTPS ports are accepted;
* the video id must be exactly 11 characters of ``[A-Za-z0-9_-]``;
* only the known YouTube path shapes are accepted.

Every rejection raises :class:`InvalidYouTubeUrl` with a stable error code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.errors import ErrorCode

#: A YouTube video id is exactly 11 URL-safe base64 characters.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Path prefixes that carry the video id directly.
_PATH_PREFIXES = ("/embed/", "/shorts/", "/live/", "/v/")

#: Ports a YouTube URL may use.
_ALLOWED_PORTS = frozenset({None, 80, 443})

#: Query parameters that are understood. Anything else is ignored and does not
#: end up in the canonical URL.
_KNOWN_QUERY_PARAMS = frozenset(
    {"v", "t", "start", "end", "list", "index", "feature", "ab_channel", "si", "pp"}
)


class InvalidYouTubeUrl(ValueError):
    """The submitted URL is not an accepted YouTube video URL."""

    def __init__(self, message: str, *, code: str = ErrorCode.INVALID_YOUTUBE_URL.value) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class YouTubeVideo:
    """A validated YouTube video reference."""

    video_id: str
    canonical_url: str


def parse_youtube_url(raw: str, *, allowed_hosts: frozenset[str]) -> YouTubeVideo:
    """Validate a YouTube URL and return its canonical form.

    Args:
        raw: the URL as submitted by the user.
        allowed_hosts: exact hostnames that may be used.

    Raises:
        InvalidYouTubeUrl: for anything that is not an accepted YouTube video
            URL. No network request is ever made, rejected or not.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidYouTubeUrl("A YouTube URL is required.")

    candidate = raw.strip()
    if len(candidate) > 2048:
        raise InvalidYouTubeUrl("The URL is too long to be a YouTube video URL.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidYouTubeUrl("The URL could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidYouTubeUrl(
            "Only http and https URLs are supported.",
            code=ErrorCode.INVALID_REQUEST.value,
        )

    if parts.username or parts.password:
        raise InvalidYouTubeUrl(
            "URLs containing a username or password are not accepted."
        )

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidYouTubeUrl("The URL has an invalid port.") from exc
    if port not in _ALLOWED_PORTS:
        raise InvalidYouTubeUrl("Only the default HTTP and HTTPS ports are accepted.")

    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise InvalidYouTubeUrl("The URL has no hostname.")
    if hostname not in allowed_hosts:
        # Exact comparison: `youtube.com.attacker.example` is not `youtube.com`.
        raise InvalidYouTubeUrl(
            "Only YouTube URLs are accepted. Upload a local file instead if the "
            "video is hosted elsewhere."
        )

    path = parts.path or "/"
    video_id: str | None = None

    if hostname.endswith("youtu.be"):
        # youtu.be/<id>
        video_id = path.lstrip("/").split("/", 1)[0]
    elif path.rstrip("/") == "/watch":
        video_id = _first_query_value(parts.query, "v")
    elif any(path.startswith(prefix) for prefix in _PATH_PREFIXES):
        for prefix in _PATH_PREFIXES:
            if path.startswith(prefix):
                video_id = path[len(prefix) :].split("/", 1)[0]
                break
    else:
        raise InvalidYouTubeUrl("The URL is not a recognised YouTube video URL.")

    if not video_id:
        raise InvalidYouTubeUrl("The URL does not contain a YouTube video id.")
    if not _VIDEO_ID_RE.match(video_id):
        raise InvalidYouTubeUrl("The YouTube video id is not valid.")

    return YouTubeVideo(
        video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _first_query_value(query: str, key: str) -> str | None:
    """Return the first value of ``key`` in a query string."""
    for pair in query.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        if name == key:
            return value
    return None
