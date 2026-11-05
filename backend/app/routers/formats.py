"""The formatted-clip endpoints.

A formatted clip is requested for an existing Phase 5 clip. The request
carries the formatting settings and nothing else: the boundaries come from
the clip, which came from the highlight candidate, which came from the
transcript.

The work is queued, never done here. A render takes seconds, and a request
handler must not wait for one.

Nothing in this router exposes a filesystem path, a command line or a raw
FFmpeg message.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import FileResponse

from app.clips import ClipStatus
from app.db import (
    count_formats,
    create_format,
    get_clip,
    get_format,
    get_format_for_fingerprint,
    list_clip_caption_edits,
    list_formats,
    reset_format_for_retry,
)
from app.deps import get_db, get_storage
from app.errors import ApiError, ErrorCode
from app.fonts import DEFAULT_FONT_FAMILY, list_font_families
from app.formatting import (
    CaptionSettings,
    FormatSettingError,
    FormatSettings,
    LayoutMode,
    configuration_fingerprint,
    is_usable_font_name,
)
from app.formats import FormatRecord
from app.schemas import (
    FontListResponse,
    FormatCreateRequest,
    FormatListResponse,
    FormatResponse,
    FormatRetryResponse,
)
from app.storage import Storage

router = APIRouter(prefix="/api/v1", tags=["formats"])

#: The largest page of formatted clips a client may ask for.
MAX_PAGE_SIZE = 100


@router.get(
    "/fonts",
    response_model=FontListResponse,
    summary="The fonts this machine has",
    description=(
        "The font families that are installed, read out of the font files "
        "themselves. A font that is not listed may still work; a listed "
        "font may still be a poor choice for a phone screen."
    ),
)
def list_fonts() -> FontListResponse:
    # Only the fonts that could actually be selected are offered: the
    # caption style is a comma-separated list, so a name with a comma in it
    # cannot be expressed.
    usable = [
        name for name in list_font_families() if is_usable_font_name(name)
    ]
    return FontListResponse(items=usable, default=DEFAULT_FONT_FAMILY)


@router.post(
    "/clips/{clip_id}/formats",
    response_model=FormatResponse,
    summary="Request a formatted clip",
    description=(
        "Queues the render. Requesting the same clip with the same "
        "settings again returns the artifact that already exists rather "
        "than rendering it twice. Changing a setting produces a different "
        "artifact, and the finished one is kept."
    ),
    responses={
        404: {"description": "No clip with that id exists."},
        409: {"description": "The clip cannot be formatted."},
    },
)
def request_format(
    clip_id: str,
    body: FormatCreateRequest,
    response: Response,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> FormatResponse:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    if clip.status is not ClipStatus.COMPLETED or not clip.output_path:
        raise ApiError(
            409,
            ErrorCode.FORMAT_SOURCE_MISSING.value,
            "This clip has not been rendered yet, so there is nothing to "
            "format.",
        )

    settings = _settings_from_request(body)
    # The caption text the user has changed on this clip is part of the
    # configuration: two different texts produce two different videos, so
    # they must not share a fingerprint.
    caption_edits = list_clip_caption_edits(conn, clip_id)
    fingerprint = configuration_fingerprint(settings, caption_edits)

    existing = get_format_for_fingerprint(conn, clip_id, fingerprint)
    if existing is not None:
        # The artifact for these settings already exists. It is returned
        # rather than rendered again.
        response.status_code = 200
        return _response(existing)

    artifact = create_format(
        conn,
        clip_id=clip_id,
        job_id=clip.job_id,
        config_fingerprint=fingerprint,
        settings_json=json.dumps(settings.normalised().to_dict(), sort_keys=True),
        captions_enabled=settings.captions.enabled,
    )
    response.status_code = 202
    return _response(artifact)


@router.get(
    "/clips/{clip_id}/formats",
    response_model=FormatListResponse,
    summary="List the formatted clips of a clip",
    responses={404: {"description": "No clip with that id exists."}},
)
def list_clip_formats(
    clip_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> FormatListResponse:
    if get_clip(conn, clip_id) is None:
        raise ApiError(
            404, ErrorCode.CLIP_NOT_FOUND.value, "No clip with that id exists."
        )
    items = list_formats(conn, clip_id=clip_id, limit=limit, offset=offset)
    return FormatListResponse(
        items=[_response(item) for item in items],
        total=count_formats(conn, clip_id=clip_id),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/formats/{format_id}",
    response_model=FormatResponse,
    summary="Read one formatted clip",
    responses={404: {"description": "No formatted clip with that id exists."}},
)
def read_format(
    format_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> FormatResponse:
    artifact = get_format(conn, format_id)
    if artifact is None:
        raise ApiError(
            404,
            ErrorCode.FORMAT_NOT_FOUND.value,
            "No formatted clip with that id exists.",
        )
    return _response(artifact)


@router.post(
    "/formats/{format_id}/retry",
    response_model=FormatRetryResponse,
    summary="Render a failed formatted clip again",
    description=(
        "Puts a failed artifact back on the queue, with the settings it "
        "was created with. An artifact that has not failed is not retried."
    ),
    responses={404: {"description": "No formatted clip with that id exists."}},
)
def retry_format(
    format_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> FormatRetryResponse:
    artifact = get_format(conn, format_id)
    if artifact is None:
        raise ApiError(
            404,
            ErrorCode.FORMAT_NOT_FOUND.value,
            "No formatted clip with that id exists.",
        )
    if artifact.status is not ClipStatus.FAILED:
        raise ApiError(
            409,
            ErrorCode.INVALID_STATE.value,
            "Only a failed formatted clip can be rendered again.",
        )
    reset = reset_format_for_retry(conn, format_id)
    if reset is None:
        raise ApiError(
            409,
            ErrorCode.INVALID_STATE.value,
            "Only a failed formatted clip can be rendered again.",
        )
    return FormatRetryResponse(format=_response(reset))


@router.get(
    "/formats/{format_id}/media",
    summary="Play or download a finished formatted clip",
    description=(
        "Streams the verified file. The path is the one the application "
        "stored, and it is resolved inside the storage root, so a "
        "formatted clip can never be used to reach a file elsewhere on the "
        "machine."
    ),
    responses={
        404: {"description": "No formatted clip with that id exists, or it has no file yet."},
    },
)
def format_media(
    format_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> Response:
    artifact = get_format(conn, format_id)
    if artifact is None:
        raise ApiError(
            404,
            ErrorCode.FORMAT_NOT_FOUND.value,
            "No formatted clip with that id exists.",
        )
    path = _output_path(storage, artifact)
    if path is None:
        raise ApiError(
            404,
            ErrorCode.FORMAT_NOT_FOUND.value,
            "This formatted clip has no finished file yet.",
        )
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"clipforge-{artifact.id}.mp4",
    )


def _output_path(storage: Storage, artifact: FormatRecord):
    """The file of a finished artifact, when it has one."""
    from app.clip_service import clip_output_path

    return clip_output_path(storage, artifact)


def _settings_from_request(body: FormatCreateRequest) -> FormatSettings:
    """Build the settings from a request and validate them.

    Raises:
        ApiError: with a stable code when a setting cannot be used.
    """
    captions = body.settings.captions
    try:
        settings = FormatSettings(
            width=body.settings.width,
            height=body.settings.height,
            layout=LayoutMode(body.settings.layout),
            padding_colour=body.settings.padding_colour,
            captions=CaptionSettings(
                enabled=captions.enabled,
                font_family=captions.font_family,
                font_size=captions.font_size,
                text_colour=captions.text_colour,
                outline_colour=captions.outline_colour,
                outline_thickness=captions.outline_thickness,
                background_colour=captions.background_colour,
                shadow=captions.shadow,
                alignment=captions.alignment,
                margin_vertical=captions.margin_vertical,
                max_characters_per_line=captions.max_characters_per_line,
                max_lines=captions.max_lines,
            ),
        ).validate()
    except FormatSettingError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    except ValueError as exc:
        raise ApiError(
            400,
            ErrorCode.FORMAT_INVALID_SETTINGS.value,
            "Those settings cannot be used.",
        ) from exc
    return settings


def _response(artifact: FormatRecord) -> FormatResponse:
    """Build the response for a formatted clip.

    The output path is not included: the frontend plays the artifact
    through the media route.
    """
    try:
        settings = json.loads(artifact.settings_json)
    except (json.JSONDecodeError, TypeError):
        settings = {}
    summary = settings if isinstance(settings, dict) else {}

    return FormatResponse(
        id=artifact.id,
        clip_id=artifact.clip_id,
        job_id=artifact.job_id,
        status=artifact.status.value,
        stage=artifact.stage.value,
        progress=artifact.progress,
        settings=summary,
        config_fingerprint=artifact.config_fingerprint,
        output_size_bytes=artifact.output_size_bytes,
        actual_duration_seconds=artifact.actual_duration_seconds,
        width=artifact.width,
        height=artifact.height,
        video_codec=artifact.video_codec,
        audio_codec=artifact.audio_codec,
        pixel_format=artifact.pixel_format,
        captions_enabled=artifact.captions_enabled,
        captions_applied=artifact.captions_applied,
        caption_count=artifact.caption_count,
        attempt_count=artifact.attempt_count,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
        started_at=artifact.started_at,
        completed_at=artifact.completed_at,
        error=(
            {"code": artifact.error_code, "message": artifact.error_message or ""}
            if artifact.error_code
            else None
        ),
        media_url=(
            f"/api/v1/formats/{artifact.id}/media" if artifact.is_playable else None
        ),
    )
