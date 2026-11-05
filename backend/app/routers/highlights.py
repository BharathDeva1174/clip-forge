"""The highlight analysis endpoints.

Starting an analysis queues work for the worker; it never calls a provider
from a request handler. Reading one reports what the worker has found so
far, and accepting or rejecting a candidate records the user's decision.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.db import (
    active_analysis_for_job,
    create_analysis,
    get_default_integration,
    get_integration,
    get_job,
    get_transcript,
    latest_analysis_for_job,
    list_candidates,
    set_candidate_decision,
)
from app.deps import get_db
from app.errors import ApiError, ErrorCode
from app.highlight_prompt import (
    HIGHLIGHT_PROMPT_VERSION,
    HIGHLIGHT_SCHEMA_VERSION,
)
from app.integrations import IntegrationRole
from app.providers import get_preset
from app.schemas import (
    CandidateDecisionRequest,
    HighlightAnalysisResponse,
    HighlightCandidateResponse,
)

router = APIRouter(prefix="/api/v1/jobs", tags=["highlights"])

#: The notice shown before an analysis runs.
#:
#: The provider receives the transcript text. What it does with it is the
#: provider's business, and ClipForge does not know it. The notice says so
#: rather than implying that nothing is retained.
DATA_NOTICE = (
    "The selected provider receives the transcript text of this video and "
    "processes it under its own policies. ClipForge does not send the "
    "video or the audio."
)


@router.post(
    "/{job_id}/highlights",
    response_model=HighlightAnalysisResponse,
    status_code=202,
    summary="Start a highlight analysis",
    description=(
        "Queues the analysis for the worker. The transcript is read from "
        "the database, never from the request. Starting an analysis when "
        "one is already running returns that one instead of starting a "
        "second."
    ),
    responses={
        404: {"description": "No job with that id exists."},
        409: {"description": "The job cannot be analysed, or no integration is configured."},
    },
)
def start_analysis(
    job_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HighlightAnalysisResponse:
    job = get_job(conn, job_id)
    if job is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )

    transcript = get_transcript(conn, job_id)
    if transcript is None:
        raise ApiError(
            409,
            ErrorCode.HIGHLIGHT_NO_TRANSCRIPT.value,
            "This job has no transcript, so there is nothing to analyse.",
        )

    integration = get_default_integration(
        conn, IntegrationRole.HIGHLIGHT_DETECTION
    )
    if integration is None:
        # A default that the user disabled is reported as disabled, not as
        # missing: the difference matters to them.
        disabled = get_default_integration(
            conn, IntegrationRole.HIGHLIGHT_DETECTION, include_disabled=True
        )
        if disabled is not None:
            raise ApiError(
                409,
                ErrorCode.INTEGRATION_DISABLED.value,
                "The selected integration is disabled.",
            )
        raise ApiError(
            409,
            ErrorCode.INTEGRATION_NOT_CONFIGURED.value,
            "No integration is selected for highlight detection. Choose one "
            "on the Integrations page.",
        )
    if not integration.enabled:
        raise ApiError(
            409,
            ErrorCode.INTEGRATION_DISABLED.value,
            "The selected integration is disabled.",
        )
    if not integration.protocol_is_implemented:
        raise ApiError(
            409,
            ErrorCode.AI_NOT_SUPPORTED.value,
            "ClipForge cannot speak the protocol this integration uses.",
        )
    if not integration.model_id:
        raise ApiError(
            409,
            ErrorCode.INTEGRATION_INVALID.value,
            "The selected integration has no model, so it cannot be used.",
        )

    existing = active_analysis_for_job(conn, job_id)
    if existing is not None:
        # One analysis at a time. Polling must not start a second one.
        return _response(conn, job_id, existing)

    analysis = create_analysis(
        conn,
        job_id=job_id,
        integration_id=integration.id,
        provider_id=integration.provider_id,
        protocol=integration.protocol.value,
        base_url=integration.base_url,
        model_id=integration.model_id or "",
        model_name=integration.model_name,
        prompt_version=HIGHLIGHT_PROMPT_VERSION,
        schema_version=HIGHLIGHT_SCHEMA_VERSION,
        transcript_id=transcript.id,
        source_duration_seconds=transcript.duration_seconds,
    )
    return _response(conn, job_id, analysis)


@router.get(
    "/{job_id}/highlights",
    response_model=HighlightAnalysisResponse,
    summary="Read the analysis and its candidates",
    responses={404: {"description": "No job with that id exists."}},
)
def read_analysis(
    job_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> HighlightAnalysisResponse:
    if get_job(conn, job_id) is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )
    analysis = latest_analysis_for_job(conn, job_id)
    if analysis is None:
        return HighlightAnalysisResponse(
            job_id=job_id, status="none", data_notice=DATA_NOTICE
        )
    return _response(conn, job_id, analysis, limit=limit, offset=offset)


@router.patch(
    "/{job_id}/highlights/{candidate_id}",
    response_model=HighlightAnalysisResponse,
    summary="Accept or reject a candidate",
    responses={
        404: {"description": "No job, or no candidate, with that id exists."},
    },
)
def decide(
    job_id: str,
    candidate_id: str,
    body: CandidateDecisionRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HighlightAnalysisResponse:
    if get_job(conn, job_id) is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )
    analysis = latest_analysis_for_job(conn, job_id)
    if analysis is None:
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "This job has no highlight analysis.",
        )
    updated = set_candidate_decision(
        conn, analysis.id, candidate_id, body.decision
    )
    if updated is None:
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "No candidate with that id belongs to this analysis.",
        )
    return _response(conn, job_id, analysis)


def _response(
    conn: sqlite3.Connection,
    job_id: str,
    analysis,
    *,
    limit: int = 100,
    offset: int = 0,
) -> HighlightAnalysisResponse:
    """Build the response for an analysis."""
    candidates = list_candidates(conn, analysis.id)
    page = candidates[offset : offset + limit]

    provider_name = None
    if analysis.integration_id:
        integration = get_integration(conn, analysis.integration_id)
        if integration is not None:
            preset = get_preset(integration.provider_id)
            provider_name = preset.name if preset else integration.provider_id

    return HighlightAnalysisResponse(
        job_id=job_id,
        analysis_id=analysis.id,
        status=analysis.status,
        provider_name=provider_name,
        model_id=analysis.model_id,
        model_name=analysis.model_name,
        prompt_version=analysis.prompt_version,
        candidate_count=len(candidates),
        candidates=[
            HighlightCandidateResponse.from_candidate(candidate)
            for candidate in page
        ],
        error=(
            {
                "code": analysis.error_code,
                "message": analysis.error_message or "",
            }
            if analysis.error_code
            else None
        ),
        data_notice=DATA_NOTICE,
    )
