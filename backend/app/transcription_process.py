"""The supervisor for the isolated inference process.

This is the parent half of the contract implemented in
:mod:`app.transcription_host`. It starts the child, hands it the model
configuration, sends it work, and enforces the deadline.

The timeout contract
--------------------

``CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS`` is a **hard** timeout when the
engine runs in process-isolated mode. The guarantee is:

    A transcription that has not finished within the configured number of
    seconds is stopped, and the child process that was doing the inference
    is terminated and reaped. The worker carries on with the next job.

The bound is the configured timeout plus a short termination grace period:
the child is asked to stop, and killed if it does not, so an inference that
has stalled cannot occupy the worker for longer than that.

What makes this possible is that the child is a separate operating-system
process. The model decodes audio in C++ and releases the GIL, so a Python
level interruption cannot reach it. Only a process boundary can.

Everything the child needs travels over a pipe, never through a command
line, so no media path and no secret is ever visible in a process argument
list. The child's standard error goes to a temporary file, and only a
truncated tail of it is ever logged.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.errors import ErrorCode
from app.transcription import (
    TranscriptionEngine,
    TranscriptionError,
    TranscriptionSettings,
    model_is_cached,
)
from app.transcripts import Transcript, TranscriptValidationError, validate_segments

logger = logging.getLogger("clipforge.transcription")

#: The directory that holds the ``app`` package, so the child can import it.
_BACKEND_DIR = Path(__file__).resolve().parent.parent

#: The script that hosts the inference process.
_DEFAULT_HOST = Path(__file__).resolve().parent / "transcription_host.py"

#: How many messages may wait for the parent before the reader stops.
#:
#: The queue is bounded so that a child that produces output faster than the
#: parent consumes it cannot make the parent use an unbounded amount of
#: memory. When the queue is full the child simply blocks on its pipe, and
#: the deadline still applies.
_MESSAGE_QUEUE_SIZE = 2000

#: How long the parent waits for the child to stop before killing it.
_TERMINATE_GRACE_SECONDS = 2.0

#: How long the parent waits for a killed child to be reaped.
_KILL_GRACE_SECONDS = 2.0

#: How much of the child's standard error is kept for the log.
_MAX_DIAGNOSTIC_BYTES = 2000

#: How often the parent reports that it is still waiting.
#:
#: The heartbeat renews the worker's lease while the child is decoding a
#: single window. It is deliberately infrequent: it writes to the database.
_HEARTBEAT_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class _RawSegment:
    """One segment as the child reported it."""

    start: float | None
    end: float | None
    text: str
    avg_logprob: float | None
    no_speech_prob: float | None


class _ChildError(TranscriptionError):
    """A failure the child reported about the work it was given.

    The child is still healthy: it caught the problem and is waiting for
    the next request, so it is kept and the model is not loaded again.
    """


class ProcessTranscriptionEngine:
    """Runs the speech engine in a child process.

    The child is started the first time a transcription is asked for, kept
    for later jobs so that the model is loaded only once, and stopped when
    the worker shuts down.
    """

    name = "faster-whisper"

    def __init__(
        self,
        settings: TranscriptionSettings,
        *,
        cache_dir: Path,
        sample_rate: int = 16000,
        channels: int = 1,
        host_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.cache_dir = Path(cache_dir)
        self.sample_rate = sample_rate
        self.channels = channels
        self.host_path = Path(host_path) if host_path is not None else _DEFAULT_HOST
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict] = queue.Queue(maxsize=_MESSAGE_QUEUE_SIZE)
        self._reader: threading.Thread | None = None
        self._reader_stop: threading.Event | None = None
        self._diagnostics = None
        # `_lock` serialises the requests: there is one child, so there is
        # one transcription at a time. `_child_lock` guards the child itself
        # and is deliberately a *different* lock, so that `terminate()` can
        # stop a running child from another thread -- or from the progress
        # callback of the request that is running -- without deadlocking.
        self._lock = threading.Lock()
        self._child_lock = threading.RLock()

    # -- public API ----------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        on_progress: Callable[[float], None] | None = None,
        duration_seconds: float | None = None,
    ) -> Transcript:
        """Transcribe ``audio_path`` in the child process.

        Raises:
            TranscriptionError: with a stable error code for every failure
                mode, including a timeout.
        """
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise TranscriptionError(
                ErrorCode.SOURCE_NOT_FOUND.value,
                "The extracted audio could not be read.",
            )

        with self._lock:
            deadline = time.monotonic() + self.settings.timeout_seconds
            try:
                segments, language, engine_duration = self._run(
                    audio_path, deadline=deadline, on_progress=on_progress
                )
            except _ChildError:
                # The child is healthy; it reported a problem with the work.
                raise
            except Exception:
                # The child is in an unknown state: it may still be decoding.
                # It is stopped here, so an interrupted or failed request
                # never leaves a process behind.
                self._stop_child()
                raise

        duration = duration_seconds if duration_seconds is not None else engine_duration
        try:
            validated = validate_segments(
                segments,
                duration_seconds=duration,
                max_segments=self.settings.max_segments,
            )
        except TranscriptValidationError as exc:
            logger.warning("the transcript failed validation: %s", exc.message)
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_INVALID_OUTPUT.value,
                "The transcription produced timestamps that could not be trusted.",
            ) from exc

        return Transcript(
            language=language,
            model_name=self.settings.model,
            audio_sample_rate=self.sample_rate,
            audio_channels=self.channels,
            duration_seconds=duration,
            segments=tuple(validated),
        )

    def terminate(self) -> None:
        """Stop the child process, if there is one.

        This never blocks on a running transcription: it stops the child, and
        the request that was using it sees the child go away.
        """
        with self._child_lock:
            self._stop_child()

    # -- internals ----------------------------------------------------------

    def _run(
        self,
        audio_path: Path,
        *,
        deadline: float,
        on_progress: Callable[[float], None] | None,
    ) -> tuple[list[_RawSegment], str | None, float | None]:
        self._ensure_child(deadline)
        self._send({"op": "transcribe", "audio_path": str(audio_path)})
        return self._collect(deadline, on_progress)

    def _ensure_child(self, deadline: float) -> None:
        """Start the child if it is not already running."""
        with self._child_lock:
            process = self._process
            if process is not None and process.poll() is None:
                return

            self._stop_child()
            self._check_model_is_available()
            self._start_child()
        self._send({"op": "configure", "config": self._config()})
        self._await_ready(deadline)

    def _check_model_is_available(self) -> None:
        """Refuse to start when the model is not cached and cannot be fetched.

        The check happens before the child exists, so an offline job never
        even reaches the network: the child is not started at all.
        """
        if not self.settings.offline:
            return
        if model_is_cached(self.settings, self.cache_dir):
            return
        raise TranscriptionError(
            ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
            "The local transcription model is not in the cache and offline "
            "mode is enabled, so it cannot be downloaded. Run the model "
            "download command once while online, or turn off offline mode.",
        )

    def _start_child(self) -> None:
        """Start the inference process."""
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{_BACKEND_DIR}{os.pathsep}{existing}" if existing else str(_BACKEND_DIR)
        )
        self._diagnostics = tempfile.TemporaryFile()
        args = [sys.executable, str(self.host_path)]
        try:
            self._process = subprocess.Popen(  # noqa: S603 - argument list, never a shell
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._diagnostics,
                cwd=str(_BACKEND_DIR),
                env=environment,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._diagnostics.close()
            self._diagnostics = None
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_FAILED.value,
                "The isolated transcription process could not be started.",
            ) from exc

        # Each reader has its own stop flag, so a reader that is still
        # finishing cannot be confused with the reader of a newer child.
        self._reader_stop = threading.Event()
        self._reader = threading.Thread(
            target=self._read_messages,
            args=(self._process, self._messages, self._reader_stop),
            name="clipforge-transcription-reader",
            daemon=True,
        )
        self._reader.start()

    def _config(self) -> dict[str, object]:
        """The configuration the child needs."""
        return {
            "model": self.settings.model,
            "device": self.settings.device,
            "compute_type": self.settings.compute_type,
            "cache_dir": str(self.cache_dir),
            "offline": self.settings.offline,
            "language": self.settings.language,
            "beam_size": self.settings.beam_size,
            "vad_filter": self.settings.vad_filter,
            "max_new_tokens": self.settings.max_new_tokens,
            "max_segments": self.settings.max_segments,
            "timeout_seconds": self.settings.timeout_seconds,
        }

    def _send(self, payload: dict[str, object]) -> None:
        """Send one message to the child."""
        process = self._process
        if process is None or process.stdin is None:
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_FAILED.value,
                "The isolated transcription process is not running.",
            )
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise TranscriptionError(
                ErrorCode.TRANSCRIPTION_FAILED.value,
                "The isolated transcription process stopped unexpectedly.",
            ) from exc

    def _await_ready(self, deadline: float) -> None:
        """Wait for the child to report that it is ready."""
        while True:
            message = self._next_message(deadline, heartbeat=None)
            if message.get("op") == "ready":
                return
            if message.get("op") == "error":
                self._stop_child()
                raise TranscriptionError(
                    str(message.get("code") or ErrorCode.TRANSCRIPTION_FAILED.value),
                    str(message.get("message") or "The transcription model is unavailable."),
                )
            if message.get("op") == "_eof":
                # The child died before it was ready. It never said why, so
                # the cause is unknown rather than a missing model.
                self._stop_child()
                raise TranscriptionError(
                    ErrorCode.TRANSCRIPTION_FAILED.value,
                    "The isolated transcription process stopped unexpectedly.",
                )

    def _collect(
        self,
        deadline: float,
        on_progress: Callable[[float], None] | None,
    ) -> tuple[list[_RawSegment], str | None, float | None]:
        """Read the child's answer until it is finished."""
        segments: list[_RawSegment] = []
        language: str | None = None
        duration: float | None = None
        fraction = 0.0

        while True:
            message = self._next_message(
                deadline, heartbeat=(lambda: on_progress(fraction)) if on_progress else None
            )
            operation = message.get("op")
            if operation == "segment":
                segments.append(
                    _RawSegment(
                        start=_read_float(message.get("start")),
                        end=_read_float(message.get("end")),
                        text=str(message.get("text") or ""),
                        avg_logprob=_read_float(message.get("avg_logprob")),
                        no_speech_prob=_read_float(message.get("no_speech_prob")),
                    )
                )
            elif operation == "progress":
                reported = _read_float(message.get("fraction"))
                if reported is not None:
                    fraction = max(0.0, min(1.0, reported))
                if on_progress is not None:
                    on_progress(fraction)
            elif operation == "done":
                language = message.get("language")
                if not isinstance(language, str) or not language.strip():
                    language = None
                else:
                    language = language.strip().lower()
                duration = _read_float(message.get("duration"))
                return segments, language, duration
            elif operation == "error":
                code = str(message.get("code") or ErrorCode.TRANSCRIPTION_FAILED.value)
                detail = str(message.get("message") or "The audio could not be transcribed.")
                if code == ErrorCode.TRANSCRIPTION_TIMEOUT.value:
                    # The child gave up on its own deadline. It may be wedged,
                    # so it is stopped rather than kept.
                    self._stop_child()
                    raise TranscriptionError(
                        code, "The transcription did not finish in time."
                    )
                raise _ChildError(code, _safe_message(code, detail))
            elif operation == "_eof":
                self._stop_child()
                raise TranscriptionError(
                    ErrorCode.TRANSCRIPTION_FAILED.value,
                    "The isolated transcription process stopped unexpectedly.",
                )

    def _next_message(
        self,
        deadline: float,
        *,
        heartbeat: Callable[[], None] | None,
    ) -> dict:
        """Wait for the next message from the child, or give up."""
        last_beat = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop_child()
                raise TranscriptionError(
                    ErrorCode.TRANSCRIPTION_TIMEOUT.value,
                    "The transcription did not finish in time.",
                )
            try:
                return self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                # Nothing happened for a moment. The child may be busy
                # decoding a single window, which is exactly the case the
                # timeout exists for. A heartbeat keeps the worker's lease
                # alive while the parent waits, without writing to the
                # database on every poll.
                if heartbeat is not None and time.monotonic() - last_beat >= (
                    _HEARTBEAT_INTERVAL_SECONDS
                ):
                    last_beat = time.monotonic()
                    heartbeat()

    def _read_messages(
        self,
        process: subprocess.Popen[str],
        messages: queue.Queue[dict],
        stop: threading.Event,
    ) -> None:
        """Read the child's output until it closes the pipe."""
        try:
            stdout = process.stdout
            if stdout is None:
                return
            for line in stdout:
                if stop.is_set():
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                while not stop.is_set():
                    try:
                        messages.put(payload, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    return
        except (OSError, ValueError):
            pass
        finally:
            while not stop.is_set():
                try:
                    messages.put({"op": "_eof"}, timeout=0.5)
                    break
                except queue.Full:
                    continue

    def _stop_child(self) -> None:
        """Stop the child process and reap it."""
        with self._child_lock:
            process = self._process
            self._process = None
            stop = self._reader_stop
            self._reader_stop = None
            if stop is not None:
                stop.set()

            if process is not None:
                _close_quietly(process.stdin)
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=_KILL_GRACE_SECONDS)
                        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                            logger.warning("the inference process could not be reaped")
                _close_quietly(process.stdout)
                self._log_diagnostics(process)

            reader = self._reader
            self._reader = None
            if reader is not None:
                reader.join(timeout=_KILL_GRACE_SECONDS)

            if self._diagnostics is not None:
                self._diagnostics.close()
                self._diagnostics = None

            # Drop anything the dead child left behind, so a stale message can
            # never be read as the answer to the next request.
            while True:
                try:
                    self._messages.get_nowait()
                except queue.Empty:
                    break

    def _log_diagnostics(self, process: subprocess.Popen[str]) -> None:
        """Log the tail of the child's standard error."""
        if self._diagnostics is None:
            return
        try:
            self._diagnostics.seek(0)
            detail = self._diagnostics.read(_MAX_DIAGNOSTIC_BYTES)
        except (OSError, ValueError):  # pragma: no cover - defensive
            return
        if not detail:
            return
        text = detail.decode("utf-8", "replace").strip()
        if text:
            logger.warning(
                "the inference process (pid %s) reported: %s",
                process.pid,
                text,
            )


def _close_quietly(stream) -> None:  # noqa: ANN001
    """Close a pipe without letting the error escape."""
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):  # pragma: no cover - defensive
        pass


def _read_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _safe_message(code: str, detail: str) -> str:
    """Turn a child's message into one that is safe to show a user."""
    if code == ErrorCode.TRANSCRIPTION_TIMEOUT.value:
        return "The transcription did not finish in time."
    if code == ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value:
        return "The local transcription model could not be loaded."
    return "The audio could not be transcribed."
