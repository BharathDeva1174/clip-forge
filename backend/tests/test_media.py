"""Tests for media inspection with FFprobe."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.errors import ErrorCode
from app.media import MediaError, ffprobe_available, inspect_media, validate_media

pytestmark = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not installed"
)


def test_a_valid_video_is_inspected(sample_video: Path) -> None:
    info = inspect_media(sample_video)

    assert info.has_video is True
    assert info.width == 320
    assert info.height == 240
    assert info.codec == "h264"
    assert info.duration_seconds > 0
    assert 0 < info.duration_seconds <= 2


def test_a_file_that_is_not_a_video_is_rejected(not_a_video: Path) -> None:
    with pytest.raises(MediaError) as excinfo:
        inspect_media(not_a_video)
    assert excinfo.value.code == ErrorCode.INVALID_MEDIA.value


def test_an_empty_file_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaError) as excinfo:
        inspect_media(empty)
    assert excinfo.value.code == ErrorCode.INVALID_MEDIA.value


def test_a_missing_ffprobe_is_reported_clearly(sample_video: Path) -> None:
    with pytest.raises(MediaError) as excinfo:
        inspect_media(sample_video, ffprobe_path="ffprobe-that-does-not-exist")
    assert excinfo.value.code == ErrorCode.MEDIA_TOOL_UNAVAILABLE.value


def test_the_availability_check_answers_correctly() -> None:
    assert ffprobe_available("ffprobe") is True
    assert ffprobe_available("ffprobe-that-does-not-exist") is False


def test_the_duration_limit_is_enforced(sample_video: Path) -> None:
    info = inspect_media(sample_video)
    with pytest.raises(MediaError) as excinfo:
        validate_media(
            info,
            max_duration_seconds=0.5,
            max_dimension=4096,
            allowed_codecs=["h264"],
        )
    assert excinfo.value.code == ErrorCode.MEDIA_TOO_LONG.value


def test_the_dimension_limit_is_enforced(sample_video: Path) -> None:
    info = inspect_media(sample_video)
    with pytest.raises(MediaError) as excinfo:
        validate_media(
            info,
            max_duration_seconds=3600,
            max_dimension=100,
            allowed_codecs=["h264"],
        )
    assert excinfo.value.code == ErrorCode.MEDIA_DIMENSIONS_EXCEEDED.value


def test_the_codec_allowlist_is_enforced(sample_video: Path) -> None:
    info = inspect_media(sample_video)
    with pytest.raises(MediaError) as excinfo:
        validate_media(
            info,
            max_duration_seconds=3600,
            max_dimension=4096,
            allowed_codecs=["mpeg4"],
        )
    assert excinfo.value.code == ErrorCode.UNSUPPORTED_CODEC.value


def test_a_media_that_meets_the_policy_is_accepted(sample_video: Path) -> None:
    info = inspect_media(sample_video)
    validate_media(
        info,
        max_duration_seconds=3600,
        max_dimension=4096,
        allowed_codecs=["h264", "mpeg4"],
    )
