"""Is the worker actually running?

The API and the worker are separate processes, so the API cannot see the
worker directly. The worker leaves a heartbeat behind, and these tests are
about that note: that it is written, that it is read, and that a missing
or stale one says the right thing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.worker_heartbeat import (
    HEARTBEAT_MAX_AGE_SECONDS,
    heartbeat_path,
    read_heartbeat,
    write_heartbeat,
)


# --- the note itself --------------------------------------------------------


def test_a_fresh_heartbeat_says_the_worker_is_running(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, "worker-1")

    heartbeat = read_heartbeat(tmp_path)

    assert heartbeat is not None
    assert heartbeat.worker_id == "worker-1"
    assert heartbeat.running is True
    assert heartbeat.age_seconds < 5


def test_a_missing_heartbeat_means_no_worker(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path) is None


def test_a_stale_heartbeat_means_no_worker(tmp_path: Path) -> None:
    """A note from an hour ago is not evidence of anything."""
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    heartbeat_path(tmp_path).write_text(
        json.dumps({"worker_id": "worker-1", "at": old.isoformat()}),
        encoding="utf-8",
    )

    heartbeat = read_heartbeat(tmp_path)

    assert heartbeat is not None
    assert heartbeat.worker_id == "worker-1"
    assert heartbeat.running is False
    assert heartbeat.age_seconds > HEARTBEAT_MAX_AGE_SECONDS


def test_a_malformed_heartbeat_is_ignored(tmp_path: Path) -> None:
    heartbeat_path(tmp_path).write_text("not json at all", encoding="utf-8")

    assert read_heartbeat(tmp_path) is None


def test_a_heartbeat_without_a_time_is_ignored(tmp_path: Path) -> None:
    heartbeat_path(tmp_path).write_text(
        json.dumps({"worker_id": "worker-1"}), encoding="utf-8"
    )

    assert read_heartbeat(tmp_path) is None


def test_a_heartbeat_with_an_unreadable_time_is_ignored(tmp_path: Path) -> None:
    heartbeat_path(tmp_path).write_text(
        json.dumps({"worker_id": "worker-1", "at": "yesterday"}), encoding="utf-8"
    )

    assert read_heartbeat(tmp_path) is None


def test_writing_the_heartbeat_leaves_no_temporary_file(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, "worker-1")

    leftovers = [path.name for path in tmp_path.iterdir() if ".tmp" in path.name]
    assert leftovers == []


def test_a_heartbeat_that_cannot_be_written_does_not_raise(
    tmp_path: Path,
) -> None:
    """The worker must keep working even if it cannot leave a note."""
    # A path whose parent is a file, not a directory.
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")

    write_heartbeat(blocked, "worker-1")

    assert read_heartbeat(blocked) is None


# --- what the API says ------------------------------------------------------


def test_the_readiness_report_says_no_worker_when_there_is_none(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The probe is pointed at a storage root with no note in it, so the
    # answer does not depend on whether a worker happens to be running on
    # the machine that runs the tests.
    monkeypatch.setenv("CLIPFORGE_STORAGE_DIR", str(settings.storage_dir))

    body = client.get("/ready").json()

    assert body["worker"]["running"] is False
    assert body["worker"]["worker_id"] is None
    assert body["worker"]["seconds_since_seen"] is None


def test_the_readiness_report_says_the_worker_is_running(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The readiness probe rebuilds the settings from the environment, so the
    # probe is pointed at the same storage root as the test.
    monkeypatch.setenv(
        "CLIPFORGE_STORAGE_DIR", str(settings.storage_dir)
    )
    write_heartbeat(settings.storage_root, "worker-1")

    body = client.get("/ready").json()

    assert body["worker"]["running"] is True
    assert body["worker"]["worker_id"] == "worker-1"
    assert body["worker"]["seconds_since_seen"] is not None


def test_the_readiness_report_says_the_worker_has_gone(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLIPFORGE_STORAGE_DIR", str(settings.storage_dir)
    )
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    heartbeat_path(settings.storage_root).write_text(
        json.dumps({"worker_id": "worker-1", "at": old.isoformat()}),
        encoding="utf-8",
    )

    body = client.get("/ready").json()

    assert body["worker"]["running"] is False
    # It still says which worker it was and when it was seen.
    assert body["worker"]["worker_id"] == "worker-1"
    assert body["worker"]["last_seen"] is not None


def test_the_health_contract_is_unchanged(client: TestClient) -> None:
    """The liveness contract is stable and says nothing about the worker."""
    body = client.get("/health").json()

    assert body == {"status": "ok", "service": "clipforge-api"}
