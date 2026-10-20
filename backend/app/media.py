"""Media inspection with FFprobe.

FFprobe is the only thing in this module that is trusted to decide whether a
file is a usable video. File extensions and client-supplied MIME types are
never treated as evidence.

FFprobe is always invoked with an argument list -- never through a shell -- so
a filename can never be interpreted as a command.

FFprobe's output is untrusted input: every value is type-checked and range
checked before it is used or stored.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.errors import ErrorCode

#: How much of FFprobe's stderr is kept for diagnostics.
_MAX_DIAGNOSTIC_BYTES = 2000


class MediaError(Exception):
    """A media file could not be accepted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """The facts about a media file that ClipForge cares about."""

    duration_seconds: float
    width: int
    height: int
    codec: str
    format_name: str
    has_video: bool
    has_audio: bool


def ffprobe_available(ffprobe_path: str = "ffprobe", *, timeout_seconds: int = 15) -> bool:
    """Return whether the configured FFprobe binary can be executed."""
    try:
        completed = subprocess.run(
            [ffprobe_path, "-version"],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def inspect_media(
    path: Path,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: int = 60,
) -> MediaInfo:
    """Read the metadata of a media file.

    Raises:
        MediaError: with a stable error code when FFprobe is missing, times
            out, cannot read the file, or the file is not a usable video.
    """
    try:
        completed = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe is not installed or not on PATH. Install FFmpeg to validate videos.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe did not finish reading the file in time.",
        ) from exc
    except OSError as exc:
        raise MediaError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe could not be executed.",
        ) from exc

    if completed.returncode != 0:
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value,
            "The file could not be read as a media file.",
        )

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value,
            "The file could not be read as a media file.",
        ) from exc

    if not isinstance(payload, dict):
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value, "The file could not be read as a media file."
        )

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value, "The file could not be read as a media file."
        )

    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value,
            "The file does not contain a video stream.",
        )

    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]

    video = video_streams[0]
    width = _positive_int(video.get("width"))
    height = _positive_int(video.get("height"))
    if width is None or height is None:
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value,
            "The video stream has no usable dimensions.",
        )

    codec = video.get("codec_name")
    codec = codec.strip().lower() if isinstance(codec, str) and codec.strip() else "unknown"

    format_info = payload.get("format")
    format_name = "unknown"
    if isinstance(format_info, dict):
        raw_format = format_info.get("format_name")
        if isinstance(raw_format, str) and raw_format.strip():
            format_name = raw_format.strip().lower()

    duration = _positive_float(_format_duration(format_info))
    if duration is None:
        duration = _positive_float(_stream_duration(video_streams))
    if duration is None:
        raise MediaError(
            ErrorCode.INVALID_MEDIA.value,
            "The video has no readable duration.",
        )

    return MediaInfo(
        duration_seconds=duration,
        width=width,
        height=height,
        codec=codec,
        format_name=format_name,
        has_video=True,
        has_audio=bool(audio_streams),
    )


def validate_media(
    info: MediaInfo,
    *,
    max_duration_seconds: float,
    max_dimension: int,
    allowed_codecs: list[str],
) -> None:
    """Apply the configured media policy to inspected media.

    Raises:
        MediaError: when the media violates the policy.
    """
    if info.duration_seconds > max_duration_seconds:
        raise MediaError(
            ErrorCode.MEDIA_TOO_LONG.value,
            f"The video is longer than the configured limit of "
            f"{max_duration_seconds:.0f} seconds.",
        )
    if info.width > max_dimension or info.height > max_dimension:
        raise MediaError(
            ErrorCode.MEDIA_DIMENSIONS_EXCEEDED.value,
            f"The video is larger than the configured limit of "
            f"{max_dimension} pixels on a side.",
        )
    if allowed_codecs and info.codec not in allowed_codecs:
        raise MediaError(
            ErrorCode.UNSUPPORTED_CODEC.value,
            f"The video uses the '{info.codec}' codec, which is not in the list of "
            f"accepted codecs.",
        )


def _format_duration(format_info: object) -> object:
    if isinstance(format_info, dict):
        return format_info.get("duration")
    return None


def _stream_duration(streams: list[dict]) -> object:
    durations = [
        stream.get("duration")
        for stream in streams
        if isinstance(stream.get("duration"), (int, float))
    ]
    if not durations:
        return None
    return max(durations)


def _positive_int(value: object) -> int | None:
    number = _as_number(value)
    if number is None or not number.is_integer():
        return None
    result = int(number)
    return result if result > 0 else None


def _positive_float(value: object) -> float | None:
    number = _as_number(value)
    if number is None or not math.isfinite(number) or number <= 0:
        return None
    return number


def _as_number(value: object) -> float | None:
    """Read a number from FFprobe output.

    FFprobe reports some values as JSON numbers and others as decimal strings
    (``"1.000000"``). Both are accepted; anything else is rejected.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None
