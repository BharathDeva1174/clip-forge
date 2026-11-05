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

    # Audio extraction problems (Phase 3)
    FFMPEG_NOT_FOUND = "ffmpeg_not_found"
    AUDIO_EXTRACTION_FAILED = "audio_extraction_failed"
    AUDIO_EXTRACTION_TIMEOUT = "audio_extraction_timeout"
    NO_AUDIO_STREAM = "no_audio_stream"
    AUDIO_OUTPUT_INVALID = "audio_output_invalid"
    INSUFFICIENT_DISK_SPACE = "insufficient_disk_space"

    # Source location problems (Phase 3)
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_PATH_INVALID = "source_path_invalid"

    # Transcription problems (Phase 3)
    TRANSCRIPTION_MODEL_UNAVAILABLE = "transcription_model_unavailable"
    TRANSCRIPTION_FAILED = "transcription_failed"
    TRANSCRIPTION_TIMEOUT = "transcription_timeout"
    TRANSCRIPT_PERSISTENCE_FAILED = "transcript_persistence_failed"

    # Transcription problems (Phase 3.1)
    #
    # The engine produced output that could not be trusted. It is a
    # different code from `transcription_failed` on purpose: the model ran
    # and the machine was fine, but what came back was not usable.
    TRANSCRIPTION_INVALID_OUTPUT = "transcription_invalid_output"

    # Integration configuration problems (Phase 4)
    INTEGRATION_NOT_FOUND = "integration_not_found"
    INTEGRATION_DISABLED = "integration_disabled"
    INTEGRATION_CHANGED = "integration_changed"
    INTEGRATION_NOT_CONFIGURED = "integration_not_configured"
    INTEGRATION_INVALID = "integration_invalid"
    SECRET_STORE_UNAVAILABLE = "secret_store_unavailable"

    # Provider request problems (Phase 4)
    #
    # Every one of these is a stable, machine-readable code. The messages
    # are written for a person and never contain a key, a raw provider
    # response or any transcript content.
    AI_AUTH_FAILED = "ai_auth_failed"
    AI_RATE_LIMITED = "ai_rate_limited"
    AI_TIMEOUT = "ai_timeout"
    AI_UNAVAILABLE = "ai_unavailable"
    AI_INVALID_REQUEST = "ai_invalid_request"
    #: The provider rejected the request because the prompt (or transcript
    #: window) exceeded its context limit. The request can be retried with a
    #: smaller window.
    AI_CONTEXT_LIMIT_EXCEEDED = "ai_context_limit_exceeded"
    AI_INVALID_CONFIGURATION = "ai_invalid_configuration"
    AI_INVALID_OUTPUT = "ai_invalid_output"
    AI_PROTOCOL_ERROR = "ai_protocol_error"
    AI_PROTOCOL_MISMATCH = "ai_protocol_mismatch"
    AI_MODEL_UNAVAILABLE = "ai_model_unavailable"
    AI_NOT_SUPPORTED = "ai_not_supported"
    AI_UNSAFE_URL = "ai_unsafe_url"
    AI_RESPONSE_TOO_LARGE = "ai_response_too_large"

    # Highlight analysis problems (Phase 4)
    HIGHLIGHT_NO_TRANSCRIPT = "highlight_no_transcript"
    HIGHLIGHT_TRANSCRIPT_EMPTY = "highlight_transcript_empty"

    # Clip generation problems (Phase 5)
    CLIP_NOT_FOUND = "clip_not_found"
    CLIP_INVALID_BOUNDARIES = "clip_invalid_boundaries"
    CLIP_DURATION_TOO_SHORT = "clip_duration_too_short"
    CLIP_DURATION_TOO_LONG = "clip_duration_too_long"
    CLIP_SOURCE_MISSING = "clip_source_missing"
    CLIP_SOURCE_INVALID = "clip_source_invalid"
    CLIP_SOURCE_TOO_SHORT = "clip_source_too_short"
    CLIP_CANDIDATE_MISMATCH = "clip_candidate_mismatch"
    CLIP_RENDER_FAILED = "clip_render_failed"
    CLIP_RENDER_TIMEOUT = "clip_render_timeout"
    CLIP_VERIFICATION_FAILED = "clip_verification_failed"
    CLIP_OUTPUT_INVALID = "clip_output_invalid"
    CLIP_DURATION_MISMATCH = "clip_duration_mismatch"
    CLIP_DISK_SPACE = "clip_disk_space"

    # Editing and exporting (Phase 8)
    CLIP_EDIT_INVALID_BOUNDARIES = "clip_edit_invalid_boundaries"
    CLIP_EDIT_NOT_EDITABLE = "clip_edit_not_editable"
    CLIP_EDIT_INVALID_TITLE = "clip_edit_invalid_title"
    CLIP_CAPTION_EDIT_INVALID = "clip_caption_edit_invalid"
    CLIP_EXPORT_EMPTY = "clip_export_empty"
    CLIP_EXPORT_INVALID = "clip_export_invalid"
    CLIP_EXPORT_TOO_LARGE = "clip_export_too_large"
    CLIP_EXPORT_UNAVAILABLE = "clip_export_unavailable"
    JOB_CANCELLED = "job_cancelled"
    JOB_NOT_CANCELLABLE = "job_not_cancellable"
    JOB_NOT_DELETABLE = "job_not_deletable"

    # Formatted-clip problems (Phase 6)
    FORMAT_NOT_FOUND = "format_not_found"
    FORMAT_INVALID_SETTINGS = "format_invalid_settings"
    FORMAT_SOURCE_MISSING = "format_source_missing"
    FORMAT_SOURCE_INVALID = "format_source_invalid"
    FORMAT_NO_TRANSCRIPT = "format_no_transcript"
    FORMAT_RENDER_FAILED = "format_render_failed"
    FORMAT_RENDER_TIMEOUT = "format_render_timeout"
    FORMAT_VERIFICATION_FAILED = "format_verification_failed"
    FORMAT_OUTPUT_INVALID = "format_output_invalid"
    FORMAT_DURATION_MISMATCH = "format_duration_mismatch"
    FORMAT_CAPTIONS_UNAVAILABLE = "format_captions_unavailable"
    FORMAT_FONTS_UNAVAILABLE = "format_fonts_unavailable"

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
