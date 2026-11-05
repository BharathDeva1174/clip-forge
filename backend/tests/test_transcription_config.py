"""Model configuration and offline behaviour.

The questions these answer are the ones a user has to be able to answer
without reading the source: which model is used, how a different one is
selected, where the model is kept, and what happens when the machine has no
internet connection.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.transcription import (
    KNOWN_MODELS,
    TranscriptionError,
    TranscriptionSettings,
    model_is_cached,
    resolve_cache_dir,
)
from app.transcription_process import ProcessTranscriptionEngine

STUB_HOST = Path(__file__).resolve().parent / "host_stub.py"


def _cache_with(tmp_path: Path, name: str) -> Path:
    """Build a model cache that holds one model."""
    cache = tmp_path / "models"
    snapshot = cache / f"models--Systran--faster-whisper-{name}" / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"weights")
    return cache


# --- choosing a model -------------------------------------------------------


def test_the_default_model_is_tiny_en() -> None:
    assert TranscriptionSettings(_env_file=None).model == "tiny.en"


def test_the_model_can_be_chosen_with_an_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_MODEL", "base.en")

    assert TranscriptionSettings(_env_file=None).model == "base.en"


@pytest.mark.parametrize("name", sorted(KNOWN_MODELS))
def test_every_known_model_is_accepted(name: str) -> None:
    assert TranscriptionSettings(_env_file=None, model=name).model == name


def test_a_model_repository_is_accepted() -> None:
    """A custom fine-tune on the model hub is a legitimate choice."""
    settings = TranscriptionSettings(_env_file=None, model="deepdml/faster-whisper-test")
    assert settings.model == "deepdml/faster-whisper-test"


def test_a_local_model_directory_is_accepted(tmp_path: Path) -> None:
    model_dir = tmp_path / "my-model"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"weights")

    settings = TranscriptionSettings(_env_file=None, model=str(model_dir))

    assert settings.model == str(model_dir)


def test_an_unknown_model_name_is_refused_with_an_actionable_message() -> None:
    with pytest.raises(ValidationError) as failure:
        TranscriptionSettings(_env_file=None, model="tiny.eng")

    message = str(failure.value)
    assert "tiny.en" in message
    assert "not a known transcription model" in message


def test_a_model_name_that_could_not_be_used_is_refused() -> None:
    with pytest.raises(ValidationError):
        TranscriptionSettings(_env_file=None, model="../../etc/passwd")


def test_an_empty_model_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        TranscriptionSettings(_env_file=None, model="   ")


def test_a_bad_model_name_does_not_load_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is a configuration check, not a load."""
    import faster_whisper

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the model must not be loaded to validate a name")

    monkeypatch.setattr(faster_whisper, "WhisperModel", refuse)

    with pytest.raises(ValidationError):
        TranscriptionSettings(_env_file=None, model="tiny.eng")


# --- the model cache --------------------------------------------------------


def test_the_cache_lives_under_the_storage_root_by_default(tmp_path: Path) -> None:
    settings = TranscriptionSettings(_env_file=None)
    assert resolve_cache_dir(settings, tmp_path) == tmp_path.resolve() / "models"


def test_an_explicit_cache_wins(tmp_path: Path) -> None:
    settings = TranscriptionSettings(_env_file=None, cache_dir=tmp_path / "elsewhere")
    assert resolve_cache_dir(settings, tmp_path / "storage") == (
        tmp_path / "elsewhere"
    ).resolve()


def test_a_cached_model_is_recognised(tmp_path: Path) -> None:
    cache = _cache_with(tmp_path, "tiny.en")
    settings = TranscriptionSettings(_env_file=None, model="tiny.en")
    assert model_is_cached(settings, cache) is True


def test_a_different_model_is_not_mistaken_for_a_cached_one(tmp_path: Path) -> None:
    """``base`` is not satisfied by a cached ``base.en``."""
    cache = _cache_with(tmp_path, "base.en")
    settings = TranscriptionSettings(_env_file=None, model="base")
    assert model_is_cached(settings, cache) is False


def test_an_empty_cache_reports_nothing_as_cached(tmp_path: Path) -> None:
    settings = TranscriptionSettings(_env_file=None)
    assert model_is_cached(settings, tmp_path / "models") is False


# --- offline operation ------------------------------------------------------


def test_offline_mode_without_a_cached_model_never_starts_the_inference_process(
    tmp_path: Path,
) -> None:
    """An offline job must not even reach the network."""
    settings = TranscriptionSettings(_env_file=None, offline=True)
    engine = ProcessTranscriptionEngine(
        settings, cache_dir=tmp_path / "models", host_path=STUB_HOST
    )
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(audio)

    assert failure.value.code == "transcription_model_unavailable"
    # No child was started, so no download could have been attempted.
    assert engine._process is None


def test_offline_mode_with_a_cached_model_starts_the_inference_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFORGE_TEST_HOST_MODE", "normal")
    cache = _cache_with(tmp_path, "tiny.en")
    settings = TranscriptionSettings(_env_file=None, offline=True)
    engine = ProcessTranscriptionEngine(
        settings, cache_dir=cache, host_path=STUB_HOST
    )
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    transcript = engine.transcribe(audio)

    assert transcript.segments
    engine.terminate()


def test_the_offline_flag_is_passed_to_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is told to read the model from the cache only."""
    captured: dict[str, object] = {}

    import faster_whisper

    class RecordingModel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ANN003
            return iter(()), type("Info", (), {"duration": 1.0, "language": "en"})()

    monkeypatch.setattr(faster_whisper, "WhisperModel", RecordingModel)

    from app.transcription import FasterWhisperEngine

    cache = _cache_with(tmp_path, "tiny.en")
    settings = TranscriptionSettings(_env_file=None, offline=True)
    engine = FasterWhisperEngine(settings, cache_dir=cache)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    engine.transcribe(audio)

    assert captured["local_files_only"] is True


def test_the_api_never_loads_the_model(
    client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness probe must not pull a model into memory."""
    import faster_whisper

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the API must never load the speech model")

    monkeypatch.setattr(faster_whisper, "WhisperModel", refuse)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["capabilities"]["transcription"] is True


def test_the_model_is_not_loaded_when_the_settings_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the configuration must not load anything."""
    import faster_whisper

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("reading the settings must not load the model")

    monkeypatch.setattr(faster_whisper, "WhisperModel", refuse)

    TranscriptionSettings(_env_file=None)
    resolve_cache_dir(TranscriptionSettings(_env_file=None), Path("data"))


def test_the_offline_flag_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_OFFLINE", "true")

    assert TranscriptionSettings(_env_file=None).offline is True


def test_the_isolation_mode_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_ISOLATION", "inline")

    assert TranscriptionSettings(_env_file=None).isolation == "inline"


def test_an_unknown_isolation_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_ISOLATION", "thread")

    with pytest.raises(ValidationError):
        TranscriptionSettings(_env_file=None)


def test_the_environment_does_not_leak_into_the_inference_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is configured through the pipe, not through the environment."""
    monkeypatch.setenv("CLIPFORGE_TEST_HOST_MODE", "normal")
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_MODEL", "base.en")
    settings = TranscriptionSettings(_env_file=None, model="tiny.en")
    engine = ProcessTranscriptionEngine(
        settings, cache_dir=tmp_path / "models", host_path=STUB_HOST
    )
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    # The engine was told tiny.en, whatever the environment says.
    assert engine._config()["model"] == "tiny.en"
    assert os.environ["CLIPFORGE_TRANSCRIPTION_MODEL"] == "base.en"
    engine.terminate()
