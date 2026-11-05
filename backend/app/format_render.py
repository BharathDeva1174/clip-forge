"""Rendering a formatted clip, and proving the result.

This is the Phase 6 counterpart of :mod:`app.clip_render`. It builds the
FFmpeg command for a portrait version of a clip, runs it under the same
bounds, and verifies the output independently.

The vertical layout
-------------------

Two modes, and neither of them stretches the picture:

``center_crop``
    scale to **cover** the canvas, then crop the excess. The frame fills
    the screen and the edges are cut off.

``fit_pad``
    scale to **fit inside** the canvas, then pad the rest with a colour.

Both are written as a ``scale`` with
``force_original_aspect_ratio``, so the aspect ratio is preserved by
construction, and both end with an explicit canvas size, so the output is
exactly the configured resolution.

The captions
-----------

The caption file is written into a directory the application controls, and
FFmpeg is run with that directory as its working directory. The filter
therefore refers to the file by a plain, server-generated name, and the
path of the directory -- which may contain a colon, a comma or a space --
never reaches the filter at all.

The verification
----------------

FFmpeg exiting with zero is not evidence. The output is probed again and has
to have the exact configured dimensions, a finite duration that matches the
clip's measured duration, the expected codec and pixel format, and the
expected audio. When captions were requested, the number of caption events
that were burned in is recorded on the artifact.
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

from app.captions import colour_to_ffmpeg, colour_to_libass, escape_filter_path
from app.errors import ErrorCode
from app.formatting import FormatSettings, LayoutMode

logger = logging.getLogger("clipforge.formats")

#: The container the formatted video is written in.
OUTPUT_CONTAINER = "mp4"

#: The video codec of a formatted video.
OUTPUT_VIDEO_CODEC = "h264"

#: The pixel format of a formatted video.
OUTPUT_PIXEL_FORMAT = "yuv420p"

#: The audio codec of a formatted video.
OUTPUT_AUDIO_CODEC = "aac"

#: The default encoder preset.
DEFAULT_ENCODER_PRESET = "veryfast"

#: The default video quality.
DEFAULT_CRF = 23

#: The default audio bitrate.
DEFAULT_AUDIO_BITRATE = "128k"

#: How much of FFmpeg's stderr is kept for the log.
_MAX_DIAGNOSTIC_BYTES = 2000

#: The smallest output that is accepted as a real file.
DEFAULT_MIN_OUTPUT_BYTES = 1024


class FormatRenderError(Exception):
    """A formatted clip could not be rendered, or did not survive verification."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FormatVerification:
    """The measured facts about a formatted video."""

    path: Path
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    pixel_format: str
    audio_codec: str | None
    has_audio: bool


# --- the filters -------------------------------------------------------------


def build_scale_filter(settings: FormatSettings) -> str:
    """The filter that puts the source frame into the portrait canvas."""
    width = settings.width
    height = settings.height
    if settings.layout is LayoutMode.FIT_PAD:
        colour = colour_to_ffmpeg(settings.padding_colour)
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={colour}"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )


def build_style(settings: FormatSettings) -> str:
    """The libass style, built from the validated settings.

    The value is a comma-separated list of libass options, so it is wrapped
    in single quotes: inside them a comma is just a comma. The font name has
    already been restricted to characters that cannot end the value.
    """
    captions = settings.captions
    parts = [
        f"FontName={captions.font_family}",
        f"FontSize={captions.font_size}",
        f"PrimaryColour={colour_to_libass(captions.text_colour)}",
        f"OutlineColour={colour_to_libass(captions.outline_colour)}",
        f"Outline={captions.outline_thickness}",
        f"Shadow={captions.shadow}",
        f"Alignment={captions.alignment}",
        f"MarginV={captions.margin_vertical}",
    ]
    if captions.background_colour:
        # BorderStyle 3 draws a box behind the text.
        parts.append("BorderStyle=3")
        parts.append(f"BackColour={colour_to_libass(captions.background_colour)}")
    else:
        parts.append("BorderStyle=1")
    return ",".join(parts)


def build_filter_chain(
    settings: FormatSettings,
    *,
    subtitle_filename: str | None,
) -> str:
    """The whole ``-vf`` value.

    The subtitle file is referred to by its plain name, and the renderer runs
    FFmpeg in the directory that holds it, so no path from the machine ever
    reaches the filter.
    """
    chain = [build_scale_filter(settings)]
    if subtitle_filename is not None:
        escaped = escape_filter_path(subtitle_filename)
        chain.append(f"subtitles=filename='{escaped}':force_style='{build_style(settings)}'")
    return ",".join(chain)


def build_format_args(
    *,
    ffmpeg_path: str,
    source: Path,
    output: Path,
    settings: FormatSettings,
    subtitle_filename: str | None = None,
    encoder_preset: str = DEFAULT_ENCODER_PRESET,
    has_audio: bool = True,
    crf: int = DEFAULT_CRF,
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE,
) -> list[str]:
    """Build the FFmpeg argument list for one formatted clip."""
    args = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        build_filter_chain(settings, subtitle_filename=subtitle_filename),
        "-map",
        "0:v:0",
    ]
    if has_audio:
        args += ["-map", "0:a:0?"]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        encoder_preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        OUTPUT_PIXEL_FORMAT,
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", audio_bitrate]
    args += [
        "-movflags",
        "+faststart",
        "-f",
        OUTPUT_CONTAINER,
        str(output),
    ]
    return args


# --- running ----------------------------------------------------------------


def render_format(
    args: list[str],
    *,
    timeout_seconds: int,
    cwd: Path | None = None,
    on_tick=None,
) -> None:
    """Run FFmpeg under a bound.

    The child has a timeout, is terminated and then killed if it does not
    stop, and is always reaped. Its stderr goes to a temporary file rather
    than to an unread pipe.

    Raises:
        FormatRenderError: when FFmpeg is missing, times out, or fails.
    """
    with tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(  # noqa: S603 - argument list, never a shell
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                shell=False,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            raise FormatRenderError(
                ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
                "FFmpeg is not installed or not on PATH.",
            ) from exc
        except OSError as exc:
            raise FormatRenderError(
                ErrorCode.FORMAT_RENDER_FAILED.value,
                "FFmpeg could not be started.",
            ) from exc

        started = time.monotonic()
        try:
            while True:
                if time.monotonic() - started >= timeout_seconds:
                    _stop(process)
                    raise FormatRenderError(
                        ErrorCode.FORMAT_RENDER_TIMEOUT.value,
                        "The formatted clip did not finish rendering in time.",
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
            "ffmpeg exited with %s while formatting a clip: %s",
            process.returncode,
            stderr_text,
        )
        raise FormatRenderError(
            ErrorCode.FORMAT_RENDER_FAILED.value,
            "The formatted clip could not be rendered.",
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


def verify_format_output(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    expected_duration_seconds: float,
    expected_has_audio: bool,
    tolerance_seconds: float,
    output_dir: Path,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: int = 60,
    min_output_bytes: int = DEFAULT_MIN_OUTPUT_BYTES,
) -> FormatVerification:
    """Check that a formatted file is a real, playable, portrait video.

    FFmpeg exiting with zero is not evidence. The file is probed again and
    has to have:

    1. the exact configured dimensions;
    2. a finite, positive duration, within the tolerance of the clip's
       measured duration;
    3. the expected video codec and pixel format;
    4. the expected audio presence;
    5. a size above the minimum, and a location inside the output
       directory.

    Raises:
        FormatRenderError: with a stable code when a check fails.
    """
    if not path.exists() or not path.is_file():
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip is missing.",
        )

    try:
        resolved = path.resolve()
        root = output_dir.resolve()
    except OSError as exc:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip could not be resolved.",
        ) from exc
    if resolved != root and root not in resolved.parents:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip is outside the output directory.",
        )

    size = path.stat().st_size
    if size < min_output_bytes:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip is too small to be a real video.",
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
        raise FormatRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe is not installed or not on PATH.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FormatRenderError(
            ErrorCode.FORMAT_VERIFICATION_FAILED.value,
            "The formatted clip could not be verified in time.",
        ) from exc
    except OSError as exc:
        raise FormatRenderError(
            ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
            "FFprobe could not be executed.",
        ) from exc

    if completed.returncode != 0:
        raise FormatRenderError(
            ErrorCode.FORMAT_VERIFICATION_FAILED.value,
            "The formatted clip could not be read back as a media file.",
        )

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FormatRenderError(
            ErrorCode.FORMAT_VERIFICATION_FAILED.value,
            "The formatted clip could not be read back as a media file.",
        ) from exc

    if not isinstance(payload, dict):
        raise FormatRenderError(
            ErrorCode.FORMAT_VERIFICATION_FAILED.value,
            "The formatted clip could not be read back as a media file.",
        )

    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []
    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")

    if video is None:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip has no video stream.",
        )

    width = _positive_int(video.get("width"))
    height = _positive_int(video.get("height"))
    if not width or not height:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip has no usable dimensions.",
        )
    if width != expected_width or height != expected_height:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip does not have the configured dimensions.",
        )

    duration = _duration(payload, streams)
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip has no usable duration.",
        )
    if abs(duration - expected_duration_seconds) > tolerance_seconds:
        raise FormatRenderError(
            ErrorCode.FORMAT_DURATION_MISMATCH.value,
            "The formatted clip does not have the length of the clip it came "
            "from.",
        )

    video_codec = str(video.get("codec_name") or "").lower()
    if video_codec != OUTPUT_VIDEO_CODEC:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip is not in the expected video format.",
        )
    pixel_format = str(video.get("pix_fmt") or "").lower()
    if pixel_format != OUTPUT_PIXEL_FORMAT:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip is not in the expected pixel format.",
        )

    if expected_has_audio and audio is None:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip lost its audio track.",
        )
    if not expected_has_audio and audio is not None:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip has an audio track the clip did not have.",
        )
    audio_codec = (
        str(audio.get("codec_name") or "").lower() if audio is not None else None
    )
    if audio is not None and audio_codec != OUTPUT_AUDIO_CODEC:
        raise FormatRenderError(
            ErrorCode.FORMAT_OUTPUT_INVALID.value,
            "The formatted clip is not in the expected audio format.",
        )

    return FormatVerification(
        path=path,
        size_bytes=size,
        duration_seconds=duration,
        width=width,
        height=height,
        video_codec=video_codec,
        pixel_format=pixel_format,
        audio_codec=audio_codec,
        has_audio=audio is not None,
    )


def _first_stream(streams: list, kind: str) -> dict | None:
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == kind:
            return stream
    return None


def _duration(payload: dict, streams: list) -> float | None:
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
