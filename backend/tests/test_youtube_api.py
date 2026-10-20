"""Tests for the YouTube submission endpoint.

None of these tests reach the network. The acquisition adapter is replaced
with a spy so that the tests can prove it is never called for a URL that
failed validation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import get_job
from app.errors import ErrorCode
from app.main import create_app

VIDEO_ID = "dQw4w9WgXcQ"


class _ExplodingAcquirer:
    """Fails the test if the acquisition adapter is reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "exploding"

    def available(self) -> bool:
        return True

    def acquire(self, video, destination_dir, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(video.canonical_url)
        raise AssertionError("the acquirer must not be called during validation")


@pytest.fixture()
def acquirer_spy(monkeypatch: pytest.MonkeyPatch) -> _ExplodingAcquirer:
    spy = _ExplodingAcquirer()
    import app.routers.videos as videos_module

    monkeypatch.setattr(videos_module, "build_acquirer", lambda mode: spy)
    return spy


def _submit(client: TestClient, url: str, authorized: bool = True):
    return client.post(
        "/api/v1/videos/youtube", json={"url": url, "authorized": authorized}
    )


def test_a_valid_url_is_queued(client: TestClient, db: sqlite3.Connection) -> None:
    response = _submit(client, f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["source_type"] == "youtube"
    assert body["source_reference"] == f"https://www.youtube.com/watch?v={VIDEO_ID}"

    job = get_job(db, body["id"])
    assert job is not None
    assert job.source_type.value == "youtube"
    assert job.source_path is None


def test_a_short_url_is_queued_in_its_canonical_form(client: TestClient) -> None:
    response = _submit(client, f"https://youtu.be/{VIDEO_ID}")

    assert response.status_code == 202
    assert (
        response.json()["source_reference"]
        == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )


def test_the_user_must_confirm_authorisation(client: TestClient) -> None:
    response = _submit(client, f"https://www.youtube.com/watch?v={VIDEO_ID}", False)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.UNAUTHORIZED_SOURCE.value


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.attacker.example/watch?v=dQw4w9WgXcQ",
        "file:///etc/passwd",
        "https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=bad",
        "https://www.youtube.com:8080/watch?v=dQw4w9WgXcQ",
        "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
    ],
)
def test_an_unacceptable_url_is_rejected(client: TestClient, url: str) -> None:
    response = _submit(client, url)

    assert response.status_code == 400
    assert response.json()["error"]["code"] in (
        ErrorCode.INVALID_YOUTUBE_URL.value,
        ErrorCode.INVALID_REQUEST.value,
    )


def test_a_rejected_url_never_reaches_the_acquisition_adapter(
    client: TestClient, acquirer_spy: _ExplodingAcquirer
) -> None:
    response = _submit(client, "https://example.com/watch?v=dQw4w9WgXcQ")

    assert response.status_code == 400
    assert acquirer_spy.calls == []


def test_an_accepted_url_also_does_not_reach_the_adapter(
    client: TestClient, acquirer_spy: _ExplodingAcquirer
) -> None:
    # Submitting only queues the job; the download happens in the worker.
    response = _submit(client, f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert response.status_code == 202
    assert acquirer_spy.calls == []


def test_the_endpoint_refuses_when_acquisition_is_disabled(
    storage_root: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        storage_dir=storage_root,
        database_path=storage_root / "database" / "clipforge.db",
        youtube_acquisition_mode="disabled",
        log_level="WARNING",
    )
    with TestClient(create_app(settings)) as client:
        response = _submit(client, f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == ErrorCode.YOUTUBE_ACQUISITION_UNAVAILABLE.value
    assert "upload" in body["error"]["message"].lower()


def test_an_overlong_url_is_rejected(client: TestClient) -> None:
    response = _submit(client, "https://www.youtube.com/watch?v=" + "x" * 4000)

    assert response.status_code == 422


def test_a_missing_body_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/videos/youtube", json={})

    assert response.status_code == 422
