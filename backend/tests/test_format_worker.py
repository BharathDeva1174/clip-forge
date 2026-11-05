"""The formatted clip: the worker, the persistence and the recovery.

The renders are real: a clip is cut from a real video, then formatted from
that clip, so the whole chain is exercised.
"""

from __future__ import annotations

import shutil
import subprocess
import sqlite3
from pathlib import Path

import pytest

from app.clips import ClipStatus
from app.config import Settings
from app.db import (
    SCHEMA_VERSION,
    claim_next_format,
    create_clip,
    create_job,
    create_format,
    get_clip,
    get_format,
    get_job,
    list_formats,
    recover_expired_format_leases,
    reset_format_for_retry,
    session,
    update_clip,
    update_format,
    update_job,
)
from app.errors import ErrorCode
from app.formatting import CaptionSettings, FormatSettings, LayoutMode
from app.jobs import JobStage, JobStatus, SourceType
from app.storage import Storage
from app.transcripts import Transcript, TranscriptSegment
from app.worker import Worker

FFMPEG = shutil.which("ffmpeg")


def _source_video(tmp_path: Path) -> Path:
    """A short landscape video with sound."""
    target = tmp_path / "source.mp4"
    subprocess.run(
        [
            FFMPEG or "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if not target.exists():
        pytest.skip("ffmpeg could not build the source fixture")
    return target


def _transcript() -> Transcript:
    """A transcript whose segments sit on the source timeline."""
    return Transcript(
        language="en",
        model_name="tiny.en",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=6.0,
        segments=(
            TranscriptSegment(
                index=0,
                start_seconds=0.0,
                end_seconds=2.0,
                text="The first thing worth clipping.",
                average_log_probability=-0.2,
                no_speech_probability=0.01,
            ),
            TranscriptSegment(
                index=1,
                start_seconds=2.0,
                end_seconds=4.0,
                text="The second thing worth clipping.",
                average_log_probability=-0.2,
                no_speech_probability=0.01,
            ),
        ),
    )


def _job(settings: Settings, source: Path) -> str:
    """A completed job with a transcript and a stored source."""
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    shutil.copy(source, storage.sources_dir / "clip.mp4")

    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="a" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
            source_path="sources/clip.mp4",
        )
        for status, stage in (
            (JobStatus.VALIDATING, JobStage.VALIDATING),
            (JobStatus.INSPECTING, JobStage.INSPECTING),
            (JobStatus.EXTRACTING_AUDIO, JobStage.EXTRACTING_AUDIO),
            (JobStatus.TRANSCRIBING, JobStage.TRANSCRIBING),
            (JobStatus.COMPLETED, JobStage.READY),
        ):
            update_job(conn, "a" * 32, status=status, stage=stage)
        from app.db import save_transcript

        save_transcript(conn, job_id="a" * 32, transcript=_transcript())
    return "a" * 32


def _rendered_clip(settings: Settings, job_id: str) -> str:
    """A Phase 5 clip, rendered for real."""
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id=job_id,
            analysis_id=None,
            candidate_id=None,
            start_seconds=0.0,
            end_seconds=4.0,
            clip_id="c" * 32,
        )
    Worker(settings, worker_id="test-worker").run_clips_once()
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, "c" * 32)
    assert clip is not None
    assert clip.status is ClipStatus.COMPLETED
    return clip.id


def _ensure_parents(settings: Settings) -> None:
    """Make sure a job and a clip exist, so the foreign keys are satisfied."""
    with session(settings.resolved_database_path) as conn:
        if get_job(conn, "a" * 32) is None:
            create_job(
                conn,
                job_id="a" * 32,
                source_type=SourceType.UPLOAD,
                source_reference="clip.mp4",
            )
        if get_clip(conn, "c" * 32) is None:
            create_clip(
                conn,
                job_id="a" * 32,
                analysis_id=None,
                candidate_id=None,
                start_seconds=0.0,
                end_seconds=4.0,
                clip_id="c" * 32,
            )


def _artifact(settings: Settings, clip_id: str, job_id: str, **settings_kwargs) -> str:
    """A queued formatted clip."""
    _ensure_parents(settings)
    settings_object = FormatSettings(**settings_kwargs).validate()
    import json

    with session(settings.resolved_database_path) as conn:
        artifact = create_format(
            conn,
            clip_id=clip_id,
            job_id=job_id,
            config_fingerprint=settings_object.fingerprint(),
            settings_json=json.dumps(settings_object.normalised().to_dict()),
            captions_enabled=settings_object.captions.enabled,
            format_id="f" * 32,
        )
    return artifact.id


def test_the_migration_adds_the_format_table(settings: Settings) -> None:
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

    assert "clip_formats" in tables
    assert version == SCHEMA_VERSION


# --- the worker -------------------------------------------------------------


def test_the_worker_formats_a_clip(
    settings: Settings, tmp_path: Path
) -> None:
    """The whole chain: a real clip, formatted into a real portrait video."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so nothing can be formatted")

    source = _source_video(tmp_path)
    job_id = _job(settings, source)
    clip_id = _rendered_clip(settings, job_id)
    artifact_id = _artifact(settings, clip_id, job_id)

    worker = Worker(settings, worker_id="test-worker")
    assert worker.run_formats_once() is True

    with session(settings.resolved_database_path) as conn:
        artifact = get_format(conn, artifact_id)
    assert artifact is not None
    assert artifact.status is ClipStatus.COMPLETED
    # The output is exactly the configured portrait canvas.
    assert (artifact.width, artifact.height) == (720, 1280)
    assert artifact.video_codec == "h264"
    assert artifact.pixel_format == "yuv420p"
    assert artifact.output_size_bytes and artifact.output_size_bytes > 1024
    assert artifact.actual_duration_seconds == pytest.approx(4.0, abs=0.4)
    # The captions were burned in, and the count is on the record.
    assert artifact.captions_applied is True
    assert artifact.caption_count == 2

    storage = Storage(settings.storage_root)
    assert (storage.root / artifact.output_path).is_file()


def test_the_original_clip_is_untouched(
    settings: Settings, tmp_path: Path
) -> None:
    """The Phase 5 clip stays exactly as it was."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so nothing can be formatted")

    source = _source_video(tmp_path)
    job_id = _job(settings, source)
    clip_id = _rendered_clip(settings, job_id)
    with session(settings.resolved_database_path) as conn:
        before = get_clip(conn, clip_id)
    assert before is not None
    storage = Storage(settings.storage_root)
    before_bytes = (storage.root / before.output_path).read_bytes()

    _artifact(settings, clip_id, job_id)
    Worker(settings, worker_id="test-worker").run_formats_once()

    with session(settings.resolved_database_path) as conn:
        after = get_clip(conn, clip_id)
    assert after is not None
    assert after.status is ClipStatus.COMPLETED
    assert after.output_path == before.output_path
    assert (storage.root / after.output_path).read_bytes() == before_bytes


def test_the_worker_fails_a_format_whose_clip_is_gone(
    settings: Settings, tmp_path: Path
) -> None:
    source = _source_video(tmp_path)
    job_id = _job(settings, source)
    clip_id = _rendered_clip(settings, job_id)
    artifact_id = _artifact(settings, clip_id, job_id)

    # The clip's file is removed.
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, clip_id)
    storage = Storage(settings.storage_root)
    (storage.root / clip.output_path).unlink(missing_ok=True)

    Worker(settings, worker_id="test-worker").run_formats_once()

    with session(settings.resolved_database_path) as conn:
        artifact = get_format(conn, artifact_id)
    assert artifact is not None
    assert artifact.status is ClipStatus.FAILED
    assert artifact.error_code == ErrorCode.FORMAT_SOURCE_MISSING.value
    # The error is safe to show.
    assert "C:\\" not in (artifact.error_message or "")


def test_a_format_without_captions_renders_without_them(
    settings: Settings, tmp_path: Path
) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so nothing can be formatted")

    source = _source_video(tmp_path)
    job_id = _job(settings, source)
    clip_id = _rendered_clip(settings, job_id)
    artifact_id = _artifact(
        settings,
        clip_id,
        job_id,
        captions=CaptionSettings(enabled=False),
    )

    Worker(settings, worker_id="test-worker").run_formats_once()

    with session(settings.resolved_database_path) as conn:
        artifact = get_format(conn, artifact_id)
    assert artifact is not None
    assert artifact.status is ClipStatus.COMPLETED
    assert artifact.captions_enabled is False
    assert artifact.captions_applied is False
    assert artifact.caption_count == 0


def test_a_format_with_no_caption_text_says_so(
    settings: Settings, tmp_path: Path
) -> None:
    """The artifact must not claim captions that were not burned in."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so nothing can be formatted")

    source = _source_video(tmp_path)
    job_id = _job(settings, source)
    clip_id = _rendered_clip(settings, job_id)
    # A clip that sits after the transcript ends, so no caption text overlaps
    # it. The source is six seconds long and the transcript covers the
    # first four.
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id=job_id,
            analysis_id=None,
            candidate_id=None,
            start_seconds=4.5,
            end_seconds=5.5,
            clip_id="d" * 32,
        )
    Worker(settings, worker_id="test-worker").run_clips_once()
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, "d" * 32)
    assert clip is not None
    if clip.status is not ClipStatus.COMPLETED:
        pytest.skip("the out-of-range clip could not be rendered")

    artifact_id = _artifact(settings, "d" * 32, job_id)
    Worker(settings, worker_id="test-worker").run_formats_once()

    with session(settings.resolved_database_path) as conn:
        artifact = get_format(conn, artifact_id)
    assert artifact is not None
    assert artifact.status is ClipStatus.COMPLETED
    assert artifact.captions_enabled is True
    # Nothing was burned in, and the artifact says so.
    assert artifact.captions_applied is False
    assert artifact.caption_count == 0


# --- the queue --------------------------------------------------------------


def test_a_format_is_claimed_by_one_worker(settings: Settings) -> None:
    _artifact(settings, "c" * 32, "a" * 32)

    with session(settings.resolved_database_path) as conn:
        first = claim_next_format(conn, worker_id="w1", lease_seconds=60)
        second = claim_next_format(conn, worker_id="w2", lease_seconds=60)

    assert first is not None
    assert first.status is ClipStatus.PREPARING
    assert first.attempt_count == 1
    assert second is None


def test_a_stale_worker_cannot_complete_a_format(settings: Settings) -> None:
    artifact_id = _artifact(settings, "c" * 32, "a" * 32)

    with session(settings.resolved_database_path) as conn:
        claim_next_format(conn, worker_id="w1", lease_seconds=60)
        conn.execute(
            "UPDATE clip_formats SET worker_id = 'w2' WHERE id = ?", (artifact_id,)
        )
        result = update_format(
            conn,
            artifact_id,
            status=ClipStatus.COMPLETED,
            expected_worker_id="w1",
        )

    assert result is None
    with session(settings.resolved_database_path) as conn:
        artifact = get_format(conn, artifact_id)
    assert artifact is not None
    assert artifact.status is ClipStatus.PREPARING


def test_recovery_requeues_a_format_whose_worker_died(
    settings: Settings,
) -> None:
    artifact_id = _artifact(settings, "c" * 32, "a" * 32)

    with session(settings.resolved_database_path) as conn:
        claim_next_format(conn, worker_id="w1", lease_seconds=60)
        conn.execute(
            "UPDATE clip_formats SET lease_expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (artifact_id,),
        )
        recovered = recover_expired_format_leases(conn, max_attempts=3)

    assert len(recovered) == 1
    assert recovered[0].status is ClipStatus.QUEUED


def test_recovery_fails_a_format_that_has_run_out_of_attempts(
    settings: Settings,
) -> None:
    artifact_id = _artifact(settings, "c" * 32, "a" * 32)

    with session(settings.resolved_database_path) as conn:
        claim_next_format(conn, worker_id="w1", lease_seconds=60)
        conn.execute(
            "UPDATE clip_formats SET lease_expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (artifact_id,),
        )
        recovered = recover_expired_format_leases(conn, max_attempts=1)

    assert len(recovered) == 1
    assert recovered[0].status is ClipStatus.FAILED
    assert recovered[0].error_code == "max_attempts_exceeded"


def test_recovery_does_not_requeue_a_completed_format(settings: Settings) -> None:
    artifact_id = _artifact(settings, "c" * 32, "a" * 32)

    with session(settings.resolved_database_path) as conn:
        claim_next_format(conn, worker_id="w1", lease_seconds=60)
        update_format(conn, artifact_id, status=ClipStatus.RENDERING)
        update_format(conn, artifact_id, status=ClipStatus.VERIFYING)
        update_format(conn, artifact_id, status=ClipStatus.COMPLETED)
        conn.execute(
            "UPDATE clip_formats SET lease_expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (artifact_id,),
        )
        recovered = recover_expired_format_leases(conn, max_attempts=3)

    assert recovered == []
    with session(settings.resolved_database_path) as conn:
        artifact = get_format(conn, artifact_id)
    assert artifact is not None
    assert artifact.status is ClipStatus.COMPLETED


def test_only_a_failed_format_can_be_retried(settings: Settings) -> None:
    artifact_id = _artifact(settings, "c" * 32, "a" * 32)

    with session(settings.resolved_database_path) as conn:
        claim_next_format(conn, worker_id="w1", lease_seconds=60)
        update_format(conn, artifact_id, status=ClipStatus.FAILED, error_code="x")
        retried = reset_format_for_retry(conn, artifact_id)
        assert retried is not None
        assert retried.status is ClipStatus.QUEUED
        assert retried.error_code is None

        update_format(conn, artifact_id, status=ClipStatus.RENDERING)
        assert reset_format_for_retry(conn, artifact_id) is None


# --- the cleanup ------------------------------------------------------------


def test_the_reconciliation_removes_only_old_working_directories(
    settings: Settings,
) -> None:
    import os
    import time

    storage = Storage(settings.storage_root)
    storage.ensure_layout()

    old = storage.new_format_work_dir("f" * 32, 1)
    (old / "captions.srt").write_text("x", encoding="utf-8")
    fresh = storage.new_format_work_dir("f" * 32, 2)
    elsewhere = storage.clips_dir / "clip.mp4"
    elsewhere.write_bytes(b"x")

    ancient = time.time() - 7200
    os.utime(old, (ancient, ancient))

    removed = Worker(settings, worker_id="test-worker").reconcile_formats()

    assert removed == 1
    assert not old.exists()
    # The fresh directory and the finished clip are untouched.
    assert fresh.exists()
    assert elsewhere.exists()


def test_no_temporary_file_is_left_after_a_render(
    settings: Settings, tmp_path: Path
) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so nothing can be formatted")

    source = _source_video(tmp_path)
    job_id = _job(settings, source)
    clip_id = _rendered_clip(settings, job_id)
    _artifact(settings, clip_id, job_id)

    Worker(settings, worker_id="test-worker").run_formats_once()

    storage = Storage(settings.storage_root)
    leftovers = list(storage.temp_dir.glob("format-*"))
    assert leftovers == []
