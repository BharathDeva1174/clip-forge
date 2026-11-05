"""Test doubles for the Phase 3 pipeline.

The speech engine is faked so that the ordinary test suite stays fast,
deterministic and offline. The fake still goes through the real validation
code, so a test that uses it exercises the same rules as the real engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.transcription import TranscriptionError
from app.transcripts import Transcript, validate_segments


@dataclass(frozen=True)
class FakeSegment:
    """A segment as a speech engine would return it."""

    start: float
    end: float
    text: str
    avg_logprob: float | None = -0.2
    no_speech_prob: float | None = 0.01


class FakeTranscriber:
    """A scripted speech-to-text engine."""

    name = "fake"

    def __init__(
        self,
        *,
        segments: list[FakeSegment] | None = None,
        error: Exception | None = None,
        language: str | None = "en",
        duration: float = 1.0,
    ) -> None:
        self.segments = (
            segments
            if segments is not None
            else [FakeSegment(start=0.0, end=1.0, text="hello world")]
        )
        self.error = error
        self.language = language
        self.duration = duration
        self.calls: list[Path] = []
        #: The durations the engine was told about, one per call.
        self.durations: list[float | None] = []

    def transcribe(self, audio_path, *, on_progress=None, duration_seconds=None):  # noqa: ANN001, ANN003
        self.calls.append(Path(audio_path))
        self.durations.append(duration_seconds)
        if self.error is not None:
            raise self.error
        if on_progress is not None:
            on_progress(0.5)
        return Transcript(
            language=self.language,
            model_name="fake-model",
            audio_sample_rate=16000,
            audio_channels=1,
            duration_seconds=self.duration,
            segments=tuple(
                validate_segments(self.segments, duration_seconds=self.duration)
            ),
        )

    def terminate(self) -> None:
        """Nothing to stop."""
        return None


class FakeAudioExtractor:
    """An audio extractor that writes a placeholder file."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        duration: float = 1.0,
        contents: bytes = b"RIFF-fake-wave-data",
    ) -> None:
        self.error = error
        self.duration = duration
        self.contents = contents
        self.calls: list[tuple[Path, Path]] = []
        self.terminated = False

    def extract(self, source, destination_dir, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append((Path(source), Path(destination_dir)))
        if self.error is not None:
            raise self.error
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "fake.wav"
        target.write_bytes(self.contents)
        from app.audio import ExtractedAudio

        return ExtractedAudio(
            path=target,
            duration_seconds=self.duration,
            sample_rate=16000,
            channels=1,
            size_bytes=len(self.contents),
            codec="pcm_s16le",
        )

    def terminate(self) -> None:
        self.terminated = True


def transcription_error(code: str, message: str = "nope") -> TranscriptionError:
    """Build a transcription failure for a test to raise."""
    return TranscriptionError(code, message)
