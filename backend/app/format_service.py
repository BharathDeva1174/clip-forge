"""Rendering a formatted clip, end to end.

The service does the whole job for one artifact, in the order the work has
to happen in:

1. **Resolve the clip.** The artifact points at a Phase 5 clip, and the
   clip's file is resolved inside the storage root. A clip that has gone
   is a clear failure, not a crash.
2. **Probe the clip.** Its real duration, its dimensions and whether it has
   sound are measured, not assumed. The *measured* duration is what the
   captions are constrained to.
3. **Build the captions** from the persisted transcript segments of the
   clip's job, on the clip's own timeline.
4. **Render** to a temporary path that belongs to this attempt alone, with
   the caption file in a directory the application controls.
5. **Verify** the temporary file with FFprobe.
6. **Finalise** by renaming the verified file into place, and only then
   commit the completed row, with an ownership check.

The captions come from the transcript that is already stored. The
transcription model is not called again, and no AI request is made.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.captions import (
    CaptionEvent,
    TranscriptSegment,
    build_caption_events,
    write_srt,
)
from app.clips import ClipStatus
from app.db import (
    get_clip,
    get_transcript,
    list_clip_caption_edits,
    list_transcript_segments,
    update_format,
)
from app.errors import ErrorCode
from app.interruption import WorkCancelled, WorkInterrupted
from app.format_render import (
    FormatRenderError,
    build_format_args,
    render_format,
    verify_format_output,
)
from app.formatting import FormatSettings, settings_from_dict
from app.formats import FormatRecord
from app.storage import Storage, StorageEscapeError

logger = logging.getLogger("clipforge.formats")

#: The name of the subtitle file inside the attempt's work directory.
#:
#: It is a plain name, chosen by the application, and the renderer runs
#: FFmpeg in that directory, so the path of the directory never reaches the
#: filter. FFmpeg's subtitle filter cannot open a path that contains a
#: single quote at all, and this arrangement does not care.
SUBTITLE_FILENAME = "captions.srt"


@dataclass(frozen=True, slots=True)
class FormatOutcome:
    """What happened to one formatted clip."""

    status: FormatStatus
    error_code: str | None = None
    error_message: str | None = None
    caption_count: int = 0


class FormatRunner:
    """Renders formatted clips for one worker."""

    def __init__(
        self,
        *,
        storage: Storage,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: int = 600,
        duration_tolerance_seconds: float = 0.5,
        encoder_preset: str = "veryfast",
    ) -> None:
        self.storage = storage
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds
        self.duration_tolerance_seconds = duration_tolerance_seconds
        self.encoder_preset = encoder_preset

    # -- the whole job -------------------------------------------------------

    def run(
        self,
        conn,
        artifact: FormatRecord,
        *,
        worker_id: str,
        on_tick=None,
    ) -> FormatOutcome:
        """Render and verify one formatted clip, and record what happened.

        ``on_tick``, when given, runs before the render, every quarter of a
        second during it, and once before verification. It is where the
        caller renews the artifact's lease and notices a cancellation:
        raising :class:`WorkCancelled` here stops the FFmpeg child within
        one tick and records the artifact as cancelled with its job.
        """
        try:
            source, settings = self._resolve(conn, artifact)
        except FormatRenderError as exc:
            return self._fail(conn, artifact, worker_id, exc.code, exc.message)

        try:
            probe = self._probe(source)
        except FormatRenderError as exc:
            return self._fail(conn, artifact, worker_id, exc.code, exc.message)

        segments = self._transcript_segments(conn, artifact)
        events = build_caption_events(
            segments,
            clip_start=self._clip_start(conn, artifact),
            clip_end=self._clip_end(conn, artifact),
            playable_duration_seconds=probe["duration_seconds"],
            settings=settings.captions,
        )

        update_format(
            conn,
            artifact.id,
            status=ClipStatus.RENDERING,
            stage="rendering",
            expected_worker_id=worker_id,
        )

        # The work directory belongs to this attempt alone: the subtitle file
        # and the rendered file both live in it, and it is named after the
        # attempt so two attempts never share one.
        work_dir = self.storage.new_format_work_dir(
            artifact.id, artifact.attempt_count
        )
        temporary = work_dir / "output.mp4"

        subtitle_name: str | None = None
        if settings.captions.enabled and events:
            subtitle = work_dir / SUBTITLE_FILENAME
            try:
                write_srt(subtitle, events)
            except ValueError:
                return self._fail(
                    conn,
                    artifact,
                    worker_id,
                    ErrorCode.FORMAT_OUTPUT_INVALID.value,
                    "The captions were too large to write.",
                )
            subtitle_name = SUBTITLE_FILENAME

        try:
            if on_tick is not None:
                on_tick()
            args = build_format_args(
                ffmpeg_path=self.ffmpeg_path,
                source=source,
                output=temporary,
                settings=settings,
                subtitle_filename=subtitle_name,
                encoder_preset=self.encoder_preset,
                has_audio=probe["has_audio"],
            )
            render_format(
                args,
                timeout_seconds=self.timeout_seconds,
                cwd=work_dir,
                on_tick=on_tick,
            )
            if on_tick is not None:
                on_tick()
        except WorkCancelled:
            self._clean(work_dir)
            return self._fail(
                conn,
                artifact,
                worker_id,
                ErrorCode.JOB_CANCELLED.value,
                "The job was cancelled while this formatted clip was being "
                "rendered.",
            )
        except WorkInterrupted:
            # The worker is shutting down, not the job. Leave the artifact
            # claimed: when its lease expires, recovery queues it again.
            self._clean(work_dir)
            raise
        except FormatRenderError as exc:
            self._clean(work_dir)
            return self._fail(conn, artifact, worker_id, exc.code, exc.message)
        except Exception:
            self._clean(work_dir)
            logger.exception("the formatted clip %s could not be rendered", artifact.id)
            return self._fail(
                conn,
                artifact,
                worker_id,
                ErrorCode.FORMAT_RENDER_FAILED.value,
                "The formatted clip could not be rendered.",
            )

        update_format(
            conn,
            artifact.id,
            status=ClipStatus.VERIFYING,
            stage="verifying",
            expected_worker_id=worker_id,
        )

        try:
            verification = verify_format_output(
                temporary,
                expected_width=settings.width,
                expected_height=settings.height,
                expected_duration_seconds=probe["duration_seconds"],
                expected_has_audio=probe["has_audio"],
                tolerance_seconds=self.duration_tolerance_seconds,
                output_dir=work_dir,
                ffprobe_path=self.ffprobe_path,
                timeout_seconds=min(self.timeout_seconds, 60),
            )
        except FormatRenderError as exc:
            self._clean(work_dir)
            return self._fail(conn, artifact, worker_id, exc.code, exc.message)

        # The file is verified, so it can be moved into place. The rename is
        # atomic on the same filesystem, which is why the work directory is
        # inside the storage root.
        final = self.storage.new_format_path(artifact.id)
        try:
            os.replace(temporary, final)
        except OSError:
            self._clean(work_dir)
            return self._fail(
                conn,
                artifact,
                worker_id,
                ErrorCode.FORMAT_OUTPUT_INVALID.value,
                "The formatted clip could not be stored.",
            )
        self._clean(work_dir)

        try:
            relative = self.storage.relative_to_root(final)
        except StorageEscapeError:
            final.unlink(missing_ok=True)
            return self._fail(
                conn,
                artifact,
                worker_id,
                ErrorCode.FORMAT_OUTPUT_INVALID.value,
                "The formatted clip could not be stored.",
            )

        updated = update_format(
            conn,
            artifact.id,
            status=ClipStatus.COMPLETED,
            stage="completed",
            progress=100,
            output_path=relative,
            output_size_bytes=verification.size_bytes,
            actual_duration_seconds=verification.duration_seconds,
            width=verification.width,
            height=verification.height,
            video_codec=verification.video_codec,
            audio_codec=verification.audio_codec,
            pixel_format=verification.pixel_format,
            captions_applied=subtitle_name is not None,
            caption_count=len(events) if subtitle_name is not None else 0,
            clear_error=True,
            expected_worker_id=worker_id,
        )
        if updated is None:
            # A newer attempt took the artifact over while this one was
            # rendering. The file belongs to this attempt, so it is removed
            # rather than left behind as an orphan.
            final.unlink(missing_ok=True)
            return FormatOutcome(status=ClipStatus.FAILED)

        logger.info(
            "formatted clip %s rendered: %sx%s, %.2fs, %s captions",
            artifact.id,
            verification.width,
            verification.height,
            verification.duration_seconds,
            len(events) if subtitle_name is not None else 0,
        )
        return FormatOutcome(
            status=ClipStatus.COMPLETED,
            caption_count=len(events) if subtitle_name is not None else 0,
        )

    # -- the pieces -----------------------------------------------------------

    def _resolve(
        self, conn, artifact: FormatRecord
    ) -> tuple[Path, FormatSettings]:
        """The clip to format, and the settings to format it with."""
        clip = get_clip(conn, artifact.clip_id)
        if clip is None or not clip.output_path:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_MISSING.value,
                "The clip this was made from is no longer available.",
            )
        try:
            source = self.storage.resolve(clip.output_path)
        except StorageEscapeError as exc:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_MISSING.value,
                "The clip this was made from is no longer available.",
            ) from exc
        if not source.is_file() or source.stat().st_size == 0:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_MISSING.value,
                "The clip this was made from is no longer available.",
            )
        try:
            payload = json.loads(artifact.settings_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise FormatRenderError(
                ErrorCode.FORMAT_INVALID_SETTINGS.value,
                "The settings of this request are not readable.",
            ) from exc
        if not isinstance(payload, dict):
            raise FormatRenderError(
                ErrorCode.FORMAT_INVALID_SETTINGS.value,
                "The settings of this request are not readable.",
            )
        try:
            settings = settings_from_dict(payload).validate()
        except Exception as exc:
            raise FormatRenderError(
                ErrorCode.FORMAT_INVALID_SETTINGS.value,
                "The settings of this request are not valid.",
            ) from exc
        return source, settings

    def _probe(self, source: Path) -> dict[str, object]:
        """Measure the clip that is being formatted."""
        import subprocess

        try:
            completed = subprocess.run(
                [
                    self.ffprobe_path,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(source),
                ],
                capture_output=True,
                timeout=min(self.timeout_seconds, 60),
                check=False,
            )
        except FileNotFoundError as exc:
            raise FormatRenderError(
                ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
                "FFprobe is not installed or not on PATH.",
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise FormatRenderError(
                ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
                "FFprobe could not be executed.",
            ) from exc

        if completed.returncode != 0:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_INVALID.value,
                "The clip could not be read as a media file.",
            )
        try:
            payload = json.loads(completed.stdout.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_INVALID.value,
                "The clip could not be read as a media file.",
            ) from exc

        streams = payload.get("streams") if isinstance(payload, dict) else None
        streams = streams if isinstance(streams, list) else []
        video = next(
            (
                s
                for s in streams
                if isinstance(s, dict) and s.get("codec_type") == "video"
            ),
            None,
        )
        audio = next(
            (
                s
                for s in streams
                if isinstance(s, dict) and s.get("codec_type") == "audio"
            ),
            None,
        )
        if video is None:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_INVALID.value,
                "The clip has no video stream.",
            )

        duration = None
        format_info = payload.get("format") if isinstance(payload, dict) else None
        if isinstance(format_info, dict):
            try:
                candidate = float(format_info.get("duration"))
                if candidate > 0:
                    duration = candidate
            except (TypeError, ValueError):
                duration = None
        if duration is None:
            try:
                candidate = float(video.get("duration"))
                if candidate > 0:
                    duration = candidate
            except (TypeError, ValueError):
                duration = None
        if duration is None or duration <= 0:
            raise FormatRenderError(
                ErrorCode.FORMAT_SOURCE_INVALID.value,
                "The clip has no usable duration.",
            )

        return {
            "duration_seconds": duration,
            "has_audio": audio is not None,
            "width": video.get("width"),
            "height": video.get("height"),
        }

    def _transcript_segments(
        self, conn, artifact: FormatRecord
    ) -> list[TranscriptSegment]:
        """The transcript of the clip's job, in order.

        The text the user has changed on the clip replaces the transcript's
        own, for the segments they changed and for no others. The timings
        are never touched: only the words are the user's to edit.
        """
        transcript = get_transcript(conn, artifact.job_id)
        if transcript is None:
            return []
        edits = dict(list_clip_caption_edits(conn, artifact.clip_id))
        rows = list_transcript_segments(conn, transcript.id)
        return [
            TranscriptSegment(
                index=row.segment_index,
                start_seconds=row.start_seconds,
                end_seconds=row.end_seconds,
                text=edits.get(row.segment_index, row.text),
            )
            for row in rows
        ]

    def _clip_start(self, conn, artifact: FormatRecord) -> float:
        """Where the clip sits on the source timeline."""
        clip = get_clip(conn, artifact.clip_id)
        return clip.start_seconds if clip is not None else 0.0

    def _clip_end(self, conn, artifact: FormatRecord) -> float:
        """Where the clip ends on the source timeline."""
        clip = get_clip(conn, artifact.clip_id)
        return clip.end_seconds if clip is not None else 0.0

    def _clean(self, work_dir: Path) -> None:
        """Remove an attempt's scratch directory."""
        shutil.rmtree(work_dir, ignore_errors=True)

    def _fail(
        self,
        conn,
        artifact: FormatRecord,
        worker_id: str,
        code: str,
        message: str,
    ) -> FormatOutcome:
        """Record a failure on the artifact."""
        logger.info("formatted clip %s failed with %s", artifact.id, code)
        update_format(
            conn,
            artifact.id,
            status=ClipStatus.FAILED,
            stage="failed",
            progress=0,
            error_code=code,
            error_message=message,
            expected_worker_id=worker_id,
        )
        return FormatOutcome(status=ClipStatus.FAILED, error_code=code, error_message=message)

    # -- recovery -------------------------------------------------------------

    def reconcile(self, *, older_than_seconds: float = 3600.0) -> int:
        """Remove the scratch directories of attempts that are long gone.

        The rule is deliberately narrow: only directories that match the
        formatting naming pattern, that are older than the age limit, and
        that live inside the scratch directory. Nothing else is ever
        touched, and a directory that a live attempt is using is too young
        to be removed.
        """
        removed = 0
        scratch = self.storage.temp_dir
        if not scratch.is_dir():
            return 0
        now = time.time()
        for entry in scratch.glob("format-*"):
            try:
                if not entry.is_dir():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            if now - stat.st_mtime < older_than_seconds:
                continue
            try:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info("removed %s abandoned formatting directories", removed)
        return removed
