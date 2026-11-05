"""Rendering a clip, and proving that the result is real.

This module is the only place that runs FFmpeg or FFprobe for a clip, and
it does three things in order:

1. **Builds the command** from validated, typed parameters. The arguments
   are a list, the process is started with ``shell=False``, and the source
   path is one argument even when it contains spaces, Unicode or shell
   metacharacters. Nothing from the API is ever interpolated into a filter
   expression.
2. **Runs it under a bound.** The child has a timeout, is terminated and
   then killed if it does not stop, and is always reaped. FFmpeg's stderr
   goes to a temporary file rather than to an unread pipe, so a chatty
   encoder can never deadlock the worker.
3. **Verifies the output independently.** FFmpeg exiting with zero is not
   evidence of anything. The file is probed again, and it has to have a
   video stream, positive dimensions, a finite duration, and a duration
   that matches the one that was asked for.

The timestamp policy
--------------------

The requested start and end are seconds on the **source media timeline**.
FFmpeg's ``-ss`` before ``-i`` seeks relative to the file's own start time,
which is what the media timeline is, so the two agree. The clip is
**re-encoded**, not stream-copied: a stream copy cuts on keyframes, so the
result would silently start earlier and end later than the candidate asks
for. Re-encoding is slower and it is the price of the boundary guarantee.

The tolerance is a single number -- ``clip_duration_tolerance_seconds`` --
and the measured duration of the output has to be within it of the
requested one. A clip that cannot be produced within the tolerance fails;
it is never quietly shifted to a keyframe.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.errors import ErrorCode

logger = logging.getLogger("clipforge.clips")

#: The container the clips are written in.
OUTPUT_CONTAINER = "mp4"

#: The video codec of a rendered clip.
OUTPUT_VIDEO_CODEC = "h264"

#: The audio codec of a rendered clip.
OUTPUT_AUDIO_CODEC = "aac"

#: How much of FFmpeg's stderr is kept for the log.
_MAX_DIAGNOSTIC_BYTES = 2000

#: The smallest output that is accepted as a real file.
DEFAULT_MIN_OUTPUT_BYTES = 1024

#: The default encoder preset.
DEFAULT_ENCODER_PRESET = "veryfast"

#: The default video quality.
DEFAULT_CRF = 23

#: The default audio bitrate.
DEFAULT_AUDIO_BITRATE = "128k"


class ClipRenderError(Exception):
    """A clip could not be rendered, or did not survive verification."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SourceProbe:
    """What the source video is."""

    path: Path
    duration_seconds: float
    #: The time the media timeline starts at, which is not always zero.
    start_time_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    has_video: bool
    has_audio: bool


@dataclass(frozen=True, slots=True)
class ClipVerification:
    """The measured facts about a rendered clip."""

    path: Path
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    has_audio: bool


# --- probing ----------------------------------------------------------------


def probe_source(
    path: Path,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: int = 60,
) -> SourceProbe:
    """Read the metadata of the source video.

    Raises:
        ClipRenderError: with a stable code when the file cannot be probed
            or holds no usable video stream.
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
        raise ClipRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe is not installed or not on PATH.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe did not finish reading the source in time.",
        ) from exc
    except OSError as exc:
        raise ClipRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe could not be executed.",
        ) from exc

    if completed.returncode != 0:
        raise ClipRenderError(
            ErrorCode.CLIP_SOURCE_INVALID.value,
            "The source video could not be read as a media file.",
        )

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClipRenderError(
            ErrorCode.CLIP_SOURCE_INVALID.value,
            "The source video could not be read as a media file.",
        ) from exc

    if not isinstance(payload, dict):
        raise ClipRenderError(
            ErrorCode.CLIP_SOURCE_INVALID.value,
            "The source video could not be read as a media file.",
        )

    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []
    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")

    if video is None:
        raise ClipRenderError(
            ErrorCode.CLIP_SOURCE_INVALID.value,
            "The source video has no video stream, so a clip cannot be made "
            "from it.",
        )

    duration = _duration(payload, streams)
    if duration is None or duration <= 0:
        raise ClipRenderError(
            ErrorCode.CLIP_SOURCE_INVALID.value,
            "The source video has no usable duration.",
        )

    return SourceProbe(
        path=path,
        duration_seconds=duration,
        start_time_seconds=_start_time(payload, video),
        width=_positive_int(video.get("width")) or 0,
        height=_positive_int(video.get("height")) or 0,
        video_codec=str(video.get("codec_name") or "").lower(),
        audio_codec=(
            str(audio.get("codec_name") or "").lower() if audio is not None else None
        ),
        has_video=True,
        has_audio=audio is not None,
    )


def _first_stream(streams: list, kind: str) -> dict | None:
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == kind:
            return stream
    return None


def _duration(payload: dict, streams: list) -> float | None:
    """The duration of the media, preferring the container's own."""
    format_info = payload.get("format")
    if isinstance(format_info, dict):
        value = _number(format_info.get("duration"))
        if value is not None and value > 0:
            return value
    values = [
        _number(stream.get("duration"))
        for stream in streams
        if isinstance(stream, dict)
    ]
    usable = [value for value in values if value is not None and value > 0]
    return max(usable) if usable else None


def _start_time(payload: dict, video: dict) -> float:
    """The time the media timeline starts at.

    It is not always zero: a file whose first frame is stamped a little
    into the file has a positive start time, and the requested boundaries
    are relative to the media timeline, not to the file.
    """
    format_info = payload.get("format")
    if isinstance(format_info, dict):
        value = _number(format_info.get("start_time"))
        if value is not None:
            return value
    value = _number(video.get("start_time"))
    if value is not None:
        return value
    return 0.0


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


# --- the command -------------------------------------------------------------


def build_render_args(
    *,
    ffmpeg_path: str,
    source: Path,
    output: Path,
    start_seconds: float,
    duration_seconds: float,
    encoder_preset: str = DEFAULT_ENCODER_PRESET,
    has_audio: bool = True,
    crf: int = DEFAULT_CRF,
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE,
) -> list[str]:
    """Build the FFmpeg argument list for one clip.

    The arguments are fixed and typed. There is no filter expression and no
    value from the API in them, so there is nothing for an argument to be
    injected into.
    """
    args = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        # Seek before the input: with a re-encode this is frame accurate, and
        # it does not decode the whole file up to the boundary. The position
        # is on the source's media timeline, which is what the candidate's
        # times are on.
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(source),
        # The length, so the clip cannot run past the requested end.
        "-t",
        f"{duration_seconds:.3f}",
        # Only the first video and the first audio stream. Nothing else in
        # the source is carried over, so a subtitle or data stream cannot
        # end up in the output.
        "-map",
        "0:v:0",
    ]
    if has_audio:
        # The trailing "?" makes the audio optional, so a source whose audio
        # turns out to be missing still renders rather than failing.
        args += ["-map", "0:a:0?"]
    args += [
        # Deliberate encoding settings rather than an inherited source
        # codec: H.264 in yuv420p and AAC are what plays everywhere.
        "-c:v",
        "libx264",
        "-preset",
        encoder_preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", audio_bitrate]
    args += [
        # Put the index at the front so the file can be played while it is
        # still downloading.
        "-movflags",
        "+faststart",
        "-f",
        OUTPUT_CONTAINER,
        str(output),
    ]
    return args


# --- running ----------------------------------------------------------------


def render_clip(
    args: list[str],
    *,
    timeout_seconds: int,
    on_tick=None,
) -> None:
    """Run FFmpeg under a bound.

    FFmpeg's stderr goes to a temporary file, so a chatty encoder cannot
    fill a pipe and deadlock. The child is terminated and then killed if it
    does not stop, and is always reaped.

    Raises:
        ClipRenderError: when FFmpeg is missing, times out, or fails.
    """
    with tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(  # noqa: S603 - argument list, never a shell
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ClipRenderError(
                ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
                "FFmpeg is not installed or not on PATH.",
            ) from exc
        except OSError as exc:
            raise ClipRenderError(
                ErrorCode.CLIP_RENDER_FAILED.value,
                "FFmpeg could not be started.",
            ) from exc

        started = time.monotonic()
        try:
            while True:
                if time.monotonic() - started >= timeout_seconds:
                    _stop(process)
                    raise ClipRenderError(
                        ErrorCode.CLIP_RENDER_TIMEOUT.value,
                        "The clip did not finish rendering in time.",
                    )
                try:
                    process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if on_tick is not None:
                    on_tick()
        finally:
            if process.poll() is None:
                _stop(process)

        diagnostics.seek(0)
        detail = diagnostics.read(_MAX_DIAGNOSTIC_BYTES)
        stderr_text = (
            detail.decode("utf-8", "replace").strip()
            if isinstance(detail, bytes)
            else str(detail).strip()
        )

    if process.returncode != 0:
        logger.warning(
            "ffmpeg exited with %s while rendering a clip: %s",
            process.returncode,
            stderr_text,
        )
        raise ClipRenderError(
            ErrorCode.CLIP_RENDER_FAILED.value,
            "The clip could not be rendered from that part of the source.",
        )


def _stop(process: subprocess.Popen) -> None:
    """Stop a child: terminate, then kill, then reap."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            logger.warning("the ffmpeg process could not be reaped")


# --- verification -----------------------------------------------------------


def verify_clip(
    path: Path,
    *,
    requested_duration_seconds: float,
    expected_has_audio: bool,
    tolerance_seconds: float,
    output_dir: Path,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: int = 60,
    min_output_bytes: int = DEFAULT_MIN_OUTPUT_BYTES,
) -> ClipVerification:
    """Check that a rendered file is a real, playable clip.

    FFmpeg exiting with zero proves nothing, so the file is probed again
    and every one of these has to hold:

    1. the file exists and is a regular file;
    2. it resolves inside the output directory;
    3. it is bigger than the minimum;
    4. FFprobe can parse it;
    5. it has a video stream;
    6. the width and the height are positive;
    7. the duration is finite and positive;
    8. the duration is within the tolerance of the requested one;
    9. the audio presence matches the source;
    10. the codecs are the ones the output policy asks for.

    Raises:
        ClipRenderError: with a stable code when a check fails.
    """
    if not path.exists() or not path.is_file():
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip is missing.",
        )

    try:
        resolved = path.resolve()
        root = output_dir.resolve()
    except OSError as exc:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip could not be resolved.",
        ) from exc
    if resolved != root and root not in resolved.parents:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip is outside the output directory.",
        )

    size = path.stat().st_size
    if size < min_output_bytes:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip is too small to be a real video.",
        )

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
        raise ClipRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe is not installed or not on PATH.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipRenderError(
            ErrorCode.CLIP_VERIFICATION_FAILED.value,
            "The rendered clip could not be verified in time.",
        ) from exc
    except OSError as exc:
        raise ClipRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe could not be executed.",
        ) from exc

    if completed.returncode != 0:
        raise ClipRenderError(
            ErrorCode.CLIP_VERIFICATION_FAILED.value,
            "The rendered clip could not be read back as a media file.",
        )

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClipRenderError(
            ErrorCode.CLIP_VERIFICATION_FAILED.value,
            "The rendered clip could not be read back as a media file.",
        ) from exc

    if not isinstance(payload, dict):
        raise ClipRenderError(
            ErrorCode.CLIP_VERIFICATION_FAILED.value,
            "The rendered clip could not be read back as a media file.",
        )

    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []
    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")

    if video is None:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip has no video stream.",
        )

    width = _positive_int(video.get("width"))
    height = _positive_int(video.get("height"))
    if not width or not height:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip has no usable dimensions.",
        )

    duration = _duration(payload, streams)
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip has no usable duration.",
        )

    if abs(duration - requested_duration_seconds) > tolerance_seconds:
        raise ClipRenderError(
            ErrorCode.CLIP_DURATION_MISMATCH.value,
            "The rendered clip does not have the length that was asked for.",
        )

    if expected_has_audio and audio is None:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip lost its audio track.",
        )
    if not expected_has_audio and audio is not None:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip has an audio track the source did not have.",
        )

    video_codec = str(video.get("codec_name") or "").lower()
    if video_codec != OUTPUT_VIDEO_CODEC:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip is not in the expected video format.",
        )
    audio_codec = (
        str(audio.get("codec_name") or "").lower() if audio is not None else None
    )
    if audio is not None and audio_codec != OUTPUT_AUDIO_CODEC:
        raise ClipRenderError(
            ErrorCode.CLIP_OUTPUT_INVALID.value,
            "The rendered clip is not in the expected audio format.",
        )

    return ClipVerification(
        path=path,
        size_bytes=size,
        duration_seconds=duration,
        width=width,
        height=height,
        video_codec=video_codec,
        audio_codec=audio_codec,
        has_audio=audio is not None,
    )
