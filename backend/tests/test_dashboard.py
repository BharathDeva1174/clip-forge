"""The dashboard's view of a job: what it has produced.

The interface shows how many candidates, clips and formatted artifacts a
job has. Those numbers have to be **counted from the database**, not
guessed, and they have to agree with the detail endpoints that report the
same things.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import (
    create_analysis,
    create_clip,
    create_format,
    create_job,
    job_artifacts,
    save_candidates,
    session,
    update_analysis,
    update_clip,
    update_format,
)
from app.highlights import HighlightCandidate
from app.jobs import JobStage, JobStatus, SourceType


def _job(conn, job_id: str) -> None:
    create_job(
        conn,
        job_id=job_id,
        source_type=SourceType.UPLOAD,
        source_reference=f"{job_id}.mp4",
    )


def _candidate(position: int, title: str) -> HighlightCandidate:
    return HighlightCandidate(
        id=f"cand{position:032d}",
        position=position,
        start_segment_index=position,
        end_segment_index=position + 1,
        start_seconds=float(position),
        end_seconds=float(position) + 1.0,
        title=title,
        reason="Because.",
        moment_type="hook",
        score=0.5,
    )


def _candidate_for(analysis_id: str, position: int, title: str) -> HighlightCandidate:
    """A candidate whose id is unique to the analysis that owns it."""
    return HighlightCandidate(
        id=f"{analysis_id[:16]}{position:016d}",
        position=position,
        start_segment_index=position,
        end_segment_index=position + 1,
        start_seconds=float(position),
        end_seconds=float(position) + 1.0,
        title=title,
        reason="Because.",
        moment_type="hook",
        score=0.5,
    )


def _analysis(conn, job_id: str, analysis_id: str, *, status: str = "completed"):
    analysis = create_analysis(
        conn,
        job_id=job_id,
        integration_id=None,
        provider_id="custom",
        protocol="openai_chat_completions",
        base_url="http://127.0.0.1:1/v1",
        model_id="model",
        model_name="model",
        prompt_version="v1",
        schema_version="v1",
        analysis_id=analysis_id,
    )
    if status != "pending":
        update_analysis(conn, analysis_id, status=status)
    return analysis


# --- the counts --------------------------------------------------------------


def test_a_fresh_job_has_produced_nothing(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)

        counts = job_artifacts(conn, ["a" * 32])["a" * 32]

    assert counts.analysis_status is None
    assert counts.candidate_count == 0
    assert counts.clip_count == 0
    assert counts.format_count == 0


def test_the_counts_are_collected_for_a_whole_page(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)
        _job(conn, "b" * 32)
        _analysis(conn, "a" * 32, "d" * 32)
        save_candidates(
            conn, "d" * 32, [_candidate(0, "One"), _candidate(1, "Two")]
        )
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id="d" * 32,
            candidate_id=None,
            start_seconds=0.0,
            end_seconds=2.0,
            clip_id="e" * 32,
        )

        counts = job_artifacts(conn, ["a" * 32, "b" * 32])

    assert counts["a" * 32].analysis_status == "completed"
    assert counts["a" * 32].candidate_count == 2
    assert counts["a" * 32].clip_count == 1
    assert counts["b" * 32].analysis_status is None
    assert counts["b" * 32].candidate_count == 0
    assert counts["b" * 32].clip_count == 0


def test_an_empty_page_costs_no_queries(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        assert job_artifacts(conn, []) == {}
        assert job_artifacts(conn, ["", ""]) == {}


def test_the_count_follows_the_most_recent_analysis(settings: Settings) -> None:
    """A second analysis replaces the first in the count.

    The count must use the same rule as the detail endpoint: the most
    recent analysis, by ``created_at`` and then by id.
    """
    with session(settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)
        _analysis(conn, "a" * 32, "d" * 32)
        save_candidates(conn, "d" * 32, [_candidate_for("d" * 32, 0, "Old")])
        _analysis(conn, "a" * 32, "f" * 32)
        save_candidates(
            conn,
            "f" * 32,
            [
                _candidate_for("f" * 32, 0, "New one"),
                _candidate_for("f" * 32, 1, "New two"),
            ],
        )

        counts = job_artifacts(conn, ["a" * 32])

    assert counts["a" * 32].candidate_count == 2


def test_the_formatted_artifacts_are_counted(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id=None,
            candidate_id=None,
            start_seconds=0.0,
            end_seconds=2.0,
            clip_id="e" * 32,
        )
        create_format(
            conn,
            clip_id="e" * 32,
            job_id="a" * 32,
            config_fingerprint="1" * 32,
            settings_json=json.dumps({"width": 720, "height": 1280}),
            format_id="f" * 32,
        )

        counts = job_artifacts(conn, ["a" * 32])

    assert counts["a" * 32].clip_count == 1
    assert counts["a" * 32].format_count == 1


# --- the API -----------------------------------------------------------------


def test_the_job_list_reports_the_counts(client: TestClient) -> None:
    with session(client.app.state.settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)
        _analysis(conn, "a" * 32, "d" * 32)
        save_candidates(conn, "d" * 32, [_candidate(0, "One")])
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id="d" * 32,
            candidate_id=None,
            start_seconds=0.0,
            end_seconds=2.0,
            clip_id="e" * 32,
        )

    body = client.get("/api/v1/jobs").json()

    assert body["total"] == 1
    entry = body["items"][0]
    assert entry["analysis_status"] == "completed"
    assert entry["candidate_count"] == 1
    assert entry["clip_count"] == 1
    assert entry["format_count"] == 0


def test_one_job_reports_the_same_counts_as_the_list(
    client: TestClient,
) -> None:
    """The two endpoints must not disagree."""
    with session(client.app.state.settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)
        _analysis(conn, "a" * 32, "d" * 32)
        save_candidates(conn, "d" * 32, [_candidate(0, "One")])

    listed = client.get("/api/v1/jobs").json()["items"][0]
    single = client.get(f"/api/v1/jobs/{'a' * 32}").json()

    assert single["analysis_status"] == listed["analysis_status"]
    assert single["candidate_count"] == listed["candidate_count"]
    assert single["clip_count"] == listed["clip_count"]
    assert single["format_count"] == listed["format_count"]


def test_the_candidate_count_agrees_with_the_analysis_endpoint(
    client: TestClient,
) -> None:
    """The library and the detail view must not disagree about the count."""
    with session(client.app.state.settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)
        _analysis(conn, "a" * 32, "d" * 32)
        save_candidates(
            conn, "d" * 32, [_candidate(0, "One"), _candidate(1, "Two")]
        )

    listed = client.get("/api/v1/jobs").json()["items"][0]
    analysis = client.get(f"/api/v1/jobs/{'a' * 32}/highlights").json()

    assert listed["candidate_count"] == analysis["candidate_count"]


def test_a_job_without_an_analysis_says_so(client: TestClient) -> None:
    with session(client.app.state.settings.resolved_database_path) as conn:
        _job(conn, "a" * 32)

    entry = client.get("/api/v1/jobs").json()["items"][0]

    assert entry["analysis_status"] is None
    assert entry["candidate_count"] == 0


def test_the_list_still_works_without_any_jobs(client: TestClient) -> None:
    body = client.get("/api/v1/jobs").json()

    assert body["items"] == []
    assert body["total"] == 0
