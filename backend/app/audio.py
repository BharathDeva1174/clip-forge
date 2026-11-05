"""Audio extraction with FFmpeg.

This module turns an accepted source video into the mono, 16 kHz, PCM WAV that
an offline speech engine can read. It is the only place that knows how FFmpeg
is invoked.

Guarantees:

* **No shell.** FFmpeg is started with an argument list and ``shell=False``, so
  a path that contains spaces, quotes or Unicode can never become a command.
* **Bounded.** The child process has a timeout, and its output is captured to
  a file rather than a pipe, so a chatty FFmpeg can never fill a pipe buffer
  and deadlock.
* **Bounded diagnostics.** Only a small, truncated part of FFmpeg's stderr is
  kept for the server-side log. It never reaches an API response.
* **Verified output.** A zero exit code is not trusted: the produced file is
  probed again and must really be mono, 16 kHz, PCM and non-empty before it is
  handed on.
* **No collisions.** The output file name is generated here and is unique, so an
  extraction can never overwrite a source video or another job's artefact.
* **Interruptible.** The extractor keeps hold of the running child, so a
  worker that is asked to shut down can stop FFmpeg instead of waiting for it.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.errors import ErrorCode
from app.interruption import WorkInterrupted

logger = logging.getLogger("clipforge.audio")

#: How much of FFmpeg's stderr is kept for the server-side log.
_MAX_DIAGNOSTIC_BYTES = 2000

#: How long FFprobe may spend reading the source or the extracted audio.
_PROBE_TIMEOUT_SECONDS = 60


class AudioExtractionError(Exception):
    """The audio could not be extracted from the source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExtractedAudio:
    """The audio that was produced, and what is known about it."""

    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    size_bytes: int
    codec: str


@dataclass(frozen=True, slots=True)
class SourceAudioInfo:
    """What the source tells us about its own audio track."""

    duration_seconds: float | None
    has_audio: bool
    codec: str | None


class AudioExtractor(Protocol):
    """Produces the audio file that the speech engine reads."""

    def extract(
        self,
        source: Path,
        destination_dir: Path,
        *,
        timeout_seconds: int = 1800,
        sample_rate: int = 16000,
        channels: int = 1,
        max_duration_seconds: float | None = None,
        max_output_bytes: int | None = None,
        min_free_disk_bytes: int = 0,
        source_root: Path | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> ExtractedAudio:
        """Produce the audio that the speech engine reads."""
        ...

    def terminate(self) -> None:
        """Stop a running extraction, if there is one."""
        ...


class FfmpegAudioExtractor:
    """Extracts audio with FFmpeg.

    The extractor remembers the running child process so that a worker
    shutdown can terminate it rather than waiting for a long conversion to
    finish.
    """

    def __init__(self, *, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self._process: subprocess.Popen[bytes] | None = None

    def extract(
        self,
        source: Path,
        destination_dir: Path,
        *,
        timeout_seconds: int = 1800,
        sample_rate: int = 16000,
        channels: int = 1,
        max_duration_seconds: float | None = None,
        max_output_bytes: int | None = None,
        min_free_disk_bytes: int = 0,
        source_root: Path | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> ExtractedAudio:
        """Extract the audio of ``source`` into ``destination_dir``.

        Raises:
            AudioExtractionError: with a stable error code for every failure
                mode.
        """
        source = Path(source)
        if not source.exists():
            raise AudioExtractionError(
                ErrorCode.SOURCE_NOT_FOUND.value,
                "The source file no longer exists on disk.",
            )
        if not source.is_file():
            raise AudioExtractionError(
                ErrorCode.SOURCE_PATH_INVALID.value,
                "The source path does not point at a file.",
            )
        if source_root is not None and not _is_inside(source, source_root):
            raise AudioExtractionError(
                ErrorCode.SOURCE_PATH_INVALID.value,
                "The source file is outside the storage directory.",
            )

        info = probe_source_audio(
            source,
            ffprobe_path=self.ffprobe_path,
            timeout_seconds=timeout_seconds,
        )
        if not info.has_audio:
            raise AudioExtractionError(
                ErrorCode.NO_AUDIO_STREAM.value,
                "The source does not contain an audio track, so there is "
                "nothing to transcribe.",
            )
        if (
            max_duration_seconds is not None
            and info.duration_seconds is not None
            and info.duration_seconds > max_duration_seconds
        ):
            raise AudioExtractionError(
                ErrorCode.MEDIA_TOO_LONG.value,
                f"The source is longer than the configured limit of "
                f"{max_duration_seconds:.0f} seconds.",
            )

        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)

        # The name is generated here, so two workers can never write to the
        # same file and an extraction can never overwrite a source video.
        output_path = destination_dir / f"{uuid.uuid4().hex}.wav"

        estimated_bytes = _estimate_output_bytes(
            info.duration_seconds, sample_rate=sample_rate, channels=channels
        )
        if min_free_disk_bytes > 0:
            free = shutil.disk_usage(destination_dir).free
            if free < min_free_disk_bytes + estimated_bytes:
                raise AudioExtractionError(
                    ErrorCode.INSUFFICIENT_DISK_SPACE.value,
                    "There is not enough free disk space to extract the audio.",
                )

        args = [
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output_path),
        ]

        logger.info(
            "extracting audio from %s (%.1fs of audio expected)",
            source.name,
            info.duration_seconds or 0.0,
        )

        try:
            self._run(args, timeout_seconds=timeout_seconds, on_tick=on_tick)
        except AudioExtractionError:
            output_path.unlink(missing_ok=True)
            raise

        if not output_path.exists():
            raise AudioExtractionError(
                ErrorCode.AUDIO_EXTRACTION_FAILED.value,
                "FFmpeg finished without producing an audio file.",
            )

        size = output_path.stat().st_size
        if size == 0:
            output_path.unlink(missing_ok=True)
            raise AudioExtractionError(
                ErrorCode.AUDIO_OUTPUT_INVALID.value,
                "The extracted audio file was empty.",
            )
        if max_output_bytes is not None and size > max_output_bytes:
            output_path.unlink(missing_ok=True)
            raise AudioExtractionError(
                ErrorCode.AUDIO_OUTPUT_INVALID.value,
                "The extracted audio is larger than the configured limit.",
            )

        extracted = self._verify_output(
            output_path,
            expected_sample_rate=sample_rate,
            expected_channels=channels,
        )
        if extracted is None:
            output_path.unlink(missing_ok=True)
            raise AudioExtractionError(
                ErrorCode.AUDIO_OUTPUT_INVALID.value,
                "The extracted audio could not be read back as the expected "
                "audio.",
            )

        logger.info(
            "extracted %s bytes of mono %d Hz audio from %s",
            extracted.size_bytes,
            extracted.sample_rate,
            source.name,
        )
        return extracted

    def terminate(self) -> None:
        """Stop a running FFmpeg, if there is one."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        _terminate(process)

    # -- internals ----------------------------------------------------------

    def _run(
        self,
        args: list[str],
        *,
        timeout_seconds: int,
        on_tick: Callable[[], None] | None,
    ) -> None:
        """Run FFmpeg with a timeout, and abort it if the caller asks to stop.

        FFmpeg's stderr goes to a temporary file rather than a pipe: a pipe
        has a fixed buffer, and a child that fills it while the parent is not
        reading would block forever.
        """
        try:
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
                    raise AudioExtractionError(
                        ErrorCode.FFMPEG_NOT_FOUND.value,
                        "FFmpeg is not installed or not on PATH. Install FFmpeg "
                        "to extract audio.",
                    ) from exc
                except OSError as exc:
                    raise AudioExtractionError(
                        ErrorCode.AUDIO_EXTRACTION_FAILED.value,
                        "FFmpeg could not be started.",
                    ) from exc

                self._process = process
                started = time.monotonic()
                try:
                    while True:
                        # The deadline is checked before every wait, so a
                        # timeout of zero is honoured immediately and a
                        # process is never left running past its limit.
                        if time.monotonic() - started > timeout_seconds:
                            _terminate(process)
                            raise AudioExtractionError(
                                ErrorCode.AUDIO_EXTRACTION_TIMEOUT.value,
                                "The audio extraction did not finish in time.",
                            )

                        try:
                            process.wait(timeout=0.25)
                            break
                        except subprocess.TimeoutExpired:
                            pass

                        if on_tick is not None:
                            try:
                                on_tick()
                            except WorkInterrupted:
                                _terminate(process)
                                raise
                            except Exception:
                                _terminate(process)
                                raise
                finally:
                    self._process = None

                diagnostics.seek(0)
                detail = diagnostics.read(_MAX_DIAGNOSTIC_BYTES)
                stderr_text = (
                    detail.decode("utf-8", "replace").strip()
                    if isinstance(detail, bytes)
                    else str(detail).strip()
                )

                if process.returncode != 0:
                    logger.warning(
                        "ffmpeg exited with %s while extracting audio: %s",
                        process.returncode,
                        stderr_text,
                    )
                    raise AudioExtractionError(
                        ErrorCode.AUDIO_EXTRACTION_FAILED.value,
                        "The audio could not be extracted from the source.",
                    )
                if stderr_text:
                    logger.info(
                        "ffmpeg reported while extracting audio: %s", stderr_text
                    )
        except (AudioExtractionError, WorkInterrupted):
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise AudioExtractionError(
                ErrorCode.AUDIO_EXTRACTION_FAILED.value,
                "The audio could not be extracted from the source.",
            ) from exc

    def _verify_output(
        self,
        path: Path,
        *,
        expected_sample_rate: int,
        expected_channels: int,
    ) -> ExtractedAudio | None:
        """Probe the extracted file and check that it is what was asked for."""
        payload = self._probe(path)
        if payload is None:
            return None

        streams = payload.get("streams")
        if not isinstance(streams, list):
            return None
        audio_streams = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ]
        if len(audio_streams) != 1:
            return None

        stream = audio_streams[0]
        sample_rate = _read_int(stream.get("sample_rate"))
        channels = _read_int(stream.get("channels"))
        codec = stream.get("codec_name")
        codec = codec.strip().lower() if isinstance(codec, str) and codec.strip() else ""

        if sample_rate != expected_sample_rate or channels != expected_channels:
            logger.warning(
                "extracted audio has %s channels at %s Hz, expected %s at %s",
                channels,
                sample_rate,
                expected_channels,
                expected_sample_rate,
            )
            return None
        if codec not in ("pcm_s16le", ""):
            return None

        duration = _read_duration(payload, audio_streams)
        if duration is None or duration <= 0:
            return None

        return ExtractedAudio(
            path=path,
            duration_seconds=duration,
            sample_rate=sample_rate or expected_sample_rate,
            channels=channels or expected_channels,
            size_bytes=path.stat().st_size,
            codec=codec or "pcm_s16le",
        )

    def _probe(self, path: Path) -> dict | None:
        try:
            completed = subprocess.run(  # noqa: S603 - argument list, never a shell
                [
                    self.ffprobe_path,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if completed.returncode != 0:
            return None

        try:
            payload = json.loads(completed.stdout.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


def extract_audio(
    source: Path,
    destination_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    **kwargs: object,
) -> ExtractedAudio:
    """Extract the audio of ``source`` with a one-off FFmpeg extractor."""
    extractor = FfmpegAudioExtractor(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
    return extractor.extract(source, destination_dir, **kwargs)  # type: ignore[arg-type]


def probe_source_audio(
    source: Path,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
) -> SourceAudioInfo:
    """Read what the source says about its own audio track.

    Raises:
        AudioExtractionError: when the file cannot be read at all.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argument list, never a shell
            [
                ffprobe_path,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(source),
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AudioExtractionError(
            ErrorCode.FFMPEG_NOT_FOUND.value,
            "FFprobe is not installed or not on PATH. Install FFmpeg to extract "
            "audio.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioExtractionError(
            ErrorCode.AUDIO_EXTRACTION_TIMEOUT.value,
            "The source could not be read in time.",
        ) from exc
    except OSError as exc:
        raise AudioExtractionError(
            ErrorCode.FFMPEG_NOT_FOUND.value,
            "FFprobe could not be executed.",
        ) from exc

    if completed.returncode != 0:
        raise AudioExtractionError(
            ErrorCode.INVALID_MEDIA.value,
            "The source could not be read as a media file.",
        )

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AudioExtractionError(
            ErrorCode.INVALID_MEDIA.value,
            "The source could not be read as a media file.",
        ) from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise AudioExtractionError(
            ErrorCode.INVALID_MEDIA.value,
            "The source could not be read as a media file.",
        )

    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        return SourceAudioInfo(duration_seconds=None, has_audio=False, codec=None)

    codec = audio_streams[0].get("codec_name")
    codec = codec.strip().lower() if isinstance(codec, str) and codec.strip() else None

    duration = _read_duration(payload, audio_streams)
    return SourceAudioInfo(duration_seconds=duration, has_audio=True, codec=codec)


def ffmpeg_available(
    ffmpeg_path: str = "ffmpeg", *, timeout_seconds: int = 15
) -> bool:
    """Return whether the configured FFmpeg binary can be executed."""
    try:
        completed = subprocess.run(  # noqa: S603 - argument list, never a shell
            [ffmpeg_path, "-version"],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


# --- internals --------------------------------------------------------------


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop a running FFmpeg, first politely and then forcefully."""
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
            pass


def _read_duration(payload: dict, streams: list[dict]) -> float | None:
    format_info = payload.get("format")
    if isinstance(format_info, dict):
        duration = _read_float(format_info.get("duration"))
        if duration is not None and duration > 0:
            return duration
    durations = [
        _read_float(stream.get("duration"))
        for stream in streams
        if isinstance(stream, dict)
    ]
    usable = [value for value in durations if value is not None and value > 0]
    return max(usable) if usable else None


def _read_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _read_float(value: object) -> float | None:
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


def _estimate_output_bytes(
    duration_seconds: float | None,
    *,
    sample_rate: int,
    channels: int,
) -> int:
    """Roughly how large the PCM output will be, used for the disk check."""
    if duration_seconds is None:
        return 0
    # 16-bit signed samples plus a small WAV header.
    return int(duration_seconds * sample_rate * channels * 2) + 1024


def _is_inside(path: Path, root: Path) -> bool:
    """Return whether ``path`` is inside ``root``.

    The comparison happens on resolved paths, so a symlink that points out of
    the storage directory is refused as well.
    """
    try:
        resolved = Path(path).resolve()
        root = Path(root).resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents
