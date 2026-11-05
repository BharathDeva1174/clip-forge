"""Offline speech-to-text.

The default engine is `faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_,
a local implementation of OpenAI's Whisper models. It runs entirely on the
machine that owns the video: no audio, no transcript and no request ever
leaves it.

Design rules:

* **The model is loaded lazily and reused.** It is loaded on the first
  transcription and then kept for the lifetime of the process, so a long
  recording does not pay the load cost repeatedly. It is never loaded at API
  start-up.
* **Offline is a mode, not a hope.** With ``offline`` enabled the engine only
  ever looks in the local model cache and never attempts a download, so a
  missing model produces an actionable error instead of a silent network call.
* **The engine is behind a narrow interface.** The rest of the application
  depends on :class:`TranscriptionEngine`, not on faster-whisper, so the
  implementation can be replaced or faked in tests.
* **Bounded execution.** The audio duration is bounded by the caller, the
  decoder output is bounded by ``max_new_tokens``, the number of segments is
  bounded by ``max_segments``, and the wall-clock time is checked between
  decoded segments.

Two implementations exist:

* ``process`` (the default) runs the model in a child process, so the
  timeout is a real timeout -- see :mod:`app.transcription_process`.
* ``inline`` runs the model in the worker itself. It is the fallback for
  environments where a child process cannot be started, and its timeout is a
  *soft* one: it is checked between decoded segments, so a single slow
  inference can overshoot it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ErrorCode
from app.interruption import WorkInterrupted
from app.transcripts import (
    Transcript,
    TranscriptValidationError,
    validate_segments,
)

logger = logging.getLogger("clipforge.transcription")

#: How often the progress callback is called while segments are decoded.
_PROGRESS_INTERVAL_SECONDS = 2.0

#: The Whisper models this build knows about.
#:
#: A name that is not in this list is still accepted when it looks like a
#: model repository on the Hugging Face hub (``owner/name``) or points at a
#: local model directory, so a custom fine-tune can be used. A name that is
#: neither is a configuration mistake and is refused before anything is
#: downloaded.
KNOWN_MODELS: frozenset[str] = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large",
        "large-v1",
        "large-v2",
        "large-v3",
        "large-v3-turbo",
        "turbo",
        "distil-small.en",
        "distil-medium.en",
        "distil-large-v2",
        "distil-large-v3",
    }
)

#: A model name that is neither a known size nor a repository id.
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A model repository id, such as ``owner/name``.
_MODEL_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)


class TranscriptionError(Exception):
    """The speech could not be transcribed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TranscriptionSettings(BaseSettings):
    """Configuration for the local speech-to-text engine.

    Every field is read from a ``CLIPFORGE_TRANSCRIPTION_*`` environment
    variable, for example ``CLIPFORGE_TRANSCRIPTION_MODEL=base.en``. The
    engine's settings live here rather than in :mod:`app.config` so that the
    main settings module stays about the application rather than about one
    engine's internals.
    """

    model_config = SettingsConfigDict(
        env_prefix="CLIPFORGE_TRANSCRIPTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    #: The Whisper model to use. ``tiny.en`` is small enough to run on any
    #: development machine; ``base.en`` and ``small.en`` are more accurate and
    #: proportionally slower.
    model: str = "tiny.en"

    #: Where the model runs. ``cpu`` is the default because a GPU is not
    #: required and not assumed.
    device: Literal["cpu", "cuda", "auto"] = "cpu"

    #: How the inference is run.
    #:
    #: ``process`` puts the model in a child process, which is what makes the
    #: timeout enforceable: a stalled inference is stopped by ending the
    #: process that is doing it. ``inline`` runs the model in the worker
    #: itself; its timeout is only checked between decoded segments, so a
    #: single slow inference can overshoot it.
    isolation: Literal["process", "inline"] = "process"

    #: The quantisation used by the decoder. Left unset, it is derived from
    #: the device: ``int8`` on the CPU, ``float16`` on a GPU.
    compute_type: str | None = None

    #: The spoken language. Left unset, the engine detects it.
    language: str | None = None

    #: Where the model files are cached. Left unset, the worker uses
    #: ``<storage root>/models``.
    cache_dir: Path | None = None

    #: When true the engine never reaches the network. A model that is not in
    #: the cache produces an actionable error instead.
    offline: bool = False

    #: How long a single transcription may run.
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)

    #: How many transcriptions may run at the same time.
    #:
    #: In process-isolated mode there is exactly one inference process, so
    #: the effective concurrency is one whatever this says.
    max_concurrency: int = Field(default=1, ge=1, le=8)

    #: The beam size used by the decoder.
    beam_size: int = Field(default=5, ge=1, le=10)

    #: Whether the voice-activity filter is used to skip silence.
    vad_filter: bool = True

    #: An upper bound on the number of tokens the decoder may produce.
    max_new_tokens: int | None = Field(default=None, ge=1)

    #: The largest number of segments one transcript may hold.
    #:
    #: The default is far above what a four-hour recording produces at a
    #: normal speaking rate, and exists so that a malformed model output
    #: cannot make the worker collect an unbounded number of segments.
    max_segments: int = Field(default=50000, ge=1, le=1000000)

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        """Refuse a model name that could never work.

        The check is a configuration check, not a load: nothing is
        downloaded and no model is touched, so it is safe to run while the
        API starts up.
        """
        name = value.strip()
        if not name:
            raise ValueError("the transcription model name must not be empty")
        if name in KNOWN_MODELS:
            return name
        if _MODEL_REPOSITORY.match(name):
            return name
        if Path(name).is_dir():
            return name
        if _MODEL_NAME.match(name):
            raise ValueError(
                f"{name!r} is not a known transcription model. Use one of "
                f"{', '.join(sorted(KNOWN_MODELS))}, a model repository id "
                "such as 'owner/name', or the path of a local model directory."
            )
        raise ValueError(
            f"{value!r} is not a usable transcription model name. Use a model "
            "size such as 'tiny.en', a model repository id such as "
            "'owner/name', or the path of a local model directory."
        )


class TranscriptionEngine(Protocol):
    """Turns an audio file into a validated transcript."""

    name: str

    def transcribe(
        self,
        audio_path: Path,
        *,
        on_progress: Callable[[float], None] | None = None,
        duration_seconds: float | None = None,
    ) -> Transcript:
        """Transcribe ``audio_path``.

        Args:
            audio_path: The audio file to read.
            on_progress: Called with a fraction between 0 and 1 as the
                transcription advances. The fraction is an *estimate* derived
                from the decoded position, not an exact percentage.
            duration_seconds: The duration of the audio, as measured from the
                extracted file itself. When it is given it is the duration the
                timestamps are normalised against, so a segment can never be
                placed outside the audio that was actually transcribed.

        Raises:
            TranscriptionError: with a stable error code for every failure.
        """
        ...

    def terminate(self) -> None:
        """Stop anything the engine started, if it started anything."""
        ...


def model_is_cached(settings: TranscriptionSettings, cache_dir: Path) -> bool:
    """Whether the configured model is already on disk.

    This never touches the network, so it is safe to call from an API
    request that only wants to tell the user what to expect.

    The cache is a Hugging Face style directory: ``models--<owner>--<repo>``
    with a ``snapshots/<hash>`` directory inside it. The match is exact, so a
    request for ``base`` is not satisfied by a cached ``base.en`` -- which
    would otherwise turn a clear "the model is not cached" error into a
    confusing download attempt.
    """
    name = settings.model
    if not name:
        return False
    candidate = Path(name)
    if candidate.is_dir():
        return True

    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return False

    # A bare model size is cached under the ``faster-whisper`` repository; a
    # model repository id is cached under its own name.
    if "/" in name:
        wanted = name.replace("/", "--").lower()
    else:
        wanted = f"faster-whisper-{name}".lower()

    for folder in cache_dir.glob("models--*"):
        if not folder.is_dir():
            continue
        folder_name = folder.name.lower()
        if not (
            folder_name == f"models--{wanted}" or folder_name.endswith(f"--{wanted}")
        ):
            continue
        snapshots = folder / "snapshots"
        if not snapshots.is_dir():
            continue
        for snapshot in snapshots.iterdir():
            if (snapshot / "model.bin").is_file():
                return True
    return False


class FasterWhisperEngine:
    """The inline engine: a local faster-whisper model in this process.

    This is the fallback for environments where a child process cannot be
    started. Its timeout is a *soft* one: it is checked between decoded
    segments, so a single slow inference can overshoot it.
    """

    name = "faster-whisper"

    def __init__(
        self,
        settings: TranscriptionSettings,
        *,
        cache_dir: Path,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self.settings = settings
        self.cache_dir = Path(cache_dir)
        self.sample_rate = sample_rate
        self.channels = channels
        self._model = None
        self._model_lock = threading.Lock()
        self._slots = threading.Semaphore(settings.max_concurrency)

    # -- model handling ------------------------------------------------------

    @property
    def model_name(self) -> str:
        """The name of the model this engine will use."""
        return self.settings.model

    def model_is_cached(self) -> bool:
        """Whether the configured model is already on disk."""
        return model_is_cached(self.settings, self.cache_dir)

    def terminate(self) -> None:
        """Nothing to stop: the model runs in this process."""
        return None

    def load_model(self):
        """Load the model, or return the one that is already loaded."""
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            self._model = self._create_model()
            return self._model

    def _create_model(self):
        if self.settings.offline and not self.model_is_cached():
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
                "The local transcription model is not in the cache and offline "
                "mode is enabled, so it cannot be downloaded. Run the model "
                "download command once while online, or turn off offline mode.",
            )
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
                "The faster-whisper package is not installed, so speech cannot "
                "be transcribed.",
            ) from exc

        compute_type = self.settings.compute_type or self._default_compute_type()
        try:
            logger.info(
                "loading transcription model %s (%s, %s)",
                self.settings.model,
                self.settings.device,
                compute_type,
            )
            return WhisperModel(
                self.settings.model,
                device=self.settings.device,
                compute_type=compute_type,
                download_root=str(self.cache_dir),
                # In offline mode the model may only be read from the cache.
                local_files_only=self.settings.offline,
            )
        except TranscriptionError:
            raise
        except Exception as exc:
            logger.warning(
                "the transcription model %s could not be loaded: %s",
                self.settings.model,
                exc,
            )
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
                "The local transcription model could not be loaded. It may not "
                "be downloaded yet, or the cache may be incomplete.",
            ) from exc

    def _default_compute_type(self) -> str:
        if self.settings.device == "cuda":
            return "float16"
        if self.settings.device == "auto":
            return "default"
        return "int8"

    # -- transcription -------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        on_progress: Callable[[float], None] | None = None,
        duration_seconds: float | None = None,
    ) -> Transcript:
        """Transcribe ``audio_path`` into a validated transcript."""
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise TranscriptionError(
                ErrorCode.SOURCE_NOT_FOUND.value,
                "The extracted audio could not be read.",
            )

        with self._slots:
            return self._transcribe_locked(
                audio_path,
                on_progress=on_progress,
                duration_seconds=duration_seconds,
            )

    def _transcribe_locked(
        self,
        audio_path: Path,
        *,
        on_progress: Callable[[float], None] | None,
        duration_seconds: float | None = None,
    ) -> Transcript:
        model = self.load_model()
        started = time.monotonic()
        deadline = started + self.settings.timeout_seconds

        try:
            segments, info = model.transcribe(
                str(audio_path),
                language=self.settings.language,
                beam_size=self.settings.beam_size,
                vad_filter=self.settings.vad_filter,
                max_new_tokens=self.settings.max_new_tokens,
            )
        except (TranscriptionError, WorkInterrupted):
            raise
        except Exception as exc:
            logger.warning("the transcription could not be started: %s", exc)
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_FAILED.value,
                "The audio could not be transcribed.",
            ) from exc

        # The duration of the audio that was actually decoded wins over the
        # one the caller measured, but the caller's measurement is used when
        # the engine cannot report one: without a duration no timestamp can
        # be checked against the audio.
        duration = _read_duration(info) or duration_seconds

        collected: list[object] = []
        last_report = started
        try:
            for segment in segments:
                if time.monotonic() > deadline:
                    raise TranscriptionError(
                        ErrorCode.TRANSCRIPTION_TIMEOUT.value,
                        "The transcription did not finish in time.",
                    )
                if len(collected) >= self.settings.max_segments:
                    logger.warning(
                        "the transcript reached the limit of %s segments and "
                        "was cut short",
                        self.settings.max_segments,
                    )
                    break
                collected.append(segment)

                if on_progress is not None and duration:
                    now = time.monotonic()
                    if now - last_report >= _PROGRESS_INTERVAL_SECONDS:
                        last_report = now
                        position = _read_float(getattr(segment, "end", None))
                        fraction = 0.0 if position is None else min(
                            1.0, max(0.0, position / duration)
                        )
                        on_progress(fraction)
        except (TranscriptionError, WorkInterrupted):
            raise
        except Exception as exc:
            logger.warning("the transcription failed: %s", exc)
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_FAILED.value,
                "The audio could not be transcribed.",
            ) from exc

        if time.monotonic() > deadline:
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_TIMEOUT.value,
                "The transcription did not finish in time.",
            )

        try:
            validated = validate_segments(
                collected,
                duration_seconds=duration,
                max_segments=self.settings.max_segments,
            )
        except TranscriptValidationError as exc:
            logger.warning("the transcript failed validation: %s", exc.message)
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_INVALID_OUTPUT.value,
                "The transcription produced timestamps that could not be trusted.",
            ) from exc

        language = _read_language(info)
        logger.info(
            "transcribed %s segments in %.1fs (language=%s)",
            len(validated),
            time.monotonic() - started,
            language or "unknown",
        )

        return Transcript(
            language=language,
            model_name=self.settings.model,
            audio_sample_rate=self.sample_rate,
            audio_channels=self.channels,
            duration_seconds=duration,
            segments=tuple(validated),
        )


def build_engine(
    settings: TranscriptionSettings,
    *,
    cache_dir: Path,
    sample_rate: int = 16000,
    channels: int = 1,
) -> TranscriptionEngine:
    """Return the engine for the current configuration.

    In the default ``process`` mode the model runs in a child process, so
    the timeout can actually be enforced. The ``inline`` mode keeps the model
    in this process and is only a fallback.
    """
    if settings.isolation == "inline":
        return FasterWhisperEngine(
            settings,
            cache_dir=cache_dir,
            sample_rate=sample_rate,
            channels=channels,
        )

    from app.transcription_process import ProcessTranscriptionEngine

    return ProcessTranscriptionEngine(
        settings,
        cache_dir=cache_dir,
        sample_rate=sample_rate,
        channels=channels,
    )


def resolve_cache_dir(settings: TranscriptionSettings, storage_root: Path) -> Path:
    """Where the model files are cached.

    An explicit setting wins. Otherwise the models live under the storage
    root, so that everything the application owns stays on one disk and can be
    deleted by removing the storage directory.
    """
    if settings.cache_dir is not None:
        return Path(settings.cache_dir).expanduser().resolve()
    return Path(storage_root).expanduser().resolve() / "models"


def _read_duration(info: object) -> float | None:
    return _read_float(getattr(info, "duration", None))


def _read_language(info: object) -> str | None:
    value = getattr(info, "language", None)
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _read_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
