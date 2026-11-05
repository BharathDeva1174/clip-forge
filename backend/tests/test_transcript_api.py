"""Tests for the transcript API.

The endpoint has to be honest: it must never claim that a transcript exists
before it has been stored, it must never leak a path, and it must answer with
a stable shape whatever state the job is in.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_job, get_transcript, save_transcript, session
from app.jobs import JobStage, JobStatus, SourceType
from app.transcripts import Transcript, TranscriptSegment


def _transcript(*texts: str) -> Transcript:
    segments = tuple(
        TranscriptSegment(
            index=index,
            start_seconds=float(index),
            end_seconds=float(index) + 0.5,
            text=text,
            average_log_probability=-0.2,
            no_speech_probability=0.01,
        )
        for index, text in enumerate(texts)
    )
    return Transcript(
        language="en",
        model_name="tiny.en",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=float(len(texts)),
        segments=segments,
    )


def _completed_job(
    settings: Settings, *, with_transcript: bool = True, job_id: str = "a" * 32
) -> str:
    """Insert a finished job, with or without a transcript."""
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, progress = 100 WHERE id = ?",
            (JobStatus.COMPLETED.value, JobStage.READY.value, job_id),
        )
        if with_transcript:
            save_transcript(conn, job_id=job_id, transcript=_transcript("one", "two"))
    return job_id


# --- a finished job --------------------------------------------------------


def test_the_transcript_of_a_finished_job_is_returned(
    client: TestClient, settings: Settings
) -> None:
    job_id = _completed_job(settings)

    response = client.get(f"/api/v1/jobs/{job_id}/transcript")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["job_id"] == job_id
    assert body["language"] == "en"
    assert body["model_name"] == "tiny.en"
    assert body["audio_sample_rate"] == 16000
    assert body["audio_channels"] == 1
    assert body["segment_count"] == 2
    assert [segment["text"] for segment in body["segments"]] == ["one", "two"]


def test_the_segments_carry_their_timestamps(
    client: TestClient, settings: Settings
) -> None:
    job_id = _completed_job(settings)

    body = client.get(f"/api/v1/jobs/{job_id}/transcript").json()

    first = body["segments"][0]
    assert first["index"] == 0
    assert first["start_seconds"] == 0.0
    assert first["end_seconds"] == 0.5
    assert first["text"] == "one"
    assert body["segments"][1]["start_seconds"] == 1.0


def test_the_segments_are_paginated(
    client: TestClient, settings: Settings
) -> None:
    job_id = "b" * 32
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        save_transcript(
            conn,
            job_id=job_id,
            transcript=_transcript("a", "b", "c", "d", "e"),
        )

    body = client.get(f"/api/v1/jobs/{job_id}/transcript?limit=2&offset=0").json()
    assert [segment["text"] for segment in body["segments"]] == ["a", "b"]
    assert body["segment_count"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0

    body = client.get(f"/api/v1/jobs/{job_id}/transcript?limit=2&offset=2").json()
    assert [segment["text"] for segment in body["segments"]] == ["c", "d"]


def test_the_pagination_is_stable_across_repeated_polls(
    client: TestClient, settings: Settings
) -> None:
    """Polling must not change what the endpoint says."""
    job_id = "b" * 32
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        save_transcript(
            conn,
            job_id=job_id,
            transcript=_transcript("a", "b", "c", "d", "e"),
        )

    first = client.get(f"/api/v1/jobs/{job_id}/transcript?limit=2&offset=2").json()
    for _ in range(5):
        again = client.get(f"/api/v1/jobs/{job_id}/transcript?limit=2&offset=2").json()
        assert again == first


def test_polling_does_not_start_any_work(
    client: TestClient, settings: Settings
) -> None:
    """The API never extracts or transcribes, however often it is polled."""
    job_id = _completed_job(settings)

    for _ in range(5):
        assert client.get(f"/api/v1/jobs/{job_id}/transcript").status_code == 200

    with session(settings.resolved_database_path) as conn:
        row = conn.execute(
            "SELECT attempt_count, status, stage FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    assert row["attempt_count"] == 0
    assert row["status"] == JobStatus.COMPLETED.value
    assert row["stage"] == JobStage.READY.value


# --- a job that is not finished --------------------------------------------


def test_a_job_that_is_still_being_transcribed_reports_that_it_is_pending(
    client: TestClient, settings: Settings
) -> None:
    job_id = "c" * 32
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, progress = 85 WHERE id = ?",
            (JobStatus.TRANSCRIBING.value, JobStage.TRANSCRIBING.value, job_id),
        )

    response = client.get(f"/api/v1/jobs/{job_id}/transcript")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["job_status"] == "transcribing"
    assert body["job_stage"] == "transcribing"
    assert body["segments"] == []
    assert body["segment_count"] == 0


def test_a_failed_job_reports_the_failure_with_a_stable_code(
    client: TestClient, settings: Settings
) -> None:
    job_id = "d" * 32
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, error_code = ?, error_message = ?
             WHERE id = ?
            """,
            (
                JobStatus.FAILED.value,
                JobStage.FAILED.value,
                "transcription_model_unavailable",
                "The local transcription model could not be loaded.",
                job_id,
            ),
        )

    response = client.get(f"/api/v1/jobs/{job_id}/transcript")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "transcription_model_unavailable"
    assert "could not be loaded" in body["error"]["message"]


def test_an_unknown_job_id_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/deadbeef/transcript")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_malformed_identifier_returns_400(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/not%20a%20job%20id/transcript")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_an_over_long_identifier_returns_400(client: TestClient) -> None:
    response = client.get(f"/api/v1/jobs/{'a' * 200}/transcript")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_an_invalid_page_size_returns_422(client: TestClient, settings: Settings) -> None:
    job_id = _completed_job(settings)

    response = client.get(f"/api/v1/jobs/{job_id}/transcript?limit=0")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_a_negative_offset_returns_422(client: TestClient, settings: Settings) -> None:
    job_id = _completed_job(settings)

    response = client.get(f"/api/v1/jobs/{job_id}/transcript?offset=-1")

    assert response.status_code == 422


# --- safety ----------------------------------------------------------------


def test_the_response_never_leaks_a_path(
    client: TestClient, settings: Settings
) -> None:
    job_id = _completed_job(settings)

    body = client.get(f"/api/v1/jobs/{job_id}/transcript").text

    assert str(settings.storage_root) not in body
    assert "\\" not in body
    assert "sources/" not in body
    assert "temp/" not in body


def test_the_response_never_leaks_a_model_cache_path(
    client: TestClient, settings: Settings
) -> None:
    job_id = _completed_job(settings)

    body = client.get(f"/api/v1/jobs/{job_id}/transcript").text

    assert "models" not in body.replace("tiny.en", "")
    assert "huggingface" not in body


def test_a_job_without_a_transcript_is_not_reported_as_ready(
    client: TestClient, settings: Settings
) -> None:
    job_id = _completed_job(settings, with_transcript=False)

    body = client.get(f"/api/v1/jobs/{job_id}/transcript").json()

    assert body["status"] == "pending"
    assert body["segments"] == []


def test_the_job_response_says_whether_a_transcript_exists(
    client: TestClient, settings: Settings
) -> None:
    with_transcript = _completed_job(settings, with_transcript=True)
    without_transcript = _completed_job(
        settings, with_transcript=False, job_id="f" * 32
    )

    assert (
        client.get(f"/api/v1/jobs/{with_transcript}").json()["has_transcript"]
        is True
    )
    assert (
        client.get(f"/api/v1/jobs/{without_transcript}").json()["has_transcript"]
        is False
    )


def test_the_job_list_says_whether_a_transcript_exists(
    client: TestClient, settings: Settings
) -> None:
    with_transcript = _completed_job(settings, with_transcript=True)
    without_transcript = _completed_job(
        settings, with_transcript=False, job_id="g" * 32
    )

    body = client.get("/api/v1/jobs").json()
    flags = {item["id"]: item["has_transcript"] for item in body["items"]}

    assert flags[with_transcript] is True
    assert flags[without_transcript] is False


# --- the rest of the API still works ---------------------------------------


def test_the_upload_endpoint_still_works(
    client: TestClient, sample_video: Path
) -> None:
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_the_youtube_endpoint_still_works(client: TestClient) -> None:
    response = client.post(
        "/api/v1/videos/youtube",
        json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "authorized": True,
        },
    )
    assert response.status_code == 202


def test_the_job_endpoints_still_work(client: TestClient, settings: Settings) -> None:
    job_id = _completed_job(settings)

    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert client.get("/api/v1/jobs").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_the_job_response_still_contains_no_path(
    client: TestClient, settings: Settings
) -> None:
    job_id = _completed_job(settings)

    body = client.get(f"/api/v1/jobs/{job_id}").text

    assert str(settings.storage_root) not in body
