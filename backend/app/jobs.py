"""Job domain model: statuses, stages and the allowed state transitions.

The status set is finite and documented. Transitions between them are
validated centrally so that no code path can move a job into an impossible
state -- for example a ``completed`` job back to ``queued``.

Timestamps are always UTC. They are serialised as ISO 8601 with an explicit
offset (``2026-10-20T13:45:01.123456+00:00``) so that they sort correctly as
strings in SQLite and need no conversion in the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class SourceType(StrEnum):
    """Where the media came from."""

    UPLOAD = "upload"
    YOUTUBE = "youtube"


class JobStatus(StrEnum):
    """The finite set of job states."""

    QUEUED = "queued"
    VALIDATING = "validating"
    DOWNLOADING = "downloading"
    INSPECTING = "inspecting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    """The pipeline step a job is currently on.

    Finer grained than the status: ``COMPLETED`` always has the stage
    ``READY`` in Phase 2, because ingestion finished and the source is stored
    and validated. Later phases add their own stages.
    """

    QUEUED = "queued"
    VALIDATING = "validating"
    DOWNLOADING = "downloading"
    INSPECTING = "inspecting"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses a job never leaves on its own.
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

#: Statuses a job can be in while a worker owns it.
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.VALIDATING, JobStatus.DOWNLOADING, JobStatus.INSPECTING}
)

#: The only transitions the job API and the worker may perform.
#:
#: ``completed``/``failed``/``cancelled`` are terminal. Moving a job out of a
#: terminal status is only possible through the explicit recovery path in
#: :mod:`app.db`, never through an ordinary update.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.VALIDATING, JobStatus.DOWNLOADING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.VALIDATING: frozenset(
        {JobStatus.DOWNLOADING, JobStatus.INSPECTING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.DOWNLOADING: frozenset(
        {JobStatus.INSPECTING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.INSPECTING: frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

#: Progress values for each stage. Progress only ever moves forward.
PROGRESS_QUEUED = 0
PROGRESS_VALIDATING = 10
PROGRESS_DOWNLOADING = 30
PROGRESS_INSPECTING = 70
PROGRESS_READY = 100


class InvalidTransitionError(RuntimeError):
    """Raised when a job is asked to move to a status it cannot reach."""

    def __init__(self, current: JobStatus, new: JobStatus) -> None:
        super().__init__(f"cannot move a job from {current.value} to {new.value}")
        self.current = current
        self.new = new


@dataclass(frozen=True, slots=True)
class JobRecord:
    """A persisted job, as stored in SQLite."""

    id: str
    source_type: SourceType
    source_reference: str
    source_path: str | None
    status: JobStatus
    progress: int
    stage: JobStage
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    attempt_count: int
    error_code: str | None
    error_message: str | None
    worker_id: str | None
    lease_expires_at: str | None
    original_filename: str | None
    file_size_bytes: int | None
    video_duration_seconds: float | None
    video_width: int | None
    video_height: int | None
    video_codec: str | None
    video_format: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def can_transition(current: JobStatus, new: JobStatus) -> bool:
    """Return whether ``current -> new`` is an allowed transition."""
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: JobStatus, new: JobStatus) -> None:
    """Raise :class:`InvalidTransitionError` if the transition is not allowed."""
    if not can_transition(current, new):
        raise InvalidTransitionError(current, new)


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    """Serialise a datetime as a UTC ISO 8601 string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def utcnow_iso() -> str:
    """Current time as an ISO 8601 UTC string."""
    return to_iso(utcnow())
