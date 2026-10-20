"""API metadata for the versioned API surface."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import API_STAGE, API_VERSION

router = APIRouter(prefix="/api/v1", tags=["api"])


class ApiInfoResponse(BaseModel):
    """Identifies the API and the stage of the project it was built from."""

    name: str = "ClipForge API"
    version: str = API_VERSION
    stage: str = API_STAGE
    docs: str = "/docs"


@router.get(
    "",
    response_model=ApiInfoResponse,
    summary="API metadata",
    description="Identifies the API, its version and the current project stage.",
)
def api_info() -> ApiInfoResponse:
    return ApiInfoResponse()
