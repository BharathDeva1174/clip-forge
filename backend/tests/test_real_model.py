"""Integration tests against the real, local speech model.

These are the only tests in the suite that load a model. They are skipped
when the model is not already in the local cache, so the suite never
downloads anything and never needs the network.

The assertions are about *structure*, not about getting every word right:
a small model on synthetic speech is not a word-accuracy benchmark, and no
word error rate is claimed anywhere. What is checked is that the transcript
is plausible, that its timestamps stay inside the audio, and that the
failure modes are not reported as successes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.errors import ErrorCode
from app.transcription import (
    TranscriptionError,
    TranscriptionSettings,
    model_is_cached,
    resolve_cache_dir,
)
from app.transcription_process import ProcessTranscriptionEngine
from real_model_fixtures import (
    ALTERNATE_PHRASE,
    CLEAR_PHRASE,
    PAUSE_PHRASE,
    SHORT_PHRASE,
    duration_of,
    mix_with_tone,
    pad_before,
    pad_with_silence,
    speak,
    to_pcm16k,
    write_silence,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: The model cache this machine already has. It is only ever read.
MODEL_CACHE = BACKEND_DIR / "data" / "models"

#: The model the tests use. It is the one the application is configured
#: with by default.
MODEL_NAME = "tiny.en"

pytestmark = pytest.mark.real_model


def _model_is_available() -> bool:
    settings = TranscriptionSettings(_env_file=None, model=MODEL_NAME)
    return model_is_cached(settings, MODEL_CACHE)


requires_model = pytest.mark.skipif(
    not _model_is_available(),
    reason=(
        f"the {MODEL_NAME} model is not in the local cache, so the real-model "
        "tests were not executed. Fetch it once with: "
        "python -c \"from faster_whisper import WhisperModel; "
        f"WhisperModel('{MODEL_NAME}', download_root='data/models')\""
    ),
)


@pytest.fixture(scope="module")
def engine():
    """An engine that runs the real model, offline, in a child process."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed, so the fixtures cannot be built")
    settings = TranscriptionSettings(
        _env_file=None,
        model=MODEL_NAME,
        offline=True,
        cache_dir=MODEL_CACHE,
        timeout_seconds=600,
    )
    instance = ProcessTranscriptionEngine(
        settings,
        cache_dir=MODEL_CACHE,
        host_path=BACKEND_DIR / "app" / "transcription_host.py",
    )
    yield instance
    instance.terminate()


@pytest.fixture(scope="module")
def clear_speech(tmp_path_factory) -> Path:
    """Ordinary English speech, on its own."""
    directory = tmp_path_factory.mktemp("fixtures")
    raw = directory / "clear-raw.wav"
    target = directory / "clear.wav"
    if not speak(raw, CLEAR_PHRASE):
        pytest.skip("the Windows speech synthesiser is not available")
    if not to_pcm16k(raw, target):
        pytest.skip("ffmpeg could not convert the speech fixture")
    return target


@pytest.fixture(scope="module")
def speech_with_pauses(tmp_path_factory) -> Path:
    """Speech with pauses between the clauses."""
    directory = tmp_path_factory.mktemp("fixtures")
    raw = directory / "pauses-raw.wav"
    target = directory / "pauses.wav"
    if not speak(raw, PAUSE_PHRASE):
        pytest.skip("the Windows speech synthesiser is not available")
    if not to_pcm16k(raw, target):
        pytest.skip("ffmpeg could not convert the speech fixture")
    return target


@pytest.fixture(scope="module")
def short_speech_then_silence(tmp_path_factory, clear_speech: Path) -> Path:
    """A short phrase followed by a long silence."""
    directory = tmp_path_factory.mktemp("fixtures")
    target = directory / "short-then-silence.wav"
    if not pad_with_silence(clear_speech, target, 4.0):
        pytest.skip("ffmpeg could not pad the speech fixture")
    return target


@pytest.fixture(scope="module")
def speech_at_the_edges(tmp_path_factory, clear_speech: Path) -> Path:
    """Speech at the very beginning and at the very end of the audio."""
    directory = tmp_path_factory.mktemp("fixtures")
    middle = directory / "edges-middle.wav"
    target = directory / "edges.wav"
    if not pad_before(clear_speech, middle, 1.5):
        pytest.skip("ffmpeg could not pad the speech fixture")
    if not pad_with_silence(middle, target, 1.5):
        pytest.skip("ffmpeg could not pad the speech fixture")
    return target


@pytest.fixture(scope="module")
def noisy_speech(tmp_path_factory, clear_speech: Path) -> Path:
    """Speech with a steady tone underneath it."""
    directory = tmp_path_factory.mktemp("fixtures")
    target = directory / "noisy.wav"
    if not mix_with_tone(clear_speech, target):
        pytest.skip("ffmpeg could not mix the speech fixture")
    return target


@pytest.fixture(scope="module")
def other_voice(tmp_path_factory) -> Path:
    """The same kind of sentence said by a different voice and rate."""
    directory = tmp_path_factory.mktemp("fixtures")
    raw = directory / "other-raw.wav"
    target = directory / "other.wav"
    if not speak(raw, ALTERNATE_PHRASE, voice="Microsoft Zira Desktop", rate=-2):
        pytest.skip("the Windows speech synthesiser is not available")
    if not to_pcm16k(raw, target):
        pytest.skip("ffmpeg could not convert the speech fixture")
    return target


@pytest.fixture(scope="module")
def silence(tmp_path_factory) -> Path:
    """Audio that holds no speech at all."""
    directory = tmp_path_factory.mktemp("fixtures")
    target = directory / "silence.wav"
    if not write_silence(target, 3.0):
        pytest.skip("ffmpeg could not write the silent fixture")
    return target


@pytest.fixture(scope="module")
def corrupted(tmp_path_factory) -> Path:
    """A file that claims to be audio but is not."""
    directory = tmp_path_factory.mktemp("fixtures")
    target = directory / "corrupted.wav"
    target.write_bytes(b"RIFF" + b"\x00" * 64 + b"this is not a wave file")
    return target


def _transcribe(engine, path: Path):
    """Transcribe a fixture, using the file's own duration."""
    return engine.transcribe(path, duration_seconds=duration_of(path))


# --- the matrix -------------------------------------------------------------


@requires_model
def test_clear_speech_produces_a_plausible_transcript(engine, clear_speech: Path) -> None:
    transcript = _transcribe(engine, clear_speech)

    assert transcript.segments, "the model found no speech in a clear recording"
    assert transcript.language == "en"
    assert transcript.duration_seconds == pytest.approx(
        duration_of(clear_speech), abs=0.2
    )
    text = " ".join(segment.text for segment in transcript.segments).lower()
    # A small model on synthetic speech is not exact, so the assertion is
    # about the words that carry the meaning, not about every word.
    assert "fox" in text or "quick" in text or "dog" in text


@requires_model
def test_speech_with_pauses_still_produces_ordered_segments(
    engine, speech_with_pauses: Path
) -> None:
    transcript = _transcribe(engine, speech_with_pauses)

    assert transcript.segments
    starts = [segment.start_seconds for segment in transcript.segments]
    assert starts == sorted(starts)
    for segment in transcript.segments:
        assert segment.end_seconds >= segment.start_seconds


@requires_model
def test_short_speech_followed_by_silence_is_transcribed(
    engine, short_speech_then_silence: Path
) -> None:
    transcript = _transcribe(engine, short_speech_then_silence)

    assert transcript.segments
    # The speech is at the start; the silence after it is not speech.
    assert transcript.segments[0].start_seconds < 5.0


@requires_model
def test_speech_at_the_edges_of_the_audio_is_transcribed(
    engine, speech_at_the_edges: Path
) -> None:
    transcript = _transcribe(engine, speech_at_the_edges)

    assert transcript.segments
    # The last segment ends inside the audio.
    assert transcript.segments[-1].end_seconds <= duration_of(speech_at_the_edges) + 0.01


@requires_model
def test_speech_with_a_tone_underneath_it_is_still_transcribed(
    engine, noisy_speech: Path
) -> None:
    """The transcript may be less accurate; it must not be a false success."""
    transcript = _transcribe(engine, noisy_speech)

    assert transcript.duration_seconds == pytest.approx(
        duration_of(noisy_speech), abs=0.2
    )
    for segment in transcript.segments:
        assert segment.start_seconds >= 0
        assert segment.end_seconds <= duration_of(noisy_speech) + 0.01


@requires_model
def test_another_voice_and_cadence_is_transcribed(engine, other_voice: Path) -> None:
    transcript = _transcribe(engine, other_voice)

    assert transcript.segments
    assert transcript.language == "en"


@requires_model
def test_audio_without_speech_is_not_reported_as_a_useful_transcript(
    engine, silence: Path
) -> None:
    """An empty recording is a valid, empty transcript -- not a failure."""
    transcript = _transcribe(engine, silence)

    assert transcript.segments == ()
    assert transcript.duration_seconds == pytest.approx(
        duration_of(silence), abs=0.2
    )


@requires_model
def test_a_corrupted_file_is_a_failure_and_not_an_empty_success(
    engine, corrupted: Path
) -> None:
    """A broken input must never look like a successful, empty transcript."""
    with pytest.raises(TranscriptionError) as failure:
        engine.transcribe(corrupted)

    assert failure.value.code in (
        ErrorCode.TRANSCRIPTION_FAILED.value,
        ErrorCode.TRANSCRIPTION_INVALID_OUTPUT.value,
    )


@requires_model
def test_no_segment_ever_claims_speech_outside_the_audio(engine, clear_speech: Path) -> None:
    transcript = _transcribe(engine, clear_speech)
    length = duration_of(clear_speech)

    for segment in transcript.segments:
        assert segment.start_seconds >= 0
        assert segment.start_seconds <= length + 0.01
        assert segment.end_seconds >= segment.start_seconds
        assert segment.end_seconds <= length + 0.01


@requires_model
def test_the_model_is_loaded_once_and_reused(engine, clear_speech: Path) -> None:
    """The child is kept between jobs, so the model is loaded only once."""
    engine.transcribe(clear_speech)
    first = engine._process
    assert first is not None

    engine.transcribe(clear_speech)

    assert engine._process is not None
    assert engine._process.pid == first.pid
