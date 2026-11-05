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
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from app.clips import (
    CLIP_STAGE_PROGRESS,
    ClipRecord,
    ClipStage,
    ClipStatus,
)
from app.clips import assert_transition as assert_clip_transition
from app.formats import FormatRecord
from app.highlights import (
    HighlightAnalysis,
    HighlightCandidate,
    HighlightCandidateRecord,
)
from app.integrations import (
    ApiProtocol,
    ConnectionStatus,
    IntegrationRecord,
    IntegrationRole,
    ModelCapabilities,
    PricingStatus,
    StructuredOutputMode,
)
from app.jobs import (
    PROGRESS_QUEUED,
    PROGRESS_VALIDATING,
    ACTIVE_STATUSES,
    JobArtifacts,
    JobRecord,
    JobStage,
    JobStatus,
    SourceType,
    assert_transition,
    to_iso,
    utcnow,
    utcnow_iso,
)
from app.transcripts import (
    Transcript,
    TranscriptRecord,
    TranscriptSegmentRecord,
    assert_storable,
)

#: Highest schema version this build knows about.
SCHEMA_VERSION = 7

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
    (
        3,
        (
            # Phase 3 adds two pipeline states and two transcript tables.
            #
            # SQLite cannot change a CHECK constraint in place, so the `jobs`
            # table is rebuilt: a new table is created with the wider
            # constraints, every existing row is copied into it, the old table
            # is dropped and the new one takes its place. The whole migration
            # runs inside one transaction, so either every job survives or the
            # database is left exactly as it was.
            """
            CREATE TABLE jobs_phase3 (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL
                    CHECK (source_type IN ('upload', 'youtube')),
                source_reference TEXT NOT NULL,
                source_path TEXT,
                status TEXT NOT NULL
                    CHECK (status IN ('queued', 'validating', 'downloading',
                                      'inspecting', 'extracting_audio',
                                      'transcribing', 'completed', 'failed',
                                      'cancelled')),
                progress INTEGER NOT NULL DEFAULT 0
                    CHECK (progress BETWEEN 0 AND 100),
                stage TEXT NOT NULL
                    CHECK (stage IN ('queued', 'validating', 'downloading',
                                     'inspecting', 'extracting_audio',
                                     'transcribing', 'ready', 'failed',
                                     'cancelled')),
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
            """
            INSERT INTO jobs_phase3 (
                id, source_type, source_reference, source_path, status, progress,
                stage, created_at, updated_at, started_at, completed_at,
                attempt_count, error_code, error_message, worker_id,
                lease_expires_at, original_filename, file_size_bytes,
                video_duration_seconds, video_width, video_height, video_codec,
                video_format
            )
            SELECT
                id, source_type, source_reference, source_path, status, progress,
                stage, created_at, updated_at, started_at, completed_at,
                attempt_count, error_code, error_message, worker_id,
                lease_expires_at, original_filename, file_size_bytes,
                video_duration_seconds, video_width, video_height, video_codec,
                video_format
            FROM jobs
            """,
            "DROP TABLE jobs",
            "ALTER TABLE jobs_phase3 RENAME TO jobs",
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs (status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs (lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_worker_id ON jobs (worker_id)",
            # One transcript per job. The unique job_id is what makes a retry
            # replace a transcript instead of adding a second one.
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE
                    REFERENCES jobs (id) ON DELETE CASCADE,
                language TEXT,
                model_name TEXT NOT NULL,
                audio_sample_rate INTEGER NOT NULL
                    CHECK (audio_sample_rate > 0),
                audio_channels INTEGER NOT NULL
                    CHECK (audio_channels > 0),
                duration_seconds REAL,
                segment_count INTEGER NOT NULL DEFAULT 0
                    CHECK (segment_count >= 0),
                created_at TEXT NOT NULL
            )
            """,
            # The segment index is unique inside a transcript, so the ordering
            # of a transcript can never be ambiguous.
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL
                    REFERENCES transcripts (id) ON DELETE CASCADE,
                segment_index INTEGER NOT NULL
                    CHECK (segment_index >= 0),
                start_seconds REAL NOT NULL
                    CHECK (start_seconds >= 0),
                end_seconds REAL NOT NULL
                    CHECK (end_seconds >= 0),
                text TEXT NOT NULL,
                average_log_probability REAL,
                no_speech_probability REAL,
                UNIQUE (transcript_id, segment_index)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_transcript_segments_order ON transcript_segments (transcript_id, segment_index)",
            "CREATE INDEX IF NOT EXISTS idx_transcripts_job_id ON transcripts (job_id)",
        ),
    ),
    (
        4,
        (
            # Phase 4 adds the integration configuration and the highlight
            # analysis.
            #
            # An integration holds everything about a provider *except* the
            # key. The key lives in the operating system's credential store
            # and only a reference is kept here, so the database never holds
            # a secret and a backup of it never contains one.
            """
            CREATE TABLE IF NOT EXISTS integrations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                protocol TEXT NOT NULL
                    CHECK (protocol IN ('openai_chat_completions',
                                         'openai_responses',
                                         'anthropic_messages',
                                         'google_generate_content',
                                         'system_one')),
                base_url TEXT NOT NULL,
                model_id TEXT,
                model_name TEXT,
                context_window INTEGER
                    CHECK (context_window IS NULL OR context_window > 0),
                max_output_tokens INTEGER
                    CHECK (max_output_tokens IS NULL OR max_output_tokens > 0),
                reasoning_effort TEXT,
                reasoning_effort_parameter TEXT,
                reasoning_effort_values TEXT,
                structured_output TEXT NOT NULL DEFAULT 'none'
                    CHECK (structured_output IN ('none', 'json_object', 'json_schema')),
                model_available INTEGER
                    CHECK (model_available IS NULL OR model_available IN (0, 1)),
                metadata_source TEXT,
                metadata_verified_at TEXT,
                pricing_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (pricing_status IN ('unknown', 'free', 'paid', 'mixed')),
                local_endpoint INTEGER NOT NULL DEFAULT 0
                    CHECK (local_endpoint IN (0, 1)),
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (enabled IN (0, 1)),
                secret_ref TEXT,
                role TEXT NOT NULL DEFAULT 'highlight_detection'
                    CHECK (role IN ('highlight_detection')),
                is_default INTEGER NOT NULL DEFAULT 0
                    CHECK (is_default IN (0, 1)),
                last_tested_at TEXT,
                last_test_status TEXT,
                last_test_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            # At most one *enabled* default per role. A disabled default is
            # allowed to exist, because disabling one must not have to
            # promote another one behind the user's back.
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_integrations_default
                ON integrations (role)
                WHERE is_default = 1 AND enabled = 1
            """,
            "CREATE INDEX IF NOT EXISTS idx_integrations_role ON integrations (role, enabled)",
            # An analysis records the configuration it was queued against, so
            # a later edit or deletion cannot silently change what runs.
            """
            CREATE TABLE IF NOT EXISTS highlight_analyses (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
                integration_id TEXT REFERENCES integrations (id) ON DELETE SET NULL,
                provider_id TEXT NOT NULL,
                protocol TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_name TEXT,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                transcript_id TEXT,
                source_duration_seconds REAL,
                status TEXT NOT NULL
                    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
                error_code TEXT,
                error_message TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                worker_id TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """,
            # One *active* analysis per job. A finished one is history, and a
            # new analysis may be started after a configuration change.
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_highlight_analyses_active
                ON highlight_analyses (job_id)
                WHERE status IN ('pending', 'running')
            """,
            "CREATE INDEX IF NOT EXISTS idx_highlight_analyses_status ON highlight_analyses (status, created_at)",
            """
            CREATE TABLE IF NOT EXISTS highlight_candidates (
                id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL
                    REFERENCES highlight_analyses (id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position >= 0),
                start_segment_index INTEGER NOT NULL
                    CHECK (start_segment_index >= 0),
                end_segment_index INTEGER NOT NULL
                    CHECK (end_segment_index >= 0),
                start_seconds REAL NOT NULL CHECK (start_seconds >= 0),
                end_seconds REAL NOT NULL CHECK (end_seconds >= 0),
                title TEXT NOT NULL,
                reason TEXT NOT NULL,
                moment_type TEXT,
                score REAL
                    CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
                decision TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (decision IN ('proposed', 'accepted', 'rejected')),
                created_at TEXT NOT NULL,
                UNIQUE (analysis_id, position)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_highlight_candidates_order ON highlight_candidates (analysis_id, position)",
        ),
    ),
    (
        5,
        (
            # Phase 5 adds the rendered clips.
            #
            # A clip is cut from a highlight candidate, so it points at the
            # job (the source video), the analysis and the candidate. The
            # times are the candidate's own validated ones: the boundaries
            # are never re-invented here.
            #
            # The output path is relative to the storage root, like every
            # other path the application stores.
            """
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
                analysis_id TEXT
                    REFERENCES highlight_analyses (id) ON DELETE SET NULL,
                candidate_id TEXT
                    REFERENCES highlight_candidates (id) ON DELETE SET NULL,
                start_seconds REAL NOT NULL CHECK (start_seconds >= 0),
                end_seconds REAL NOT NULL CHECK (end_seconds > start_seconds),
                requested_duration_seconds REAL NOT NULL
                    CHECK (requested_duration_seconds > 0),
                status TEXT NOT NULL
                    CHECK (status IN ('queued', 'preparing', 'rendering',
                                      'verifying', 'completed', 'failed')),
                stage TEXT NOT NULL
                    CHECK (stage IN ('queued', 'preparing', 'rendering',
                                     'verifying', 'completed', 'failed')),
                progress INTEGER NOT NULL DEFAULT 0
                    CHECK (progress BETWEEN 0 AND 100),
                output_path TEXT,
                output_size_bytes INTEGER
                    CHECK (output_size_bytes IS NULL OR output_size_bytes >= 0),
                actual_duration_seconds REAL,
                width INTEGER CHECK (width IS NULL OR width > 0),
                height INTEGER CHECK (height IS NULL OR height > 0),
                video_codec TEXT,
                audio_codec TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                worker_id TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_code TEXT,
                error_message TEXT
            )
            """,
            # One clip per candidate. A repeated request for the same
            # candidate finds the clip that already exists instead of
            # rendering the same moment twice.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_candidate ON clips (candidate_id) WHERE candidate_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_clips_job ON clips (job_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_clips_status ON clips (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_clips_lease ON clips (lease_expires_at)",
        ),
    ),
    (
        6,
        (
            # Phase 6 adds the formatted clips: the portrait version of a
            # Phase 5 clip.
            #
            # The artifact points at the clip it came from and at the job, so
            # the interface can group them and the ownership is clear. The
            # settings are stored as an immutable snapshot, so a retry
            # renders what the user asked for even if they have since
            # changed the defaults.
            #
            # The fingerprint is the hash of the normalised settings. One
            # clip with one fingerprint is one artifact: a repeated request
            # finds the artifact that already exists, and changing a setting
            # produces a *different* artifact rather than quietly replacing
            # a finished one.
            """
            CREATE TABLE IF NOT EXISTS clip_formats (
                id TEXT PRIMARY KEY,
                clip_id TEXT NOT NULL REFERENCES clips (id) ON DELETE CASCADE,
                job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
                config_fingerprint TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('queued', 'preparing', 'rendering',
                                      'verifying', 'completed', 'failed')),
                stage TEXT NOT NULL
                    CHECK (stage IN ('queued', 'preparing', 'rendering',
                                     'verifying', 'completed', 'failed')),
                progress INTEGER NOT NULL DEFAULT 0
                    CHECK (progress BETWEEN 0 AND 100),
                output_path TEXT,
                output_size_bytes INTEGER
                    CHECK (output_size_bytes IS NULL OR output_size_bytes >= 0),
                actual_duration_seconds REAL,
                width INTEGER CHECK (width IS NULL OR width > 0),
                height INTEGER CHECK (height IS NULL OR height > 0),
                video_codec TEXT,
                audio_codec TEXT,
                pixel_format TEXT,
                captions_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (captions_enabled IN (0, 1)),
                captions_applied INTEGER NOT NULL DEFAULT 0
                    CHECK (captions_applied IN (0, 1)),
                caption_count INTEGER NOT NULL DEFAULT 0
                    CHECK (caption_count >= 0),
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                worker_id TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_code TEXT,
                error_message TEXT,
                UNIQUE (clip_id, config_fingerprint)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_clip_formats_clip ON clip_formats (clip_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_clip_formats_job ON clip_formats (job_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_clip_formats_status ON clip_formats (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_clip_formats_lease ON clip_formats (lease_expires_at)",
        ),
    ),
    (
        7,
        (
            # Phase 8 adds the editing of a clip.
            #
            # A *revision* is a new clip row that was trimmed by hand. It
            # points at the clip it came from and carries its own
            # boundaries, its own title and its own file. Two things are
            # deliberately not touched: the clip it came from, which keeps
            # its own file and its own boundaries, and the AI's candidate,
            # which is never rewritten to represent a user's edit.
            "ALTER TABLE clips ADD COLUMN parent_clip_id TEXT REFERENCES clips (id) ON DELETE SET NULL",
            # The user's own label for a clip. It is a label, not a
            # re-render: changing it never touches the file.
            "ALTER TABLE clips ADD COLUMN title TEXT",
            "CREATE INDEX IF NOT EXISTS idx_clips_parent ON clips (parent_clip_id)",
            # The same trim of the same clip is the same revision, so a
            # repeated request does not render it twice.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_revision ON clips (parent_clip_id, start_seconds, end_seconds) WHERE parent_clip_id IS NOT NULL",
            # The text a user typed for a caption, per transcript segment.
            """
            CREATE TABLE IF NOT EXISTS clip_caption_edits (
                id TEXT PRIMARY KEY,
                clip_id TEXT NOT NULL REFERENCES clips (id) ON DELETE CASCADE,
                segment_index INTEGER NOT NULL CHECK (segment_index >= 0),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (clip_id, segment_index)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_clip_caption_edits_clip ON clip_caption_edits (clip_id)",
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


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Read several rows as they were at one instant.

    A client that polls while the worker is running must not see a half
    finished answer: the job row and the transcript row are read inside one
    read transaction, so they belong to the same moment in time.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


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


def job_ids_with_transcripts(
    conn: sqlite3.Connection, job_ids: Sequence[str]
) -> set[str]:
    """Return the subset of ``job_ids`` that has a stored transcript.

    The API uses this so that it can say truthfully whether a transcript
    exists. A job that was finished before transcripts existed has none, and
    must not be presented as transcribed.
    """
    wanted = [job_id for job_id in job_ids if job_id]
    if not wanted:
        return set()
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT job_id FROM transcripts WHERE job_id IN ({placeholders})", wanted
    ).fetchall()
    return {row["job_id"] for row in rows}


def job_artifacts(
    conn: sqlite3.Connection, job_ids: Sequence[str]
) -> dict[str, JobArtifacts]:
    """Count what each of ``job_ids`` has produced, in a fixed set of queries.

    The counts are gathered for the whole page at once, so a dashboard
    showing fifty jobs does not make fifty requests. This is the same
    reasoning as :func:`job_ids_with_transcripts`, which does the same for
    the transcript flag.

    The "most recent analysis" is chosen by the same rule as
    :func:`latest_analysis_for_job` -- ``created_at`` descending, then ``id``
    descending -- so the count agrees with the analysis the detail endpoint
    reports.
    """
    wanted = [job_id for job_id in job_ids if job_id]
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)

    # The most recent analysis of each job. The ordering is global, so the
    # first row for a job is that job's most recent analysis.
    analysis_rows = conn.execute(
        f"SELECT job_id, id, status FROM highlight_analyses "
        f"WHERE job_id IN ({placeholders}) "
        f"ORDER BY created_at DESC, id DESC",
        wanted,
    ).fetchall()
    analysis_of: dict[str, str] = {}
    status_of: dict[str, str] = {}
    for row in analysis_rows:
        if row["job_id"] in analysis_of:
            continue
        analysis_of[row["job_id"]] = row["id"]
        status_of[row["job_id"]] = row["status"]

    candidate_counts: dict[str, int] = {}
    if analysis_of:
        analysis_ids = list(analysis_of.values())
        analysis_placeholders = ",".join("?" for _ in analysis_ids)
        for row in conn.execute(
            f"SELECT analysis_id, COUNT(*) AS total FROM highlight_candidates "
            f"WHERE analysis_id IN ({analysis_placeholders}) GROUP BY analysis_id",
            analysis_ids,
        ).fetchall():
            candidate_counts[row["analysis_id"]] = int(row["total"])

    clip_counts = {
        row["job_id"]: int(row["total"])
        for row in conn.execute(
            f"SELECT job_id, COUNT(*) AS total FROM clips "
            f"WHERE job_id IN ({placeholders}) GROUP BY job_id",
            wanted,
        ).fetchall()
    }
    format_counts = {
        row["job_id"]: int(row["total"])
        for row in conn.execute(
            f"SELECT job_id, COUNT(*) AS total FROM clip_formats "
            f"WHERE job_id IN ({placeholders}) GROUP BY job_id",
            wanted,
        ).fetchall()
    }

    return {
        job_id: JobArtifacts(
            analysis_status=status_of.get(job_id),
            candidate_count=candidate_counts.get(analysis_of.get(job_id, ""), 0),
            clip_count=clip_counts.get(job_id, 0),
            format_count=format_counts.get(job_id, 0),
        )
        for job_id in wanted
    }


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
    expected_worker_id: str | None = None,
    now=None,
) -> JobRecord | None:
    """Update a job, enforcing the allowed state transitions.

    ``progress`` never decreases: the stored value only ever moves forward.

    ``expected_worker_id`` makes the update conditional on the job still
    being owned by that worker. It is checked inside the same transaction as
    the write, so a worker whose lease has expired -- and whose job has
    already been taken over by another worker -- cannot overwrite the newer
    attempt. The update is skipped and ``None`` is returned in that case.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        if expected_worker_id is not None and row["worker_id"] != expected_worker_id:
            # The job belongs to somebody else now. A stale worker must not
            # touch it.
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
        if new_status in (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        ):
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
    conn: sqlite3.Connection, job_id: str, *, lease_seconds: int, now=None,
    worker_id: str | None = None,
) -> None:
    """Extend the lease on a job the worker still owns.

    When ``worker_id`` is given the renewal only applies to a job that is
    still owned by that worker, so a worker that has lost its job cannot
    keep its lease alive.
    """
    moment = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if worker_id is None:
            conn.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (to_iso(moment + timedelta(seconds=lease_seconds)), to_iso(moment), job_id),
            )
        else:
            conn.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = ?, updated_at = ?
                 WHERE id = ? AND worker_id = ?
                """,
                (
                    to_iso(moment + timedelta(seconds=lease_seconds)),
                    to_iso(moment),
                    job_id,
                    worker_id,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def renew_analysis_lease(
    conn: sqlite3.Connection,
    analysis_id: str,
    *,
    lease_seconds: int,
    worker_id: str | None = None,
    now=None,
) -> bool:
    """Extend the lease on an analysis the worker still owns.

    When ``worker_id`` is given the renewal only applies to an analysis that
    is still owned by that worker, so a worker that has lost its analysis
    cannot keep its lease alive.

    Returns ``True`` when a row was renewed, ``False`` when the analysis does
    not exist or is owned by a different worker.
    """
    moment = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if worker_id is None:
            cursor = conn.execute(
                "UPDATE highlight_analyses SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (to_iso(moment + timedelta(seconds=lease_seconds)), to_iso(moment), analysis_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE highlight_analyses
                   SET lease_expires_at = ?, updated_at = ?
                 WHERE id = ? AND worker_id = ?
                """,
                (
                    to_iso(moment + timedelta(seconds=lease_seconds)),
                    to_iso(moment),
                    analysis_id,
                    worker_id,
                ),
            )
        renewed = cursor.rowcount > 0
        conn.execute("COMMIT")
        return renewed
    except Exception:
        conn.execute("ROLLBACK")
        raise


def renew_clip_lease(
    conn: sqlite3.Connection,
    clip_id: str,
    *,
    lease_seconds: int,
    worker_id: str | None = None,
    now=None,
) -> bool:
    """Extend the lease on a clip the worker still owns.

    Same rules as :func:`renew_analysis_lease`: with ``worker_id`` the
    renewal only applies to a clip still owned by that worker, and the
    return value says whether the row was renewed.

    Returns ``True`` when a row was renewed, ``False`` when the clip does
    not exist or is owned by a different worker.
    """
    moment = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if worker_id is None:
            cursor = conn.execute(
                "UPDATE clips SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (to_iso(moment + timedelta(seconds=lease_seconds)), to_iso(moment), clip_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE clips
                   SET lease_expires_at = ?, updated_at = ?
                 WHERE id = ? AND worker_id = ?
                """,
                (
                    to_iso(moment + timedelta(seconds=lease_seconds)),
                    to_iso(moment),
                    clip_id,
                    worker_id,
                ),
            )
        renewed = cursor.rowcount > 0
        conn.execute("COMMIT")
        return renewed
    except Exception:
        conn.execute("ROLLBACK")
        raise


def renew_format_lease(
    conn: sqlite3.Connection,
    format_id: str,
    *,
    lease_seconds: int,
    worker_id: str | None = None,
    now=None,
) -> bool:
    """Extend the lease on a formatted clip the worker still owns.

    Same rules as :func:`renew_analysis_lease`: with ``worker_id`` the
    renewal only applies to an artifact still owned by that worker, and the
    return value says whether the row was renewed.

    Returns ``True`` when a row was renewed, ``False`` when the artifact
    does not exist or is owned by a different worker.
    """
    moment = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if worker_id is None:
            cursor = conn.execute(
                "UPDATE clip_formats SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (to_iso(moment + timedelta(seconds=lease_seconds)), to_iso(moment), format_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE clip_formats
                   SET lease_expires_at = ?, updated_at = ?
                 WHERE id = ? AND worker_id = ?
                """,
                (
                    to_iso(moment + timedelta(seconds=lease_seconds)),
                    to_iso(moment),
                    format_id,
                    worker_id,
                ),
            )
        renewed = cursor.rowcount > 0
        conn.execute("COMMIT")
        return renewed
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

    A job in a terminal status is left alone. Terminal states stay terminal
    unless a documented recovery path moves them, and this is not that path.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"""
            UPDATE jobs
               SET status = ?, stage = ?, updated_at = ?,
                   worker_id = NULL, lease_expires_at = NULL,
                   error_code = ?, error_message = ?,
                   completed_at = NULL
             WHERE id = ? AND status IN ({placeholders})
            """,
            (
                JobStatus.QUEUED.value,
                JobStage.QUEUED.value,
                now_iso,
                error_code,
                error_message,
                job_id,
                *(status.value for status in ACTIVE_STATUSES),
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
                        "The worker's lease on this job expired while it was "
                        "running. The job was queued again; a running worker "
                        "will pick it up.",
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


# --- transcripts ------------------------------------------------------------


def _row_to_transcript(row: sqlite3.Row) -> TranscriptRecord:
    return TranscriptRecord(
        id=row["id"],
        job_id=row["job_id"],
        language=row["language"],
        model_name=row["model_name"],
        audio_sample_rate=row["audio_sample_rate"],
        audio_channels=row["audio_channels"],
        duration_seconds=row["duration_seconds"],
        segment_count=row["segment_count"],
        created_at=row["created_at"],
    )


def _row_to_transcript_segment(row: sqlite3.Row) -> TranscriptSegmentRecord:
    return TranscriptSegmentRecord(
        id=row["id"],
        transcript_id=row["transcript_id"],
        segment_index=row["segment_index"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        text=row["text"],
        average_log_probability=row["average_log_probability"],
        no_speech_probability=row["no_speech_probability"],
    )


def save_transcript(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    transcript: Transcript,
    transcript_id: str | None = None,
    expected_worker_id: str | None = None,
    now=None,
) -> TranscriptRecord | None:
    """Store a transcript and all of its segments in one transaction.

    Any transcript that already exists for the job is removed first, so
    re-processing a job replaces its transcript instead of adding a second
    one. The delete and the inserts share a transaction, so a partially
    written transcript is never visible: either the whole transcript is there
    or none of it is.

    ``expected_worker_id`` makes the write conditional on the job still
    being owned by that worker. The check happens inside the same
    transaction, so a worker whose lease has expired cannot replace the
    result of a newer attempt. ``None`` is returned in that case, and
    nothing is written.

    Raises:
        ValueError: when the transcript is not safe to store.
    """
    assert_storable(transcript)

    moment = now or utcnow()
    now_iso = to_iso(moment)
    record_id = transcript_id or uuid.uuid4().hex

    conn.execute("BEGIN IMMEDIATE")
    try:
        if expected_worker_id is not None:
            owner = conn.execute(
                "SELECT worker_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if owner is None or owner["worker_id"] != expected_worker_id:
                # The job belongs to somebody else now. A stale worker must
                # not replace the newer attempt's transcript.
                conn.execute("COMMIT")
                return None

        conn.execute("DELETE FROM transcripts WHERE job_id = ?", (job_id,))
        conn.execute(
            """
            INSERT INTO transcripts (
                id, job_id, language, model_name, audio_sample_rate,
                audio_channels, duration_seconds, segment_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                job_id,
                transcript.language,
                transcript.model_name,
                transcript.audio_sample_rate,
                transcript.audio_channels,
                transcript.duration_seconds,
                len(transcript.segments),
                now_iso,
            ),
        )
        conn.executemany(
            """
            INSERT INTO transcript_segments (
                id, transcript_id, segment_index, start_seconds, end_seconds,
                text, average_log_probability, no_speech_probability
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    uuid.uuid4().hex,
                    record_id,
                    segment.index,
                    segment.start_seconds,
                    segment.end_seconds,
                    segment.text,
                    segment.average_log_probability,
                    segment.no_speech_probability,
                )
                for segment in transcript.segments
            ],
        )
        row = conn.execute(
            "SELECT * FROM transcripts WHERE id = ?", (record_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_transcript(row)


def get_transcript(
    conn: sqlite3.Connection, job_id: str
) -> TranscriptRecord | None:
    """Return the transcript of a job, or ``None`` when it has none."""
    row = conn.execute(
        "SELECT * FROM transcripts WHERE job_id = ?", (job_id,)
    ).fetchone()
    return _row_to_transcript(row) if row else None


def count_transcript_segments(conn: sqlite3.Connection, transcript_id: str) -> int:
    """Return how many segments a transcript has."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?",
            (transcript_id,),
        ).fetchone()[0]
    )


def list_transcript_segments(
    conn: sqlite3.Connection,
    transcript_id: str,
    *,
    limit: int = 500,
    offset: int = 0,
) -> list[TranscriptSegmentRecord]:
    """Return one ordered page of a transcript's segments."""
    rows = conn.execute(
        """
        SELECT * FROM transcript_segments
         WHERE transcript_id = ?
         ORDER BY segment_index ASC
         LIMIT ? OFFSET ?
        """,
        (transcript_id, limit, offset),
    ).fetchall()
    return [_row_to_transcript_segment(row) for row in rows]


def delete_transcript(conn: sqlite3.Connection, job_id: str) -> None:
    """Remove the transcript of a job, if it has one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM transcripts WHERE job_id = ?", (job_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --- integrations (Phase 4) -------------------------------------------------


def _row_to_integration(row: sqlite3.Row) -> IntegrationRecord:
    """Build an integration record from a row."""
    effort_values = row["reasoning_effort_values"]
    return IntegrationRecord(
        id=row["id"],
        name=row["name"],
        provider_id=row["provider_id"],
        protocol=ApiProtocol(row["protocol"]),
        base_url=row["base_url"],
        model_id=row["model_id"],
        model_name=row["model_name"],
        capabilities=ModelCapabilities(
            context_window=row["context_window"],
            max_output_tokens=row["max_output_tokens"],
            reasoning_effort_parameter=row["reasoning_effort_parameter"],
            reasoning_effort_values=tuple(
                part for part in (effort_values or "").split(",") if part
            ),
            structured_output=StructuredOutputMode(row["structured_output"]),
            available=(
                None if row["model_available"] is None else bool(row["model_available"])
            ),
            metadata_source=row["metadata_source"],
            verified_at=row["metadata_verified_at"],
        ),
        reasoning_effort=row["reasoning_effort"],
        pricing_status=PricingStatus(row["pricing_status"]),
        local_endpoint=bool(row["local_endpoint"]),
        enabled=bool(row["enabled"]),
        secret_ref=row["secret_ref"],
        role=IntegrationRole(row["role"]),
        is_default=bool(row["is_default"]),
        last_tested_at=row["last_tested_at"],
        last_test_status=ConnectionStatus(
            row["last_test_status"] or ConnectionStatus.UNKNOWN.value
        ),
        last_test_code=row["last_test_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_integration(
    conn: sqlite3.Connection,
    *,
    name: str,
    provider_id: str,
    protocol: ApiProtocol,
    base_url: str,
    model_id: str | None = None,
    model_name: str | None = None,
    capabilities: ModelCapabilities | None = None,
    reasoning_effort: str | None = None,
    pricing_status: PricingStatus = PricingStatus.UNKNOWN,
    local_endpoint: bool = False,
    enabled: bool = True,
    secret_ref: str | None = None,
    role: IntegrationRole = IntegrationRole.HIGHLIGHT_DETECTION,
    is_default: bool = False,
    integration_id: str | None = None,
    now=None,
) -> IntegrationRecord:
    """Save a new integration configuration."""
    record_id = integration_id or uuid.uuid4().hex
    now_iso = to_iso(now or utcnow())
    caps = capabilities or ModelCapabilities()

    conn.execute("BEGIN IMMEDIATE")
    try:
        if is_default:
            # Only one enabled default per role.
            conn.execute(
                "UPDATE integrations SET is_default = 0, updated_at = ? "
                "WHERE role = ? AND is_default = 1",
                (now_iso, role.value),
            )
        conn.execute(
            """
            INSERT INTO integrations (
                id, name, provider_id, protocol, base_url, model_id, model_name,
                context_window, max_output_tokens, reasoning_effort,
                reasoning_effort_parameter, reasoning_effort_values,
                structured_output, model_available, metadata_source,
                metadata_verified_at, pricing_status, local_endpoint, enabled,
                secret_ref, role, is_default, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                name,
                provider_id,
                protocol.value,
                base_url,
                model_id,
                model_name,
                caps.context_window,
                caps.max_output_tokens,
                reasoning_effort,
                caps.reasoning_effort_parameter,
                ",".join(caps.reasoning_effort_values),
                caps.structured_output.value,
                None if caps.available is None else int(caps.available),
                caps.metadata_source,
                caps.verified_at,
                pricing_status.value,
                int(local_endpoint),
                int(enabled),
                secret_ref,
                role.value,
                int(is_default),
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (record_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_integration(row)


def get_integration(
    conn: sqlite3.Connection, integration_id: str
) -> IntegrationRecord | None:
    """One integration, when it exists."""
    row = conn.execute(
        "SELECT * FROM integrations WHERE id = ?", (integration_id,)
    ).fetchone()
    return _row_to_integration(row) if row else None


def list_integrations(
    conn: sqlite3.Connection,
    *,
    role: IntegrationRole | None = None,
) -> list[IntegrationRecord]:
    """Every integration, newest first."""
    if role is None:
        rows = conn.execute(
            "SELECT * FROM integrations ORDER BY created_at DESC, id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM integrations WHERE role = ? "
            "ORDER BY created_at DESC, id DESC",
            (role.value,),
        ).fetchall()
    return [_row_to_integration(row) for row in rows]


def get_default_integration(
    conn: sqlite3.Connection,
    role: IntegrationRole,
    *,
    include_disabled: bool = False,
) -> IntegrationRecord | None:
    """The default integration for a role, when there is one.

    A disabled default is not returned unless ``include_disabled`` is set:
    the caller usually wants to tell the user *why* the configuration they
    chose is not being used.
    """
    if include_disabled:
        row = conn.execute(
            "SELECT * FROM integrations WHERE role = ? AND is_default = 1",
            (role.value,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM integrations WHERE role = ? AND is_default = 1 "
            "AND enabled = 1",
            (role.value,),
        ).fetchone()
    return _row_to_integration(row) if row else None


def update_integration(
    conn: sqlite3.Connection,
    integration_id: str,
    *,
    name: str | None = None,
    protocol: ApiProtocol | None = None,
    base_url: str | None = None,
    model_id: str | None = None,
    model_name: str | None = None,
    capabilities: ModelCapabilities | None = None,
    reasoning_effort: str | None = None,
    pricing_status: PricingStatus | None = None,
    local_endpoint: bool | None = None,
    enabled: bool | None = None,
    secret_ref: str | None = None,
    clear_secret: bool = False,
    now=None,
) -> IntegrationRecord | None:
    """Edit an integration.

    A field that is not given is left alone. In particular the secret
    reference is only changed when it is passed explicitly, so an ordinary
    edit never erases a stored key. ``clear_secret`` removes it.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        current = _row_to_integration(row)
        caps = capabilities if capabilities is not None else current.capabilities

        conn.execute(
            """
            UPDATE integrations
               SET name = ?, protocol = ?, base_url = ?, model_id = ?,
                   model_name = ?, context_window = ?, max_output_tokens = ?,
                   reasoning_effort = ?, reasoning_effort_parameter = ?,
                   reasoning_effort_values = ?, structured_output = ?,
                   model_available = ?, metadata_source = ?,
                   metadata_verified_at = ?, pricing_status = ?,
                   local_endpoint = ?, enabled = ?, secret_ref = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                name if name is not None else current.name,
                (
                    protocol.value
                    if protocol is not None
                    else current.protocol.value
                ),
                base_url if base_url is not None else current.base_url,
                model_id if model_id is not None else current.model_id,
                model_name if model_name is not None else current.model_name,
                caps.context_window,
                caps.max_output_tokens,
                reasoning_effort
                if reasoning_effort is not None
                else current.reasoning_effort,
                caps.reasoning_effort_parameter,
                ",".join(caps.reasoning_effort_values),
                caps.structured_output.value,
                None if caps.available is None else int(caps.available),
                caps.metadata_source,
                caps.verified_at,
                (
                    pricing_status.value
                    if pricing_status is not None
                    else current.pricing_status.value
                ),
                int(
                    local_endpoint
                    if local_endpoint is not None
                    else current.local_endpoint
                ),
                int(enabled if enabled is not None else current.enabled),
                (
                    None
                    if clear_secret
                    else (secret_ref if secret_ref is not None else current.secret_ref)
                ),
                now_iso,
                integration_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_integration(updated)


def set_default_integration(
    conn: sqlite3.Connection,
    integration_id: str,
    role: IntegrationRole = IntegrationRole.HIGHLIGHT_DETECTION,
    *,
    now=None,
) -> IntegrationRecord | None:
    """Make an integration the default for a role.

    The role has at most one enabled default, so any other default is
    cleared first, in the same transaction.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE integrations SET is_default = 0, updated_at = ? "
            "WHERE role = ? AND is_default = 1 AND id <> ?",
            (now_iso, role.value, integration_id),
        )
        conn.execute(
            "UPDATE integrations SET is_default = 1, enabled = 1, updated_at = ? "
            "WHERE id = ?",
            (now_iso, integration_id),
        )
        updated = conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_integration(updated)


def clear_default_integration(
    conn: sqlite3.Connection, role: IntegrationRole, *, now=None
) -> None:
    """Remove the default for a role, without disabling the configuration."""
    now_iso = to_iso(now or utcnow())
    conn.execute(
        "UPDATE integrations SET is_default = 0, updated_at = ? "
        "WHERE role = ? AND is_default = 1",
        (now_iso, role.value),
    )


def record_integration_test(
    conn: sqlite3.Connection,
    integration_id: str,
    *,
    status: ConnectionStatus,
    code: str | None = None,
    now=None,
) -> None:
    """Record the outcome of a connection test."""
    conn.execute(
        "UPDATE integrations SET last_tested_at = ?, last_test_status = ?, "
        "last_test_code = ?, updated_at = ? WHERE id = ?",
        (
            to_iso(now or utcnow()),
            status.value,
            code,
            to_iso(now or utcnow()),
            integration_id,
        ),
    )


def delete_integration(conn: sqlite3.Connection, integration_id: str) -> bool:
    """Remove an integration. Returns whether there was one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "DELETE FROM integrations WHERE id = ?", (integration_id,)
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cursor.rowcount > 0


# --- highlight analyses (Phase 4) --------------------------------------------


def _row_to_analysis(row: sqlite3.Row) -> HighlightAnalysis:
    """Build an analysis record from a row."""
    return HighlightAnalysis(
        id=row["id"],
        job_id=row["job_id"],
        integration_id=row["integration_id"],
        provider_id=row["provider_id"],
        protocol=row["protocol"],
        base_url=row["base_url"],
        model_id=row["model_id"],
        model_name=row["model_name"],
        prompt_version=row["prompt_version"],
        schema_version=row["schema_version"],
        transcript_id=row["transcript_id"],
        source_duration_seconds=row["source_duration_seconds"],
        status=row["status"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        attempt_count=row["attempt_count"],
        worker_id=row["worker_id"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def create_analysis(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    integration_id: str | None,
    provider_id: str,
    protocol: str,
    base_url: str,
    model_id: str,
    model_name: str | None,
    prompt_version: str,
    schema_version: str,
    transcript_id: str | None = None,
    source_duration_seconds: float | None = None,
    analysis_id: str | None = None,
    now=None,
) -> HighlightAnalysis:
    """Queue a highlight analysis."""
    record_id = analysis_id or uuid.uuid4().hex
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO highlight_analyses (
                id, job_id, integration_id, provider_id, protocol, base_url,
                model_id, model_name, prompt_version, schema_version,
                transcript_id, source_duration_seconds, status, attempt_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                record_id,
                job_id,
                integration_id,
                provider_id,
                protocol,
                base_url,
                model_id,
                model_name,
                prompt_version,
                schema_version,
                transcript_id,
                source_duration_seconds,
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM highlight_analyses WHERE id = ?", (record_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_analysis(row)


def get_analysis(
    conn: sqlite3.Connection, analysis_id: str
) -> HighlightAnalysis | None:
    """One analysis, when it exists."""
    row = conn.execute(
        "SELECT * FROM highlight_analyses WHERE id = ?", (analysis_id,)
    ).fetchone()
    return _row_to_analysis(row) if row else None


def latest_analysis_for_job(
    conn: sqlite3.Connection, job_id: str
) -> HighlightAnalysis | None:
    """The most recent analysis of a job, when there is one."""
    row = conn.execute(
        "SELECT * FROM highlight_analyses WHERE job_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return _row_to_analysis(row) if row else None


def active_analysis_for_job(
    conn: sqlite3.Connection, job_id: str
) -> HighlightAnalysis | None:
    """The pending or running analysis of a job, when there is one."""
    row = conn.execute(
        "SELECT * FROM highlight_analyses WHERE job_id = ? "
        "AND status IN ('pending', 'running') "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return _row_to_analysis(row) if row else None


def claim_next_analysis(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int,
    now=None,
) -> HighlightAnalysis | None:
    """Atomically claim the oldest pending analysis.

    Two workers can never receive the same analysis: the write lock is
    taken before the candidate row is read, and the update re-checks that
    the row is still pending.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)
    lease_iso = to_iso(moment + timedelta(seconds=lease_seconds))

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM highlight_analyses WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE highlight_analyses
               SET status = 'running', worker_id = ?, lease_expires_at = ?,
                   attempt_count = attempt_count + 1, updated_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (worker_id, lease_iso, now_iso, row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM highlight_analyses WHERE id = ?", (row["id"],)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_analysis(claimed)


def update_analysis(
    conn: sqlite3.Connection,
    analysis_id: str,
    *,
    status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    worker_id: str | None = None,
    expected_worker_id: str | None = None,
    now=None,
) -> HighlightAnalysis | None:
    """Update an analysis.

    ``expected_worker_id`` makes the write conditional on the analysis
    still being owned by that worker, so a worker whose lease expired
    cannot overwrite the result of the attempt that took over.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM highlight_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        if expected_worker_id is not None and row["worker_id"] != expected_worker_id:
            conn.execute("COMMIT")
            return None

        new_status = status if status is not None else row["status"]
        completed_at = row["completed_at"]
        if new_status in ("completed", "failed"):
            completed_at = now_iso

        if clear_error:
            new_code = None
            new_message = None
        else:
            new_code = row["error_code"] if error_code is None else error_code
            new_message = (
                row["error_message"] if error_message is None else error_message
            )

        conn.execute(
            """
            UPDATE highlight_analyses
               SET status = ?, error_code = ?, error_message = ?,
                   worker_id = ?, completed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                new_status,
                new_code,
                new_message,
                worker_id if worker_id is not None else row["worker_id"],
                completed_at,
                now_iso,
                analysis_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM highlight_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_analysis(updated)


def save_candidates(
    conn: sqlite3.Connection,
    analysis_id: str,
    candidates: list[HighlightCandidate],
    *,
    now=None,
) -> list[HighlightCandidate]:
    """Replace the candidates of an analysis, in one transaction.

    A retry replaces the previous candidates rather than adding to them, so
    a recovered analysis never ends up with two sets.
    """
    now_iso = to_iso(now or utcnow())
    stored: list[HighlightCandidate] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM highlight_candidates WHERE analysis_id = ?", (analysis_id,)
        )
        for candidate in candidates:
            record_id = candidate.id or uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO highlight_candidates (
                    id, analysis_id, position, start_segment_index,
                    end_segment_index, start_seconds, end_seconds, title,
                    reason, moment_type, score, decision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    analysis_id,
                    candidate.position,
                    candidate.start_segment_index,
                    candidate.end_segment_index,
                    candidate.start_seconds,
                    candidate.end_seconds,
                    candidate.title,
                    candidate.reason,
                    candidate.moment_type,
                    candidate.score,
                    candidate.decision,
                    now_iso,
                ),
            )
            stored.append(
                HighlightCandidate(
                    id=record_id,
                    position=candidate.position,
                    start_segment_index=candidate.start_segment_index,
                    end_segment_index=candidate.end_segment_index,
                    start_seconds=candidate.start_seconds,
                    end_seconds=candidate.end_seconds,
                    title=candidate.title,
                    reason=candidate.reason,
                    moment_type=candidate.moment_type,
                    score=candidate.score,
                    decision=candidate.decision,
                )
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stored


def list_candidates(
    conn: sqlite3.Connection, analysis_id: str
) -> list[HighlightCandidate]:
    """The candidates of an analysis, in order."""
    rows = conn.execute(
        "SELECT * FROM highlight_candidates WHERE analysis_id = ? "
        "ORDER BY position ASC",
        (analysis_id,),
    ).fetchall()
    return [
        HighlightCandidate(
            id=row["id"],
            position=row["position"],
            start_segment_index=row["start_segment_index"],
            end_segment_index=row["end_segment_index"],
            start_seconds=row["start_seconds"],
            end_seconds=row["end_seconds"],
            title=row["title"],
            reason=row["reason"],
            moment_type=row["moment_type"],
            score=row["score"],
            decision=row["decision"],
        )
        for row in rows
    ]


def set_candidate_decision(
    conn: sqlite3.Connection,
    analysis_id: str,
    candidate_id: str,
    decision: str,
) -> HighlightCandidate | None:
    """Accept or reject a candidate."""
    conn.execute(
        "UPDATE highlight_candidates SET decision = ? "
        "WHERE id = ? AND analysis_id = ?",
        (decision, candidate_id, analysis_id),
    )
    row = conn.execute(
        "SELECT * FROM highlight_candidates WHERE id = ? AND analysis_id = ?",
        (candidate_id, analysis_id),
    ).fetchone()
    if row is None:
        return None
    return HighlightCandidate(
        id=row["id"],
        position=row["position"],
        start_segment_index=row["start_segment_index"],
        end_segment_index=row["end_segment_index"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        title=row["title"],
        reason=row["reason"],
        moment_type=row["moment_type"],
        score=row["score"],
        decision=row["decision"],
    )


def recover_expired_analysis_leases(
    conn: sqlite3.Connection,
    *,
    max_attempts: int,
    now=None,
) -> list[HighlightAnalysis]:
    """Requeue or fail analyses abandoned by a worker that died."""
    now_iso = to_iso(now or utcnow())
    recovered: list[HighlightAnalysis] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM highlight_analyses "
            "WHERE status = 'running' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at < ?",
            (now_iso,),
        ).fetchall()
        for row in rows:
            if row["attempt_count"] >= max_attempts:
                conn.execute(
                    """
                    UPDATE highlight_analyses
                       SET status = 'failed', error_code = ?, error_message = ?,
                           completed_at = ?, updated_at = ?, worker_id = NULL,
                           lease_expires_at = NULL
                     WHERE id = ?
                    """,
                    (
                        "max_attempts_exceeded",
                        "The analysis was abandoned by a worker too many times.",
                        now_iso,
                        now_iso,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE highlight_analyses
                       SET status = 'pending', error_code = ?, error_message = ?,
                           updated_at = ?, worker_id = NULL, lease_expires_at = NULL
                     WHERE id = ?
                    """,
                    (
                        "lease_expired",
                        "The worker's lease on this analysis expired while it "
                        "was running. It was queued again; a running worker "
                        "will pick it up.",
                        now_iso,
                        row["id"],
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM highlight_analyses WHERE id = ?", (row["id"],)
            ).fetchone()
            recovered.append(_row_to_analysis(updated))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return recovered

# --- clips (Phase 5) --------------------------------------------------------


def _row_to_clip(row: sqlite3.Row) -> ClipRecord:
    """Build a clip record from a row."""
    return ClipRecord(
        id=row["id"],
        job_id=row["job_id"],
        analysis_id=row["analysis_id"],
        candidate_id=row["candidate_id"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        duration_seconds=row["requested_duration_seconds"],
        status=ClipStatus(row["status"]),
        stage=ClipStage(row["stage"]),
        progress=row["progress"],
        output_path=row["output_path"],
        output_size_bytes=row["output_size_bytes"],
        actual_duration_seconds=row["actual_duration_seconds"],
        width=row["width"],
        height=row["height"],
        video_codec=row["video_codec"],
        audio_codec=row["audio_codec"],
        attempt_count=row["attempt_count"],
        worker_id=row["worker_id"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        parent_clip_id=row["parent_clip_id"],
        title=row["title"],
    )


def create_clip(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    analysis_id: str | None,
    candidate_id: str | None,
    start_seconds: float,
    end_seconds: float,
    clip_id: str | None = None,
    now=None,
) -> ClipRecord:
    """Queue a clip.

    The times are the candidate's own validated ones. The clip is created
    in ``queued``: nothing has been rendered yet, and the output path is
    allocated only once a render has produced a file to put there.
    """
    record_id = clip_id or uuid.uuid4().hex
    now_iso = to_iso(now or utcnow())
    duration = end_seconds - start_seconds
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO clips (
                id, job_id, analysis_id, candidate_id, start_seconds,
                end_seconds, requested_duration_seconds, status, stage,
                progress, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, 0, ?, ?)
            """,
            (
                record_id,
                job_id,
                analysis_id,
                candidate_id,
                start_seconds,
                end_seconds,
                duration,
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (record_id,)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_clip(row)


def get_clip(conn: sqlite3.Connection, clip_id: str) -> ClipRecord | None:
    """One clip, when it exists."""
    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return _row_to_clip(row) if row else None


def cancel_job(conn: sqlite3.Connection, job_id: str, *, now=None) -> JobRecord | None:
    """Cancel a job that has not finished.

    A job that has already finished cannot be cancelled: it is a real
    outcome and it is not undone by a later request. A job that is
    already cancelled is left alone, so asking twice is harmless.

    The lease is released and the worker is forgotten, so a worker that is
    still holding the job cannot write over the cancellation: its own
    updates are conditional on still owning the job.
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
        if current in (JobStatus.COMPLETED, JobStatus.FAILED):
            conn.execute("COMMIT")
            return None
        if current is JobStatus.CANCELLED:
            conn.execute("COMMIT")
            return _row_to_job(row)

        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, completed_at = ?, updated_at = ?,
                   worker_id = NULL, lease_expires_at = NULL,
                   error_code = ?, error_message = ?
             WHERE id = ?
            """,
            (
                JobStatus.CANCELLED.value,
                JobStage.CANCELLED.value,
                now_iso,
                now_iso,
                "job_cancelled",
                "The job was cancelled.",
                job_id,
            ),
        )
        cancelled = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

        # Cancellation cascades to the work nobody has claimed yet: a queued
        # child of a cancelled job must never be rendered afterwards, because
        # the job it belongs to can never complete again. Children already
        # claimed by a worker are left alone here -- the worker's tick sees
        # the cancellation, stops the child process, and records the outcome.
        for statement, params in (
            (
                "UPDATE clips SET status = 'failed', stage = 'failed', "
                "error_code = 'job_cancelled', error_message = ?, "
                "updated_at = ?, worker_id = NULL, lease_expires_at = NULL "
                "WHERE job_id = ? AND status = 'queued'",
                (
                    "The job was cancelled while this clip was queued.",
                    now_iso,
                    job_id,
                ),
            ),
            (
                "UPDATE clip_formats SET status = 'failed', stage = 'failed', "
                "error_code = 'job_cancelled', error_message = ?, "
                "updated_at = ?, worker_id = NULL, lease_expires_at = NULL "
                "WHERE job_id = ? AND status = 'queued'",
                (
                    "The job was cancelled while this formatted clip was queued.",
                    now_iso,
                    job_id,
                ),
            ),
            (
                "UPDATE highlight_analyses SET status = 'failed', "
                "error_code = 'job_cancelled', error_message = ?, "
                "updated_at = ?, worker_id = NULL, lease_expires_at = NULL "
                "WHERE job_id = ? AND status = 'pending'",
                (
                    "The job was cancelled before this analysis ran.",
                    now_iso,
                    job_id,
                ),
            ),
        ):
            conn.execute(statement, params)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_job(cancelled)


def job_deletion_artifacts(
    conn: sqlite3.Connection, job_id: str
) -> tuple[list[str], list[str]]:
    """What a job deletion must reclaim from the filesystem.

    Returns the storage-relative file paths the job and its children point
    at (the source, every rendered clip, every formatted clip), and the
    formatted-clip ids whose scratch directories live under the temp root.
    Both are read **before** the rows are gone.

    A path here is a stored relative path, which is resolved with the
    storage root's containment check before anything is removed.
    """
    paths: list[str] = []
    row = conn.execute(
        "SELECT source_path FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is not None and row["source_path"]:
        paths.append(row["source_path"])
    for row in conn.execute(
        "SELECT DISTINCT output_path FROM clips "
        "WHERE job_id = ? AND output_path IS NOT NULL",
        (job_id,),
    ):
        paths.append(row["output_path"])
    format_ids: list[str] = []
    for row in conn.execute(
        "SELECT DISTINCT id FROM clip_formats WHERE job_id = ?", (job_id,)
    ):
        format_ids.append(row["id"])
    for row in conn.execute(
        "SELECT DISTINCT output_path FROM clip_formats "
        "WHERE job_id = ? AND output_path IS NOT NULL",
        (job_id,),
    ):
        paths.append(row["output_path"])
    return paths, format_ids


def delete_job(conn: sqlite3.Connection, job_id: str) -> str:
    """Delete a job and, through the schema's cascades, everything under it.

    The transcripts and their segments, the highlight analyses and their
    candidates, the clips, the formatted clips and the caption edits all
    declare ``ON DELETE CASCADE``, and connections run with foreign keys
    enabled, so removing the job row removes the whole tree.

    A job that a worker could still be working on -- queued or running --
    is **not** deletable: cancel it first. The status is re-checked inside
    the delete's own transaction, so a job claimed between the caller's
    check and this delete is refused here rather than half-removed.

    Returns ``"deleted"``, ``"not_found"`` or ``"not_deletable"``.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return "not_found"
        if row["status"] not in (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        ):
            conn.execute("COMMIT")
            return "not_deletable"
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "deleted"


def get_clip_for_candidate(
    conn: sqlite3.Connection, candidate_id: str
) -> ClipRecord | None:
    """The clip of a candidate, when one has been asked for."""
    row = conn.execute(
        "SELECT * FROM clips WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return _row_to_clip(row) if row else None


def get_clip_revision(
    conn: sqlite3.Connection,
    *,
    parent_clip_id: str,
    start_seconds: float,
    end_seconds: float,
) -> ClipRecord | None:
    """The revision of a clip for a given range, when it exists.

    The same trim of the same clip is the same revision, so a repeated
    request finds it rather than rendering it again.
    """
    row = conn.execute(
        "SELECT * FROM clips WHERE parent_clip_id = ? "
        "AND start_seconds = ? AND end_seconds = ?",
        (parent_clip_id, start_seconds, end_seconds),
    ).fetchone()
    return _row_to_clip(row) if row else None


def list_clip_revisions(
    conn: sqlite3.Connection, parent_clip_id: str
) -> list[ClipRecord]:
    """The revisions of a clip, oldest first."""
    rows = conn.execute(
        "SELECT * FROM clips WHERE parent_clip_id = ? "
        "ORDER BY created_at ASC, id ASC",
        (parent_clip_id,),
    ).fetchall()
    return [_row_to_clip(row) for row in rows]


def create_clip_revision(
    conn: sqlite3.Connection,
    *,
    parent: ClipRecord,
    start_seconds: float,
    end_seconds: float,
    title: str | None = None,
    clip_id: str | None = None,
    now=None,
) -> ClipRecord:
    """Queue a revision of a clip: the same moment, trimmed by hand.

    The revision is a **new artifact** with its own identity, its own file
    and its own lifecycle. The clip it came from is not touched, and the
    AI's candidate is not rewritten: the revision has no candidate of its
    own, only a parent.
    """
    record_id = clip_id or uuid.uuid4().hex
    now_iso = to_iso(now or utcnow())
    duration = end_seconds - start_seconds
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO clips (
                id, job_id, analysis_id, candidate_id, parent_clip_id, title,
                start_seconds, end_seconds, requested_duration_seconds,
                status, stage, progress, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'queued', 'queued', 0, 0, ?, ?)
            """,
            (
                record_id,
                parent.job_id,
                parent.analysis_id,
                parent.id,
                title,
                start_seconds,
                end_seconds,
                duration,
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM clips WHERE id = ?", (record_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_clip(row)


def set_clip_title(
    conn: sqlite3.Connection,
    clip_id: str,
    title: str | None,
    *,
    now=None,
) -> ClipRecord | None:
    """Set the user's label on a clip.

    A label is not a re-render: the file is not touched and the boundaries
    are not touched. The AI's candidate keeps its own title either way.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE clips SET title = ?, updated_at = ? WHERE id = ?",
            (title, now_iso, clip_id),
        )
        row = conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_clip(row) if row else None


def list_clip_caption_edits(
    conn: sqlite3.Connection, clip_id: str
) -> list[tuple[int, str]]:
    """The caption text a user has changed on a clip, by segment index.

    The pairs are sorted by the segment index, so the fingerprint of a
    configuration built from them is stable.
    """
    rows = conn.execute(
        "SELECT segment_index, text FROM clip_caption_edits "
        "WHERE clip_id = ? ORDER BY segment_index ASC",
        (clip_id,),
    ).fetchall()
    return [(int(row["segment_index"]), str(row["text"])) for row in rows]


def set_clip_caption_edits(
    conn: sqlite3.Connection,
    clip_id: str,
    edits: Sequence[tuple[int, str]],
    *,
    now=None,
) -> list[tuple[int, str]]:
    """Replace the caption text a user has changed on a clip.

    The whole set is replaced, so a request that removes an edit removes
    it rather than leaving it behind.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM clip_caption_edits WHERE clip_id = ?", (clip_id,)
        )
        for segment_index, text in edits:
            conn.execute(
                """
                INSERT INTO clip_caption_edits (
                    id, clip_id, segment_index, text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    clip_id,
                    segment_index,
                    text,
                    now_iso,
                    now_iso,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return list_clip_caption_edits(conn, clip_id)


def list_clips(
    conn: sqlite3.Connection,
    *,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ClipRecord]:
    """The clips of a job, or every clip, in creation order."""
    if job_id is None:
        rows = conn.execute(
            "SELECT * FROM clips ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM clips WHERE job_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
    return [_row_to_clip(row) for row in rows]


def count_clips(conn: sqlite3.Connection, *, job_id: str | None = None) -> int:
    """How many clips there are."""
    if job_id is None:
        row = conn.execute("SELECT COUNT(*) FROM clips").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE job_id = ?", (job_id,)
        ).fetchone()
    return int(row[0])


def claim_next_clip(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int,
    now=None,
) -> ClipRecord | None:
    """Atomically claim the oldest queued clip.

    Two workers can never receive the same clip: the write lock is taken
    before the candidate row is read, and the update re-checks that the row
    is still queued.
    """
    moment = now or utcnow()
    now_iso = to_iso(moment)
    lease_iso = to_iso(moment + timedelta(seconds=lease_seconds))

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM clips WHERE status = 'queued' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE clips
               SET status = 'preparing', stage = 'preparing', progress = ?,
                   worker_id = ?, lease_expires_at = ?, attempt_count = attempt_count + 1,
                   started_at = COALESCE(started_at, ?), updated_at = ?
             WHERE id = ? AND status = 'queued'
            """,
            (
                CLIP_STAGE_PROGRESS[ClipStage.PREPARING],
                worker_id,
                lease_iso,
                now_iso,
                now_iso,
                row["id"],
            ),
        )
        claimed = conn.execute(
            "SELECT * FROM clips WHERE id = ?", (row["id"],)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_clip(claimed)


def update_clip(
    conn: sqlite3.Connection,
    clip_id: str,
    *,
    status: ClipStatus | None = None,
    stage: ClipStage | None = None,
    progress: int | None = None,
    output_path: str | None = None,
    output_size_bytes: int | None = None,
    actual_duration_seconds: float | None = None,
    width: int | None = None,
    height: int | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    expected_worker_id: str | None = None,
    now=None,
) -> ClipRecord | None:
    """Update a clip.

    ``expected_worker_id`` makes the write conditional on the clip still
    being owned by that worker, so a worker whose lease expired cannot
    overwrite the result of the attempt that took the clip over.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        if expected_worker_id is not None and row["worker_id"] != expected_worker_id:
            # Somebody else owns the clip now.
            conn.execute("COMMIT")
            return None

        current = ClipStatus(row["status"])
        # A status may be given as the enum or as its stored name.
        new_status = (
            status
            if status is None
            else (status if isinstance(status, ClipStatus) else ClipStatus(status))
        )
        if new_status is not current:
            assert_clip_transition(current, new_status)

        # A stage may be given as the enum or as its stored name.
        if stage is not None:
            new_stage = (
                stage if isinstance(stage, ClipStage) else ClipStage(stage)
            )
        else:
            new_stage = ClipStage(row["stage"])
        new_progress = progress
        if new_progress is None:
            new_progress = CLIP_STAGE_PROGRESS.get(new_stage, row["progress"])
        new_progress = max(0, min(100, int(new_progress)))

        if clear_error:
            new_code = None
            new_message = None
        elif error_code is not None or error_message is not None:
            new_code = error_code if error_code is not None else row["error_code"]
            new_message = (
                error_message if error_message is not None else row["error_message"]
            )
        else:
            new_code = row["error_code"]
            new_message = row["error_message"]

        completed_at = row["completed_at"]
        if new_status in (ClipStatus.COMPLETED, ClipStatus.FAILED):
            completed_at = now_iso

        conn.execute(
            """
            UPDATE clips
               SET status = ?, stage = ?, progress = ?, output_path = ?,
                   output_size_bytes = ?, actual_duration_seconds = ?,
                   width = ?, height = ?, video_codec = ?, audio_codec = ?,
                   error_code = ?, error_message = ?, completed_at = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                new_status.value,
                new_stage.value,
                new_progress,
                output_path if output_path is not None else row["output_path"],
                output_size_bytes
                if output_size_bytes is not None
                else row["output_size_bytes"],
                actual_duration_seconds
                if actual_duration_seconds is not None
                else row["actual_duration_seconds"],
                width if width is not None else row["width"],
                height if height is not None else row["height"],
                video_codec if video_codec is not None else row["video_codec"],
                audio_codec if audio_codec is not None else row["audio_codec"],
                new_code,
                new_message,
                completed_at,
                now_iso,
                clip_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_clip(updated)


def reset_clip_for_retry(
    conn: sqlite3.Connection,
    clip_id: str,
    *,
    now=None,
) -> ClipRecord | None:
    """Put a failed clip back on the queue.

    This is the explicit retry, not a transition: a failed clip is terminal,
    and only this operation moves it. A completed clip is never reset.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None or row["status"] != ClipStatus.FAILED.value:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE clips
               SET status = 'queued', stage = 'queued', progress = 0,
                   error_code = NULL, error_message = NULL, worker_id = NULL,
                   lease_expires_at = NULL, completed_at = NULL, updated_at = ?
             WHERE id = ?
            """,
            (now_iso, clip_id),
        )
        updated = conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_clip(updated)


def recover_expired_clip_leases(
    conn: sqlite3.Connection,
    *,
    max_attempts: int,
    now=None,
) -> list[ClipRecord]:
    """Requeue or fail clips abandoned by a worker that died.

    A clip whose attempt ran out of tries is failed rather than retried for
    ever. A clip that is already finished is left alone: recovery must not
    requeue a completed clip.
    """
    now_iso = to_iso(now or utcnow())
    recovered: list[ClipRecord] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM clips "
            "WHERE status IN ('preparing', 'rendering', 'verifying') "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (now_iso,),
        ).fetchall()
        for row in rows:
            if row["attempt_count"] >= max_attempts:
                conn.execute(
                    """
                    UPDATE clips
                       SET status = 'failed', stage = 'failed', progress = 0,
                           error_code = ?, error_message = ?,
                           completed_at = ?, updated_at = ?, worker_id = NULL,
                           lease_expires_at = NULL
                     WHERE id = ?
                    """,
                    (
                        "max_attempts_exceeded",
                        "The clip was abandoned by a worker too many times.",
                        now_iso,
                        now_iso,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE clips
                       SET status = 'queued', stage = 'queued', progress = 0,
                           error_code = ?, error_message = ?,
                           updated_at = ?, worker_id = NULL, lease_expires_at = NULL
                     WHERE id = ?
                    """,
                    (
                        "lease_expired",
                        "The worker's lease on this clip expired while it was "
                        "rendering. It was queued again; a running worker "
                        "will pick it up.",
                        now_iso,
                        row["id"],
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM clips WHERE id = ?", (row["id"],)
            ).fetchone()
            recovered.append(_row_to_clip(updated))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return recovered

def get_highlight_candidate(
    conn: sqlite3.Connection, candidate_id: str
) -> HighlightCandidateRecord | None:
    """One highlight candidate, with the job it belongs to.

    The candidate table does not carry the job: the candidate belongs to an
    analysis, and the analysis belongs to the job. The join is what lets a
    candidate be checked against the video it is supposed to come from.
    """
    row = conn.execute(
        """
        SELECT c.*, a.job_id AS candidate_job_id
          FROM highlight_candidates c
          JOIN highlight_analyses a ON a.id = c.analysis_id
         WHERE c.id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    return HighlightCandidateRecord(
        id=row["id"],
        job_id=row["candidate_job_id"],
        analysis_id=row["analysis_id"],
        position=row["position"],
        start_segment_index=row["start_segment_index"],
        end_segment_index=row["end_segment_index"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        title=row["title"],
        reason=row["reason"],
        moment_type=row["moment_type"],
        score=row["score"],
        decision=row["decision"],
        created_at=row["created_at"],
    )

# --- formatted clips (Phase 6) ---------------------------------------------


def _row_to_format(row: sqlite3.Row) -> FormatRecord:
    """Build a formatted-clip record from a row."""
    return FormatRecord(
        id=row["id"],
        clip_id=row["clip_id"],
        job_id=row["job_id"],
        config_fingerprint=row["config_fingerprint"],
        settings_json=row["settings_json"],
        status=ClipStatus(row["status"]),
        stage=ClipStage(row["stage"]),
        progress=row["progress"],
        output_path=row["output_path"],
        output_size_bytes=row["output_size_bytes"],
        actual_duration_seconds=row["actual_duration_seconds"],
        width=row["width"],
        height=row["height"],
        video_codec=row["video_codec"],
        audio_codec=row["audio_codec"],
        pixel_format=row["pixel_format"],
        captions_enabled=bool(row["captions_enabled"]),
        captions_applied=bool(row["captions_applied"]),
        caption_count=row["caption_count"],
        attempt_count=row["attempt_count"],
        worker_id=row["worker_id"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def create_format(
    conn: sqlite3.Connection,
    *,
    clip_id: str,
    job_id: str,
    config_fingerprint: str,
    settings_json: str,
    captions_enabled: bool = True,
    format_id: str | None = None,
    now=None,
) -> FormatRecord:
    """Queue a formatted clip.

    The output path is allocated only once a file exists, so a queued
    artifact never points at something that is not there.
    """
    record_id = format_id or uuid.uuid4().hex
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO clip_formats (
                id, clip_id, job_id, config_fingerprint, settings_json,
                status, stage, progress, captions_enabled, captions_applied,
                caption_count, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, 0, 0, 0, ?, ?)
            """,
            (
                record_id,
                clip_id,
                job_id,
                config_fingerprint,
                settings_json,
                int(captions_enabled),
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (record_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_format(row)


def get_format(conn: sqlite3.Connection, format_id: str) -> FormatRecord | None:
    """One formatted clip, when it exists."""
    row = conn.execute(
        "SELECT * FROM clip_formats WHERE id = ?", (format_id,)
    ).fetchone()
    return _row_to_format(row) if row else None


def get_format_for_fingerprint(
    conn: sqlite3.Connection, clip_id: str, config_fingerprint: str
) -> FormatRecord | None:
    """The artifact of a clip for a given configuration, when there is one."""
    row = conn.execute(
        "SELECT * FROM clip_formats WHERE clip_id = ? AND config_fingerprint = ?",
        (clip_id, config_fingerprint),
    ).fetchone()
    return _row_to_format(row) if row else None


def list_formats(
    conn: sqlite3.Connection,
    *,
    clip_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[FormatRecord]:
    """The formatted clips of a clip, or every formatted clip."""
    if clip_id is None:
        rows = conn.execute(
            "SELECT * FROM clip_formats ORDER BY created_at ASC, id ASC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM clip_formats WHERE clip_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
            (clip_id, limit, offset),
        ).fetchall()
    return [_row_to_format(row) for row in rows]


def count_formats(
    conn: sqlite3.Connection, *, clip_id: str | None = None
) -> int:
    """How many formatted clips there are."""
    if clip_id is None:
        row = conn.execute("SELECT COUNT(*) FROM clip_formats").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM clip_formats WHERE clip_id = ?", (clip_id,)
        ).fetchone()
    return int(row[0])


def claim_next_format(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int,
    now=None,
) -> FormatRecord | None:
    """Atomically claim the oldest queued formatted clip."""
    moment = now or utcnow()
    now_iso = to_iso(moment)
    lease_iso = to_iso(moment + timedelta(seconds=lease_seconds))

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM clip_formats WHERE status = 'queued' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE clip_formats
               SET status = 'preparing', stage = 'preparing', progress = ?,
                   worker_id = ?, lease_expires_at = ?,
                   attempt_count = attempt_count + 1,
                   started_at = COALESCE(started_at, ?), updated_at = ?
             WHERE id = ? AND status = 'queued'
            """,
            (
                CLIP_STAGE_PROGRESS[ClipStage.PREPARING],
                worker_id,
                lease_iso,
                now_iso,
                now_iso,
                row["id"],
            ),
        )
        claimed = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (row["id"],)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_format(claimed)


def update_format(
    conn: sqlite3.Connection,
    format_id: str,
    *,
    status: ClipStatus | None = None,
    stage: ClipStage | None = None,
    progress: int | None = None,
    output_path: str | None = None,
    output_size_bytes: int | None = None,
    actual_duration_seconds: float | None = None,
    width: int | None = None,
    height: int | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    pixel_format: str | None = None,
    captions_applied: bool | None = None,
    caption_count: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    expected_worker_id: str | None = None,
    now=None,
) -> FormatRecord | None:
    """Update a formatted clip.

    ``expected_worker_id`` makes the write conditional on the artifact
    still being owned by that worker.
    """
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (format_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        if expected_worker_id is not None and row["worker_id"] != expected_worker_id:
            conn.execute("COMMIT")
            return None

        current = ClipStatus(row["status"])
        new_status = (
            status
            if status is None
            else (status if isinstance(status, ClipStatus) else ClipStatus(status))
        )
        if new_status is not current:
            assert_clip_transition(current, new_status)

        if stage is not None:
            new_stage = (
                stage if isinstance(stage, ClipStage) else ClipStage(stage)
            )
        else:
            new_stage = ClipStage(row["stage"])
        new_progress = progress
        if new_progress is None:
            new_progress = CLIP_STAGE_PROGRESS.get(new_stage, row["progress"])
        new_progress = max(0, min(100, int(new_progress)))

        if clear_error:
            new_code = None
            new_message = None
        elif error_code is not None or error_message is not None:
            new_code = error_code if error_code is not None else row["error_code"]
            new_message = (
                error_message if error_message is not None else row["error_message"]
            )
        else:
            new_code = row["error_code"]
            new_message = row["error_message"]

        completed_at = row["completed_at"]
        if new_status in (ClipStatus.COMPLETED, ClipStatus.FAILED):
            completed_at = now_iso

        conn.execute(
            """
            UPDATE clip_formats
               SET status = ?, stage = ?, progress = ?, output_path = ?,
                   output_size_bytes = ?, actual_duration_seconds = ?,
                   width = ?, height = ?, video_codec = ?, audio_codec = ?,
                   pixel_format = ?, captions_applied = ?, caption_count = ?,
                   error_code = ?, error_message = ?, completed_at = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                new_status.value,
                new_stage.value,
                new_progress,
                output_path if output_path is not None else row["output_path"],
                output_size_bytes
                if output_size_bytes is not None
                else row["output_size_bytes"],
                actual_duration_seconds
                if actual_duration_seconds is not None
                else row["actual_duration_seconds"],
                width if width is not None else row["width"],
                height if height is not None else row["height"],
                video_codec if video_codec is not None else row["video_codec"],
                audio_codec if audio_codec is not None else row["audio_codec"],
                pixel_format if pixel_format is not None else row["pixel_format"],
                int(captions_applied)
                if captions_applied is not None
                else row["captions_applied"],
                caption_count if caption_count is not None else row["caption_count"],
                new_code,
                new_message,
                completed_at,
                now_iso,
                format_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (format_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_format(updated)


def reset_format_for_retry(
    conn: sqlite3.Connection,
    format_id: str,
    *,
    now=None,
) -> FormatRecord | None:
    """Put a failed formatted clip back on the queue."""
    now_iso = to_iso(now or utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (format_id,)
        ).fetchone()
        if row is None or row["status"] != ClipStatus.FAILED.value:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE clip_formats
               SET status = 'queued', stage = 'queued', progress = 0,
                   error_code = NULL, error_message = NULL, worker_id = NULL,
                   lease_expires_at = NULL, completed_at = NULL, updated_at = ?
             WHERE id = ?
            """,
            (now_iso, format_id),
        )
        updated = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (format_id,)
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _row_to_format(updated)


def recover_expired_format_leases(
    conn: sqlite3.Connection,
    *,
    max_attempts: int,
    now=None,
) -> list[FormatRecord]:
    """Requeue or fail formatted clips abandoned by a worker that died."""
    now_iso = to_iso(now or utcnow())
    recovered: list[FormatRecord] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM clip_formats "
            "WHERE status IN ('preparing', 'rendering', 'verifying') "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (now_iso,),
        ).fetchall()
        for row in rows:
            if row["attempt_count"] >= max_attempts:
                conn.execute(
                    """
                    UPDATE clip_formats
                       SET status = 'failed', stage = 'failed', progress = 0,
                           error_code = ?, error_message = ?,
                           completed_at = ?, updated_at = ?, worker_id = NULL,
                           lease_expires_at = NULL
                     WHERE id = ?
                    """,
                    (
                        "max_attempts_exceeded",
                        "The formatted clip was abandoned by a worker too many "
                        "times.",
                        now_iso,
                        now_iso,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE clip_formats
                       SET status = 'queued', stage = 'queued', progress = 0,
                           error_code = ?, error_message = ?,
                           updated_at = ?, worker_id = NULL, lease_expires_at = NULL
                     WHERE id = ?
                    """,
                    (
                        "lease_expired",
                        "The worker's lease on this formatted clip expired "
                        "while it was rendering. It was queued again; a "
                        "running worker will pick it up.",
                        now_iso,
                        row["id"],
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM clip_formats WHERE id = ?", (row["id"],)
            ).fetchone()
            recovered.append(_row_to_format(updated))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return recovered
