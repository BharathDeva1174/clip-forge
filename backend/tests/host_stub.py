"""A stand-in for the inference process.

It speaks the same line-delimited JSON protocol as
:mod:`app.transcription_host`, but it does no real work. The mode is chosen
with an environment variable, so a test can make the inference behave in the
way it is trying to provoke:

``normal``
    Answer with two segments, as a working engine would.
``stall``
    Never answer. This is a stalled inference: the parent has to be able to
    stop it.
``stall_once``
    Stall the first time only, then behave normally. The stall is recorded
    in a file, so it also applies to the child that replaces the one that
    was stopped.
``crash``
    Die without answering.
``crash_midway``
    Die while the transcription is running.
``error``
    Answer with a failure.
``slow``
    Wait for ``CLIPFORGE_TEST_HOST_DELAY`` seconds and then answer.

The script never touches the network and never loads a model, so the tests
that use it are fast, deterministic and offline.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("CLIPFORGE_TEST_HOST_MODE", "normal")
DELAY = float(os.environ.get("CLIPFORGE_TEST_HOST_DELAY", "5"))
STATE = os.environ.get("CLIPFORGE_TEST_HOST_STATE")
PIDFILE = os.environ.get("CLIPFORGE_TEST_HOST_PIDFILE")


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _read() -> dict | None:
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


def _stalled_before() -> bool:
    """Whether the one permitted stall has already happened."""
    if not STATE:
        return False
    if os.path.exists(STATE):
        return True
    with open(STATE, "w", encoding="utf-8") as handle:
        handle.write("stalled")
    return False


def main() -> int:
    if PIDFILE:
        with open(PIDFILE, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")

    request = _read()
    if request is None:
        return 0

    if MODE == "crash":
        os._exit(3)

    _write({"op": "ready", "model_loaded": True})

    while True:
        request = _read()
        if request is None:
            return 0
        if request.get("op") != "transcribe":
            continue

        if MODE == "stall" or (MODE == "stall_once" and not _stalled_before()):
            # A stalled inference: no answer, ever.
            while True:
                time.sleep(3600)

        if MODE == "error":
            _write(
                {
                    "op": "error",
                    "code": "transcription_failed",
                    "message": "the stub host was told to fail",
                }
            )
            continue

        if MODE == "crash_midway":
            os._exit(4)

        if MODE == "slow":
            time.sleep(DELAY)

        _write(
            {
                "op": "segment",
                "start": 0.0,
                "end": 1.0,
                "text": "hello world",
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
            }
        )
        _write({"op": "progress", "fraction": 0.5})
        _write(
            {
                "op": "segment",
                "start": 1.0,
                "end": 2.0,
                "text": "second segment",
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
            }
        )
        _write({"op": "done", "language": "en", "duration": 2.0})


if __name__ == "__main__":
    main()
