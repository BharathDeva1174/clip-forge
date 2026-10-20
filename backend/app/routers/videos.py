"""Video ingestion endpoints.

Both endpoints validate their input, create a persistent job record and
return ``202 Accepted`` immediately. Neither of them performs long-running
work: the media is validated with a bounded FFprobe metadata read, and the
rest of the pipeline happens in the separate worker process.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status

from app.acquisition import build_acquirer
from app.config import Settings
from app.db import create_job
from app.deps import get_db, get_settings, get_storage
from app.errors import ApiError, ErrorCode
from app.jobs import SourceType
from app.media import MediaError, inspect_media, validate_media
from app.schemas import JobResponse, YouTubeSubmission
from app.storage import Storage
from app.uploads import UploadError, receive_single_upload
from app.youtube import InvalidYouTubeUrl, parse_youtube_url

logger = logging.getLogger("clipforge.videos")

router = APIRouter(prefix="/api/v1/videos", tags=["videos"])


@router.post(
    "/upload",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a video file",
    description=(
        "Accepts a single video file as multipart/form-data. The file is "
        "streamed to disk with the configured size limit enforced while it is "
        "received, validated with FFprobe, and queued for the worker."
    ),
    responses={
        400: {"description": "The upload was empty or malformed."},
        413: {"description": "The file is larger than the configured limit."},
        415: {"description": "The file type or the media itself is not supported."},
    },
)
async def upload_video(
    request: Request,
    settings: Settings = Depends(get_settings),
    storage: Storage = Depends(get_storage),
    conn: sqlite3.Connection = Depends(get_db),
) -> JobResponse:
    """Store an uploaded video and queue it for processing."""
    try:
        received = await receive_single_upload(
            request,
            storage=storage,
            max_bytes=settings.max_upload_bytes,
            field_name="file",
            allowed_extensions=settings.allowed_extension_list,
        )
    except UploadError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc

    # From here on the file exists on disk. Every failure path removes it.
    try:
        info = inspect_media(
            received.path,
            ffprobe_path=settings.ffprobe_path,
            timeout_seconds=settings.ffprobe_timeout_seconds,
        )
        validate_media(
            info,
            max_duration_seconds=settings.max_video_duration_seconds,
            max_dimension=settings.max_video_dimension,
            allowed_codecs=settings.allowed_codec_list,
        )
    except MediaError as exc:
        received.path.unlink(missing_ok=True)
        raise ApiError(415, exc.code, exc.message) from exc

    storage.sources_dir.mkdir(parents=True, exist_ok=True)
    final_path = storage.sources_dir / storage.new_source_name(received.extension)
    try:
        shutil.move(str(received.path), str(final_path))
    except OSError as exc:
        received.path.unlink(missing_ok=True)
        logger.warning("could not move the uploaded file into the sources directory")
        raise ApiError(
            500,
            ErrorCode.INTERNAL_ERROR.value,
            "The uploaded file could not be stored.",
        ) from exc

    try:
        job = create_job(
            conn,
            job_id=uuid.uuid4().hex,
            source_type=SourceType.UPLOAD,
            source_reference=received.filename,
            source_path=storage.relative_to_root(final_path),
            original_filename=received.filename,
            file_size_bytes=received.size_bytes,
        )
    except Exception:
        # The file was accepted but the job could not be recorded. Do not leave
        # an orphaned file behind.
        final_path.unlink(missing_ok=True)
        raise

    logger.info(
        "queued upload job %s (%s bytes)", job.id, received.size_bytes
    )
    return JobResponse.from_record(job)


@router.post(
    "/youtube",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit an authorised YouTube URL",
    description=(
        "Validates a YouTube URL and queues a job that acquires and inspects "
        "the video in the worker. The caller must confirm they are authorised "
        "to download and reuse the video."
    ),
    responses={
        400: {"description": "The URL is not an accepted YouTube video URL."},
        503: {"description": "YouTube acquisition is not enabled in this installation."},
    },
)
def submit_youtube(
    payload: YouTubeSubmission,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db),
) -> JobResponse:
    """Validate a YouTube URL and queue it for acquisition."""
    if not payload.authorized:
        raise ApiError(
            400,
            ErrorCode.UNAUTHORIZED_SOURCE.value,
            "Confirm that you own this video or have permission to download "
            "and reuse it before submitting it.",
        )

    try:
        video = parse_youtube_url(
            payload.url, allowed_hosts=settings.youtube_host_set
        )
    except InvalidYouTubeUrl as exc:
        raise ApiError(400, exc.code, exc.message) from exc

    acquirer = build_acquirer(settings.youtube_acquisition_mode)
    if not acquirer.available():
        raise ApiError(
            503,
            ErrorCode.YOUTUBE_ACQUISITION_UNAVAILABLE.value,
            "Downloading from YouTube is not enabled in this installation. "
            "Upload a local copy of the video instead.",
        )

    job = create_job(
        conn,
        job_id=uuid.uuid4().hex,
        source_type=SourceType.YOUTUBE,
        source_reference=video.canonical_url,
    )
    logger.info("queued youtube job %s for video %s", job.id, video.video_id)
    return JobResponse.from_record(job)
