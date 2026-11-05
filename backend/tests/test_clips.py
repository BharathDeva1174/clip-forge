"""The clip lifecycle, the boundaries, the worker and the recovery.

The renders themselves are covered by ``test_clip_render.py`` with real
FFmpeg. This file is about the rules: what may be asked for, what may
change, and what happens when a worker dies.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.clips import (
    ClipInputError,
    ClipStage,
    ClipStatus,
    ClipTransitionError,
    assert_transition,
    can_transition,
    validate_boundaries,
)
from app.config import Settings
from app.db import (
    SCHEMA_VERSION,
    claim_next_clip,
    create_clip,
    create_job,
    get_clip,
    get_clip_for_candidate,
    list_clips,
    recover_expired_clip_leases,
    reset_clip_for_retry,
    session,
    update_clip,
)
from app.errors import ErrorCode
from app.jobs import SourceType
from app.storage import Storage
from app.worker import Worker

FFMPEG = shutil.which("ffmpeg")


# --- the boundaries ----------------------------------------------------------


def test_a_valid_range_is_accepted() -> None:
    start, end = validate_boundaries(
        1.5,
        10.0,
        source_duration_seconds=30.0,
        min_duration_seconds=1.0,
        max_duration_seconds=180.0,
    )

    assert (start, end) == (1.5, 10.0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_timestamp_that_is_not_finite_is_refused(value: float) -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            0.0,
            value,
            source_duration_seconds=30.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_INVALID_BOUNDARIES.value


@pytest.mark.parametrize("value", [True, False, "5", None, [1]])
def test_a_timestamp_that_is_not_a_number_is_refused(value: object) -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            value,
            5.0,
            source_duration_seconds=30.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_INVALID_BOUNDARIES.value


def test_a_negative_start_is_refused() -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            -1.0,
            5.0,
            source_duration_seconds=30.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_INVALID_BOUNDARIES.value


def test_an_end_before_the_start_is_refused() -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            5.0,
            5.0,
            source_duration_seconds=30.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_INVALID_BOUNDARIES.value


def test_a_clip_that_is_too_short_is_refused() -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            0.0,
            0.2,
            source_duration_seconds=30.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_DURATION_TOO_SHORT.value


def test_a_clip_that_is_too_long_is_refused() -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            0.0,
            500.0,
            source_duration_seconds=1000.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_DURATION_TOO_LONG.value


def test_a_clip_that_runs_past_the_source_is_refused() -> None:
    with pytest.raises(ClipInputError) as failure:
        validate_boundaries(
            25.0,
            40.0,
            source_duration_seconds=30.0,
            min_duration_seconds=1.0,
            max_duration_seconds=180.0,
        )

    assert failure.value.code == ErrorCode.CLIP_SOURCE_TOO_SHORT.value


def test_a_clip_that_ends_a_little_past_the_source_is_allowed() -> None:
    """The tolerance is explicit, and it is small."""
    start, end = validate_boundaries(
        25.0,
        30.4,
        source_duration_seconds=30.0,
        min_duration_seconds=1.0,
        max_duration_seconds=180.0,
        duration_tolerance_seconds=0.5,
    )

    assert end == 30.4


# --- the lifecycle -----------------------------------------------------------


def test_the_lifecycle_allows_the_documented_steps() -> None:
    assert can_transition(ClipStatus.QUEUED, ClipStatus.PREPARING)
    assert can_transition(ClipStatus.PREPARING, ClipStatus.RENDERING)
    assert can_transition(ClipStatus.RENDERING, ClipStatus.VERIFYING)
    assert can_transition(ClipStatus.VERIFYING, ClipStatus.COMPLETED)


def test_every_status_may_fail() -> None:
    for status in (
        ClipStatus.QUEUED,
        ClipStatus.PREPARING,
        ClipStatus.RENDERING,
        ClipStatus.VERIFYING,
    ):
        assert can_transition(status, ClipStatus.FAILED)


def test_a_terminal_clip_is_terminal() -> None:
    assert not can_transition(ClipStatus.COMPLETED, ClipStatus.QUEUED)
    assert not can_transition(ClipStatus.FAILED, ClipStatus.RENDERING)


def test_a_clip_cannot_skip_the_verification() -> None:
    """A clip is not completed because FFmpeg exited with zero."""
    assert not can_transition(ClipStatus.RENDERING, ClipStatus.COMPLETED)
    with pytest.raises(ClipTransitionError):
        assert_transition(ClipStatus.RENDERING, ClipStatus.COMPLETED)


# --- the database ------------------------------------------------------------


def _job(settings: Settings, job_id: str = "a" * 32, source_path: str | None = None) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
            source_path=source_path,
        )


def _clip(settings: Settings, clip_id: str = "c" * 32, job_id: str = "a" * 32):
    with session(settings.resolved_database_path) as conn:
        return create_clip(
            conn,
            job_id=job_id,
            analysis_id=None,
            candidate_id=None,
            start_seconds=1.0,
            end_seconds=4.0,
            clip_id=clip_id,
        )


def test_a_clip_is_persisted(settings: Settings) -> None:
    _job(settings)

    clip = _clip(settings)

    assert clip.id == "c" * 32
    assert clip.status is ClipStatus.QUEUED
    assert clip.stage is ClipStage.QUEUED
    assert clip.progress == 0
    assert clip.duration_seconds == pytest.approx(3.0)
    assert clip.output_path is None
    assert clip.attempt_count == 0


def test_the_migration_adds_the_clip_table(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert "clips" in tables
    # The schema only ever moves forward, so the assertion follows the
    # constant rather than a number that a later phase will change.
    assert version == SCHEMA_VERSION


def test_a_clip_is_claimed_by_one_worker(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        first = claim_next_clip(conn, worker_id="w1", lease_seconds=60)
        second = claim_next_clip(conn, worker_id="w2", lease_seconds=60)

    assert first is not None
    assert first.status is ClipStatus.PREPARING
    assert first.attempt_count == 1
    # The second worker finds nothing: the claim is atomic.
    assert second is None


def test_a_stale_worker_cannot_complete_a_clip(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        claim_next_clip(conn, worker_id="w1", lease_seconds=60)
        # Another worker takes the clip over.
        conn.execute(
            "UPDATE clips SET worker_id = 'w2' WHERE id = ?", ("c" * 32,)
        )
        result = update_clip(
            conn,
            "c" * 32,
            status=ClipStatus.COMPLETED,
            expected_worker_id="w1",
        )

    assert result is None
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, "c" * 32)
    assert clip is not None
    assert clip.status is ClipStatus.PREPARING


def test_an_illegal_transition_is_refused(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        with pytest.raises(ClipTransitionError):
            update_clip(
                conn,
                "c" * 32,
                status=ClipStatus.COMPLETED,
                expected_worker_id=None,
            )


def test_only_a_failed_clip_can_be_retried(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        claim_next_clip(conn, worker_id="w1", lease_seconds=60)
        update_clip(conn, "c" * 32, status=ClipStatus.FAILED, error_code="x")
        retried = reset_clip_for_retry(conn, "c" * 32)
        assert retried is not None
        assert retried.status is ClipStatus.QUEUED
        assert retried.error_code is None

        # A clip that is not failed is never reset.
        update_clip(conn, "c" * 32, status=ClipStatus.RENDERING)
        assert reset_clip_for_retry(conn, "c" * 32) is None


def test_recovery_requeues_a_clip_whose_worker_died(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        claim_next_clip(conn, worker_id="w1", lease_seconds=60)
        conn.execute(
            "UPDATE clips SET lease_expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            ("c" * 32,),
        )
        recovered = recover_expired_clip_leases(conn, max_attempts=3)

    assert len(recovered) == 1
    assert recovered[0].status is ClipStatus.QUEUED


def test_recovery_fails_a_clip_that_has_run_out_of_attempts(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        claim_next_clip(conn, worker_id="w1", lease_seconds=60)
        conn.execute(
            "UPDATE clips SET lease_expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            ("c" * 32,),
        )
        recovered = recover_expired_clip_leases(conn, max_attempts=1)

    assert len(recovered) == 1
    assert recovered[0].status is ClipStatus.FAILED
    assert recovered[0].error_code == "max_attempts_exceeded"


def test_recovery_does_not_requeue_a_completed_clip(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        claim_next_clip(conn, worker_id="w1", lease_seconds=60)
        update_clip(conn, "c" * 32, status=ClipStatus.RENDERING)
        update_clip(conn, "c" * 32, status=ClipStatus.VERIFYING)
        update_clip(conn, "c" * 32, status=ClipStatus.COMPLETED)
        conn.execute(
            "UPDATE clips SET lease_expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            ("c" * 32,),
        )
        recovered = recover_expired_clip_leases(conn, max_attempts=3)

    assert recovered == []
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, "c" * 32)
    assert clip is not None
    assert clip.status is ClipStatus.COMPLETED


def test_the_clips_of_a_job_are_listed(settings: Settings) -> None:
    _job(settings)
    _clip(settings, clip_id="c" * 32)
    _clip(settings, clip_id="d" * 32)

    with session(settings.resolved_database_path) as conn:
        clips = list_clips(conn, job_id="a" * 32)

    assert len(clips) == 2


# --- the worker --------------------------------------------------------------


def test_the_worker_renders_a_clip(
    settings: Settings, clip_source_video: Path
) -> None:
    """The real thing: the worker renders a clip from a real video."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so no clip can be rendered")

    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    shutil.copy(clip_source_video, storage.sources_dir / "clip.mp4")

    _job(settings, source_path="sources/clip.mp4")
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id=None,
            candidate_id=None,
            start_seconds=2.0,
            end_seconds=5.0,
            clip_id="c" * 32,
        )

    worker = Worker(settings, worker_id="w1")

    assert worker.run_clips_once() is True

    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, "c" * 32)
    assert clip is not None
    assert clip.status is ClipStatus.COMPLETED
    assert clip.actual_duration_seconds == pytest.approx(3.0, abs=0.3)
    assert clip.width == 320
    assert clip.height == 240
    assert clip.video_codec == "h264"
    assert clip.output_size_bytes and clip.output_size_bytes > 1024
    # The file is where it says it is.
    assert (storage.root / clip.output_path).is_file()


def test_the_worker_fails_a_clip_whose_source_is_missing(
    settings: Settings,
) -> None:
    _job(settings)
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id=None,
            candidate_id=None,
            start_seconds=1.0,
            end_seconds=3.0,
            clip_id="c" * 32,
        )

    worker = Worker(settings, worker_id="w1")
    assert worker.run_clips_once() is True

    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, "c" * 32)
    assert clip is not None
    assert clip.status is ClipStatus.FAILED
    assert clip.error_code == ErrorCode.CLIP_SOURCE_MISSING.value
    # The error is safe to show.
    assert "C:\\" not in (clip.error_message or "")


def test_the_worker_leaves_no_temporary_file_behind(
    settings: Settings, clip_source_video: Path
) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so no clip can be rendered")

    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    shutil.copy(clip_source_video, storage.sources_dir / "clip.mp4")
    _job(settings, source_path="sources/clip.mp4")
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id=None,
            candidate_id=None,
            start_seconds=1.0,
            end_seconds=3.0,
            clip_id="c" * 32,
        )

    Worker(settings, worker_id="w1").run_clips_once()

    leftovers = list(storage.temp_dir.glob("clip-*.mp4"))
    assert leftovers == []


def test_the_reconciliation_removes_only_old_temporary_files(
    settings: Settings, tmp_path: Path
) -> None:
    """The cleanup is narrow, and it does not touch anything else."""
    storage = Storage(settings.storage_root)
    storage.ensure_layout()

    old = storage.temp_dir / "clip-old.mp4"
    old.write_bytes(b"x")
    fresh = storage.temp_dir / "clip-fresh.mp4"
    fresh.write_bytes(b"x")
    elsewhere = storage.sources_dir / "clip-source.mp4"
    elsewhere.write_bytes(b"x")

    import os
    import time

    ancient = time.time() - 7200
    os.utime(old, (ancient, ancient))

    removed = Worker(settings, worker_id="w1").reconcile_clips()

    assert removed == 1
    assert not old.exists()
    # The fresh temporary file and the source are untouched.
    assert fresh.exists()
    assert elsewhere.exists()
