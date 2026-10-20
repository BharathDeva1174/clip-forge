"""Application entry point for the ClipForge API.

Run locally with::

    python -m uvicorn app.main:app --reload

or, from the ``backend`` directory, ``python run.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import API_STAGE, API_VERSION, SERVICE_NAME, Settings, get_settings
from app.logging_config import configure_logging
from app.routers import api, health

API_TITLE = "ClipForge API"
API_DESCRIPTION = (
    "Local-first API that turns long-form videos into short-form clips for "
    "YouTube Shorts, Instagram Reels and TikTok.\n\n"
    f"**Project stage:** `{API_STAGE}`. This build exposes the health, "
    "readiness and API metadata endpoints only. Video ingestion, transcription, "
    "AI highlight detection, clip rendering and captions are planned for later "
    "phases and are reported as disabled by `/ready`."
)

logger = logging.getLogger("clipforge.api")


async def _log_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Log one line per request.

    Only the method, path, status code and duration are recorded. Query
    strings, headers and bodies are excluded because they can contain
    credentials or user content.
    """
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "%s %s -> %d (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    ``settings`` is injectable so tests can build an app with a known
    configuration without touching the process environment.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        # Explicit origins only: a wildcard is invalid together with
        # credentials, and is rejected by the settings validator.
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        max_age=600,
    )
    app.middleware("http")(_log_request)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Return a single, consistent error envelope for HTTP errors."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"status": exc.status_code, "message": exc.detail}},
            headers=getattr(exc, "headers", None),
        )

    app.include_router(health.router)
    app.include_router(api.router)

    logger.info(
        "%s %s ready (service=%s, stage=%s, cors=%s)",
        API_TITLE,
        API_VERSION,
        SERVICE_NAME,
        API_STAGE,
        ",".join(settings.cors_origin_list),
    )
    return app


app = create_app()
