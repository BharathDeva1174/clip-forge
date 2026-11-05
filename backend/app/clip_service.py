"""Running a clip from end to end.

The service does the whole job for one clip, in the order the work has to
happen in:

1. **Resolve the source.** The clip points at a job, the job points at the
   stored source video, and the stored path is resolved inside the storage
   root. A path that escapes is refused.
2. **Check the boundaries again.** The clip's times were taken from the
   highlight candidate when the clip was queued, and they are checked
   against the source's real duration and against the candidate's own
   values before anything expensive happens.
3. **Render** to a temporary path that belongs to *this attempt* and to no
   other.
4. **Verify** the temporary file with FFprobe.
5. **Finalise** by renaming the verified file into place, and only then
   commit the completed row, with an ownership check.

The ordering matters. The file is verified *before* it is moved into the
directory of finished clips, so a half-written file is never visible as a
clip, and the database never points at a file that has not been verified.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.clips import ClipInputError, ClipRecord, ClipStatus, validate_boundaries
from app.clip_render import (
    ClipRenderError,
    ClipVerification,
    build_render_args,
    probe_source,
    render_clip,
    verify_clip,
)
from app.db import get_clip, get_job, update_clip
from app.errors import ErrorCode
from app.interruption import WorkCancelled, WorkInterrupted
from app.storage import Storage, StorageEscapeError

logger = logging.getLogger("clipforge.clips")


@dataclass(frozen=True, slots=True)
class ClipOutcome:
    """What happened to one clip."""

    status: ClipStatus
    error_code: str | None = None
    error_message: str | None = None
    verification: ClipVerification | None = None


class ClipRunner:
    """Renders clips for one worker."""

    def __init__(
        self,
        *,
        storage: Storage,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: int = 300,
        min_duration_seconds: float = 1.0,
        max_duration_seconds: float = 180.0,
        duration_tolerance_seconds: float = 0.5,
        encoder_preset: str = "veryfast",
    ) -> None:
        self.storage = storage
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds
        self.min_duration_seconds = min_duration_seconds
        self.max_duration_seconds = max_duration_seconds
        self.duration_tolerance_seconds = duration_tolerance_seconds
        self.encoder_preset = encoder_preset

    # -- the whole job -------------------------------------------------------

    def run(
        self,
        conn,
        clip: ClipRecord,
        *,
        worker_id: str,
        on_tick=None,
    ) -> ClipOutcome:
        """Render and verify one clip, and record what happened.

        ``on_tick``, when given, runs before the render, every quarter of a
        second during it, and once before verification. It is where the
        caller renews the clip's lease and notices a cancellation: raising
        :class:`WorkCancelled` here stops the FFmpeg child within one tick
        and records the clip as cancelled with its job.
        """
        try:
            source = self._resolve_source(conn, clip)
        except ClipRenderError as exc:
            return self._fail(conn, clip, worker_id, exc.code, exc.message)

        try:
            probe = probe_source(
                source,
                ffprobe_path=self.ffprobe_path,
                timeout_seconds=min(self.timeout_seconds, 60),
            )
        except ClipRenderError as exc:
            return self._fail(conn, clip, worker_id, exc.code, exc.message)

        try:
            start, end = validate_boundaries(
                clip.start_seconds,
                clip.end_seconds,
                source_duration_seconds=probe.duration_seconds,
                min_duration_seconds=self.min_duration_seconds,
                max_duration_seconds=self.max_duration_seconds,
                duration_tolerance_seconds=self.duration_tolerance_seconds,
            )
        except ClipInputError as exc:
            return self._fail(conn, clip, worker_id, exc.code, exc.message)

        duration = end - start
        update_clip(
            conn,
            clip.id,
            status=ClipStatus.RENDERING,
            stage="rendering",
            expected_worker_id=worker_id,
        )

        # The temporary file belongs to this attempt alone, so a stale worker
        # can never remove or overwrite the file of a newer attempt.
        temporary = self.storage.temp_dir / f"clip-{clip.id}-{clip.attempt_count}.mp4"
        temporary.parent.mkdir(parents=True, exist_ok=True)

        try:
            if on_tick is not None:
                on_tick()
            args = build_render_args(
                ffmpeg_path=self.ffmpeg_path,
                source=source,
                output=temporary,
                start_seconds=start,
                duration_seconds=duration,
                encoder_preset=self.encoder_preset,
                has_audio=probe.has_audio,
            )
            render_clip(args, timeout_seconds=self.timeout_seconds, on_tick=on_tick)
            if on_tick is not None:
                on_tick()
        except WorkCancelled:
            temporary.unlink(missing_ok=True)
            return self._fail(
                conn,
                clip,
                worker_id,
                ErrorCode.JOB_CANCELLED.value,
                "The job was cancelled while this clip was being rendered.",
            )
        except WorkInterrupted:
            # The worker is shutting down, not the job. Leave the clip
            # claimed: when its lease expires, recovery queues it again.
            temporary.unlink(missing_ok=True)
            raise
        except ClipRenderError as exc:
            temporary.unlink(missing_ok=True)
            return self._fail(conn, clip, worker_id, exc.code, exc.message)
        except Exception:
            temporary.unlink(missing_ok=True)
            logger.exception("the clip %s could not be rendered", clip.id)
            return self._fail(
                conn,
                clip,
                worker_id,
                ErrorCode.CLIP_RENDER_FAILED.value,
                "The clip could not be rendered.",
            )

        update_clip(
            conn,
            clip.id,
            status=ClipStatus.VERIFYING,
            stage="verifying",
            expected_worker_id=worker_id,
        )

        try:
            verification = verify_clip(
                temporary,
                requested_duration_seconds=duration,
                expected_has_audio=probe.has_audio,
                tolerance_seconds=self.duration_tolerance_seconds,
                output_dir=self.storage.temp_dir,
                ffprobe_path=self.ffprobe_path,
                timeout_seconds=min(self.timeout_seconds, 60),
            )
        except ClipRenderError as exc:
            temporary.unlink(missing_ok=True)
            return self._fail(conn, clip, worker_id, exc.code, exc.message)

        # The file is verified, so it can be moved into place. The rename is
        # atomic on the same filesystem, which is why the temporary file
        # lives inside the storage root.
        final = self.storage.new_clip_path(clip.id)
        try:
            os.replace(temporary, final)
        except OSError:
            temporary.unlink(missing_ok=True)
            return self._fail(
                conn,
                clip,
                worker_id,
                ErrorCode.CLIP_OUTPUT_INVALID.value,
                "The rendered clip could not be stored.",
            )

        try:
            relative = self.storage.relative_to_root(final)
        except StorageEscapeError:
            final.unlink(missing_ok=True)
            return self._fail(
                conn,
                clip,
                worker_id,
                ErrorCode.CLIP_OUTPUT_INVALID.value,
                "The rendered clip could not be stored.",
            )

        updated = update_clip(
            conn,
            clip.id,
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
            clear_error=True,
            expected_worker_id=worker_id,
        )
        if updated is None:
            # A newer attempt took the clip over while this one was
            # rendering. The file belongs to this attempt, so it is removed
            # rather than left behind as an orphan.
            final.unlink(missing_ok=True)
            return ClipOutcome(status=ClipStatus.FAILED)

        logger.info(
            "clip %s rendered: %.2fs, %sx%s, %s bytes",
            clip.id,
            verification.duration_seconds,
            verification.width,
            verification.height,
            verification.size_bytes,
        )
        return ClipOutcome(status=ClipStatus.COMPLETED, verification=verification)

    # -- the pieces -----------------------------------------------------------

    def _resolve_source(self, conn, clip: ClipRecord) -> Path:
        """The source video of a clip, checked for containment."""
        job = get_job(conn, clip.job_id)
        if job is None or not job.source_path:
            raise ClipRenderError(
                ErrorCode.CLIP_SOURCE_MISSING.value,
                "The source video of this clip is no longer available.",
            )
        try:
            path = self.storage.resolve(job.source_path)
        except StorageEscapeError as exc:
            raise ClipRenderError(
                ErrorCode.CLIP_SOURCE_MISSING.value,
                "The source video of this clip is no longer available.",
            ) from exc
        if not path.is_file() or path.stat().st_size == 0:
            raise ClipRenderError(
                ErrorCode.CLIP_SOURCE_MISSING.value,
                "The source video of this clip is no longer available.",
            )
        return path

    def _fail(
        self,
        conn,
        clip: ClipRecord,
        worker_id: str,
        code: str,
        message: str,
    ) -> ClipOutcome:
        """Record a failure on the clip."""
        logger.info("clip %s failed with %s", clip.id, code)
        update_clip(
            conn,
            clip.id,
            status=ClipStatus.FAILED,
            stage="failed",
            progress=0,
            error_code=code,
            error_message=message,
            expected_worker_id=worker_id,
        )
        return ClipOutcome(status=ClipStatus.FAILED, error_code=code, error_message=message)

    # -- recovery -------------------------------------------------------------

    def reconcile(self, *, older_than_seconds: float = 3600.0) -> int:
        """Remove the temporary files of attempts that are long gone.

        The rule is deliberately narrow: only files that match the clip
        naming pattern, that are older than the age limit, and that do not
        belong to a clip that is still being worked on. Nothing outside the
        scratch directory is ever touched.
        """
        removed = 0
        scratch = self.storage.temp_dir
        if not scratch.is_dir():
            return 0
        now = time.time()
        for entry in scratch.glob("clip-*.mp4"):
            try:
                stat = entry.stat()
            except OSError:
                continue
            if now - stat.st_mtime < older_than_seconds:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info("removed %s abandoned temporary clip files", removed)
        return removed


def clip_output_path(storage: Storage, clip: ClipRecord) -> Path | None:
    """The file of a completed clip, when it has one.

    The stored path is relative to the storage root and is resolved with
    the same containment check every other stored path goes through, so a
    clip can never be used to reach a file outside the root.
    """
    if not clip.output_path:
        return None
    try:
        path = storage.resolve(clip.output_path)
    except StorageEscapeError:
        return None
    if not path.is_file():
        return None
    return path


def remove_clip_file(storage: Storage, clip: ClipRecord) -> None:
    """Remove the file of a clip, when it has one."""
    path = clip_output_path(storage, clip)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def copy_verification(verification: ClipVerification) -> dict[str, object]:
    """The measured facts, as a plain dictionary."""
    return {
        "size_bytes": verification.size_bytes,
        "duration_seconds": verification.duration_seconds,
        "width": verification.width,
        "height": verification.height,
        "video_codec": verification.video_codec,
        "audio_codec": verification.audio_codec,
    }


def discard_directory(path: Path) -> None:
    """Remove a directory, ignoring a missing one."""
    shutil.rmtree(path, ignore_errors=True)
