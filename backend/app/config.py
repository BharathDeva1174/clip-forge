"""Centralised, environment-driven configuration for the ClipForge API.

All settings are read from environment variables prefixed with ``CLIPFORGE_``
(and, when present, a ``.env`` file in the working directory).

The provider configuration for the AI integrations is deliberately **not** an
environment setting: it is stored in the database and managed from the
Integrations page, so that several providers can be configured and switched
between. The API key for each one is kept in the operating system's
credential store, and only a reference to it reaches the database.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Stable service identifier. The frontend asserts on this exact value, so it is
#: deliberately not configurable.
SERVICE_NAME: Final[Literal["clipforge-api"]] = "clipforge-api"

#: Version of the HTTP contract exposed by the API.
API_VERSION: Final[str] = "1.2.0"

#: Development phase this build belongs to.
API_STAGE: Final[str] = "phase-10-local-launch"


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

    # Allows the API to bind to something other than a loopback address.
    #
    # The service has no authentication, so binding it to every interface
    # would expose it to the whole network. The default therefore keeps the
    # server on this machine only, and LAN access has to be asked for
    # explicitly. Turning it on is a deliberate, visible decision.
    allow_lan_binding: bool = False

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

    # --- Audio extraction (Phase 3) -------------------------------------------
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_timeout_seconds: int = Field(default=1800, ge=1, le=7200)

    # The format the speech engine reads: mono, 16 kHz, PCM.
    audio_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    audio_channels: int = Field(default=1, ge=1, le=2)

    # How long a source may be before its audio is refused.
    audio_max_duration_seconds: float = Field(default=4 * 3600, gt=0)

    # The largest extracted audio file that is accepted.
    audio_max_bytes: int = Field(default=2 * 1024**3, ge=1024)

    # How much free disk space must be available before an extraction starts.
    audio_min_free_disk_bytes: int = Field(default=512 * 1024**2, ge=0)

    # What happens to the extracted audio once the transcription is done.
    # `delete` removes it as soon as it is no longer needed, `keep` leaves it
    # in the scratch directory so it can be inspected by hand.
    audio_temp_retention: Literal["delete", "keep"] = "delete"

    # --- Worker ---------------------------------------------------------------
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=300)
    worker_lease_seconds: int = Field(default=300, ge=10, le=86400)
    #: How often the background lease-renewal thread renews an analysis's
    #: lease while a provider call is in flight. A provider call can take
    #: longer than the lease, so renewal must happen *during* the call, not
    #: only between transcript windows. This interval leaves margin before
    #: lease expiration so recovery is not delayed.
    worker_lease_renewal_interval_seconds: float = Field(default=30.0, gt=0, le=300)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_acquisition_timeout_seconds: int = Field(default=1800, ge=10)

    # --- Highlight analysis (Phase 4) ---------------------------------------
    #: How many transcript segments one provider request may carry, by default.
    #: A long transcript is split into segment-safe windows; never truncated.
    ai_max_segments_per_window: int = Field(default=120, ge=1, le=1000)
    #: How many segments two adjacent windows share, so a moment that straddles
    #: a boundary is still whole in at least one window.
    ai_window_overlap_segments: int = Field(default=4, ge=0, le=50)
    #: The most characters of transcript text one provider request may carry.
    #: Used as a hard cap alongside segment count; a single segment longer than
    #: this still gets its own window rather than being silently dropped.
    ai_max_transcript_characters: int = Field(default=12000, ge=256, le=1_000_000)
    #: The smallest window the analysis will split down to during
    #: context-limit recovery. Below this, a provider that still rejects the
    #: window is reported as failed rather than split forever.
    ai_min_segments_per_window: int = Field(default=5, ge=1, le=1000)
    #: Maximum provider requests in flight at once. Defaults to 1 for
    #: reliable sequential analysis; parallel execution can be raised later.
    ai_max_concurrent_requests: int = Field(default=1, ge=1, le=16)
    #: The overall deadline for one analysis, in seconds. All windows, retries
    #: and context-limit recovery must finish within this.
    ai_analysis_deadline_seconds: float = Field(default=900.0, gt=0, le=86400)
    #: Maximum attempts for one provider request (first try plus retries).
    ai_max_request_attempts: int = Field(default=3, ge=1, le=20)
    #: The longest a single retry backoff may grow, in seconds.
    ai_max_backoff_seconds: float = Field(default=30.0, gt=0, le=600)

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

    # --- AI integrations (Phase 4) --------------------------------------------
    # The provider configuration is **not** here. It lives in the database, as
    # a row the Integrations page manages, so that more than one provider can
    # be configured and switched between without touching the environment.
    # The key for each one lives in the operating system's credential store.

    # How long one provider request may take before the transport gives up.
    # Free-tier and reasoning models can take minutes to answer one window of
    # a long transcript; the transport applies this to every attempt.
    ai_request_timeout_seconds: float = Field(default=60.0, gt=0)

    # How many times the transport retries one provider request on a
    # retryable failure (network error, 429, 5xx), with exponential backoff.
    # Free tiers fail in bursts; a thin budget turns one busy minute into a
    # failed analysis.
    ai_max_retries: int = Field(default=2, ge=0, le=10)

    # --- Clip generation (Phase 5) --------------------------------------------
    # The longest and the shortest clip that may be rendered.
    clip_min_duration_seconds: float = Field(default=1.0, gt=0, le=60)
    clip_max_duration_seconds: float = Field(default=180.0, gt=0, le=3600)

    # How long one render may run before it is stopped.
    clip_ffmpeg_timeout_seconds: int = Field(default=300, ge=1, le=7200)

    # How many renders may run at the same time. The worker renders one at a
    # time, so this is a ceiling rather than a target.
    clip_max_concurrency: int = Field(default=1, ge=1, le=4)

    # How far the measured length of a rendered clip may be from the length
    # that was asked for, in seconds. A clip outside the tolerance fails
    # rather than being quietly shifted.
    clip_duration_tolerance_seconds: float = Field(default=0.5, ge=0, le=5)

    # The encoder preset. "veryfast" is a good trade-off for a local
    # prototype: it is quick and the file is still small enough to play
    # everywhere.
    clip_encoder_preset: str = "veryfast"

    # --- Formatted clips (Phase 6) --------------------------------------------
    # How long a formatting render may run before it is stopped.
    format_ffmpeg_timeout_seconds: int = Field(default=600, ge=1, le=7200)

    # How far the measured length of a formatted clip may be from the length
    # of the clip it came from, in seconds.
    format_duration_tolerance_seconds: float = Field(default=0.5, ge=0, le=5)

    # The FFmpeg encoder preset for a formatted clip.
    format_encoder_preset: str = "veryfast"

    # The archive a single export request may hold is bounded: an export is
    # built in memory and streamed back, so an unbounded number of clips would
    # let one request consume an unbounded amount of RAM and disk. A local MVP
    # keeps the limit modest and explicit.
    clip_export_max_items: int = Field(default=50, ge=1, le=1000)

    # The archive a single export request may hold is built in memory, so the
    # total on-disk size of the chosen clips is also bounded. A local MVP keeps
    # the limit modest and explicit; it is checked against the actual file sizes
    # before any archive is assembled.
    clip_export_max_bytes: int = Field(default=1 * 1024**3, ge=1, le=8 * 1024**3)

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

    @model_validator(mode="after")
    def _keep_the_server_local_by_default(self) -> Settings:
        """Refuse to bind anywhere but this machine unless that was asked for.

        The API has no authentication. Binding it to ``0.0.0.0`` would put
        every unauthenticated endpoint -- including the upload endpoint -- on
        the network, so it has to be an explicit, deliberate choice.
        """
        if self.allow_lan_binding:
            return self
        if not is_loopback_host(self.host):
            raise ValueError(
                f"host {self.host!r} is not a loopback address. The API has no "
                "authentication, so it only binds to this machine by default. "
                "Use 127.0.0.1 or localhost, or set allow_lan_binding=true if "
                "you really want the network to reach it."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once on first use."""
    return Settings()


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` only reaches this machine."""
    candidate = host.strip().lower()
    if candidate in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False
