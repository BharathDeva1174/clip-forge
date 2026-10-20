"""Liveness and readiness probes.

``/health`` is a deliberately tiny, stable contract that the frontend polls.
``/ready`` reports whether the current configuration can be loaded and states
plainly which capabilities are *not* implemented yet.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ValidationError
from pydantic_settings import SettingsError

from app.config import API_VERSION, SERVICE_NAME, Settings

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
    """

    video_ingestion: bool = True
    background_worker: bool = True
    database: bool = True
    persistent_storage: bool = True
    audio_extraction: bool = False
    transcription: bool = False
    llm_highlight_detection: bool = False
    clip_rendering: bool = False
    vertical_formatting: bool = False
    subtitle_generation: bool = False


class ReadyResponse(BaseModel):
    """Readiness contract returned by ``/ready``."""

    status: Literal["ready", "not_ready"]
    service: Literal["clipforge-api"] = SERVICE_NAME
    version: str = API_VERSION
    config: Literal["loaded", "invalid"]
    capabilities: Capabilities = Capabilities()


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
        Settings()
    except (ValidationError, SettingsError):
        response.status_code = 503
        return ReadyResponse(status="not_ready", config="invalid")
    return ReadyResponse(status="ready", config="loaded")
