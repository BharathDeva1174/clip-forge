"""What cancelling a job does to the work that belongs to it.

Cancellation is documented to prevent further state transitions and result
publication. These tests pin the parts that make that true end to end:

- the queued children of a cancelled job (clips, formatted clips, analyses)
  are failed with the stable ``job_cancelled`` code and are never claimed;
- a render that is already running is stopped within one tick -- the tick
  raises, the renderer's ``finally`` stops the FFmpeg child, and the clip
  or formatted clip is recorded as cancelled with its job;
- the ticks renew the artifact's lease, so a render longer than the lease
  is not stolen by recovery in the middle of the work.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.clips import ClipStatus
from app.config import Settings
from app.db import (
    _row_to_format,
    cancel_job,
    claim_next_clip,
    claim_next_format,
    create_analysis,
    create_clip,
    create_format,
    create_job,
    get_clip,
    get_job,
    renew_clip_lease,
    renew_format_lease,
    session,
    update_clip,
)
from app.errors import ErrorCode
from app.interruption import WorkCancelled
from app.jobs import JobStatus, SourceType
from app.worker import Worker

J = "a" * 32
CLIP = "e" * 32
FORMAT = "f" * 32
ANALYSIS = "d" * 32


def _job_with_children(conn: sqlite3.Connection) -> None:
    """A job with one queued clip, one queued format and one pending analysis."""
    create_job(
        conn, job_id=J, source_type=SourceType.UPLOAD, source_reference="clip.mp4"
    )
    create_clip(
        conn,
        job_id=J,
        analysis_id=None,
        candidate_id=None,
        start_seconds=0.0,
        end_seconds=2.0,
        clip_id=CLIP,
    )
    create_format(
        conn,
        clip_id=CLIP,
        job_id=J,
        config_fingerprint="fp",
        settings_json="{}",
        format_id=FORMAT,
    )
    create_analysis(
        conn,
        job_id=J,
        integration_id=None,
        provider_id="custom",
        protocol="openai_chat_completions",
        base_url="http://127.0.0.1:1/v1",
        model_id="model",
        model_name="model",
        prompt_version="v1",
        schema_version="v1",
        analysis_id=ANALYSIS,
    )


# --- the cascade -------------------------------------------------------------


def test_cancelling_a_job_fails_its_unclaimed_children(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        _job_with_children(conn)

        cancelled = cancel_job(conn, J)

    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, CLIP)
        assert clip.status is ClipStatus.FAILED
        assert clip.error_code == ErrorCode.JOB_CANCELLED.value
        row = conn.execute(
            "SELECT status, error_code FROM clip_formats WHERE id = ?", (FORMAT,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_code"] == ErrorCode.JOB_CANCELLED.value
        row = conn.execute(
            "SELECT status, error_code FROM highlight_analyses WHERE id = ?",
            (ANALYSIS,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_code"] == ErrorCode.JOB_CANCELLED.value


def test_a_cancelled_job_never_hands_its_children_to_a_worker(
    settings: Settings,
) -> None:
    with session(settings.resolved_database_path) as conn:
        _job_with_children(conn)
        cancel_job(conn, J)

        assert claim_next_clip(
            conn, worker_id="w", lease_seconds=300
        ) is None
        assert claim_next_format(
            conn, worker_id="w", lease_seconds=300
        ) is None


def test_cancelling_leaves_a_claimed_clip_alone(settings: Settings) -> None:
    """The cascade is for unclaimed work; an in-flight clip belongs to its
    worker, which stops it through the tick."""
    with session(settings.resolved_database_path) as conn:
        _job_with_children(conn)
        claimed = claim_next_clip(conn, worker_id="w", lease_seconds=300)
        assert claimed is not None

        cancel_job(conn, J)

        clip = get_clip(conn, CLIP)
        assert clip.status is not ClipStatus.FAILED


# --- the lease renewal -------------------------------------------------------


def test_renew_clip_lease_renews_only_the_owner(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        _job_with_children(conn)
        claimed = claim_next_clip(conn, worker_id="w", lease_seconds=300)
        assert claimed is not None

        assert (
            renew_clip_lease(
                conn, claimed.id, lease_seconds=900, worker_id="w"
            )
            is True
        )
        assert (
            renew_clip_lease(
                conn, claimed.id, lease_seconds=900, worker_id="other"
            )
            is False
        )


def test_renew_format_lease_renews_only_the_owner(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        _job_with_children(conn)
        claimed = claim_next_format(conn, worker_id="w", lease_seconds=300)
        assert claimed is not None

        assert (
            renew_format_lease(
                conn, claimed.id, lease_seconds=900, worker_id="w"
            )
            is True
        )
        assert (
            renew_format_lease(
                conn, claimed.id, lease_seconds=900, worker_id="other"
            )
            is False
        )


# --- the worker's ticks ------------------------------------------------------


def _claimed_clip_worker(settings: Settings) -> tuple[Worker, object]:
    """A worker and its claimed clip, with a real file for the source."""
    storage_root = settings.storage_root
    source = storage_root / "sources" / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x" * 64)
    with session(settings.resolved_database_path) as conn:
        _job_with_children(conn)
        conn.execute(
            "UPDATE jobs SET source_path = 'sources/clip.mp4' WHERE id = ?", (J,)
        )
        claimed = claim_next_clip(conn, worker_id="test-worker", lease_seconds=300)
    worker = Worker(settings, worker_id="test-worker")
    return worker, claimed


def test_the_clip_tick_notices_a_cancelled_job(settings: Settings) -> None:
    worker, claimed = _claimed_clip_worker(settings)
    tick = worker._clip_tick(claimed)

    # The job is not cancelled yet: the tick renews the lease and passes.
    tick()

    with session(settings.resolved_database_path) as conn:
        cancel_job(conn, J)

    with pytest.raises(WorkCancelled):
        tick()


def test_the_format_tick_notices_a_cancelled_job(settings: Settings) -> None:
    worker, _claimed = _claimed_clip_worker(settings)
    with session(settings.resolved_database_path) as conn:
        claimed_format = claim_next_format(
            conn, worker_id="test-worker", lease_seconds=300
        )
    assert claimed_format is not None
    tick = worker._format_tick(claimed_format)

    tick()

    with session(settings.resolved_database_path) as conn:
        cancel_job(conn, J)

    with pytest.raises(WorkCancelled):
        tick()


# --- the render itself -------------------------------------------------------


def test_a_cancelled_job_stops_a_rendering_clip(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick fires during the render; the cancel stops it and is recorded."""
    from app import clip_service
    from app.clip_service import ClipRunner

    worker, claimed = _claimed_clip_worker(settings)

    ticks: list[int] = []

    def fake_render(args, *, timeout_seconds, on_tick=None):  # noqa: ANN001
        assert on_tick is not None
        ticks.append(1)
        # The user cancels while FFmpeg is running.
        with session(settings.resolved_database_path) as conn:
            cancel_job(conn, J)
        on_tick()  # the worker's tick sees the cancellation here
        ticks.append(2)  # never reached: the tick raised

    def fake_probe(source, *, ffprobe_path, timeout_seconds):  # noqa: ANN001
        return ClipVerificationShim()

    monkeypatch.setattr(clip_service, "render_clip", fake_render)
    monkeypatch.setattr(clip_service, "probe_source", fake_probe)

    with session(settings.resolved_database_path) as conn:
        runner = ClipRunner(storage=worker.storage)
        outcome = runner.run(
            conn,
            claimed,
            worker_id="test-worker",
            on_tick=worker._clip_tick(claimed),
        )

    assert ticks == [1], "the tick must fire during the render"
    assert outcome.status is ClipStatus.FAILED
    assert outcome.error_code == ErrorCode.JOB_CANCELLED.value
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, CLIP)
        assert clip.status is ClipStatus.FAILED
        assert clip.error_code == ErrorCode.JOB_CANCELLED.value
    # No temporary render file is left behind.
    leftovers = list((settings.storage_root / "temp").glob("clip-*.mp4"))
    assert leftovers == []


def _completed_clip_file(settings: Settings) -> None:
    """Give the claimed clip a finished output, so a format can resolve it.

    The clip walks its legal transitions (preparing -> rendering ->
    verifying -> completed); a claimed clip sits in ``preparing``.
    """
    output = settings.storage_root / "clips" / "claimed.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"y" * 64)
    with session(settings.resolved_database_path) as conn:
        for status, stage, progress in (
            (ClipStatus.RENDERING, "rendering", 40),
            (ClipStatus.VERIFYING, "verifying", 85),
            (ClipStatus.COMPLETED, "completed", 100),
        ):
            update_clip(
                conn,
                CLIP,
                status=status,
                stage=stage,
                progress=progress,
                expected_worker_id="test-worker",
            )
        update_clip(
            conn,
            CLIP,
            status=ClipStatus.COMPLETED,
            stage="completed",
            progress=100,
            output_path="clips/claimed.mp4",
            output_size_bytes=64,
            actual_duration_seconds=10.0,
            width=320,
            height=240,
            video_codec="h264",
            audio_codec="aac",
            clear_error=True,
            expected_worker_id="test-worker",
        )


def test_a_cancelled_job_stops_a_rendering_format(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract for the portrait render."""
    from app import format_service
    from app.format_service import FormatRunner

    worker, _claimed = _claimed_clip_worker(settings)
    _completed_clip_file(settings)

    ticks: list[int] = []

    def fake_render(args, *, timeout_seconds, cwd=None, on_tick=None):  # noqa: ANN001
        assert on_tick is not None
        ticks.append(1)
        with session(settings.resolved_database_path) as conn:
            cancel_job(conn, J)
        on_tick()
        ticks.append(2)

    monkeypatch.setattr(format_service, "render_format", fake_render)
    monkeypatch.setattr(
        FormatRunner,
        "_probe",
        lambda self, source: {
            "duration_seconds": 10.0,
            "has_audio": True,
        },
    )

    with session(settings.resolved_database_path) as conn:
        artifact_row = conn.execute(
            "SELECT * FROM clip_formats WHERE id = ?", (FORMAT,)
        ).fetchone()
        runner = FormatRunner(storage=worker.storage)
        outcome = runner.run(
            conn,
            _row_to_format(artifact_row),
            worker_id="test-worker",
            on_tick=worker._format_tick(_row_to_format(artifact_row)),
        )

    assert ticks == [1], "the tick must fire during the render"
    assert outcome.status is ClipStatus.FAILED
    assert outcome.error_code == ErrorCode.JOB_CANCELLED.value
    with session(settings.resolved_database_path) as conn:
        row = conn.execute(
            "SELECT status, error_code FROM clip_formats WHERE id = ?", (FORMAT,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_code"] == ErrorCode.JOB_CANCELLED.value


class ClipVerificationShim:
    """The bits of ``ClipVerification`` the clip runner's probe step needs."""

    duration_seconds = 10.0
    has_audio = True


def test_the_worker_never_claims_children_of_a_cancelled_job(
    settings: Settings,
) -> None:
    """End to end through the runner loop: nothing is rendered afterwards."""
    worker, _claimed = _claimed_clip_worker(settings)
    with session(settings.resolved_database_path) as conn:
        cancel_job(conn, J)

    # The queued format was failed by the cascade; the clip was claimed
    # before the cancellation, so the worker would only touch it through the
    # tick. Nothing is claimable any more, and nothing renders.
    assert worker.run_clips_once() is False
    assert worker.run_formats_once() is False
    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, J)
        assert job.status is JobStatus.CANCELLED
