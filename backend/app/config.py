"""Centralised, environment-driven configuration for the ClipForge API.

All settings are read from environment variables prefixed with ``CLIPFORGE_``
(and, when present, a ``.env`` file in the working directory). Secrets are held
as :class:`pydantic.SecretStr` so they are never rendered in logs, dumps or API
responses.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Stable service identifier. The frontend asserts on this exact value, so it is
#: deliberately not configurable.
SERVICE_NAME: Final[Literal["clipforge-api"]] = "clipforge-api"

#: Version of the HTTP contract exposed by the API.
API_VERSION: Final[str] = "1.1.0"

#: Development phase this build belongs to.
API_STAGE: Final[str] = "phase-2-ingestion"


class Settings(BaseSettings):
    """Runtime settings for the API and the worker.

    Every field can be overridden with a ``CLIPFORGE_``-prefixed environment
    variable, for example ``CLIPFORGE_PORT=8010``.
    """

    model_config = SettingsConfigDict(
        env_prefix="CLIPFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application metadata -------------------------------------------------
    app_name: str = "ClipForge API"
    app_version: str = API_VERSION
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    # --- Local server ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # --- Browser access -------------------------------------------------------
    # Comma-separated list of explicit origins. Wildcards are rejected by the
    # validator below because credentials are enabled on the CORS middleware.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # --- Storage --------------------------------------------------------------
    # Root of all application-owned data: uploads, sources, scratch space and
    # the SQLite database. Relative paths are resolved against the working
    # directory, so the backend should be started from `backend/`.
    storage_dir: Path = Path("data")

    # Overrides the default database location. When unset the database lives
    # inside the storage root, at `<storage_dir>/database/clipforge.db`.
    database_path: Path | None = None

    # Maximum accepted upload size in bytes (2 GiB by default).
    max_upload_bytes: int = Field(default=2 * 1024**3, ge=1024)

    # Media policy applied to every accepted source.
    max_video_duration_seconds: float = Field(default=4 * 3600, gt=0)
    max_video_dimension: int = Field(default=7680, ge=16)
    allowed_upload_extensions: str = ".mp4,.mov,.m4v,.webm,.mkv"
    allowed_video_codecs: str = "h264,hevc,vp9,av1,mpeg4"

    # --- Media tools ----------------------------------------------------------
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: int = Field(default=60, ge=1, le=3600)

    # --- Worker ---------------------------------------------------------------
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=300)
    worker_lease_seconds: int = Field(default=300, ge=10, le=86400)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_acquisition_timeout_seconds: int = Field(default=1800, ge=10)

    # --- Database -------------------------------------------------------------
    database_busy_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # WAL improves concurrent read/write behaviour but requires a local
    # filesystem. Disable it if the database is ever placed on a network share.
    database_wal_mode: bool = True

    # --- YouTube --------------------------------------------------------------
    # `yt_dlp` uses the yt-dlp package; `disabled` refuses every YouTube
    # submission and points the user at local uploads instead.
    youtube_acquisition_mode: Literal["disabled", "yt_dlp"] = "yt_dlp"
    youtube_allowed_hosts: str = (
        "youtube.com,www.youtube.com,m.youtube.com,youtu.be,www.youtu.be"
    )
    youtube_max_bytes: int = Field(default=2 * 1024**3, ge=1024)

    # --- Reserved for Phase 4 (AI highlight detection) -------------------------
    # Backend-only credentials. Never expose these to the frontend and never
    # log them.
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None

    # --- Derived values -------------------------------------------------------

    @property
    def storage_root(self) -> Path:
        """Absolute path of the application storage root."""
        return self.storage_dir.expanduser().resolve()

    @property
    def resolved_database_path(self) -> Path:
        """Absolute path of the SQLite database file."""
        if self.database_path is not None:
            return self.database_path.expanduser().resolve()
        return self.storage_root / "database" / "clipforge.db"

    @property
    def allowed_extension_list(self) -> list[str]:
        """Allowed upload extensions, lower-cased and dot-prefixed."""
        return [
            ext.strip().lower()
            for ext in self.allowed_upload_extensions.split(",")
            if ext.strip()
        ]

    @property
    def allowed_codec_list(self) -> list[str]:
        """Allowed video codecs, lower-cased."""
        return [
            codec.strip().lower()
            for codec in self.allowed_video_codecs.split(",")
            if codec.strip()
        ]

    @property
    def youtube_host_set(self) -> frozenset[str]:
        """Hostnames accepted for YouTube submissions, lower-cased."""
        return frozenset(
            host.strip().lower()
            for host in self.youtube_allowed_hosts.split(",")
            if host.strip()
        )

    @property
    def cors_origin_list(self) -> list[str]:
        """Explicit browser origins allowed to call this API."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _reject_wildcard_cors_origin(self) -> Settings:
        # `allow_credentials=True` plus a wildcard origin is rejected by browsers
        # and is unsafe, so it is a configuration error rather than a runtime one.
        if "*" in self.cors_origin_list:
            raise ValueError(
                "cors_origins must list explicit origins; the wildcard '*' is not allowed"
            )
        return self

    @model_validator(mode="after")
    def _validate_extensions(self) -> Settings:
        for ext in self.allowed_extension_list:
            if not ext.startswith(".") or len(ext) < 2:
                raise ValueError(
                    f"allowed_upload_extensions entries must look like '.mp4', got {ext!r}"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once on first use."""
    return Settings()
