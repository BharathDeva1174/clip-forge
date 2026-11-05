"""Tests for the offline speech-to-text engine.

The model itself is faked, so the suite is fast, deterministic and never
touches the network. The one test that uses the real model is in
``test_real_model.py`` and is skipped unless the model is already cached.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.errors import ErrorCode
from app.transcription import (
    FasterWhisperEngine,
    TranscriptionError,
    TranscriptionSettings,
    build_engine,
    resolve_cache_dir,
)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = -0.2
    no_speech_prob: float | None = 0.01


class FakeInfo:
    language = "en"
    language_probability = 0.99
    duration = 2.0


class FakeWhisperModel:
    """A stand-in for faster-whisper's model."""

    instances: list["FakeWhisperModel"] = []
    segments: list[Segment] = [Segment(0.0, 1.0, "hello world")]
    failure: Exception | None = None
    fail_on_start: bool = False

    def __init__(
        self,
        model_size_or_path: str,
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: str | None = None,
        local_files_only: bool = False,
        **kwargs: object,
    ) -> None:
        self.model_size_or_path = model_size_or_path
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.local_files_only = local_files_only
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio, **kwargs):  # noqa: ANN001, ANN003
        if FakeWhisperModel.fail_on_start and FakeWhisperModel.failure:
            raise FakeWhisperModel.failure

        def generate():
            if FakeWhisperModel.failure:
                raise FakeWhisperModel.failure
            for segment in FakeWhisperModel.segments:
                yield segment

        return generate(), FakeInfo()


@pytest.fixture(autouse=True)
def reset_fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the fake model and reset its shared state."""
    import faster_whisper

    FakeWhisperModel.instances = []
    FakeWhisperModel.segments = [Segment(0.0, 1.0, "hello world")]
    FakeWhisperModel.failure = None
    FakeWhisperModel.fail_on_start = False
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)


@pytest.fixture()
def audio_file(tmp_path: Path) -> Path:
    target = tmp_path / "audio.wav"
    target.write_bytes(b"RIFF-fake")
    return target


def _engine(tmp_path: Path, **overrides) -> FasterWhisperEngine:
    settings = TranscriptionSettings(_env_file=None, **overrides)
    return FasterWhisperEngine(
        settings,
        cache_dir=tmp_path / "models",
        sample_rate=16000,
        channels=1,
    )


# --- conversion ------------------------------------------------------------


def test_the_engine_output_becomes_a_transcript(audio_file: Path, tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    transcript = engine.transcribe(audio_file)

    assert transcript.language == "en"
    assert transcript.model_name == "tiny.en"
    assert transcript.audio_sample_rate == 16000
    assert transcript.audio_channels == 1
    assert len(transcript.segments) == 1
    assert transcript.segments[0].text == "hello world"
    assert transcript.segments[0].index == 0


def test_an_empty_result_is_a_valid_empty_transcript(
    audio_file: Path, tmp_path: Path
) -> None:
    FakeWhisperModel.segments = []

    transcript = _engine(tmp_path).transcribe(audio_file)

    assert transcript.segments == ()


def test_a_missing_audio_file_is_reported(audio_file: Path, tmp_path: Path) -> None:
    with pytest.raises(TranscriptionError) as failure:
        _engine(tmp_path).transcribe(tmp_path / "gone.wav")

    assert failure.value.code == ErrorCode.SOURCE_NOT_FOUND.value


# --- model handling --------------------------------------------------------


def test_the_model_is_loaded_lazily_and_only_once(
    audio_file: Path, tmp_path: Path
) -> None:
    engine = _engine(tmp_path)
    assert FakeWhisperModel.instances == []

    engine.transcribe(audio_file)
    engine.transcribe(audio_file)

    assert len(FakeWhisperModel.instances) == 1


def test_the_model_is_configured_from_the_settings(
    audio_file: Path, tmp_path: Path
) -> None:
    engine = _engine(tmp_path, model="base.en", device="cpu", compute_type="int8")
    engine.transcribe(audio_file)

    model = FakeWhisperModel.instances[0]
    assert model.model_size_or_path == "base.en"
    assert model.device == "cpu"
    assert model.compute_type == "int8"
    assert model.download_root == str(tmp_path / "models")


def test_a_gpu_device_uses_a_float16_compute_type(
    audio_file: Path, tmp_path: Path
) -> None:
    engine = _engine(tmp_path, device="cuda")
    engine.transcribe(audio_file)

    assert FakeWhisperModel.instances[0].compute_type == "float16"


def test_a_model_that_cannot_be_loaded_is_reported(
    audio_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faster_whisper

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no model for you")

    monkeypatch.setattr(faster_whisper, "WhisperModel", explode)

    with pytest.raises(TranscriptionError) as failure:
        _engine(tmp_path).transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value
    assert "model" in failure.value.message.lower()


def test_offline_mode_without_a_cached_model_never_reaches_the_network(
    audio_file: Path, tmp_path: Path
) -> None:
    engine = _engine(tmp_path, offline=True)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value
    # The model was never even constructed, so no download was attempted.
    assert FakeWhisperModel.instances == []


def test_offline_mode_uses_a_cached_model(
    audio_file: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "models"
    snapshot = cache / "models--Systran--faster-whisper-tiny.en" / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"weights")

    engine = _engine(tmp_path, offline=True)
    assert engine.model_is_cached() is True

    engine.transcribe(audio_file)
    assert FakeWhisperModel.instances[0].local_files_only is True


def test_a_cached_model_is_recognised(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert engine.model_is_cached() is False

    snapshot = (
        tmp_path / "models" / "models--Systran--faster-whisper-tiny.en" / "snapshots" / "x"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"weights")

    assert engine.model_is_cached() is True


def test_the_model_cache_lives_under_the_storage_root(tmp_path: Path) -> None:
    settings = TranscriptionSettings(_env_file=None)
    assert resolve_cache_dir(settings, tmp_path) == tmp_path.resolve() / "models"


def test_an_explicit_model_cache_wins(tmp_path: Path) -> None:
    settings = TranscriptionSettings(_env_file=None, cache_dir=tmp_path / "elsewhere")
    assert resolve_cache_dir(settings, tmp_path / "storage") == (
        tmp_path / "elsewhere"
    ).resolve()


# --- failures --------------------------------------------------------------


def test_a_failure_while_decoding_is_mapped_to_a_stable_code(
    audio_file: Path, tmp_path: Path
) -> None:
    FakeWhisperModel.failure = RuntimeError("decoder exploded")

    with pytest.raises(TranscriptionError) as failure:
        _engine(tmp_path).transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_FAILED.value
    # The engine's own words never reach the user.
    assert "decoder exploded" not in failure.value.message


def test_a_failure_to_start_is_mapped_to_a_stable_code(
    audio_file: Path, tmp_path: Path
) -> None:
    FakeWhisperModel.failure = RuntimeError("cannot open the file")
    FakeWhisperModel.fail_on_start = True

    with pytest.raises(TranscriptionError) as failure:
        _engine(tmp_path).transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_FAILED.value


def test_a_transcription_that_takes_too_long_is_stopped(
    audio_file: Path, tmp_path: Path
) -> None:
    def slow():
        for index in range(20):
            time.sleep(0.15)
            yield Segment(index * 0.2, index * 0.2 + 0.1, f"segment {index}")

    class SlowModel(FakeWhisperModel):
        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ANN003
            return slow(), FakeInfo()

    import faster_whisper

    faster_whisper.WhisperModel = SlowModel  # type: ignore[attr-defined]

    engine = _engine(tmp_path, timeout_seconds=1)

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio_file)

    assert failure.value.code == ErrorCode.TRANSCRIPTION_TIMEOUT.value


# --- progress, concurrency and network -------------------------------------


def test_the_progress_callback_reports_a_fraction(
    audio_file: Path, tmp_path: Path
) -> None:
    seen: list[float] = []
    _engine(tmp_path).transcribe(audio_file, on_progress=seen.append)

    for value in seen:
        assert 0.0 <= value <= 1.0


def test_the_configured_concurrency_is_respected(
    audio_file: Path, tmp_path: Path
) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def counting_segments():
        nonlocal active, peak
        for index in range(4):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            yield Segment(index * 1.0, index * 1.0 + 0.5, f"segment {index}")

    class CountingModel(FakeWhisperModel):
        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ANN003
            info = FakeInfo()
            info.duration = 10.0
            return counting_segments(), info

    import faster_whisper

    faster_whisper.WhisperModel = CountingModel  # type: ignore[attr-defined]

    engine = _engine(tmp_path, max_concurrency=1)

    threads = [
        threading.Thread(target=engine.transcribe, args=(audio_file,)) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1


def test_the_engine_never_opens_a_network_connection(
    audio_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the transcription must not use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)

    transcript = _engine(tmp_path).transcribe(audio_file)

    assert transcript.segments


def test_build_engine_returns_the_process_isolated_engine(tmp_path: Path) -> None:
    """The default engine puts the model in a child process.

    That is what makes the timeout enforceable, so it is the default.
    """
    from app.transcription_process import ProcessTranscriptionEngine

    settings = TranscriptionSettings(_env_file=None)
    engine = build_engine(settings, cache_dir=tmp_path / "models")
    assert isinstance(engine, ProcessTranscriptionEngine)


def test_build_engine_can_fall_back_to_the_inline_engine(tmp_path: Path) -> None:
    """The inline engine is the fallback when a child process is not wanted."""
    settings = TranscriptionSettings(_env_file=None, isolation="inline")
    engine = build_engine(settings, cache_dir=tmp_path / "models")
    assert isinstance(engine, FasterWhisperEngine)
