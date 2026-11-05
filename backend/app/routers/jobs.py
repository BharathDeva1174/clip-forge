"""Job status, listing and transcript endpoints."""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3

from fastapi import APIRouter, Depends, Query

from app.db import (
    cancel_job,
    count_jobs,
    delete_job,
    get_job,
    get_transcript,
    job_artifacts,
    job_deletion_artifacts,
    job_ids_with_transcripts,
    list_jobs,
    list_transcript_segments,
    snapshot,
)
from app.deps import get_db, get_storage
from app.errors import ApiError, ErrorCode
from app.jobs import JobRecord, JobStatus
from app.schemas import (
    JobCancelResponse,
    JobListResponse,
    JobResponse,
    TranscriptResponse,
)
from app.storage import Storage, StorageEscapeError

logger = logging.getLogger("clipforge.jobs")

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

#: Largest page a client may ask for.
MAX_PAGE_SIZE = 200

#: Largest page of transcript segments a client may ask for.
MAX_TRANSCRIPT_PAGE_SIZE = 1000

#: What a job identifier may look like. Job ids are hexadecimal strings, but
#: the check is deliberately loose so that an unknown-but-well-formed id is a
#: 404 rather than a 400.
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.get(
    "",
    response_model=JobListResponse,
    summary="List recent jobs",
    description="Returns the most recent jobs, newest first.",
)
def list_recent_jobs(
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE, description="Page size."),
    offset: int = Query(0, ge=0, description="Number of jobs to skip."),
    conn: sqlite3.Connection = Depends(get_db),
) -> JobListResponse:
    items = list_jobs(conn, limit=limit, offset=offset)
    with_transcripts = job_ids_with_transcripts(conn, [job.id for job in items])
    # The counts are gathered for the whole page in one go, so a dashboard
    # showing fifty jobs does not make fifty requests.
    artifacts = job_artifacts(conn, [job.id for job in items])
    return JobListResponse(
        items=[
            JobResponse.from_record(
                job,
                has_transcript=job.id in with_transcripts,
                artifacts=artifacts.get(job.id),
            )
            for job in items
        ],
        total=count_jobs(conn),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get one job",
    description="Returns the status, progress and stage of a single job.",
    responses={404: {"description": "No job with that id exists."}},
)
def read_job(
    job_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> JobResponse:
    job: JobRecord | None = get_job(conn, job_id)
    if job is None:
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "No job was found with that id.",
        )
    has_transcript = job.id in job_ids_with_transcripts(conn, [job.id])
    return JobResponse.from_record(
        job,
        has_transcript=has_transcript,
        artifacts=job_artifacts(conn, [job.id]).get(job.id),
    )


@router.post(
    "/{job_id}/cancel",
    response_model=JobCancelResponse,
    summary="Cancel a job",
    description=(
        "Cancels a job that has not finished.\n\n"
        "A job that is still waiting is simply stopped: the worker will "
        "never claim it. A job that a worker is working on is interrupted — "
        "a download in progress is stopped and the temporary file is "
        "removed — and the worker does not put it back on the queue, because "
        "a cancelled job is not to be run.\n\n"
        "A job that has already finished cannot be cancelled: it is a real "
        "outcome, and a later request does not undo it. Cancelling an "
        "already cancelled job is harmless."
    ),
    responses={
        404: {"description": "No job with that id exists."},
        409: {"description": "The job has already finished."},
    },
)
def cancel_job_endpoint(
    job_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> JobCancelResponse:
    job = get_job(conn, job_id)
    if job is None:
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "No job was found with that id.",
        )
    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
        raise ApiError(
            409,
            ErrorCode.JOB_NOT_CANCELLABLE.value,
            "This job has already finished, so it cannot be cancelled.",
        )

    cancelled = cancel_job(conn, job_id)
    if cancelled is None:
        raise ApiError(
            409,
            ErrorCode.JOB_NOT_CANCELLABLE.value,
            "This job has already finished, so it cannot be cancelled.",
        )
    has_transcript = cancelled.id in job_ids_with_transcripts(conn, [cancelled.id])
    return JobCancelResponse(
        job=JobResponse.from_record(
            cancelled,
            has_transcript=has_transcript,
            artifacts=job_artifacts(conn, [cancelled.id]).get(cancelled.id),
        )
    )


@router.delete(
    "/{job_id}",
    status_code=204,
    response_model=None,
    summary="Delete a finished job",
    description=(
        "Removes a job and everything that belongs to it: the transcript "
        "and its segments, the highlight analyses and their candidates, the "
        "clips, the formatted clips and the caption edits, together with the "
        "files they produced on this machine.\n\n"
        "A job that a worker could still be working on is refused — cancel "
        "it first. A job that has already finished (completed, failed or "
        "cancelled) is removed for good; this is not undoable."
    ),
    responses={
        404: {"description": "No job with that id exists."},
        409: {"description": "The job has not finished, so it cannot be deleted."},
    },
)
def delete_job_endpoint(
    job_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> None:
    if not JOB_ID_PATTERN.match(job_id):
        raise ApiError(
            400,
            ErrorCode.INVALID_REQUEST.value,
            "The job id is not a valid identifier.",
        )

    # The file paths are read before the rows are gone: after the delete,
    # nothing records where the files were.
    paths, format_ids = job_deletion_artifacts(conn, job_id)

    outcome = delete_job(conn, job_id)
    if outcome == "not_found":
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "No job was found with that id.",
        )
    if outcome == "not_deletable":
        raise ApiError(
            409,
            ErrorCode.JOB_NOT_DELETABLE.value,
            "This job has not finished, so it cannot be deleted. Cancel it "
            "first, and delete it once it is cancelled.",
        )

    # The database rows are gone; reclaim the files. A file that has already
    # disappeared, or a path that fails the containment check, is logged and
    # skipped -- the deletion itself has already succeeded.
    for relative in paths:
        try:
            path = storage.resolve(relative)
        except StorageEscapeError:
            logger.warning(
                "job %s: stored path %r is outside the storage root; left alone",
                job_id,
                relative,
            )
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "job %s: the file %r could not be removed",
                job_id,
                relative,
                exc_info=True,
            )

    # The scratch directories: the downloader's and one per format attempt.
    shutil.rmtree(storage.temp_dir / f"yt-{job_id}", ignore_errors=True)
    for format_id in format_ids:
        for directory in storage.temp_dir.glob(f"format-{format_id}-*"):
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)

    logger.info("job %s was deleted, with %s files reclaimed", job_id, len(paths))
    return None


@router.get(
    "/{job_id}/transcript",
    response_model=TranscriptResponse,
    summary="Get the transcript of a job",
    description=(
        "Returns the stored transcript of a job together with its timestamped "
        "segments, in order.\n\n"
        "The response status says what to expect: `ready` when the transcript "
        "is stored, `pending` while the job is still being processed, and "
        "`failed` when the job failed. A job that is not finished yet has no "
        "segments.\n\n"
        "Long transcripts are paginated. `segment_count` is the total number "
        "of segments; ask for the next page with `offset` until every segment "
        "has been read."
    ),
    responses={
        400: {"description": "The job id is not a valid identifier."},
        404: {"description": "No job with that id exists."},
    },
)
def read_transcript(
    job_id: str,
    limit: int = Query(
        500, ge=1, le=MAX_TRANSCRIPT_PAGE_SIZE, description="Page size."
    ),
    offset: int = Query(0, ge=0, description="Number of segments to skip."),
    conn: sqlite3.Connection = Depends(get_db),
) -> TranscriptResponse:
    """Return the transcript of a job and the state it is in."""
    if not JOB_ID_PATTERN.match(job_id):
        raise ApiError(
            400,
            ErrorCode.INVALID_REQUEST.value,
            "The job id is not a valid identifier.",
        )

    job = get_job(conn, job_id)
    if job is None:
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "No job was found with that id.",
        )

    # The job and its transcript are read as one snapshot, so a poll that
    # lands while the worker is committing never sees a half finished state.
    with snapshot(conn):
        transcript = get_transcript(conn, job_id)
        if transcript is None:
            # No transcript: the job either failed or has not reached the
            # transcription stage yet.
            if job.status is JobStatus.FAILED:
                raise ApiError(
                    409,
                    job.error_code or ErrorCode.INVALID_STATE.value,
                    job.error_message
                    or "The job failed before its transcript could be produced.",
                )
            return TranscriptResponse(
                job_id=job.id,
                status="pending",
                job_status=job.status,
                job_stage=job.stage,
                limit=limit,
                offset=offset,
            )

        segments = list_transcript_segments(
            conn, transcript.id, limit=limit, offset=offset
        )
        return TranscriptResponse.from_records(
            job, transcript, segments, limit=limit, offset=offset
        )
