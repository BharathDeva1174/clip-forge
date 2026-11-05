"""The background worker.

The worker is a separate operating-system process. It shares the database and
the configuration with the API, and it is the only thing that performs the
expensive part of the pipeline.

Since Phase 3 the pipeline is::

    queued -> validating -> downloading -> inspecting
           -> extracting_audio -> transcribing -> completed

Only the worker performs audio extraction and transcription. The API never
runs FFmpeg and never loads a speech model, so a request is never blocked by
the length of a recording.

Guarantees:

* **At-least-once, not exactly-once.** A job may be processed twice if a
  worker dies after doing the work but before recording the result. Each step
  is written to be idempotent: an upload is never downloaded twice, a source
  that is already stored is re-inspected rather than re-fetched, and a
  transcript is replaced rather than duplicated.
* **A crash never leaves a job stuck.** A claimed job carries a lease. When
  the lease expires the job is either queued again or failed permanently, so
  it can never stay in ``validating``/``downloading``/``inspecting``/
  ``extracting_audio``/``transcribing`` forever.
* **Claiming is atomic.** Two workers can never own the same job.
* **A job is only completed when its transcript is stored.** The transcript is
  committed before the job is marked ``completed``, so a client that sees a
  completed job can always read its transcript.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from app.acquisition import AcquisitionError, SourceAcquirer, build_acquirer
from app.audio import (
    AudioExtractionError,
    AudioExtractor,
    ExtractedAudio,
    FfmpegAudioExtractor,
)
from app.clips import ClipRecord, ClipStatus
from app.config import Settings
from app.db import (
    claim_next_analysis,
    claim_next_clip,
    claim_next_format,
    claim_next_job,
    get_job,
    get_transcript,
    list_transcript_segments,
    recover_expired_analysis_leases,
    recover_expired_clip_leases,
    recover_expired_format_leases,
    recover_expired_leases,
    release_for_retry,
    renew_clip_lease,
    renew_format_lease,
    renew_lease,
    save_transcript,
    session,
    update_analysis,
    update_clip,
    update_format,
    update_job,
)
from app.errors import ErrorCode
from app.formats import FormatRecord
from app.interruption import WorkCancelled, WorkInterrupted
from app.jobs import (
    PROGRESS_DOWNLOADING,
    PROGRESS_EXTRACTING_AUDIO,
    PROGRESS_INSPECTING,
    PROGRESS_READY,
    PROGRESS_TRANSCRIBING,
    PROGRESS_TRANSCRIPTION_MAX,
    JobRecord,
    JobStage,
    JobStatus,
    to_iso,
    utcnow,
)
from app.media import MediaError, inspect_media, validate_media
from app.storage import Storage, StorageEscapeError
from app.worker_heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    write_heartbeat,
)
from app.transcription import (
    TranscriptionEngine,
    TranscriptionError,
    TranscriptionSettings,
    build_engine,
    resolve_cache_dir,
)

logger = logging.getLogger("clipforge.worker")

#: Failures that are worth another attempt. Everything else is permanent:
#: a file that is not a video will not become a video on the next try.
RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.ACQUISITION_FAILED.value,
        ErrorCode.ACQUISITION_TIMEOUT.value,
        ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
        ErrorCode.AUDIO_EXTRACTION_TIMEOUT.value,
        ErrorCode.INSUFFICIENT_DISK_SPACE.value,
        ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
        ErrorCode.TRANSCRIPTION_TIMEOUT.value,
        ErrorCode.TRANSCRIPT_PERSISTENCE_FAILED.value,
    }
)


class _WorkerStopping(WorkInterrupted):
    """Raised inside a job when the worker is asked to shut down."""


class _JobCancelled(WorkInterrupted):
    """Raised inside a job when the user cancels it.

    This is not a failure and it is not a shutdown: the job was cancelled
    on purpose, so it is **not** put back on the queue. A cancelled job is
    not to be run.
    """


def default_worker_id() -> str:
    """A readable identifier for this worker process."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    """Claims and processes queued jobs."""

    def __init__(
        self,
        settings: Settings,
        *,
        worker_id: str | None = None,
        acquirer: SourceAcquirer | None = None,
        transcriber: TranscriptionEngine | None = None,
        audio_extractor: AudioExtractor | None = None,
        transcription_settings: TranscriptionSettings | None = None,
        ai_client=None,
        credential_store=None,
    ) -> None:
        self.settings = settings
        self.storage = Storage(settings.storage_root)
        self.worker_id = worker_id or default_worker_id()
        self.acquirer = acquirer or build_acquirer(settings.youtube_acquisition_mode)
        self.audio_extractor = audio_extractor or FfmpegAudioExtractor(
            ffmpeg_path=settings.ffmpeg_path,
            ffprobe_path=settings.ffprobe_path,
        )
        self._transcriber = transcriber
        self._transcription_settings = transcription_settings
        self._ai_client = ai_client
        self._credential_store = credential_store
        self._stop = threading.Event()

    # -- infrastructure ------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a database connection with the schema applied."""
        with session(
            self.settings.resolved_database_path,
            busy_timeout_seconds=self.settings.database_busy_timeout_seconds,
            wal_mode=self.settings.database_wal_mode,
        ) as conn:
            yield conn

    def request_stop(self) -> None:
        """Ask the worker to stop claiming new work."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- public API ----------------------------------------------------------

    def recover(self) -> list[JobRecord]:
        """Requeue or fail jobs whose worker died."""
        with self._connect() as conn:
            recovered = recover_expired_leases(
                conn, max_attempts=self.settings.worker_max_attempts
            )
            recover_expired_analysis_leases(
                conn, max_attempts=self.settings.worker_max_attempts
            )
            recover_expired_clip_leases(
                conn, max_attempts=self.settings.worker_max_attempts
            )
            recover_expired_format_leases(
                conn, max_attempts=self.settings.worker_max_attempts
            )
        for job in recovered:
            logger.info(
                "recovered job %s (attempt %s)", job.id, job.attempt_count
            )
        return recovered

    def run_once(self) -> JobRecord | None:
        """Claim and process at most one job.

        Returns the processed job, or ``None`` when there was nothing to do.
        """
        with self._connect() as conn:
            job = claim_next_job(
                conn,
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_seconds,
            )
        if job is None:
            return None

        logger.info("claimed job %s (%s)", job.id, job.source_type.value)
        try:
            self._process(job)
        except _JobCancelled:
            # The user cancelled it. It is not put back on the queue: a
            # cancelled job is not to be run. The status was already set by
            # the request that cancelled it.
            logger.info("job %s was cancelled", job.id)
        except WorkInterrupted:
            self._release(job, "The worker was stopped before the job finished.")
        except Exception as exc:  # never swallow without recording the outcome
            logger.exception("job %s failed unexpectedly", job.id)
            self._fail(job, ErrorCode.INTERNAL_ERROR.value, "The job could not be processed.")
        return job

    def run_forever(self) -> None:
        """Poll for jobs until the worker is asked to stop."""
        logger.info(
            "worker %s started (storage=%s, database=%s)",
            self.worker_id,
            self.settings.storage_root,
            self.settings.resolved_database_path.name,
        )
        self.storage.ensure_layout()
        self.recover()
        # Temporary files from attempts that are long gone are removed here
        # rather than accumulating in the scratch directory.
        try:
            self.reconcile_clips()
        except Exception:  # pragma: no cover - housekeeping must not stop the worker
            logger.exception("the temporary clip files could not be reconciled")
        try:
            self.reconcile_formats()
        except Exception:  # pragma: no cover - housekeeping must not stop the worker
            logger.exception(
                "the temporary format files could not be reconciled"
            )

        last_recovery = 0.0
        last_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_recovery > max(10.0, self.settings.worker_lease_seconds / 2):
                self.recover()
                last_recovery = now

            # Leave a note that the worker is alive, so the status view can
            # say whether it is running. It is written at most every few
            # seconds, and a failure to write it never stops the worker.
            if now - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS:
                write_heartbeat(self.settings.storage_root, self.worker_id)
                last_heartbeat = now

            try:
                job = self.run_once()
            except Exception:
                logger.exception("the worker loop hit an unexpected error")
                job = None

            if job is None:
                # There may still be a highlight analysis or a clip waiting.
                try:
                    worked = self.run_highlights_once()
                except Exception:
                    logger.exception("the highlight worker hit an unexpected error")
                    worked = False
                if not worked:
                    try:
                        worked = self.run_clips_once()
                    except Exception:
                        logger.exception("the clip worker hit an unexpected error")
                        worked = False
                if not worked:
                    try:
                        worked = self.run_formats_once()
                    except Exception:
                        logger.exception(
                            "the formatting worker hit an unexpected error"
                        )
                        worked = False
                if not worked:
                    self._stop.wait(self.settings.worker_poll_interval_seconds)

        # The inference process is a child of this one, so it has to be
        # stopped explicitly. A worker that is killed cannot do this, but the
        # child then sees its pipe close and exits by itself.
        self._terminate_children()
        logger.info("worker %s stopped", self.worker_id)

    # -- job processing ------------------------------------------------------

    def _process(self, job: JobRecord) -> None:
        if job.source_type.value == "youtube":
            self._process_youtube(job)
        else:
            self._process_upload(job)

    def _process_upload(self, job: JobRecord) -> None:
        """Inspect a source file that was uploaded through the API."""
        if not job.source_path:
            self._fail(
                job,
                ErrorCode.INVALID_MEDIA.value,
                "The job does not reference a stored file.",
            )
            return

        try:
            path = self.storage.resolve(job.source_path)
        except StorageEscapeError:
            self._fail(
                job,
                ErrorCode.INVALID_STATE.value,
                "The stored file could not be resolved safely.",
            )
            return

        if not path.is_file() or path.stat().st_size == 0:
            self._fail(
                job,
                ErrorCode.INVALID_MEDIA.value,
                "The stored file is missing or empty.",
            )
            return

        self._inspect(job, path)

    def _process_youtube(self, job: JobRecord) -> None:
        """Acquire an authorised YouTube video and inspect it."""
        if job.source_path:
            # Already acquired by an earlier attempt. Re-inspect instead of
            # downloading the same file again.
            try:
                path = self.storage.resolve(job.source_path)
            except StorageEscapeError:
                path = None
            if path is not None and path.is_file():
                self._inspect(job, path)
                return

        from app.youtube import YouTubeVideo

        video_id = _video_id_from_reference(job.source_reference)
        if video_id is None:
            self._fail(
                job,
                ErrorCode.INVALID_YOUTUBE_URL.value,
                "The job does not reference a valid YouTube video.",
            )
            return
        video = YouTubeVideo(
            video_id=video_id,
            canonical_url=job.source_reference,
        )

        with self._connect() as conn:
            update_job(
                conn,
                job.id,
                status=JobStatus.DOWNLOADING,
                stage=JobStage.DOWNLOADING,
                progress=PROGRESS_DOWNLOADING,
                expected_worker_id=self.worker_id,
            )

        work_dir = self.storage.temp_dir / f"yt-{job.id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            def _download_progress(pct: int) -> None:
                # Map the yt-dlp percentage (0-100) onto the downloading stage
                # range (30-70) so the client sees the download advance.
                progress = PROGRESS_DOWNLOADING + int(
                    (PROGRESS_INSPECTING - PROGRESS_DOWNLOADING) * pct / 100
                )
                with self._connect() as conn:
                    update_job(
                        conn,
                        job.id,
                        progress=progress,
                        expected_worker_id=self.worker_id,
                    )

            downloaded = self.acquirer.acquire(
                video,
                work_dir,
                max_bytes=self.settings.youtube_max_bytes,
                timeout_seconds=self.settings.worker_acquisition_timeout_seconds,
                on_tick=self._lease_tick(job),
                on_progress=_download_progress,
            )
        except AcquisitionError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._fail(job, exc.code, exc.message)
            return
        except WorkInterrupted:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.exception("could not acquire the youtube source for job %s", job.id)
            self._fail(
                job,
                ErrorCode.ACQUISITION_FAILED.value,
                "The video could not be downloaded.",
            )
            return

        self.storage.sources_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.storage.sources_dir / self.storage.new_source_name(
            Path(downloaded).suffix.lower() or ".mp4"
        )
        try:
            shutil.move(str(downloaded), str(final_path))
        except OSError:
            logger.exception("could not store the downloaded file for job %s", job.id)
            self._fail(
                job,
                ErrorCode.ACQUISITION_FAILED.value,
                "The downloaded file could not be stored.",
            )
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        with self._connect() as conn:
            update_job(
                conn,
                job.id,
                source_path=self.storage.relative_to_root(final_path),
                file_size_bytes=final_path.stat().st_size,
                expected_worker_id=self.worker_id,
            )

        self._inspect(job, final_path)

    def _inspect(self, job: JobRecord, path: Path) -> None:
        """Run the media inspection and record the outcome."""
        with self._connect() as conn:
            update_job(
                conn,
                job.id,
                status=JobStatus.INSPECTING,
                stage=JobStage.INSPECTING,
                progress=PROGRESS_INSPECTING,
                expected_worker_id=self.worker_id,
            )

        try:
            info = inspect_media(
                path,
                ffprobe_path=self.settings.ffprobe_path,
                timeout_seconds=self.settings.ffprobe_timeout_seconds,
            )
            validate_media(
                info,
                max_duration_seconds=self.settings.max_video_duration_seconds,
                max_dimension=self.settings.max_video_dimension,
                allowed_codecs=self.settings.allowed_codec_list,
            )
        except MediaError as exc:
            self._fail(job, exc.code, exc.message)
            return

        with self._connect() as conn:
            update_job(
                conn,
                job.id,
                video_duration_seconds=info.duration_seconds,
                video_width=info.width,
                video_height=info.height,
                video_codec=info.codec,
                video_format=info.format_name,
                file_size_bytes=path.stat().st_size,
                clear_error=True,
                expected_worker_id=self.worker_id,
            )

        logger.info(
            "job %s inspected: %sx%s %s (%.1fs)",
            job.id,
            info.width,
            info.height,
            info.codec,
            info.duration_seconds,
        )

        self._extract_and_transcribe(job, path)

    # -- audio extraction and transcription ----------------------------------

    def _extract_and_transcribe(self, job: JobRecord, source: Path) -> None:
        """Extract the audio, transcribe it and store the transcript.

        The three stages are separate on purpose: the job status says which
        one is running, and each one can fail on its own with its own error
        code.
        """
        if not self._owns_job(job):
            # Another worker took the job over. Doing the work would only
            # waste this worker's time, and every write would be refused.
            logger.info("job %s was taken over, so it was not processed", job.id)
            return

        work_dir = self.storage.temp_dir / f"audio-{job.id}-{uuid.uuid4().hex}"
        try:
            audio = self._extract_audio(job, source, work_dir)
            if not self._owns_job(job):
                logger.info("job %s was taken over, so it was not transcribed", job.id)
                return
            transcript = self._transcribe(job, audio)
        except _StageFailed:
            # The stage has already recorded its own failure, so there is
            # nothing left to do here.
            return
        finally:
            self._discard_audio(work_dir)

        self._store_transcript(job, transcript)

    def _owns_job(self, job: JobRecord) -> bool:
        """Whether this worker still owns the job."""
        with self._connect() as conn:
            current = get_job(conn, job.id)
        return current is not None and current.worker_id == self.worker_id

    # -- highlight analysis (Phase 4) ----------------------------------------

    def run_highlights_once(self) -> bool:
        """Claim and run one highlight analysis.

        Returns whether there was anything to do. A provider call takes
        seconds, so this runs in the worker and never inside a request
        handler.
        """
        with self._connect() as conn:
            analysis = claim_next_analysis(
                conn,
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_seconds,
            )
        if analysis is None:
            return False

        logger.info("claimed highlight analysis %s for job %s", analysis.id, analysis.job_id)
        try:
            self._run_highlights(analysis)
        except Exception:  # never swallow without recording the outcome
            logger.exception("highlight analysis %s failed unexpectedly", analysis.id)
            with self._connect() as conn:
                update_analysis(
                    conn,
                    analysis.id,
                    status="failed",
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    error_message="The analysis could not be completed.",
                    expected_worker_id=self.worker_id,
                )
        return True

    def _run_highlights(self, analysis) -> None:
        """Run one analysis against the stored transcript."""
        from app.ai_client import AiClient
        from app.credentials import DEFAULT_STORE
        from app.highlight_service import HighlightRunner, segment_references

        with self._connect() as conn:
            transcript = get_transcript(conn, analysis.job_id)
            segments = (
                list_transcript_segments(conn, transcript.id)
                if transcript is not None
                else []
            )
            job = get_job(conn, analysis.job_id)
            source_duration = transcript.duration_seconds if transcript else None
            source_name = job.source_reference if job is not None else None

            runner = HighlightRunner(
                client=self._ai_client
                or AiClient(
                    timeout_seconds=self.settings.ai_request_timeout_seconds,
                    max_retries=self.settings.ai_max_retries,
                ),
                credential_store=self._credential_store or DEFAULT_STORE,
            )
            runner.run(
                conn,
                analysis,
                transcript=transcript,
                segments=segment_references(segments, source_duration),
                source_duration_seconds=source_duration,
                source_name=source_name,
                on_tick=lambda: self._renew_analysis_lease(analysis.id),
                db_path=self.settings.resolved_database_path,
                lease_seconds=self.settings.worker_lease_seconds,
                renewal_interval_seconds=self.settings.worker_lease_renewal_interval_seconds,
                settings=self.settings,
            )

    def _renew_analysis_lease(self, analysis_id: str) -> None:
        """Keep the lease on a running analysis alive.

        A tick that fails transiently -- SQLite can return "database is
        locked" under contention -- must not kill a healthy analysis: the
        background renewer is the real owner-protection, and it detects a
        genuinely lost analysis. So a failed tick is logged and dropped.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE highlight_analyses SET lease_expires_at = ?, updated_at = ? "
                    "WHERE id = ? AND worker_id = ?",
                    (
                        to_iso(
                            utcnow()
                            + timedelta(seconds=self.settings.worker_lease_seconds)
                        ),
                        to_iso(utcnow()),
                        analysis_id,
                        self.worker_id,
                    ),
                )
        except sqlite3.Error:
            logger.warning(
                "could not renew the lease on analysis %s; the background "
                "renewer will keep trying",
                analysis_id,
                exc_info=True,
            )

    # -- clip rendering (Phase 5) --------------------------------------------

    def run_clips_once(self) -> bool:
        """Claim and render one clip.

        Returns whether there was anything to do. A render takes seconds or
        minutes, so it happens here and never inside a request handler.
        """
        with self._connect() as conn:
            clip = claim_next_clip(
                conn,
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_seconds,
            )
        if clip is None:
            return False

        logger.info("claimed clip %s for job %s", clip.id, clip.job_id)
        runner = self._clip_runner()
        try:
            with self._connect() as conn:
                runner.run(
                    conn,
                    clip,
                    worker_id=self.worker_id,
                    on_tick=self._clip_tick(clip),
                )
        except WorkInterrupted:
            # A cancellation was recorded by the service; a shutdown leaves
            # the clip claimed, so lease recovery can queue it again.
            logger.info("clip %s was interrupted", clip.id)
        except Exception:  # never swallow without recording the outcome
            logger.exception("clip %s failed unexpectedly", clip.id)
            with self._connect() as conn:
                update_clip(
                    conn,
                    clip.id,
                    status=ClipStatus.FAILED,
                    stage="failed",
                    progress=0,
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    error_message="The clip could not be rendered.",
                    expected_worker_id=self.worker_id,
                )
        return True

    def _clip_runner(self):
        """The renderer, built from the current settings."""
        from app.clip_service import ClipRunner

        return ClipRunner(
            storage=self.storage,
            ffmpeg_path=self.settings.ffmpeg_path,
            ffprobe_path=self.settings.ffprobe_path,
            timeout_seconds=self.settings.clip_ffmpeg_timeout_seconds,
            min_duration_seconds=self.settings.clip_min_duration_seconds,
            max_duration_seconds=self.settings.clip_max_duration_seconds,
            duration_tolerance_seconds=self.settings.clip_duration_tolerance_seconds,
            encoder_preset=self.settings.clip_encoder_preset,
        )

    def _clip_tick(self, clip: ClipRecord) -> Callable[[], None]:
        """Renew a claimed clip's lease and notice a cancellation.

        Runs before, during (every quarter of a second) and after the render.
        A cancelled parent job raises :class:`WorkCancelled`, which stops the
        FFmpeg child within one tick. The lease renewal is what lets a render
        outlive ``worker_lease_seconds`` without being stolen by recovery.
        """

        def tick() -> None:
            if self._stop.is_set():
                raise _WorkerStopping("the worker is shutting down")
            with self._connect() as conn:
                current = get_job(conn, clip.job_id)
                if current is not None and current.status is JobStatus.CANCELLED:
                    raise WorkCancelled("the job was cancelled")
                renew_clip_lease(
                    conn,
                    clip.id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    worker_id=self.worker_id,
                )

        return tick

    def reconcile_clips(self) -> int:
        """Remove the temporary files of attempts that are long gone."""
        return self._clip_runner().reconcile()

    # -- formatted clips (Phase 6) -------------------------------------------

    def run_formats_once(self) -> bool:
        """Claim and render one formatted clip.

        Returns whether there was anything to do. A render takes seconds or
        minutes, so it happens here and never inside a request handler.
        """
        with self._connect() as conn:
            artifact = claim_next_format(
                conn,
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_seconds,
            )
        if artifact is None:
            return False

        logger.info(
            "claimed formatted clip %s for clip %s", artifact.id, artifact.clip_id
        )
        runner = self._format_runner()
        try:
            with self._connect() as conn:
                runner.run(
                    conn,
                    artifact,
                    worker_id=self.worker_id,
                    on_tick=self._format_tick(artifact),
                )
        except WorkInterrupted:
            # A cancellation was recorded by the service; a shutdown leaves
            # the artifact claimed, so lease recovery can queue it again.
            logger.info("formatted clip %s was interrupted", artifact.id)
        except Exception:  # never swallow without recording the outcome
            logger.exception(
                "formatted clip %s failed unexpectedly", artifact.id
            )
            with self._connect() as conn:
                update_format(
                    conn,
                    artifact.id,
                    status=ClipStatus.FAILED,
                    stage="failed",
                    progress=0,
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    error_message="The formatted clip could not be rendered.",
                    expected_worker_id=self.worker_id,
                )
        return True

    def _format_runner(self):
        """The formatter, built from the current settings."""
        from app.format_service import FormatRunner

        return FormatRunner(
            storage=self.storage,
            ffmpeg_path=self.settings.ffmpeg_path,
            ffprobe_path=self.settings.ffprobe_path,
            timeout_seconds=self.settings.format_ffmpeg_timeout_seconds,
            duration_tolerance_seconds=(
                self.settings.format_duration_tolerance_seconds
            ),
            encoder_preset=self.settings.format_encoder_preset,
        )

    def _format_tick(self, artifact: FormatRecord) -> Callable[[], None]:
        """Renew a claimed formatted clip's lease and notice a cancellation.

        Same contract as :meth:`_clip_tick`, for the portrait render of a
        clip: the lease is renewed on every tick so a slow render cannot be
        stolen by recovery, and a cancelled parent job stops the FFmpeg
        child within one tick.
        """

        def tick() -> None:
            if self._stop.is_set():
                raise _WorkerStopping("the worker is shutting down")
            with self._connect() as conn:
                current = get_job(conn, artifact.job_id)
                if current is not None and current.status is JobStatus.CANCELLED:
                    raise WorkCancelled("the job was cancelled")
                renew_format_lease(
                    conn,
                    artifact.id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    worker_id=self.worker_id,
                )

        return tick

    def reconcile_formats(self) -> int:
        """Remove the scratch directories of attempts that are long gone."""
        return self._format_runner().reconcile()

    def _extract_audio(
        self, job: JobRecord, source: Path, work_dir: Path
    ) -> ExtractedAudio:
        """Pull the audio track out of the source."""
        with self._connect() as conn:
            update_job(
                conn,
                job.id,
                status=JobStatus.EXTRACTING_AUDIO,
                stage=JobStage.EXTRACTING_AUDIO,
                progress=PROGRESS_EXTRACTING_AUDIO,
                expected_worker_id=self.worker_id,
            )

        try:
            return self.audio_extractor.extract(
                source,
                work_dir,
                timeout_seconds=self.settings.ffmpeg_timeout_seconds,
                sample_rate=self.settings.audio_sample_rate,
                channels=self.settings.audio_channels,
                max_duration_seconds=self.settings.audio_max_duration_seconds,
                max_output_bytes=self.settings.audio_max_bytes,
                min_free_disk_bytes=self.settings.audio_min_free_disk_bytes,
                source_root=self.settings.storage_root,
                on_tick=self._lease_tick(job),
            )
        except AudioExtractionError as exc:
            self._fail(job, exc.code, exc.message)
            raise _StageFailed from exc
        except WorkInterrupted:
            raise
        except Exception:
            logger.exception("could not extract the audio for job %s", job.id)
            self._fail(
                job,
                ErrorCode.AUDIO_EXTRACTION_FAILED.value,
                "The audio could not be extracted from the source.",
            )
            raise _StageFailed from None

    def _transcribe(self, job: JobRecord, audio: ExtractedAudio):
        """Transcribe the extracted audio."""
        with self._connect() as conn:
            update_job(
                conn,
                job.id,
                status=JobStatus.TRANSCRIBING,
                stage=JobStage.TRANSCRIBING,
                progress=PROGRESS_TRANSCRIBING,
                expected_worker_id=self.worker_id,
            )

        try:
            transcript = self._engine().transcribe(
                audio.path,
                on_progress=self._transcription_reporter(job),
                # The duration of the extracted file, measured by FFprobe.
                # It is the timeline the segments are normalised against, so
                # a segment can never be placed outside the audio that was
                # really transcribed.
                duration_seconds=audio.duration_seconds,
            )
        except TranscriptionError as exc:
            self._fail(job, exc.code, exc.message)
            raise _StageFailed from exc
        except WorkInterrupted:
            raise
        except Exception:
            logger.exception("could not transcribe the audio for job %s", job.id)
            self._fail(
                job,
                ErrorCode.TRANSCRIPTION_FAILED.value,
                "The audio could not be transcribed.",
            )
            raise _StageFailed from None
        return transcript

    def _store_transcript(self, job: JobRecord, transcript) -> None:
        """Commit the transcript, then mark the job as completed.

        The order matters: the job is only ``completed`` once the transcript
        is in the database, so a client never sees a finished job without a
        transcript behind it.

        Both writes are conditional on this worker still owning the job. A
        worker whose lease expired while it was transcribing must not
        replace the result of the attempt that took the job over.
        """
        try:
            with self._connect() as conn:
                stored = save_transcript(
                    conn,
                    job_id=job.id,
                    transcript=transcript,
                    expected_worker_id=self.worker_id,
                )
        except Exception:
            logger.exception("could not store the transcript for job %s", job.id)
            self._fail(
                job,
                ErrorCode.TRANSCRIPT_PERSISTENCE_FAILED.value,
                "The transcript could not be saved.",
            )
            return

        if stored is None:
            # Another worker owns the job now. Its result stands.
            logger.info(
                "job %s was taken over while it was being transcribed, so its "
                "transcript was discarded",
                job.id,
            )
            return

        with self._connect() as conn:
            updated = update_job(
                conn,
                job.id,
                status=JobStatus.COMPLETED,
                stage=JobStage.READY,
                progress=PROGRESS_READY,
                clear_error=True,
                expected_worker_id=self.worker_id,
            )
        if updated is None:
            logger.info(
                "job %s was taken over before it could be completed", job.id
            )
            return
        logger.info(
            "job %s completed: %s transcript segments",
            job.id,
            len(transcript.segments),
        )

    def _discard_audio(self, work_dir: Path) -> None:
        """Apply the retention policy to the extracted audio.

        By default the audio is removed as soon as it is no longer needed: it
        is a large temporary artefact and the transcript is the durable
        result. Keeping it is an explicit configuration choice.
        """
        if self.settings.audio_temp_retention == "keep":
            return
        shutil.rmtree(work_dir, ignore_errors=True)

    def _engine(self) -> TranscriptionEngine:
        """The speech engine, built on first use and then reused."""
        if self._transcriber is None:
            settings = self._transcription_settings or self._transcription_config()
            self._transcriber = build_engine(
                settings,
                cache_dir=resolve_cache_dir(settings, self.settings.storage_root),
                sample_rate=self.settings.audio_sample_rate,
                channels=self.settings.audio_channels,
            )
        return self._transcriber

    def _transcription_config(self) -> TranscriptionSettings:
        """The transcription settings, with a readable error when they are wrong.

        A bad model name is a configuration mistake, not a transient
        failure, so it is reported as one instead of as an internal error.
        """
        try:
            return TranscriptionSettings()
        except ValidationError as exc:
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
                "The transcription model configuration is not valid. Check "
                f"CLIPFORGE_TRANSCRIPTION_MODEL: {exc.error_count()} problem(s) "
                "in the transcription settings.",
            ) from exc

    # -- failure handling ----------------------------------------------------

    def _fail(self, job: JobRecord, code: str, message: str) -> None:
        """Record a failure, retrying it when the error is transient.

        The write is conditional on this worker still owning the job, so a
        worker that was taken over cannot fail the attempt that replaced it.
        """
        with self._connect() as conn:
            current = get_job(conn, job.id)
            if current is None:
                return
            if current.worker_id not in (None, self.worker_id):
                # Somebody else owns the job now.
                return
            if (
                code in RETRYABLE_ERROR_CODES
                and current.attempt_count < self.settings.worker_max_attempts
            ):
                release_for_retry(
                    conn, job.id, error_code=code, error_message=message
                )
                logger.info(
                    "job %s failed with %s and was queued for another attempt",
                    job.id,
                    code,
                )
                return
            if code in RETRYABLE_ERROR_CODES:
                # The underlying cause is kept, because it is the most useful
                # part of the message, and the retries are made explicit.
                message = f"{message} (gave up after {current.attempt_count} attempts)"
            update_job(
                conn,
                job.id,
                status=JobStatus.FAILED,
                stage=JobStage.FAILED,
                error_code=code,
                error_message=message,
                expected_worker_id=self.worker_id,
            )
        logger.info("job %s failed permanently with %s", job.id, code)

    def _release(self, job: JobRecord, message: str) -> None:
        """Put a job back on the queue after an interrupted shutdown."""
        with self._connect() as conn:
            release_for_retry(
                conn,
                job.id,
                error_code=ErrorCode.LEASE_EXPIRED.value,
                error_message=message,
            )
        logger.info("job %s was released back to the queue", job.id)

    def _lease_tick(self, job: JobRecord) -> Callable[[], None]:
        """Return a callback that renews the lease while a step runs.

        It also watches for a cancellation. A long step — a download, a
        transcription — calls this as it goes, so this is where a cancel
        request takes effect: the step is interrupted, its temporary file is
        removed, and the job is not put back on the queue.
        """

        def tick() -> None:
            if self._stop.is_set():
                self._terminate_children()
                raise _WorkerStopping("the worker is shutting down")
            with self._connect() as conn:
                current = get_job(conn, job.id)
                if current is not None and current.status is JobStatus.CANCELLED:
                    raise _JobCancelled("the job was cancelled")
                renew_lease(
                    conn,
                    job.id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    worker_id=self.worker_id,
                )

        return tick

    def _transcription_reporter(self, job: JobRecord) -> Callable[[float], None]:
        """Return a callback that reports transcription progress.

        The reported progress is an estimate derived from how far the decoder
        has read into the audio, so it is kept inside a narrow band below
        100 and the API labels it as approximate.
        """

        def report(fraction: float) -> None:
            bounded = max(0.0, min(1.0, float(fraction)))
            progress = PROGRESS_TRANSCRIBING + int(
                (PROGRESS_TRANSCRIPTION_MAX - PROGRESS_TRANSCRIBING) * bounded
            )
            # Renewing the lease here keeps a long recording from being
            # recovered by another worker while it is still being transcribed.
            self._lease_tick(job)()
            with self._connect() as conn:
                update_job(conn, job.id, progress=progress, expected_worker_id=self.worker_id)

        return report

    def _terminate_children(self) -> None:
        """Stop any child process this worker started."""
        for child in (self.acquirer, self.audio_extractor, self._transcriber):
            if child is None:
                continue
            terminate = getattr(child, "terminate", None)
            if callable(terminate):
                with suppress(Exception):
                    terminate()


class _StageFailed(Exception):
    """Raised when a pipeline stage has already recorded its own failure."""


def _video_id_from_reference(reference: str) -> str | None:
    """Extract the video id from a canonical YouTube URL."""
    marker = "watch?v="
    if not reference.startswith("https://www.youtube.com/") or marker not in reference:
        return None
    video_id = reference.split(marker, 1)[1]
    return video_id or None


def main() -> None:
    """Entry point for ``python worker.py``."""
    from app.config import get_settings
    from app.logging_config import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    _report_transcription_configuration()
    worker = Worker(settings)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        logger.info("worker interrupted, shutting down")
        worker.request_stop()


def _report_transcription_configuration() -> None:
    """Say what the worker is configured to do, and complain early.

    A model name that cannot work is a configuration mistake, so it is
    reported here rather than once per job.
    """
    try:
        transcription = TranscriptionSettings()
    except ValidationError as exc:
        logger.error(
            "the transcription configuration is not valid: %s. Jobs that need "
            "a transcript will fail with %s.",
            exc,
            ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
        )
        return
    logger.info(
        "transcription: model=%s device=%s isolation=%s timeout=%ss offline=%s",
        transcription.model,
        transcription.device,
        transcription.isolation,
        transcription.timeout_seconds,
        transcription.offline,
    )


if __name__ == "__main__":
    main()
