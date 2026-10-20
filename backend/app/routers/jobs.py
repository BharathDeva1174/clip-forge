"""Job status and listing endpoints."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from app.db import count_jobs, get_job, list_jobs
from app.deps import get_db
from app.errors import ApiError, ErrorCode
from app.jobs import JobRecord
from app.schemas import JobListResponse, JobResponse

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

#: Largest page a client may ask for.
MAX_PAGE_SIZE = 200


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
    return JobListResponse(
        items=[JobResponse.from_record(job) for job in items],
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
    return JobResponse.from_record(job)
