"""Stable API error codes and the shared error envelope.

Every error the API returns uses the same shape::

    {"error": {"status": 400, "code": "invalid_youtube_url", "message": "..."}}

The ``code`` is a stable, machine-readable identifier. Clients should branch on
it; the ``message`` is for humans and may change.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes."""

    # Request problems
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    EMPTY_FILE = "empty_file"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"

    # Source problems
    INVALID_YOUTUBE_URL = "invalid_youtube_url"
    UNAUTHORIZED_SOURCE = "unauthorized_source"
    YOUTUBE_ACQUISITION_UNAVAILABLE = "youtube_acquisition_unavailable"

    # Media problems
    MEDIA_TOOL_UNAVAILABLE = "media_tool_unavailable"
    INVALID_MEDIA = "invalid_media"
    UNSUPPORTED_CODEC = "unsupported_codec"
    MEDIA_TOO_LONG = "media_too_long"
    MEDIA_DIMENSIONS_EXCEEDED = "media_dimensions_exceeded"

    # Processing problems
    ACQUISITION_FAILED = "acquisition_failed"
    ACQUISITION_TIMEOUT = "acquisition_timeout"
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    LEASE_EXPIRED = "lease_expired"
    INVALID_STATE = "invalid_state"

    # Fallback
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"


class ApiError(Exception):
    """An error that the API turns into the standard error envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_payload(status: int, code: str, message: str) -> dict[str, object]:
    """Build the standard error envelope."""
    return {"error": {"status": status, "code": code, "message": message}}
