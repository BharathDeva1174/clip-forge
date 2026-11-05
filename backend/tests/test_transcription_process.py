"""Tests for the process-isolated transcription engine.

These use a stub host instead of the real model, so they are fast,
deterministic and never touch the network. The stub speaks the same
protocol as the real host, so the code under test is the code that runs in
production.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from app.errors import ErrorCode
from app.transcription import TranscriptionError, TranscriptionSettings
from app.transcription_process import ProcessTranscriptionEngine

STUB_HOST = Path(__file__).resolve().parent / "host_stub.py"


@pytest.fixture()
def audio_file(tmp_path: Path) -> Path:
    target = tmp_path / "audio.wav"
    target.write_bytes(b"RIFF-fake")
    return target


@pytest.fixture()
def host_mode(monkeypatch: pytest.MonkeyPatch):
    """Choose how the stub host behaves."""

    def choose(mode: str, **extra: str) -> None:
        monkeypatch.setenv("CLIPFORGE_TEST_HOST_MODE", mode)
        for key, value in extra.items():
            monkeypatch.setenv(key, value)

    return choose


def _engine(tmp_path: Path, **overrides) -> ProcessTranscriptionEngine:
    settings = TranscriptionSettings(_env_file=None, **overrides)
    return ProcessTranscriptionEngine(
        settings,
        cache_dir=tmp_path / "models",
        host_path=STUB_HOST,
    )


def _child_pids(parent_pid: int) -> list[int]:
    """The process ids of the direct children of ``parent_pid``."""
    if os.name == "nt":
        script = (
            f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={parent_pid}')"
            ".ProcessId"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return [int(part) for part in completed.stdout.split() if part.strip().isdigit()]

    completed = subprocess.run(
        ["ps", "-o", "pid=", "--ppid", str(parent_pid)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return [int(part) for part in completed.stdout.split() if part.strip().isdigit()]


# --- the normal path --------------------------------------------------------


def test_a_transcription_comes_back_from_the_child(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    host_mode("normal")

    transcript = _engine(tmp_path).transcribe(audio_file)

    assert transcript.language == "en"
    assert [segment.text for segment in transcript.segments] == [
        "hello world",
        "second segment",
    ]
    assert transcript.segments[0].start_seconds == 0.0
    assert transcript.segments[0].end_seconds == 1.0


def test_the_duration_of_the_audio_is_used_to_normalise_the_timestamps(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    """The caller's measurement of the audio is the timeline to normalise to."""
    host_mode("normal")

    # The stub reports a second segment that ends at 2.0 s, but the audio is
    # only 1.5 s long, so the timestamp is pulled back to the end of it.
    transcript = _engine(tmp_path).transcribe(audio_file, duration_seconds=1.5)

    assert transcript.duration_seconds == 1.5
    assert transcript.segments[1].end_seconds == 1.5


def test_the_progress_callback_is_called(audio_file: Path, tmp_path: Path, host_mode) -> None:
    host_mode("normal")
    seen: list[float] = []

    _engine(tmp_path).transcribe(audio_file, on_progress=seen.append)

    assert seen
    assert all(0.0 <= value <= 1.0 for value in seen)


def test_the_child_is_started_once_and_reused(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    """The model is loaded once, not once per job."""
    host_mode("normal")
    engine = _engine(tmp_path)

    engine.transcribe(audio_file)
    first = engine._process
    engine.transcribe(audio_file)

    assert engine._process is not None
    assert first is not None
    # The very same child is still running.
    assert engine._process.pid == first.pid


def test_the_engine_stops_the_child_when_it_is_terminated(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    host_mode("normal")
    engine = _engine(tmp_path)
    engine.transcribe(audio_file)
    child = engine._process
    assert child is not None

    engine.terminate()

    assert engine._process is None
    assert child.poll() is not None


# --- the timeout ------------------------------------------------------------


def test_a_stalled_inference_is_stopped_and_reported_as_a_timeout(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    """The whole point: a stalled inference cannot hold the worker forever."""
    host_mode("stall")
    engine = _engine(tmp_path, timeout_seconds=2)

    started = time.monotonic()
    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)
    elapsed = time.monotonic() - started

    assert failure.value.code == ErrorCode.TRANSCRIPTION_TIMEOUT.value
    # The timeout is honoured, plus a short grace period to stop the child.
    assert elapsed < 20
    # The child was stopped, not left behind.
    assert engine._process is None


def test_a_timed_out_transcription_leaves_no_child_process_behind(
    audio_file: Path, tmp_path: Path, host_mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pids.txt"
    monkeypatch.setenv("CLIPFORGE_TEST_HOST_PIDFILE", str(pidfile))
    host_mode("stall")
    engine = _engine(tmp_path, timeout_seconds=2)

    with pytest.raises(TranscriptionError):
        engine.transcribe(audio_file)

    # The child that was doing the stalled inference is gone.
    pids = [int(line) for line in pidfile.read_text().split() if line.strip()]
    assert pids
    assert not set(pids) & set(_child_pids(os.getpid()))


def test_the_engine_works_again_after_a_timeout(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    """A timeout does not break the engine: the next job is transcribed."""
    host_mode("stall_once", CLIPFORGE_TEST_HOST_STATE=str(tmp_path / "stalled"))
    engine = _engine(tmp_path, timeout_seconds=2)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)
    assert failure.value.code == ErrorCode.TRANSCRIPTION_TIMEOUT.value

    # The child that stalled was killed and a new one took over.
    transcript = engine.transcribe(audio_file)

    assert [segment.text for segment in transcript.segments] == [
        "hello world",
        "second segment",
    ]


# --- failures ---------------------------------------------------------------


def test_a_child_that_dies_is_reported_with_a_stable_code(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    host_mode("crash")
    engine = _engine(tmp_path, timeout_seconds=30)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_FAILED.value


def test_a_child_that_dies_midway_is_reported_with_a_stable_code(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    host_mode("crash_midway")
    engine = _engine(tmp_path, timeout_seconds=30)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_FAILED.value


def test_a_failure_reported_by_the_child_is_mapped_to_a_stable_code(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    host_mode("error")
    engine = _engine(tmp_path, timeout_seconds=30)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_FAILED.value
    # The child's own words never reach the user.
    assert "stub host" not in failure.value.message


def test_a_missing_audio_file_is_reported(audio_file: Path, tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(tmp_path / "gone.wav")

    assert failure.value.code == ErrorCode.SOURCE_NOT_FOUND.value


def test_the_engine_can_be_stopped_from_the_progress_callback(
    audio_file: Path, tmp_path: Path, host_mode
) -> None:
    """The worker stops the engine from inside the progress callback.

    That is what happens on a shutdown, and it must not deadlock: the
    callback runs while the engine holds the request lock.
    """
    host_mode("normal")
    engine = _engine(tmp_path, timeout_seconds=30)
    stopped: list[bool] = []

    def on_progress(fraction: float) -> None:
        engine.terminate()
        stopped.append(True)

    with pytest.raises(TranscriptionError):
        engine.transcribe(audio_file, on_progress=on_progress)

    assert stopped
    assert engine._process is None


def test_a_request_that_is_interrupted_leaves_no_child_behind(
    audio_file: Path, tmp_path: Path, host_mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted request must not leave the inference process running."""
    pidfile = tmp_path / "pids.txt"
    monkeypatch.setenv("CLIPFORGE_TEST_HOST_PIDFILE", str(pidfile))
    host_mode("normal")
    engine = _engine(tmp_path, timeout_seconds=30)

    class Interrupted(Exception):
        pass

    def on_progress(fraction: float) -> None:
        raise Interrupted("the worker is stopping")

    with pytest.raises(Interrupted):
        engine.transcribe(audio_file, on_progress=on_progress)

    assert engine._process is None
    pids = [int(line) for line in pidfile.read_text().split() if line.strip()]
    assert pids
    assert not set(pids) & set(_child_pids(os.getpid()))


# --- the inference process cannot use the network ---------------------------


def test_the_inference_process_refuses_to_use_the_network() -> None:
    """Nothing leaves the machine: the child is not allowed to open a socket."""
    import socket

    from app.transcription_host import _forbid_the_network

    _forbid_the_network()
    try:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 9), timeout=1)
        with pytest.raises(OSError):
            socket.socket().connect(("127.0.0.1", 9))
    finally:
        # Put the real functions back so the rest of the suite is unaffected.
        import importlib

        importlib.reload(socket)
