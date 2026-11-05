"""Liveness and readiness probes.

``/health`` is a deliberately tiny, stable contract that the frontend polls.
``/ready`` reports whether the current configuration can be loaded and states
plainly which capabilities are *not* implemented yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ValidationError
from pydantic_settings import SettingsError

from app.config import API_VERSION, SERVICE_NAME, Settings
from app.transcription import (
    TranscriptionSettings,
    model_is_cached,
    resolve_cache_dir,
)
from app.worker_heartbeat import read_heartbeat

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Stable liveness contract.

    The field names and values are part of the public API contract: the
    frontend asserts on them, so they must not change without a version bump.
    """

    status: Literal["ok"] = "ok"
    service: Literal["clipforge-api"] = SERVICE_NAME


class Capabilities(BaseModel):
    """Feature flags for the capabilities of the application.

    A flag is ``True`` only when the capability is implemented *and* usable.
    They exist so that a client can tell the difference between "not
    implemented yet" and "not available".

    The flags follow the implementation. The highlight detection, the clip
    rendering and the vertical formatting were built in Phases 4 to 6, so
    they are reported as available now -- a flag that stayed ``False``
    after the feature was shipped would tell the user the opposite of the
    truth.
    """

    video_ingestion: bool = True
    background_worker: bool = True
    database: bool = True
    persistent_storage: bool = True
    audio_extraction: bool = True
    transcription: bool = True
    llm_highlight_detection: bool = True
    clip_rendering: bool = True
    vertical_formatting: bool = True
    subtitle_generation: bool = True


class WorkerStatus(BaseModel):
    """What the status view can say about the worker.

    The API and the worker are separate processes, so the API cannot see
    the worker directly. The worker leaves a heartbeat behind, and this is
    what that heartbeat says: whether the worker looks alive, which one it
    is, and when it was last seen.
    """

    running: bool = False
    worker_id: str | None = None
    last_seen: datetime | None = None
    seconds_since_seen: float | None = None


class ReadyResponse(BaseModel):
    """Readiness contract returned by ``/ready``."""

    status: Literal["ready", "not_ready"]
    service: Literal["clipforge-api"] = SERVICE_NAME
    version: str = API_VERSION
    config: Literal["loaded", "invalid"]
    capabilities: Capabilities = Capabilities()
    #: Whether the local speech model is already in the cache. A ``False``
    #: value does not mean the feature is missing: it means the model still
    #: has to be fetched, which needs a network connection the first time.
    transcription_model_cached: bool = False
    #: Whether the background worker is running.
    worker: WorkerStatus = WorkerStatus()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Always returns the stable ClipForge liveness contract.",
)
def health() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    description=(
        "Confirms that the current configuration can be loaded and reports "
        "which capabilities are implemented."
    ),
    responses={503: {"description": "The application configuration could not be loaded."}},
)
def ready(response: Response) -> ReadyResponse:
    # Reload the settings from the environment so the probe reflects the
    # configuration as it is right now rather than a cached startup snapshot.
    try:
        settings = Settings()
    except (ValidationError, SettingsError):
        response.status_code = 503
        return ReadyResponse(status="not_ready", config="invalid")

    return ReadyResponse(
        status="ready",
        config="loaded",
        transcription_model_cached=_model_is_cached(settings),
        worker=_worker_status(settings),
    )


def _worker_status(settings: Settings) -> WorkerStatus:
    """What the worker's heartbeat says.

    A missing, unreadable or stale heartbeat all mean the same thing to a
    user: the worker is not running. The probe must never fail the request.
    """
    try:
        heartbeat = read_heartbeat(settings.storage_root)
    except Exception:  # pragma: no cover - the probe must never fail the API
        return WorkerStatus()
    if heartbeat is None:
        return WorkerStatus()
    return WorkerStatus(
        running=heartbeat.running,
        worker_id=heartbeat.worker_id,
        last_seen=heartbeat.at,
        seconds_since_seen=round(heartbeat.age_seconds, 1),
    )


def _model_is_cached(settings: Settings) -> bool:
    """Whether the configured speech model is already on this machine.

    The check only looks at the local cache and never touches the network, so
    it is safe to run from a request.
    """
    try:
        transcription_settings = TranscriptionSettings()
        cache_dir = resolve_cache_dir(transcription_settings, settings.storage_root)
        return model_is_cached(transcription_settings, cache_dir)
    except Exception:  # pragma: no cover - the probe must never fail the API
        return False
