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
API_VERSION: Final[str] = "1.0.0"

#: Development phase this build belongs to.
API_STAGE: Final[str] = "phase-1-foundation"


class Settings(BaseSettings):
    """Runtime settings for the API.

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

    # --- Reserved for later phases -------------------------------------------
    # Local root for media files and the SQLite database (Phase 2+). Declared
    # here so the location is documented from day one; no endpoint uses it yet.
    storage_dir: Path = Path("data")

    # LLM provider credentials for AI highlight detection (Phase 4+). Backend
    # only: never expose these to the frontend and never log them.
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once on first use."""
    return Settings()
