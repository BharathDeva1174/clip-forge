"""Shared pytest fixtures.

Every test runs against a temporary storage root and a temporary SQLite
database, so the suite never touches the developer's real data and never
depends on an absolute path from this machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import session
from app.main import create_app
from app.storage import Storage


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any CLIPFORGE_* variables so the tests are deterministic."""
    for key in list(os.environ):
        if key.startswith("CLIPFORGE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def storage_root(tmp_path: Path) -> Path:
    """A throwaway storage root."""
    root = tmp_path / "storage"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def settings(storage_root: Path) -> Settings:
    """Settings pointing at the temporary storage root and database."""
    return Settings(
        _env_file=None,
        storage_dir=storage_root,
        database_path=storage_root / "database" / "clipforge.db",
        max_upload_bytes=8 * 1024 * 1024,
        max_video_duration_seconds=60,
        max_video_dimension=1920,
        worker_poll_interval_seconds=0.05,
        worker_lease_seconds=10,
        worker_max_attempts=2,
        log_level="WARNING",
    )


@pytest.fixture()
def storage(settings: Settings) -> Storage:
    """The storage layout for the temporary storage root."""
    layout = Storage(settings.storage_root)
    layout.ensure_layout()
    return layout


@pytest.fixture()
def app(settings: Settings):
    """The application under test."""
    return create_app(settings)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    """A test client bound to the temporary application."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db(settings: Settings) -> Iterator:
    """A database connection with the schema applied."""
    with session(
        settings.resolved_database_path,
        busy_timeout_seconds=settings.database_busy_timeout_seconds,
        wal_mode=settings.database_wal_mode,
    ) as conn:
        yield conn


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """A tiny, valid, synthetic video.

    It is generated with FFmpeg rather than committed, so the repository stays
    free of binary media and the test never touches third-party content.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed, so the test video cannot be generated")

    target = tmp_path_factory.mktemp("media") / "sample.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(target),
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        pytest.skip("ffmpeg could not generate the synthetic test video")
    return target


@pytest.fixture()
def not_a_video(tmp_path: Path) -> Path:
    """A file that claims to be an MP4 but is not a media file."""
    target = tmp_path / "fake.mp4"
    target.write_bytes(b"this is definitely not a video file, not even a little bit")
    return target
