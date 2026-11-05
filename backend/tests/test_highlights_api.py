"""The highlight analysis endpoints.

The rules these hold the API to: an analysis is queued rather than run, a
job without a transcript cannot be analysed, a missing integration is
refused rather than replaced, and polling never starts a second analysis.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import (
    create_analysis,
    create_integration,
    create_job,
    get_analysis,
    latest_analysis_for_job,
    list_candidates,
    save_candidates,
    save_transcript,
    session,
    update_job,
)
from app.errors import ErrorCode
from app.integrations import ApiProtocol, IntegrationRole
from app.jobs import JobStage, JobStatus, SourceType
from app.transcripts import Transcript, TranscriptSegment


def _transcript() -> Transcript:
    return Transcript(
        language="en",
        model_name="tiny.en",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=3.0,
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


def _job_with_transcript(settings: Settings, job_id: str = "a" * 32) -> str:
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
            update_job(conn, job_id, status=status, stage=stage)
        save_transcript(conn, job_id=job_id, transcript=_transcript())
    return job_id


def _default_integration(settings: Settings, **overrides) -> str:
    with session(settings.resolved_database_path) as conn:
        record = create_integration(
            conn,
            name="Test",
            provider_id="custom",
            protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://api.example.test/v1",
            model_id="test-model",
            is_default=True,
            **overrides,
        )
    return record.id


# --- reading ----------------------------------------------------------------


def test_a_job_without_an_analysis_reports_none(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)

    response = client.get(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "none"
    assert body["candidates"] == []
    assert body["candidate_count"] == 0
    # The notice is always there: it says what the provider receives.
    assert "transcript text" in body["data_notice"]


def test_an_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/jobs/{'f' * 32}/highlights")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


# --- starting ---------------------------------------------------------------


def test_an_analysis_is_queued_and_not_run(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _default_integration(settings)

    response = client.post(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["analysis_id"] is not None
    assert body["model_id"] == "test-model"

    with session(settings.resolved_database_path) as conn:
        analysis = get_analysis(conn, body["analysis_id"])
    assert analysis is not None
    assert analysis.status == "pending"


def test_a_job_without_a_transcript_cannot_be_analysed(
    client: TestClient, settings: Settings
) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="b" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
    _default_integration(settings)

    response = client.post(f"/api/v1/jobs/{'b' * 32}/highlights")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.HIGHLIGHT_NO_TRANSCRIPT.value


def test_an_analysis_needs_a_configured_integration(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)

    response = client.post(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 409
    assert (
        response.json()["error"]["code"]
        == ErrorCode.INTEGRATION_NOT_CONFIGURED.value
    )


def test_a_disabled_integration_cannot_be_used(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _default_integration(settings, enabled=False)

    response = client.post(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.INTEGRATION_DISABLED.value


def test_an_integration_without_a_model_cannot_be_used(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    with session(settings.resolved_database_path) as conn:
        record = create_integration(
            conn,
            name="Test",
            provider_id="custom",
            protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://api.example.test/v1",
            is_default=True,
        )
        from app.db import update_integration

        update_integration(conn, record.id, model_id=None)

    response = client.post(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.INTEGRATION_INVALID.value


def test_starting_an_analysis_twice_does_not_start_a_second(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _default_integration(settings)

    first = client.post(f"/api/v1/jobs/{job_id}/highlights")
    second = client.post(f"/api/v1/jobs/{job_id}/highlights")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["analysis_id"] == first.json()["analysis_id"]

    with session(settings.resolved_database_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM highlight_analyses WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert rows[0] == 1


def test_polling_never_starts_an_analysis(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _default_integration(settings)

    for _ in range(5):
        assert client.get(f"/api/v1/jobs/{job_id}/highlights").status_code == 200

    with session(settings.resolved_database_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM highlight_analyses WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert rows[0] == 0


# --- the results ------------------------------------------------------------


def _completed_analysis(settings: Settings, job_id: str) -> str:
    integration_id = _default_integration(settings)
    with session(settings.resolved_database_path) as conn:
        analysis = create_analysis(
            conn,
            job_id=job_id,
            integration_id=integration_id,
            provider_id="custom",
            protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS.value,
            base_url="https://api.example.test/v1",
            model_id="test-model",
            model_name="Test model",
            prompt_version="1",
            schema_version="1",
            transcript_id="t" * 32,
            source_duration_seconds=3.0,
        )
        save_candidates(
            conn,
            analysis.id,
            [
                type(
                    "C",
                    (),
                    {
                        "id": "",
                        "position": 0,
                        "start_segment_index": 0,
                        "end_segment_index": 0,
                        "start_seconds": 0.0,
                        "end_seconds": 2.0,
                        "title": "The opening line",
                        "reason": "It states the point.",
                        "moment_type": "hook",
                        "score": 0.9,
                        "decision": "proposed",
                    },
                )()
            ],
        )
        from app.db import update_analysis

        update_analysis(conn, analysis.id, status="completed")
    return analysis.id


def test_the_candidates_are_returned(client: TestClient, settings: Settings) -> None:
    job_id = _job_with_transcript(settings)
    _completed_analysis(settings, job_id)

    response = client.get(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["candidate_count"] == 1
    candidate = body["candidates"][0]
    assert candidate["title"] == "The opening line"
    assert candidate["start_seconds"] == 0.0
    assert candidate["end_seconds"] == 2.0
    assert candidate["duration_seconds"] == 2.0
    # The score is the model's own ranking, and it is labelled as such.
    assert candidate["score"] == 0.9
    assert body["provider_name"] == "Custom"
    assert body["model_id"] == "test-model"


def test_a_candidate_can_be_accepted(client: TestClient, settings: Settings) -> None:
    job_id = _job_with_transcript(settings)
    analysis_id = _completed_analysis(settings, job_id)
    with session(settings.resolved_database_path) as conn:
        candidate_id = list_candidates(conn, analysis_id)[0].id

    response = client.patch(
        f"/api/v1/jobs/{job_id}/highlights/{candidate_id}",
        json={"decision": "accepted"},
    )

    assert response.status_code == 200
    assert response.json()["candidates"][0]["decision"] == "accepted"

    with session(settings.resolved_database_path) as conn:
        assert list_candidates(conn, analysis_id)[0].decision == "accepted"


def test_a_candidate_can_be_rejected(client: TestClient, settings: Settings) -> None:
    job_id = _job_with_transcript(settings)
    analysis_id = _completed_analysis(settings, job_id)
    with session(settings.resolved_database_path) as conn:
        candidate_id = list_candidates(conn, analysis_id)[0].id

    response = client.patch(
        f"/api/v1/jobs/{job_id}/highlights/{candidate_id}",
        json={"decision": "rejected"},
    )

    assert response.json()["candidates"][0]["decision"] == "rejected"


def test_an_unknown_candidate_returns_404(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _completed_analysis(settings, job_id)

    response = client.patch(
        f"/api/v1/jobs/{job_id}/highlights/{'z' * 32}",
        json={"decision": "accepted"},
    )

    assert response.status_code == 404


def test_an_invalid_decision_is_refused(client: TestClient, settings: Settings) -> None:
    job_id = _job_with_transcript(settings)
    analysis_id = _completed_analysis(settings, job_id)
    with session(settings.resolved_database_path) as conn:
        candidate_id = list_candidates(conn, analysis_id)[0].id

    response = client.patch(
        f"/api/v1/jobs/{job_id}/highlights/{candidate_id}",
        json={"decision": "maybe"},
    )

    assert response.status_code == 422


def test_a_failed_analysis_reports_its_stable_code(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _default_integration(settings)
    with session(settings.resolved_database_path) as conn:
        create_analysis(
            conn,
            job_id=job_id,
            integration_id=None,
            provider_id="custom",
            protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS.value,
            base_url="https://api.example.test/v1",
            model_id="test-model",
            model_name=None,
            prompt_version="1",
            schema_version="1",
        )
        from app.db import update_analysis

        update_analysis(
            conn,
            latest_analysis_for_job(conn, job_id).id,
            status="failed",
            error_code="ai_rate_limited",
            error_message="The provider is rate limiting this account.",
        )

    response = client.get(f"/api/v1/jobs/{job_id}/highlights")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "ai_rate_limited"
    assert "rate limiting" in body["error"]["message"]


def test_the_response_never_leaks_a_path_or_a_key(
    client: TestClient, settings: Settings
) -> None:
    job_id = _job_with_transcript(settings)
    _default_integration(settings, secret_ref=None)

    response = client.get(f"/api/v1/jobs/{job_id}/highlights")

    assert "C:\\" not in response.text
    assert "/home/" not in response.text
    assert "api_key" not in response.text
