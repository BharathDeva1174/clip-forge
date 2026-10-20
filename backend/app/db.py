"""SQLite persistence for jobs.

The schema is versioned. :func:`ensure_schema` applies any migration the
database has not seen yet, inside a transaction, and is safe to call from both
the API and the worker.

Writes that matter use ``BEGIN IMMEDIATE`` so that two workers cannot claim
the same job: the write lock is taken before the candidate row is read, so the
second worker sees the first worker's committed state.

No transaction is ever held open across file I/O, media probing or network
downloads.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from app.jobs import (
    PROGRESS_QUEUED,
    PROGRESS_VALIDATING,
    ACTIVE_STATUSES,
    JobRecord,
    JobStage,
    JobStatus,
    SourceType,
    assert_transition,
    to_iso,
    utcnow,
    utcnow_iso,
)

#: Highest schema version this build knows about.
SCHEMA_VERSION = 2

#: Ordered list of migrations. Each entry is a version number and the
#: statements that make up that version.
MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL
                    CHECK (source_type IN ('upload', 'youtube')),
                source_reference TEXT NOT NULL,
                source_path TEXT,
                status TEXT NOT NULL
                    CHECK (status IN ('queued', 'validating', 'downloading',
                                      'inspecting', 'completed', 'failed', 'cancelled')),
                progress INTEGER NOT NULL DEFAULT 0
                    CHECK (progress BETWEEN 0 AND 100),
                stage TEXT NOT NULL
                    CHECK (stage IN ('queued', 'validating', 'downloading',
                                     'inspecting', 'ready', 'failed', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                error_code TEXT,
                error_message TEXT,
                worker_id TEXT,
                lease_expires_at TEXT,
                original_filename TEXT,
                file_size_bytes INTEGER CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
                video_duration_seconds REAL,
                video_width INTEGER CHECK (video_width IS NULL OR video_width > 0),
                video_height INTEGER CHECK (video_height IS NULL OR video_height > 0),
                video_codec TEXT,
                video_format TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs (status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs (lease_expires_at)",
        ),
    ),
    (
        2,
        (
            # Helps the worker find the jobs it currently owns when it renews
            # a lease or reports on its own work.
            "CREATE INDEX IF NOT EXISTS idx_jobs_worker_id ON jobs (worker_id)",
        ),
    ),
]

_MIGRATION_LOCK = threading.Lock()


def connect(
    db_path: Path,
    *,
    busy_timeout_seconds: float = 30.0,
    wal_mode: bool = True,
) -> sqlite3.Connection:
    """Open a SQLite connection with the settings this application needs."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None turns off the driver's implicit transaction handling
    # so that transactions are explicit and their boundaries are obvious.
    conn = sqlite3.connect(
        db_path,
        timeout=busy_timeout_seconds,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_seconds * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    if wal_mode:
        # WAL keeps readers from blocking the writer. It must not be used on a
        # network filesystem, which is why it can be switched off.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(
    db_path: Path,
    *,
    busy_timeout_seconds: float = 30.0,
    wal_mode: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Open a connection, make sure the schema is current, and close it after."""
    conn = connect(
        db_path,
        busy_timeout_seconds=busy_timeout_seconds,
        wal_mode=wal_mode,
    )
    try:
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply pending migrations. The common path does not take a write lock."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        migrate(conn)
        return

    current = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if current is None or current < SCHEMA_VERSION:
        migrate(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every pending migration in a single transaction."""
    with _MIGRATION_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
            for version, statements in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utcnow_iso()),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        source_type=SourceType(row["source_type"]),
        source_reference=row["source_reference"],
        source_path=row["source_path"],
        status=JobStatus(row["status"]),
        progress=row["progress"],
        stage=JobStage(row["stage"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        attempt_count=row["attempt_count"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        worker_id=row["worker_id"],
        lease_expires_at=row["lease_expires_at"],
        original_filename=row["original_filename"],
        file_size_bytes=row["file_size_bytes"],
        video_duration_seconds=row["video_duration_seconds"],
        video_width=row["video_width"],
        video_height=row["video_height"],
        video_codec=row["video_codec"],
        video_format=row["video_format"],
    )


def create_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    source_type: SourceType,
    source_reference: str,
    source_path: str | None = None,
    original_filename: str | None = None,
    file_size_bytes: int | None = None,
) -> JobRecord:
    """Insert a new queued job and return it."""
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, source_type, source_reference, source_path, status, progress,
                stage, created_at, updated_at, attempt_count,
                original_filename, file_size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                job_id,
                source_type.value,
                source_reference,
                source_path,
                JobStatus.QUEUED.value,
                PROGRESS_QUEUED,
                JobStage.QUEUED.value,
                now,
                now,
                original_filename,
                file_size_bytes,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_job(row)


def get_job(conn: sqlite3.Connection, job_id: str) -> JobRecord | None:
    """Return one job, or ``None`` when it does not exist."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(
    conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[JobRecord]:
    """Return the most recent jobs, newest first."""
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_to_job(row) for row in rows]


def count_jobs(conn: sqlite3.Connection) -> int:
    """Return the total number of jobs."""
    return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int,
    now=None,
) -> JobRecord | None:
    """Atomically claim the oldest queued job.

    Returns the claimed job, or ``None`` when there is nothing to do. Two
    workers calling this concurrently can never receive the same job: the write
    lock is taken before the candidate row is read, and the update re-checks
    that the row is still queued.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)
    lease_iso = to_iso(moment + timedelta(seconds=lease_seconds))

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT id FROM jobs
             WHERE status = ?
             ORDER BY created_at ASC, id ASC
             LIMIT 1
            """,
            (JobStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        conn.execute(
            """
            UPDATE jobs
               SET status = ?,
                   stage = ?,
                   progress = MAX(progress, ?),
                   worker_id = ?,
                   lease_expires_at = ?,
                   started_at = COALESCE(started_at, ?),
                   updated_at = ?,
                   attempt_count = attempt_count + 1,
                   error_code = NULL,
                   error_message = NULL
             WHERE id = ? AND status = ?
            """,
            (
                JobStatus.VALIDATING.value,
                JobStage.VALIDATING.value,
                PROGRESS_VALIDATING,
                worker_id,
                lease_iso,
                now_iso,
                now_iso,
                row["id"],
                JobStatus.QUEUED.value,
            ),
        )
        claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_job(claimed)


def update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: JobStatus | None = None,
    stage: JobStage | None = None,
    progress: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    worker_id: str | None = None,
    lease_expires_at: str | None = None,
    file_size_bytes: int | None = None,
    video_duration_seconds: float | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
    video_codec: str | None = None,
    video_format: str | None = None,
    source_path: str | None = None,
    now=None,
) -> JobRecord | None:
    """Update a job, enforcing the allowed state transitions.

    ``progress`` never decreases: the stored value only ever moves forward.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        current = JobStatus(row["status"])
        if status is not None and status != current:
            assert_transition(current, status)

        new_progress = row["progress"]
        if progress is not None:
            new_progress = max(new_progress, min(100, max(0, progress)))

        new_status = status.value if status is not None else current.value
        new_stage = stage.value if stage is not None else row["stage"]
        completed_at = row["completed_at"]
        if new_status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
            completed_at = now_iso

        new_error_code = row["error_code"]
        new_error_message = row["error_message"]
        if clear_error:
            new_error_code = None
            new_error_message = None
        elif error_code is not None:
            new_error_code = error_code
            new_error_message = error_message

        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, progress = ?, updated_at = ?,
                   completed_at = ?, error_code = ?, error_message = ?,
                   worker_id = ?, lease_expires_at = ?,
                   file_size_bytes = COALESCE(?, file_size_bytes),
                   video_duration_seconds = COALESCE(?, video_duration_seconds),
                   video_width = COALESCE(?, video_width),
                   video_height = COALESCE(?, video_height),
                   video_codec = COALESCE(?, video_codec),
                   video_format = COALESCE(?, video_format),
                   source_path = COALESCE(?, source_path)
             WHERE id = ?
            """,
            (
                new_status,
                new_stage,
                new_progress,
                now_iso,
                completed_at,
                new_error_code,
                new_error_message,
                worker_id if worker_id is not None else row["worker_id"],
                lease_expires_at if lease_expires_at is not None else row["lease_expires_at"],
                file_size_bytes,
                video_duration_seconds,
                video_width,
                video_height,
                video_codec,
                video_format,
                source_path,
                job_id,
            ),
        )
        updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_job(updated)


def renew_lease(
    conn: sqlite3.Connection, job_id: str, *, lease_seconds: int, now=None
) -> None:
    """Extend the lease on a job the worker still owns."""
    moment = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (to_iso(moment + timedelta(seconds=lease_seconds)), to_iso(moment), job_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release_for_retry(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    error_code: str,
    error_message: str,
    now=None,
) -> JobRecord | None:
    """Put a job back on the queue after a retryable failure.

    This is an explicit recovery operation, not an ordinary transition: the
    job is allowed to leave an active status and return to ``queued``.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, updated_at = ?,
                   worker_id = NULL, lease_expires_at = NULL,
                   error_code = ?, error_message = ?,
                   completed_at = NULL
             WHERE id = ?
            """,
            (
                JobStatus.QUEUED.value,
                JobStage.QUEUED.value,
                now_iso,
                error_code,
                error_message,
                job_id,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_job(row) if row else None


def recover_expired_leases(
    conn: sqlite3.Connection,
    *,
    max_attempts: int,
    now=None,
) -> list[JobRecord]:
    """Requeue or fail jobs abandoned by a worker that died.

    A job whose lease expired is either put back on the queue (so another
    worker can pick it up) or failed permanently when it has already used up
    its attempts.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    recovered: list[JobRecord] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
             WHERE status IN ({placeholders})
               AND lease_expires_at IS NOT NULL
               AND lease_expires_at < ?
            """,
            (*(status.value for status in ACTIVE_STATUSES), now_iso),
        ).fetchall()

        for row in rows:
            job = _row_to_job(row)
            if job.attempt_count >= max_attempts:
                conn.execute(
                    """
                    UPDATE jobs
                       SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                           worker_id = NULL, lease_expires_at = NULL,
                           error_code = ?, error_message = ?
                     WHERE id = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        JobStage.FAILED.value,
                        now_iso,
                        now_iso,
                        "max_attempts_exceeded",
                        "The job was abandoned by a worker too many times and was not retried again.",
                        job.id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                       SET status = ?, stage = ?, updated_at = ?,
                           worker_id = NULL, lease_expires_at = NULL,
                           error_code = ?, error_message = ?, completed_at = NULL
                     WHERE id = ?
                    """,
                    (
                        JobStatus.QUEUED.value,
                        JobStage.QUEUED.value,
                        now_iso,
                        "lease_expired",
                        "A worker stopped while this job was running. It was queued again.",
                        job.id,
                    ),
                )
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
            recovered.append(_row_to_job(updated))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return recovered
