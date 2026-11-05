"""Tests for the acquisition adapter.

The tests never reach the network. The downloader process is replaced with a
recording double so that the argument list, the failure handling and the size
limits can all be checked.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from app.acquisition import (
    AcquisitionError,
    DisabledAcquirer,
    YtDlpAcquirer,
    build_acquirer,
)
from app.errors import ErrorCode
from app.youtube import YouTubeVideo

VIDEO = YouTubeVideo(video_id="dQw4w9WgXcQ", canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")


class FakeProcess:
    """Records how the downloader was started."""

    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = io.BytesIO(stderr)
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture()
def recorded(monkeypatch: pytest.MonkeyPatch):
    """Capture the arguments the downloader is started with."""
    calls: list[dict] = []

    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003
        calls.append({"args": args, "kwargs": kwargs})
        # A successful download leaves a file behind.
        home = args[args.index("--paths") + 1].split("home:", 1)[1]
        target = Path(home)
        target.mkdir(parents=True, exist_ok=True)
        (target / "clip.mp4").write_bytes(b"downloaded")
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def test_the_disabled_acquirer_refuses_everything(tmp_path: Path) -> None:
    acquirer = DisabledAcquirer()

    assert acquirer.available() is False
    with pytest.raises(AcquisitionError) as excinfo:
        acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=10)

    assert excinfo.value.code == ErrorCode.YOUTUBE_ACQUISITION_UNAVAILABLE.value
    assert "upload" in excinfo.value.message.lower()


def test_the_disabled_mode_builds_the_disabled_acquirer() -> None:
    acquirer = build_acquirer("disabled")
    assert isinstance(acquirer, DisabledAcquirer)
    assert acquirer.available() is False


def test_the_yt_dlp_mode_builds_the_yt_dlp_acquirer() -> None:
    acquirer = build_acquirer("yt_dlp")
    assert isinstance(acquirer, YtDlpAcquirer)
    # The package is a dependency, so it must be importable.
    assert acquirer.available() is True


def test_the_downloader_is_started_with_an_argument_list(
    recorded, tmp_path: Path
) -> None:
    acquirer = YtDlpAcquirer()
    acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=10)

    assert len(recorded) == 1
    call = recorded[0]
    assert call["kwargs"].get("shell") is False
    assert isinstance(call["args"], list)
    assert all(isinstance(argument, str) for argument in call["args"])
    # The canonical URL is a single argument, never interpolated into a shell
    # command.
    assert call["args"][-1] == VIDEO.canonical_url
    assert "-m" in call["args"]
    assert "yt_dlp" in call["args"]
    # No cookies, no login, no credentials.
    assert not any("cookie" in argument.lower() for argument in call["args"])
    assert not any("username" in argument.lower() for argument in call["args"])


def test_the_download_size_is_bounded(recorded, tmp_path: Path) -> None:
    acquirer = YtDlpAcquirer()
    acquirer.acquire(VIDEO, tmp_path, max_bytes=12345, timeout_seconds=10)

    args = recorded[0]["args"]
    assert "--max-filesize" in args
    assert args[args.index("--max-filesize") + 1] == "12345"


def test_a_failing_downloader_produces_a_generic_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003
        process = FakeProcess(returncode=1, stderr=b"ERROR: private video")
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    acquirer = YtDlpAcquirer()

    with pytest.raises(AcquisitionError) as excinfo:
        acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=10)

    assert excinfo.value.code == ErrorCode.ACQUISITION_FAILED.value
    # The user-facing message must not leak the downloader's output.
    assert "private video" not in excinfo.value.message


def test_a_download_that_produces_no_file_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    acquirer = YtDlpAcquirer()

    with pytest.raises(AcquisitionError) as excinfo:
        acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=10)

    assert excinfo.value.code == ErrorCode.ACQUISITION_FAILED.value


def test_an_empty_download_is_rejected(recorded, tmp_path: Path) -> None:
    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003
        target = tmp_path / "empty.mp4"
        target.write_bytes(b"")
        return FakeProcess()

    recorded.clear()
    import app.acquisition as acquisition_module

    original = acquisition_module.YtDlpAcquirer._find_downloaded_file

    def find_empty(directory: Path) -> Path | None:
        candidate = directory / "empty.mp4"
        candidate.write_bytes(b"")
        return candidate

    acquisition_module.YtDlpAcquirer._find_downloaded_file = staticmethod(find_empty)
    try:
        acquirer = YtDlpAcquirer()
        with pytest.raises(AcquisitionError) as excinfo:
            acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=10)
    finally:
        acquisition_module.YtDlpAcquirer._find_downloaded_file = original

    assert excinfo.value.code == ErrorCode.ACQUISITION_FAILED.value


def test_a_download_that_exceeds_the_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import app.acquisition as acquisition_module

    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003
        process = FakeProcess()
        # An oversized download leaves an oversized file behind.
        target = tmp_path / "big.mp4"
        target.write_bytes(b"0" * 4096)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    original = acquisition_module.YtDlpAcquirer._find_downloaded_file

    def find_big(directory: Path) -> Path | None:
        candidate = directory / "big.mp4"
        candidate.write_bytes(b"0" * 4096)
        return candidate

    acquisition_module.YtDlpAcquirer._find_downloaded_file = staticmethod(find_big)
    try:
        acquirer = YtDlpAcquirer()
        with pytest.raises(AcquisitionError) as excinfo:
            acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=10)
    finally:
        acquisition_module.YtDlpAcquirer._find_downloaded_file = original

    assert excinfo.value.code == ErrorCode.ACQUISITION_FAILED.value


def test_a_timeout_terminates_the_child_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class HangingProcess(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="yt_dlp", timeout=timeout)
            return 0

        def poll(self) -> int | None:
            return None

    created: list[HangingProcess] = []

    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003
        process = HangingProcess()
        created.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("time.monotonic", lambda counter=iter(range(0, 10_000)): next(counter))

    acquirer = YtDlpAcquirer()
    with pytest.raises(AcquisitionError) as excinfo:
        acquirer.acquire(VIDEO, tmp_path, max_bytes=1024, timeout_seconds=1)

    assert excinfo.value.code == ErrorCode.ACQUISITION_TIMEOUT.value
    assert created and created[0].terminated


def test_terminate_is_safe_when_nothing_is_running() -> None:
    acquirer = YtDlpAcquirer()
    acquirer.terminate()
