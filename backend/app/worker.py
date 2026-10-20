"""The background worker.

The worker is a separate operating-system process. It shares the database and
the configuration with the API, and it is the only thing that performs the
expensive part of ingestion.

Guarantees:

* **At-least-once, not exactly-once.** A job may be processed twice if a
  worker dies after doing the work but before recording the result. Each step
  is written to be idempotent: an upload is never downloaded twice, and a
  source that is already stored is re-inspected rather than re-fetched.
* **A crash never leaves a job stuck.** A claimed job carries a lease. When
  the lease expires the job is either queued again or failed permanently, so
  it can never stay in ``validating``/``downloading``/``inspecting`` forever.
* **Claiming is atomic.** Two workers can never own the same job.
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
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from app.acquisition import AcquisitionError, SourceAcquirer, build_acquirer
from app.config import Settings
from app.db import (
    claim_next_job,
    get_job,
    recover_expired_leases,
    release_for_retry,
    renew_lease,
    session,
    update_job,
)
from app.errors import ErrorCode
from app.jobs import (
    PROGRESS_DOWNLOADING,
    PROGRESS_INSPECTING,
    PROGRESS_READY,
    JobRecord,
    JobStage,
    JobStatus,
)
from app.media import MediaError, inspect_media, validate_media
from app.storage import Storage, StorageEscapeError

logger = logging.getLogger("clipforge.worker")

#: Failures that are worth another attempt. Everything else is permanent:
#: a file that is not a video will not become a video on the next try.
RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.ACQUISITION_FAILED.value,
        ErrorCode.ACQUISITION_TIMEOUT.value,
        ErrorCode.MEDIA_TOOL_UNAVAILABLE.value,
    }
)


class _WorkerStopping(Exception):
    """Raised inside a job when the worker is asked to shut down."""


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
    ) -> None:
        self.settings = settings
        self.storage = Storage(settings.storage_root)
        self.worker_id = worker_id or default_worker_id()
        self.acquirer = acquirer or build_acquirer(settings.youtube_acquisition_mode)
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
        except _WorkerStopping:
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

        last_recovery = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_recovery > max(10.0, self.settings.worker_lease_seconds / 2):
                self.recover()
                last_recovery = now

            try:
                job = self.run_once()
            except Exception:
                logger.exception("the worker loop hit an unexpected error")
                job = None

            if job is None:
                self._stop.wait(self.settings.worker_poll_interval_seconds)

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
            )

        work_dir = self.storage.temp_dir / f"yt-{job.id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = self.acquirer.acquire(
                video,
                work_dir,
                max_bytes=self.settings.youtube_max_bytes,
                timeout_seconds=self.settings.worker_acquisition_timeout_seconds,
                on_tick=self._lease_tick(job),
            )
        except AcquisitionError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._fail(job, exc.code, exc.message)
            return
        except _WorkerStopping:
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
                status=JobStatus.COMPLETED,
                stage=JobStage.READY,
                progress=PROGRESS_READY,
                video_duration_seconds=info.duration_seconds,
                video_width=info.width,
                video_height=info.height,
                video_codec=info.codec,
                video_format=info.format_name,
                file_size_bytes=path.stat().st_size,
                clear_error=True,
            )
        logger.info(
            "job %s completed: %sx%s %s (%.1fs)",
            job.id,
            info.width,
            info.height,
            info.codec,
            info.duration_seconds,
        )

    # -- failure handling ----------------------------------------------------

    def _fail(self, job: JobRecord, code: str, message: str) -> None:
        """Record a failure, retrying it when the error is transient."""
        with self._connect() as conn:
            current = get_job(conn, job.id)
            if current is None:
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

    def _lease_tick(self, job: JobRecord):
        """Return a callback that renews the lease while a download runs."""

        def tick() -> None:
            if self._stop.is_set():
                terminate = getattr(self.acquirer, "terminate", None)
                if callable(terminate):
                    with suppress(Exception):
                        terminate()
                raise _WorkerStopping("the worker is shutting down")
            with self._connect() as conn:
                renew_lease(
                    conn,
                    job.id,
                    lease_seconds=self.settings.worker_lease_seconds,
                )

        return tick


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
    worker = Worker(settings)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        logger.info("worker interrupted, shutting down")
        worker.request_stop()


if __name__ == "__main__":
    main()
