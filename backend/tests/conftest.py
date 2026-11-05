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

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.ai_client import AiClient
from app.config import Settings
from app.credentials import MemoryCredentialStore
from app.db import session
from app.main import create_app
from app.storage import Storage

#: The phrase the speech fixture says. A real transcription is checked against
#: these words.
EXPECTED_PHRASE = "The quick brown fox jumps over the lazy dog."


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
def credential_store() -> MemoryCredentialStore:
    """An in-memory credential store.

    No test may write to the real credential store of this machine, and no
    test may read one either.
    """
    return MemoryCredentialStore()


@pytest.fixture()
def ai_client() -> AiClient:
    """A provider client whose transport is mocked.

    It never reaches the network, and it never resolves a host.
    """
    return AiClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={})
        ),
        max_retries=0,
        sleep=lambda _seconds: None,
        resolver=lambda _host: ["8.8.8.8"],
    )


@pytest.fixture()
def app(
    settings: Settings,
    credential_store: MemoryCredentialStore,
    ai_client: AiClient,
):
    """The application under test."""
    application = create_app(settings)
    application.state.credential_store = credential_store
    application.state.ai_client = ai_client
    return application


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


@pytest.fixture(scope="session")
def sample_video_without_audio(tmp_path_factory) -> Path:
    """A valid video that has no audio track at all."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed, so the test video cannot be generated")

    target = tmp_path_factory.mktemp("media") / "silent.mp4"
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
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        pytest.skip("ffmpeg could not generate the synthetic silent video")
    return target


@pytest.fixture(scope="session")
def sample_speech_audio(tmp_path_factory) -> Path:
    """A WAV file with real speech saying a known phrase.

    The speech is produced by the Windows speech synthesiser, so the fixture
    is generated locally, is free of third-party content and contains a
    phrase the transcription can be checked against.
    """
    target = tmp_path_factory.mktemp("media") / "speech.wav"
    if not _synthesise_speech(EXPECTED_PHRASE, target):
        pytest.skip(
            "the Windows speech synthesiser is not available, so the speech "
            "fixture cannot be generated"
        )
    return target


@pytest.fixture(scope="session")
def sample_video_with_speech(tmp_path_factory, sample_speech_audio: Path) -> Path:
    """A short video whose audio track contains real speech."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed, so the test video cannot be generated")

    target = tmp_path_factory.mktemp("media") / "speech.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=4:size=320x240:rate=10",
            "-i",
            str(sample_speech_audio),
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
        timeout=180,
        check=False,
    )
    if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        pytest.skip("ffmpeg could not build the speech video fixture")
    return target


def _synthesise_speech(phrase: str, target: Path) -> bool:
    """Speak ``phrase`` into a WAV file with the Windows speech synthesiser."""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{target}'); "
        f"$s.Speak('{phrase}'); "
        "$s.Dispose();"
    )
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


@pytest.fixture()
def not_a_video(tmp_path: Path) -> Path:
    """A file that claims to be an MP4 but is not a media file."""
    target = tmp_path / "fake.mp4"
    target.write_bytes(b"this is definitely not a video file, not even a little bit")
    return target


# --- clip fixtures (Phase 5) ------------------------------------------------


def _ffmpeg() -> str:
    """The FFmpeg binary, or an empty string when it is not installed."""
    return shutil.which("ffmpeg") or ""


@pytest.fixture(scope="session")
def clip_source_video(tmp_path_factory) -> Path:
    """A short MP4 with a video stream and an audio stream."""
    binary = _ffmpeg()
    if not binary:
        pytest.skip("ffmpeg is not installed, so the clip fixtures cannot be made")
    target = tmp_path_factory.mktemp("clips") / "source.mp4"
    completed = subprocess.run(
        [
            binary, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=10:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0 or not target.exists():
        pytest.skip("ffmpeg could not build the clip fixture")
    return target


@pytest.fixture(scope="session")
def clip_source_without_audio(tmp_path_factory) -> Path:
    """A valid video that has no audio track at all."""
    binary = _ffmpeg()
    if not binary:
        pytest.skip("ffmpeg is not installed, so the clip fixtures cannot be made")
    target = tmp_path_factory.mktemp("clips") / "silent.mp4"
    completed = subprocess.run(
        [
            binary, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0 or not target.exists():
        pytest.skip("ffmpeg could not build the silent clip fixture")
    return target


@pytest.fixture(scope="session")
def clip_source_with_offset(tmp_path_factory) -> Path:
    """A video whose media timeline starts part-way into the file.

    The file's own start time is not zero, which is what a file with a
    non-zero stream start looks like. The content still starts at its own
    zero.
    """
    binary = _ffmpeg()
    if not binary:
        pytest.skip("ffmpeg is not installed, so the clip fixtures cannot be made")
    directory = tmp_path_factory.mktemp("clips")
    plain = directory / "plain.mp4"
    completed = subprocess.run(
        [
            binary, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(plain),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0:
        pytest.skip("ffmpeg could not build the offset clip fixture")
    target = directory / "offset.mp4"
    completed = subprocess.run(
        [
            binary, "-y", "-loglevel", "error",
            "-i", str(plain), "-c", "copy", "-output_ts_offset", "2.5",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0 or not target.exists():
        pytest.skip("ffmpeg could not build the offset clip fixture")
    return target


@pytest.fixture(scope="session")
def clip_source_odd_name(tmp_path_factory) -> Path:
    """A video whose file name has spaces, Unicode and a semicolon in it."""
    binary = _ffmpeg()
    if not binary:
        pytest.skip("ffmpeg is not installed, so the clip fixtures cannot be made")
    directory = tmp_path_factory.mktemp("clips")
    target = directory / "a clip; ünïcode & more.mp4"
    completed = subprocess.run(
        [
            binary, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0 or not target.exists():
        pytest.skip("ffmpeg could not build the oddly named clip fixture")
    return target


@pytest.fixture(scope="session")
def clip_source_broken(tmp_path_factory) -> Path:
    """A file that is not a media file at all."""
    target = tmp_path_factory.mktemp("clips") / "broken.mp4"
    target.write_bytes(b"this is not a video, it is a sentence about one")
    return target
