"""The clip endpoints.

The rules these hold the API to: the boundaries come from the candidate,
a repeated request does not render twice, nothing exposes a filesystem
path, and the media route only serves a file inside the storage root.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import (
    create_analysis,
    create_job,
    get_clip,
    save_transcript,
    session,
    update_clip,
)
from app.errors import ErrorCode
from app.jobs import JobStage, JobStatus, SourceType
from app.storage import Storage
from app.transcripts import Transcript, TranscriptSegment

FFMPEG = shutil.which("ffmpeg")


def _transcript() -> Transcript:
    return Transcript(
        language="en",
        model_name="tiny.en",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=30.0,
        segments=(
            TranscriptSegment(
                index=0,
                start_seconds=0.0,
                end_seconds=3.0,
                text="The quick brown fox jumps over the lazy dog.",
                average_log_probability=-0.2,
                no_speech_probability=0.01,
            ),
        ),
    )


def _job_with_a_candidate(
    settings: Settings, job_id: str = "a" * 32
) -> str:
    """A completed job with a transcript, an analysis and a candidate."""
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        for status, stage in (
            (JobStatus.VALIDATING, JobStage.VALIDATING),
            (JobStatus.INSPECTING, JobStage.INSPECTING),
            (JobStatus.EXTRACTING_AUDIO, JobStage.EXTRACTING_AUDIO),
            (JobStatus.TRANSCRIBING, JobStage.TRANSCRIBING),
            (JobStatus.COMPLETED, JobStage.READY),
        ):
            from app.db import update_job

            update_job(conn, job_id, status=status, stage=stage)
        save_transcript(conn, job_id=job_id, transcript=_transcript())
        analysis = create_analysis(
            conn,
            job_id=job_id,
            integration_id=None,
            provider_id="custom",
            protocol="openai_chat_completions",
            base_url="https://api.example.test/v1",
            model_id="test-model",
            model_name=None,
            prompt_version="1",
            schema_version="1",
            transcript_id="t" * 32,
            source_duration_seconds=30.0,
        )
        from app.db import save_candidates

        save_candidates(
            conn,
            analysis.id,
            [
                type(
                    "C",
                    (),
                    {
                        "id": f"candidate-{job_id[:4]}",
                        "position": 0,
                        "start_segment_index": 0,
                        "end_segment_index": 0,
                        "start_seconds": 2.0,
                        "end_seconds": 8.0,
                        "title": "The opening line",
                        "reason": "It states the point.",
                        "moment_type": "hook",
                        "score": 0.9,
                        "decision": "proposed",
                    },
                )()
            ],
        )
    return f"candidate-{job_id[:4]}"


# --- requesting a clip ------------------------------------------------------


def test_a_clip_is_queued_for_a_candidate(
    client: TestClient, settings: Settings
) -> None:
    candidate_id = _job_with_a_candidate(settings)

    response = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["stage"] == "queued"
    assert body["progress"] == 0
    # The boundaries are the candidate's own.
    assert body["start_seconds"] == 2.0
    assert body["end_seconds"] == 8.0
    assert body["candidate_id"] == candidate_id
    assert body["media_url"] is None


def test_the_boundaries_cannot_be_chosen_by_the_client(
    client: TestClient, settings: Settings
) -> None:
    """A request cannot introduce a timestamp of its own."""
    candidate_id = _job_with_a_candidate(settings)

    response = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips",
        json={
            "candidate_id": candidate_id,
            "start_seconds": 100.0,
            "end_seconds": 200.0,
        },
    )

    assert response.status_code == 202
    assert response.json()["start_seconds"] == 2.0
    assert response.json()["end_seconds"] == 8.0


def test_a_candidate_from_another_job_is_refused(
    client: TestClient, settings: Settings
) -> None:
    _job_with_a_candidate(settings, job_id="a" * 32)
    _job_with_a_candidate(settings, job_id="b" * 32)

    response = client.post(
        f"/api/v1/jobs/{'b' * 32}/clips", json={"candidate_id": "candidate-aaaa"}
    )

    # The candidate belongs to the other job.
    assert response.status_code in (404, 409)


def test_an_unknown_candidate_is_refused(client: TestClient, settings: Settings) -> None:
    _job_with_a_candidate(settings)

    response = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": "nope"}
    )

    assert response.status_code == 404


def test_an_unknown_job_is_refused(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/jobs/{'f' * 32}/clips", json={"candidate_id": "anything"}
    )

    assert response.status_code == 404


def test_a_request_without_a_candidate_is_refused(
    client: TestClient, settings: Settings
) -> None:
    _job_with_a_candidate(settings)

    response = client.post(f"/api/v1/jobs/{'a' * 32}/clips", json={})

    assert response.status_code == 422


def test_a_repeated_request_does_not_render_twice(
    client: TestClient, settings: Settings
) -> None:
    """A repeated click finds the clip that already exists."""
    candidate_id = _job_with_a_candidate(settings)

    first = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    )
    second = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]

    with session(settings.resolved_database_path) as conn:
        clips = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    assert clips == 1


# --- reading ----------------------------------------------------------------


def test_a_clip_is_read_back(client: TestClient, settings: Settings) -> None:
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    response = client.get(f"/api/v1/clips/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["status"] == "queued"


def test_an_unknown_clip_is_refused(client: TestClient) -> None:
    response = client.get(f"/api/v1/clips/{'f' * 32}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.CLIP_NOT_FOUND.value


def test_the_clips_of_a_job_are_listed(client: TestClient, settings: Settings) -> None:
    candidate_id = _job_with_a_candidate(settings)
    client.post(f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id})

    response = client.get(f"/api/v1/jobs/{'a' * 32}/clips")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_the_pagination_is_bounded(client: TestClient, settings: Settings) -> None:
    _job_with_a_candidate(settings)

    assert client.get(f"/api/v1/jobs/{'a' * 32}/clips?limit=0").status_code == 422
    assert client.get(f"/api/v1/jobs/{'a' * 32}/clips?limit=1000").status_code == 422
    assert client.get(f"/api/v1/jobs/{'a' * 32}/clips?offset=-1").status_code == 422


def test_the_response_never_exposes_a_path(
    client: TestClient, settings: Settings
) -> None:
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    response = client.get(f"/api/v1/clips/{created['id']}")

    assert "output_path" not in response.json()
    assert "C:\\" not in response.text
    assert "/home/" not in response.text


# --- retrying ---------------------------------------------------------------


def test_a_failed_clip_can_be_rendered_again(
    client: TestClient, settings: Settings
) -> None:
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    with session(settings.resolved_database_path) as conn:
        update_clip(conn, created["id"], status="failed", error_code="clip_render_failed")

    response = client.post(f"/api/v1/clips/{created['id']}/retry")

    assert response.status_code == 200
    assert response.json()["clip"]["status"] == "queued"
    assert response.json()["clip"]["error"] is None


def test_a_clip_that_has_not_failed_is_not_retried(
    client: TestClient, settings: Settings
) -> None:
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    response = client.post(f"/api/v1/clips/{created['id']}/retry")

    assert response.status_code == 409


# --- the media route --------------------------------------------------------


def test_the_media_route_refuses_a_clip_without_a_file(
    client: TestClient, settings: Settings
) -> None:
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    response = client.get(f"/api/v1/clips/{created['id']}/media")

    assert response.status_code == 404


def test_the_media_route_serves_a_finished_clip(
    client: TestClient, settings: Settings, clip_source_video: Path
) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so no clip can be rendered")

    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    shutil.copy(clip_source_video, storage.sources_dir / "clip.mp4")
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    from app.worker import Worker

    with session(settings.resolved_database_path) as conn:
        from app.db import update_job

        update_job(
            conn,
            "a" * 32,
            source_path="sources/clip.mp4",
        )
    Worker(settings, worker_id="w1").run_clips_once()

    response = client.get(f"/api/v1/clips/{created['id']}/media")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/mp4")
    assert len(response.content) > 1024


def test_the_media_route_refuses_a_path_outside_the_root(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """A clip whose stored path escapes the root serves nothing."""
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x" * 4096)
    candidate_id = _job_with_a_candidate(settings)
    created = client.post(
        f"/api/v1/jobs/{'a' * 32}/clips", json={"candidate_id": candidate_id}
    ).json()

    with session(settings.resolved_database_path) as conn:
        conn.execute(
            "UPDATE clips SET output_path = ?, status = 'completed' WHERE id = ?",
            ("../outside.mp4", created["id"]),
        )

    response = client.get(f"/api/v1/clips/{created['id']}/media")

    assert response.status_code == 404
