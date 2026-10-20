"""Tests for YouTube URL validation.

These tests never touch the network. They check that only genuinely accepted
YouTube URLs survive validation and that nothing else can reach the
acquisition adapter.
"""

from __future__ import annotations

import pytest

from app.errors import ErrorCode
from app.youtube import InvalidYouTubeUrl, parse_youtube_url

ALLOWED = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
)

VIDEO_ID = "dQw4w9WgXcQ"


# --- accepted forms ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"http://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://www.youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
    ],
)
def test_accepted_url_forms(url: str) -> None:
    video = parse_youtube_url(url, allowed_hosts=ALLOWED)
    assert video.video_id == VIDEO_ID
    assert video.canonical_url == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_urls_with_allowed_query_parameters_are_accepted() -> None:
    video = parse_youtube_url(
        f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s&list=PL123&feature=share",
        allowed_hosts=ALLOWED,
    )
    assert video.video_id == VIDEO_ID


def test_the_canonical_url_drops_extra_query_parameters() -> None:
    video = parse_youtube_url(
        f"https://youtu.be/{VIDEO_ID}?t=90&si=tracking",
        allowed_hosts=ALLOWED,
    )
    assert video.canonical_url == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_a_short_url_is_normalised_to_the_canonical_form() -> None:
    video = parse_youtube_url(f"https://youtu.be/{VIDEO_ID}", allowed_hosts=ALLOWED)
    assert video.canonical_url.startswith("https://www.youtube.com/watch?v=")


# --- rejected forms ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_arbitrary_external_hosts_are_rejected(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com.attacker.example/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "https://evil-youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
    ],
)
def test_deceptive_hostnames_are_rejected(url: str) -> None:
    # The hostname is compared exactly, so a suffix or prefix trick fails.
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "gopher://youtube.com/",
    ],
)
def test_unsupported_schemes_are_rejected(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://user@www.youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_urls_with_credentials_are_rejected(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=waytoolongvideoid",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=has spaces",
        "https://www.youtube.com/watch?v=dQw4w9WgXc!",
        "https://youtu.be/nope",
    ],
)
def test_malformed_video_ids_are_rejected(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com:8080/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com:22/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com:notaport/watch?v=dQw4w9WgXcQ",
    ],
)
def test_invalid_ports_are_rejected(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
        "https://localhost/watch?v=dQw4w9WgXcQ",
        "https://192.168.0.1/watch?v=dQw4w9WgXcQ",
        "https://10.0.0.5/watch?v=dQw4w9WgXcQ",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/watch?v=dQw4w9WgXcQ",
    ],
)
def test_loopback_and_private_targets_are_rejected(url: str) -> None:
    # Loopback and private addresses are never in the allowlist, and the
    # comparison is exact.
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url at all",
        "https://www.youtube.com/",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/channel/UCabc",
        "https://www.youtube.com/playlist?list=PL123",
    ],
)
def test_malformed_and_unsupported_paths_are_rejected(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(url, allowed_hosts=ALLOWED)


def test_a_rejected_url_carries_a_stable_error_code() -> None:
    with pytest.raises(InvalidYouTubeUrl) as excinfo:
        parse_youtube_url("https://example.com/x", allowed_hosts=ALLOWED)
    assert excinfo.value.code == ErrorCode.INVALID_YOUTUBE_URL.value


def test_an_unsupported_scheme_uses_the_generic_request_error_code() -> None:
    with pytest.raises(InvalidYouTubeUrl) as excinfo:
        parse_youtube_url("file:///etc/passwd", allowed_hosts=ALLOWED)
    assert excinfo.value.code == ErrorCode.INVALID_REQUEST.value


def test_the_allowlist_is_honoured_exactly() -> None:
    # A host that is not in the allowlist is rejected even if it is a real
    # YouTube host.
    with pytest.raises(InvalidYouTubeUrl):
        parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
            allowed_hosts=frozenset({"youtu.be"}),
        )
