"""The isolated inference process.

The speech model runs in a child process of the worker, not inside it. That
is what makes the transcription timeout a real timeout: the model decodes
audio in C++ and does not react to a Python-level interruption, so the only
way to stop a stalled inference is to stop the process that is doing it.

The parent speaks a small line-delimited JSON protocol over the child's
standard input and output:

* the parent sends one ``configure`` message with the model settings, and
  then one ``transcribe`` message per job;
* the child answers with ``ready``, then a stream of ``segment`` and
  ``progress`` messages, and finally ``done`` or ``error``.

Only these messages are ever written to standard output. Everything else --
the model library's own logging, tracebacks -- goes to standard error, which
the parent captures into a file and truncates. A stray ``print`` in a
dependency therefore cannot corrupt the protocol.

The child never reads the environment for its configuration: everything it
needs arrives through the pipe, so the parent's settings are the only truth.
The audio path travels through the pipe too, so no media path and no secret
ever appears in a process argument list.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger("clipforge.transcription_host")

#: How often the child reports its progress while it decodes.
_PROGRESS_INTERVAL_SECONDS = 2.0

#: The error code used when the model itself cannot be loaded.
_MODEL_UNAVAILABLE = "transcription_model_unavailable"

#: The error code used when the audio could not be decoded.
_FAILED = "transcription_failed"

#: The error code used when the child's own deadline is reached.
_TIMEOUT = "transcription_timeout"


def _write(payload: dict[str, Any]) -> None:
    """Send one message to the parent."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read() -> dict[str, Any] | None:
    """Read one message from the parent.

    ``None`` means the parent closed the pipe, which happens when the worker
    stops. The child then exits instead of lingering as an orphan.
    """
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _forbid_the_network() -> None:
    """Make any outbound connection fail.

    The speech model is a local file, so the inference process has no reason
    to open a socket. Blocking it here turns "nothing leaves this machine"
    from a promise into something the process itself enforces: if a
    dependency ever tried to fetch something, the attempt would fail loudly
    instead of quietly sending data out.
    """
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("the inference process may not use the network")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]


def _load_model(config: dict[str, Any]):
    """Load the speech model described by ``config``."""
    from faster_whisper import WhisperModel

    device = str(config.get("device") or "cpu")
    compute_type = config.get("compute_type") or (
        "float16" if device == "cuda" else "default" if device == "auto" else "int8"
    )
    return WhisperModel(
        str(config.get("model") or "tiny.en"),
        device=device,
        compute_type=str(compute_type),
        download_root=str(config.get("cache_dir") or ""),
        # In offline mode the model may only be read from the local cache.
        local_files_only=bool(config.get("offline")),
    )


def _transcribe(model, config: dict[str, Any], request: dict[str, Any]) -> None:
    """Transcribe one audio file and report the result to the parent."""
    audio_path = str(request.get("audio_path") or "")
    timeout_seconds = float(config.get("timeout_seconds") or 3600)
    max_segments = int(config.get("max_segments") or 50000)
    deadline = time.monotonic() + timeout_seconds

    try:
        segments, info = model.transcribe(
            audio_path,
            language=config.get("language"),
            beam_size=int(config.get("beam_size") or 5),
            vad_filter=bool(config.get("vad_filter", True)),
            max_new_tokens=config.get("max_new_tokens"),
        )
    except Exception as exc:  # noqa: BLE001 - the parent maps this to a code
        logger.warning("the transcription could not be started: %s", exc)
        _write({"op": "error", "code": _FAILED, "message": str(exc)[:500]})
        return

    duration = _read_float(getattr(info, "duration", None))
    language = getattr(info, "language", None)
    if not isinstance(language, str) or not language.strip():
        language = None

    last_report = time.monotonic()
    count = 0
    try:
        for segment in segments:
            if time.monotonic() > deadline:
                _write(
                    {
                        "op": "error",
                        "code": _TIMEOUT,
                        "message": "The transcription did not finish in time.",
                    }
                )
                return
            count += 1
            if count > max_segments:
                break
            _write(
                {
                    "op": "segment",
                    "start": _read_float(getattr(segment, "start", None)),
                    "end": _read_float(getattr(segment, "end", None)),
                    "text": getattr(segment, "text", "") or "",
                    "avg_logprob": _read_float(getattr(segment, "avg_logprob", None)),
                    "no_speech_prob": _read_float(
                        getattr(segment, "no_speech_prob", None)
                    ),
                }
            )
            now = time.monotonic()
            if now - last_report >= _PROGRESS_INTERVAL_SECONDS:
                last_report = now
                position = _read_float(getattr(segment, "end", None))
                fraction = 0.0
                if duration and position is not None:
                    fraction = max(0.0, min(1.0, position / duration))
                _write({"op": "progress", "fraction": fraction})
    except Exception as exc:  # noqa: BLE001 - the parent maps this to a code
        logger.warning("the transcription failed: %s", exc)
        _write({"op": "error", "code": _FAILED, "message": str(exc)[:500]})
        return

    _write({"op": "done", "language": language, "duration": duration})


def _read_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def main() -> int:
    """Run the host until the parent closes the pipe."""
    # The model library logs to standard error, which is what the parent
    # captures. Nothing may write to standard output except the protocol.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    _forbid_the_network()
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    request = _read()
    if request is None:
        return 0
    config = request.get("config")
    if not isinstance(config, dict):
        config = {}

    try:
        model = _load_model(config)
    except Exception as exc:  # noqa: BLE001 - the parent maps this to a code
        logger.warning("the transcription model could not be loaded: %s", exc)
        _write(
            {
                "op": "error",
                "code": _MODEL_UNAVAILABLE,
                "message": "The local transcription model could not be loaded.",
            }
        )
        return 1

    _write({"op": "ready", "model_loaded": True})

    while True:
        request = _read()
        if request is None:
            # The worker is gone. Exit rather than linger as an orphan.
            return 0
        if request.get("op") != "transcribe":
            continue
        _transcribe(model, config, request)


if __name__ == "__main__":
    # A prompt, deterministic exit matters for a process whose whole purpose
    # is to be stoppable, so the interpreter is left without a chance to hang
    # in an atexit handler.
    os._exit(main())
