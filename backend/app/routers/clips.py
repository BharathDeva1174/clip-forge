"""The clip endpoints.

A clip is requested for a highlight candidate that already exists, so the
boundaries come from the persisted candidate and never from the request
body. The request enqueues the work and returns; the worker does the
rendering.

Phase 8 adds the editing of a clip. Trimming produces a **revision**: a new
clip with its own identity, its own file and its own lifecycle. The clip
that was trimmed is never touched, and the AI's candidate is never
rewritten to represent a user's edit.

Nothing here exposes a filesystem path, a command line or a raw FFmpeg
message.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from fastapi.responses import FileResponse

from app.clip_service import clip_output_path
from app.clips import (
    ClipInputError,
    ClipRecord,
    ClipStatus,
    validate_boundaries,
)
from app.config import Settings
from app.db import (
    cancel_job,
    count_clips,
    create_clip,
    create_clip_revision,
    get_clip,
    get_clip_for_candidate,
    get_clip_revision,
    get_highlight_candidate,
    get_job,
    get_transcript,
    list_clip_caption_edits,
    list_clips,
    list_transcript_segments,
    reset_clip_for_retry,
    set_clip_caption_edits,
    set_clip_title,
)
from app.deps import get_db, get_settings, get_storage
from app.errors import ApiError, ErrorCode
from app.export_service import build_export_archive, read_export_archive
from app.schemas import (
    ClipCaptionsRequest,
    ClipCaptionsResponse,
    ClipCreateRequest,
    ClipEditRequest,
    ClipListResponse,
    ClipResponse,
    ClipRetryResponse,
    ClipTitleRequest,
    ExportRequest,
)
from app.storage import Storage

router = APIRouter(prefix="/api/v1", tags=["clips"])

#: The largest page of clips a client may ask for.
MAX_PAGE_SIZE = 100

#: The longest title a clip may carry.
MAX_TITLE_LENGTH = 200


@router.post(
    "/jobs/{job_id}/clips",
    response_model=ClipResponse,
    status_code=202,
    summary="Request a clip for a highlight candidate",
    description=(
        "Queues the render. The boundaries are the candidate's own "
        "validated ones, read from the database: they are never taken from "
        "the request. Requesting the same candidate twice returns the clip "
        "that already exists instead of rendering it again."
    ),
    responses={
        404: {"description": "No job, or no candidate, with that id exists."},
        409: {"description": "The candidate cannot be rendered."},
    },
)
def request_clip(
    job_id: str,
    body: ClipCreateRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> ClipResponse:
    if get_job(conn, job_id) is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )

    candidate = get_highlight_candidate(conn, body.candidate_id)
    if candidate is None:
        raise ApiError(
            404,
            ErrorCode.NOT_FOUND.value,
            "No highlight candidate with that id exists.",
        )
    if candidate.job_id != job_id:
        # The candidate belongs to another job. Checking it here is what
        # stops a candidate id from being used against the wrong video.
        raise ApiError(
            409,
            ErrorCode.CLIP_CANDIDATE_MISMATCH.value,
            "That candidate belongs to a different job.",
        )

    existing = get_clip_for_candidate(conn, candidate.id)
    if existing is not None:
        # The clip already exists. A repeated request is answered with it
        # rather than rendering the same moment twice.
        return _response(conn, existing)

    clip = create_clip(
        conn,
        job_id=job_id,
        analysis_id=candidate.analysis_id,
        candidate_id=candidate.id,
        start_seconds=candidate.start_seconds,
        end_seconds=candidate.end_seconds,
    )
    return _response(conn, clip)


@router.get(
    "/jobs/{job_id}/clips",
    response_model=ClipListResponse,
    summary="List the clips of a job",
    responses={404: {"description": "No job with that id exists."}},
)
def list_job_clips(
    job_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> ClipListResponse:
    if get_job(conn, job_id) is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )
    items = list_clips(conn, job_id=job_id, limit=limit, offset=offset)
    return ClipListResponse(
        items=[_response(conn, clip) for clip in items],
        total=count_clips(conn, job_id=job_id),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/clips/{clip_id}/revisions",
    response_model=ClipResponse,
    status_code=202,
    summary="Trim a clip",
    description=(
        "Queues a **revision** of the clip: the same moment, trimmed to the "
        "given boundaries. The boundaries are on the source media timeline "
        "and are checked against the source's real duration before anything "
        "is queued.\n\n"
        "The revision is a new artifact with its own file and its own "
        "lifecycle. The clip that was trimmed keeps its own file and its own "
        "boundaries, and the AI's candidate is left exactly as the model "
        "made it. Trimming the same clip to the same boundaries again "
        "returns the revision that already exists."
    ),
    responses={
        404: {"description": "No clip with that id exists."},
        400: {"description": "The boundaries cannot be used."},
    },
)
def create_clip_revision_endpoint(
    clip_id: str,
    body: ClipEditRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ClipResponse:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    job = get_job(conn, clip.job_id)
    if job is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )

    try:
        start, end = validate_boundaries(
            body.start_seconds,
            body.end_seconds,
            # The source's real duration, as the worker measured it when it
            # inspected the media. It is the server's own number, not
            # something the request could have introduced.
            source_duration_seconds=job.video_duration_seconds,
            min_duration_seconds=settings.clip_min_duration_seconds,
            max_duration_seconds=settings.clip_max_duration_seconds,
            duration_tolerance_seconds=settings.clip_duration_tolerance_seconds,
        )
    except ClipInputError as exc:
        raise ApiError(400, exc.code, exc.message) from exc

    title = _clean_title(body.title)
    if title is None:
        # A revision starts with the title of the clip it came from, so it
        # reads the same until the user gives it a name of its own.
        title = _resolved_title(conn, clip)

    existing = get_clip_revision(
        conn, parent_clip_id=clip.id, start_seconds=start, end_seconds=end
    )
    if existing is not None:
        # The same trim of the same clip is the same revision.
        return _response(conn, existing)

    revision = create_clip_revision(
        conn,
        parent=clip,
        start_seconds=start,
        end_seconds=end,
        title=title,
    )
    return _response(conn, revision)


@router.patch(
    "/clips/{clip_id}",
    response_model=ClipResponse,
    summary="Relabel a clip",
    description=(
        "Sets the user's own label on a clip. A label is not a re-render: "
        "the file is not touched and the boundaries are not touched. The "
        "AI's candidate keeps its own title either way."
    ),
    responses={404: {"description": "No clip with that id exists."}},
)
def rename_clip(
    clip_id: str,
    body: ClipTitleRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> ClipResponse:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    title = _clean_title(body.title)
    updated = set_clip_title(conn, clip_id, title)
    if updated is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    return _response(conn, updated)


@router.put(
    "/clips/{clip_id}/captions",
    response_model=ClipCaptionsResponse,
    summary="Change the caption text of a clip",
    description=(
        "Replaces the caption text the user has changed on a clip. Each edit "
        "names a transcript segment, so the server matches it against the "
        "transcript it already holds rather than trusting the text.\n\n"
        "The captions are burned into the short-form version, so a change "
        "here produces a **different** short-form version: the edited text is "
        "part of the configuration, and two different texts never share a "
        "fingerprint."
    ),
    responses={
        404: {"description": "No clip with that id exists."},
        400: {"description": "An edit names a segment the transcript does not have."},
    },
)
def edit_clip_captions(
    clip_id: str,
    body: ClipCaptionsRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> ClipCaptionsResponse:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )

    segments = _transcript_segment_indexes(conn, clip.job_id)
    if segments is None:
        raise ApiError(
            400,
            ErrorCode.CLIP_CAPTION_EDIT_INVALID.value,
            "This job has no transcript, so its captions cannot be edited.",
        )

    edits: list[tuple[int, str]] = []
    for edit in body.edits:
        if edit.segment_index not in segments:
            raise ApiError(
                400,
                ErrorCode.CLIP_CAPTION_EDIT_INVALID.value,
                f"The transcript has no segment {edit.segment_index}.",
            )
        text = edit.text.strip()
        if not text:
            raise ApiError(
                400,
                ErrorCode.CLIP_CAPTION_EDIT_INVALID.value,
                "A caption cannot be empty.",
            )
        edits.append((edit.segment_index, text))

    stored = set_clip_caption_edits(conn, clip_id, edits)
    return ClipCaptionsResponse(
        clip_id=clip_id,
        edits=[
            {"segment_index": index, "text": text} for index, text in stored
        ],
    )


@router.post(
    "/jobs/{job_id}/exports",
    summary="Download a ZIP of some of a job's clips",
    description=(
        "Builds a ZIP of the chosen clips and streams it.\n\n"
        "Every clip has to belong to this job, has to have finished, and "
        "has to have a file. The archive is built for this request and is "
        "not kept, so there is no stale copy to clean up and no second "
        "storage scheme to look after."
    ),
    responses={
        404: {"description": "No job with that id exists."},
        409: {"description": "One of the clips cannot be exported."},
    },
)
def export_clips(
    job_id: str,
    body: ExportRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if get_job(conn, job_id) is None:
        raise ApiError(
            404, ErrorCode.NOT_FOUND.value, "No job was found with that id."
        )

    clips: list[ClipRecord] = []
    for clip_id in dict.fromkeys(body.clip_ids):
        clip = get_clip(conn, clip_id)
        if clip is None:
            raise ApiError(
                404,
                ErrorCode.CLIP_NOT_FOUND.value,
                "No clip with that id exists.",
            )
        if clip.job_id != job_id:
            # A clip from another job must not be exported through this one.
            raise ApiError(
                409,
                ErrorCode.CLIP_EXPORT_INVALID.value,
                "That clip belongs to a different job.",
            )
        if clip.status is not ClipStatus.COMPLETED or not clip.output_path:
            raise ApiError(
                409,
                ErrorCode.CLIP_EXPORT_UNAVAILABLE.value,
                "Only a finished clip can be exported.",
            )
        clips.append(clip)

    if len(clips) > settings.clip_export_max_items:
        raise ApiError(
            413,
            ErrorCode.CLIP_EXPORT_TOO_LARGE.value,
            f"An export may hold at most {settings.clip_export_max_items} clips.",
        )

    if not clips:
        raise ApiError(
            400,
            ErrorCode.CLIP_EXPORT_EMPTY.value,
            "Choose at least one clip to export.",
        )

    try:
        archive_path = build_export_archive(clips, storage, settings)
    except ValueError as exc:
        raise ApiError(
            413,
            ErrorCode.CLIP_EXPORT_TOO_LARGE.value,
            str(exc),
        )
    if archive_path is None:
        raise ApiError(
            409,
            ErrorCode.CLIP_EXPORT_UNAVAILABLE.value,
            "One of the clips has no file to export.",
        )
    # Stream the finished archive straight off disk instead of reading it
    # whole into memory: the response is a file response, so the archive is
    # never held in RAM a second time. The temporary file is removed once the
    # response has been sent.
    from pathlib import Path as _Path

    def _cleanup(path: _Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    background = BackgroundTasks()
    background.add_task(_cleanup, archive_path)
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"clipforge-{job_id[:8]}-clips.zip",
        background=background,
    )


@router.get(
    "/clips/{clip_id}",
    response_model=ClipResponse,
    summary="Read one clip",
    responses={404: {"description": "No clip with that id exists."}},
)
def read_clip(
    clip_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> ClipResponse:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    return _response(conn, clip)


@router.post(
    "/clips/{clip_id}/retry",
    response_model=ClipRetryResponse,
    summary="Render a failed clip again",
    description=(
        "Puts a failed clip back on the queue. A clip that has not failed "
        "is not retried, so a finished clip is never re-rendered by accident."
    ),
    responses={404: {"description": "No clip with that id exists."}},
)
def retry_clip(
    clip_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> ClipRetryResponse:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    if clip.status is not ClipStatus.FAILED:
        raise ApiError(
            409,
            ErrorCode.INVALID_STATE.value,
            "Only a failed clip can be rendered again.",
        )
    reset = reset_clip_for_retry(conn, clip_id)
    if reset is None:
        raise ApiError(
            409,
            ErrorCode.INVALID_STATE.value,
            "Only a failed clip can be rendered again.",
        )
    return ClipRetryResponse(clip=_response(conn, reset))


@router.get(
    "/clips/{clip_id}/media",
    summary="Play or download a finished clip",
    description=(
        "Streams the verified file. The path is the one the application "
        "stored, and it is resolved inside the storage root, so a clip can "
        "never be used to reach a file elsewhere on the machine."
    ),
    responses={
        404: {"description": "No clip with that id exists, or it has no file yet."},
    },
)
def clip_media(
    clip_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> Response:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    path = clip_output_path(storage, clip)
    if path is None:
        raise ApiError(
            404,
            ErrorCode.CLIP_NOT_FOUND.value,
            "This clip has no finished file yet.",
        )
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"clipforge-{clip.id}.mp4",
    )


def _clean_title(title: str | None) -> str | None:
    """The user's label for a clip, or nothing.

    A title is trimmed and shortened. An empty title is the same as no
    title: it means "use the one it already had".
    """
    if title is None:
        return None
    cleaned = " ".join(title.split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ApiError(
            400,
            ErrorCode.CLIP_EDIT_INVALID_TITLE.value,
            f"A title cannot be longer than {MAX_TITLE_LENGTH} characters.",
        )
    return cleaned


def _resolved_title(conn: sqlite3.Connection, clip: ClipRecord) -> str | None:
    """The title a clip is shown with.

    The user's own label wins. Otherwise it is the title the AI gave the
    moment, and for a revision the title of the clip it came from.
    """
    if clip.title:
        return clip.title
    if clip.candidate_id:
        candidate = get_highlight_candidate(conn, clip.candidate_id)
        if candidate is not None:
            return candidate.title
    if clip.parent_clip_id:
        parent = get_clip(conn, clip.parent_clip_id)
        if parent is not None:
            return _resolved_title(conn, parent)
    return None


def _transcript_segment_indexes(
    conn: sqlite3.Connection, job_id: str
) -> set[int] | None:
    """The segment indexes of a job's transcript, when it has one.

    ``None`` means the job has no transcript, so there are no captions to
    edit. The set is what an edit is checked against: a caption that names
    a segment the transcript does not have is refused.
    """
    transcript = get_transcript(conn, job_id)
    if transcript is None:
        return None
    return {
        int(row.segment_index)
        for row in list_transcript_segments(conn, transcript.id)
    }


def _response(conn: sqlite3.Connection, clip: ClipRecord) -> ClipResponse:
    """Build the response for a clip.

    The output path is not included. The frontend plays the clip through
    the media route, so it never needs to know where the file is.
    """
    source_title = None
    if clip.candidate_id:
        candidate = get_highlight_candidate(conn, clip.candidate_id)
        if candidate is not None:
            source_title = candidate.title

    return ClipResponse(
        id=clip.id,
        job_id=clip.job_id,
        analysis_id=clip.analysis_id,
        candidate_id=clip.candidate_id,
        status=clip.status.value,
        stage=clip.stage.value,
        progress=clip.progress,
        start_seconds=clip.start_seconds,
        end_seconds=clip.end_seconds,
        duration_seconds=clip.duration_seconds,
        actual_duration_seconds=clip.actual_duration_seconds,
        output_size_bytes=clip.output_size_bytes,
        width=clip.width,
        height=clip.height,
        video_codec=clip.video_codec,
        audio_codec=clip.audio_codec,
        attempt_count=clip.attempt_count,
        created_at=clip.created_at,
        updated_at=clip.updated_at,
        started_at=clip.started_at,
        completed_at=clip.completed_at,
        error=(
            {"code": clip.error_code, "message": clip.error_message or ""}
            if clip.error_code
            else None
        ),
        media_url=(
            f"/api/v1/clips/{clip.id}/media" if clip.is_playable else None
        ),
        parent_clip_id=clip.parent_clip_id,
        is_revision=clip.is_revision,
        title=_resolved_title(conn, clip),
        source_title=source_title,
        caption_edits=[
            {"segment_index": index, "text": text}
            for index, text in list_clip_caption_edits(conn, clip.id)
        ],
    )
