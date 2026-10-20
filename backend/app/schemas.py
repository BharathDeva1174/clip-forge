"""Pydantic models for the Phase 2 API surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.jobs import JobRecord, JobStage, JobStatus, SourceType


class JobError(BaseModel):
    """A sanitised, user-facing error attached to a job."""

    code: str
    message: str


class JobResponse(BaseModel):
    """The public representation of a job.

    It deliberately contains no filesystem path, no worker identifier and no
    lease information: the client only needs to know what happened and what to
    show the user.
    """

    id: str
    source_type: SourceType
    source_reference: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    stage: JobStage
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt_count: int
    error: JobError | None = None
    file_size_bytes: int | None = None
    video_duration_seconds: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_codec: str | None = None

    @classmethod
    def from_record(cls, job: JobRecord) -> JobResponse:
        return cls(
            id=job.id,
            source_type=job.source_type,
            source_reference=job.source_reference,
            status=job.status,
            progress=job.progress,
            stage=job.stage,
            created_at=_parse(job.created_at),
            updated_at=_parse(job.updated_at),
            started_at=_parse(job.started_at),
            completed_at=_parse(job.completed_at),
            attempt_count=job.attempt_count,
            error=(
                JobError(code=job.error_code, message=job.error_message)
                if job.error_code and job.error_message
                else None
            ),
            file_size_bytes=job.file_size_bytes,
            video_duration_seconds=job.video_duration_seconds,
            video_width=job.video_width,
            video_height=job.video_height,
            video_codec=job.video_codec,
        )


class JobListResponse(BaseModel):
    """A bounded page of jobs, newest first."""

    items: list[JobResponse]
    total: int
    limit: int
    offset: int


class YouTubeSubmission(BaseModel):
    """A request to ingest a YouTube video the user is authorised to use."""

    url: str = Field(
        max_length=2048,
        description="A YouTube watch, short, embed or shorts URL.",
    )
    authorized: bool = Field(
        description=(
            "Must be true. Confirm that you own this video or have permission "
            "to download and reuse it."
        )
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)
