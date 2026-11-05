"""Application entry point for the ClipForge API.

Run locally with::

    python -m uvicorn app.main:app --reload

or, from the ``backend`` directory, ``python run.py``.

The background worker that actually processes the queued jobs lives in
``backend/worker.py`` and runs as its own process.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from app.ai_client import AiClient
from app.config import API_STAGE, API_VERSION, SERVICE_NAME, Settings, get_settings
from app.credentials import DEFAULT_STORE
from app.deps import get_db  # noqa: F401 - imported so the dependency is registered
from app.errors import ApiError, ErrorCode, error_payload
from app.logging_config import configure_logging
from app.routers import (
    api,
    clips,
    formats,
    health,
    highlights,
    integrations,
    jobs,
    videos,
)
from app.storage import Storage

API_TITLE = "ClipForge API"
API_DESCRIPTION = (
    "Local-first API that turns long-form videos into short-form clips for "
    "YouTube Shorts, Instagram Reels and TikTok.\n\n"
    f"**Project stage:** `{API_STAGE}`.\n\n"
    "This build accepts video uploads and authorised YouTube URLs, stores them, "
    "extracts their audio with FFmpeg and transcribes that audio with a local "
    "speech-to-text model in a separate background worker. It detects highlights "
    "with the provider the user configures, renders clips from the accepted "
    "candidates, converts a clip to portrait with burned-in captions, and "
    "exports finished clips as a streamed ZIP. `/ready` reports what is "
    "available in this particular installation."
)

logger = logging.getLogger("clipforge.api")


async def _log_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Log one line per request.

    Only the method, path, status code and duration are recorded. Query
    strings, headers and bodies are excluded because they can carry
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


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before they are read.

    This is a cheap first line of defence: a client that sends a
    ``Content-Length`` header never has its body read at all. Uploads are
    additionally limited while they stream (see :mod:`app.uploads`), which
    covers clients that use chunked transfer encoding.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            raw_length = headers.get("content-length")
            if raw_length is not None and raw_length.isdigit():
                if int(raw_length) > self.max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content=error_payload(
                            413,
                            ErrorCode.PAYLOAD_TOO_LARGE.value,
                            "The request body is larger than the configured limit.",
                        ),
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


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

    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    app.state.settings = settings
    app.state.storage = storage
    # The provider client and the credential store live on the application
    # state, so a test can put a mocked client and an in-memory credential
    # store in their place.
    app.state.ai_client = AiClient(
        timeout_seconds=settings.ai_request_timeout_seconds,
        max_retries=settings.ai_max_retries,
    )
    app.state.credential_store = DEFAULT_STORE

    # The limit applies to the whole request body. The multipart envelope adds
    # a little overhead on top of the file itself, so allow for that.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_upload_bytes + 1024 * 1024,
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

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        """Return the standard error envelope for application errors."""
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.status_code, exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return the standard error envelope for a rejected request body.

        Only the field location and the reason are echoed back. The submitted
        values are never included, because they can contain user content.
        """
        problems = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            problems.append(f"{location}: {error.get('msg', 'invalid value')}")
        return JSONResponse(
            status_code=422,
            content=error_payload(
                422,
                ErrorCode.INVALID_REQUEST.value,
                "; ".join(problems) or "The request could not be processed.",
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Return a single, consistent error envelope for HTTP errors."""
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                exc.status_code, ErrorCode.HTTP_ERROR.value, str(exc.detail)
            ),
            headers=getattr(exc, "headers", None),
        )

    app.include_router(health.router)
    app.include_router(api.router)
    app.include_router(jobs.router)
    app.include_router(videos.router)
    app.include_router(integrations.router)
    app.include_router(highlights.router)
    app.include_router(clips.router)
    app.include_router(formats.router)

    logger.info(
        "%s %s ready (service=%s, stage=%s, cors=%s, storage=%s)",
        API_TITLE,
        API_VERSION,
        SERVICE_NAME,
        API_STAGE,
        ",".join(settings.cors_origin_list),
        settings.storage_root,
    )
    return app


app = create_app()
