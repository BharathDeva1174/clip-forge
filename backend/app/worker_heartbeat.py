"""Is the worker actually running?

The API and the worker are two separate processes. The API can say that it
is alive and that its configuration loaded, but it cannot see the worker:
a job can sit in the queue for ever with nothing to explain why.

So the worker leaves a small note behind. It writes a heartbeat file into
the storage root every few seconds, and the API reads it to say how long
ago the worker was last seen.

It is a file rather than a row on purpose: the heartbeat is about the
*process*, not about a job, it changes constantly, and it must not need a
migration or a transaction to read. A file that is missing or old says
the same thing — the worker is not running — and that is the answer the
user needs.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("clipforge.worker")

#: The name of the file the worker leaves behind.
HEARTBEAT_FILENAME = "worker-heartbeat.json"

#: How old a heartbeat may be and still count as "the worker is running".
#:
#: The worker writes one every few seconds, so half a minute is generous
#: without ever saying "running" about a worker that has gone away.
HEARTBEAT_MAX_AGE_SECONDS = 30.0

#: How often the worker writes one.
HEARTBEAT_INTERVAL_SECONDS = 5.0

#: How long a full warning about an unwritable heartbeat is suppressed after
#: the previous one. The write repeats every five seconds; without this, one
#: stuck file produces a traceback every five seconds until it clears.
_WRITE_WARNING_INTERVAL_SECONDS = 300.0

#: When the last full warning was logged (monotonic seconds); 0.0 logs at once.
_last_write_warning = 0.0


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """What the worker last said about itself."""

    worker_id: str
    at: datetime
    age_seconds: float

    @property
    def running(self) -> bool:
        """Whether the worker looks alive."""
        return self.age_seconds <= HEARTBEAT_MAX_AGE_SECONDS


def heartbeat_path(storage_root: Path) -> Path:
    """Where the heartbeat file lives."""
    return Path(storage_root) / HEARTBEAT_FILENAME


def write_heartbeat(storage_root: Path, worker_id: str) -> None:
    """Leave a note that the worker is alive.

    The file is written to a temporary name and then renamed, so a reader
    never sees half a note. The temporary name is unique per process: a
    fixed name can be held by a reader or a scanner for a while, and
    reusing it turns one slow write into a permanent ``PermissionError``
    every five seconds (observed with antivirus locks).

    A failure here is never fatal: the worker must keep working even if it
    cannot write to the storage directory. The warning is rate-limited for
    the same reason -- one locked file must not flood the log.
    """
    path = heartbeat_path(storage_root)
    payload = {
        "worker_id": worker_id,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    global _last_write_warning
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload), encoding="utf-8", errors="replace"
        )
        os.replace(temporary, path)
        _last_write_warning = 0.0
    except OSError:
        moment = time.monotonic()
        if moment - _last_write_warning >= _WRITE_WARNING_INTERVAL_SECONDS:
            _last_write_warning = moment
            logger.warning(
                "the worker could not write its heartbeat to %s",
                path,
                exc_info=True,
            )
        else:
            logger.debug(
                "the worker could not write its heartbeat to %s", path
            )


def read_heartbeat(storage_root: Path) -> WorkerHeartbeat | None:
    """Read what the worker last said, if it is still fresh enough to mean
    anything.

    Returns ``None`` when there is no note, when it cannot be read, or
    when it is too old. A missing note and a stale note mean the same
    thing to a user: the worker is not running.
    """
    path = heartbeat_path(storage_root)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    worker_id = payload.get("worker_id")
    at = payload.get("at")
    if not isinstance(worker_id, str) or not isinstance(at, str):
        return None
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - moment).total_seconds()
    return WorkerHeartbeat(
        worker_id=worker_id, at=moment, age_seconds=max(0.0, age)
    )
