"""Tests for the transcript tables and the Phase 3 migration.

The migration is exercised against a database that was left behind by the
Phase 2 build, not only against a fresh one, because that is the case that
matters: an existing installation must keep its jobs and its sources.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.db import (
    MIGRATIONS,
    SCHEMA_VERSION,
    connect,
    count_transcript_segments,
    create_job,
    delete_transcript,
    ensure_schema,
    get_transcript,
    list_transcript_segments,
    save_transcript,
    session,
)
from app.jobs import SourceType
from app.transcripts import Transcript, TranscriptSegment

PHASE_2_MIGRATION_COUNT = 2


def _transcript(*texts: str, language: str | None = "en") -> Transcript:
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
        language=language,
        model_name="fake-model",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=float(len(texts)),
        segments=segments,
    )


def _phase2_database(path: Path) -> None:
    """Build a database exactly as the Phase 2 build left it."""
    conn = connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for version, statements in MIGRATIONS[:PHASE_2_MIGRATION_COUNT]:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2026-10-20T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO jobs (
                id, source_type, source_reference, source_path, status, progress,
                stage, created_at, updated_at, attempt_count, original_filename,
                file_size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 32,
                "upload",
                "clip.mp4",
                "sources/clip.mp4",
                "completed",
                100,
                "ready",
                "2026-10-20T00:00:00+00:00",
                "2026-10-20T00:00:01+00:00",
                1,
                "clip.mp4",
                4096,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()


# --- migration -------------------------------------------------------------


def test_a_phase_2_database_migrates_without_losing_anything(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)

    conn = connect(db_path)
    ensure_schema(conn)

    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (
        SCHEMA_VERSION
    )
    row = conn.execute(
        "SELECT id, source_type, source_reference, source_path, status, progress, "
        "stage, original_filename, file_size_bytes FROM jobs"
    ).fetchone()
    assert row is not None
    assert row["id"] == "a" * 32
    assert row["source_reference"] == "clip.mp4"
    assert row["source_path"] == "sources/clip.mp4"
    assert row["status"] == "completed"
    assert row["progress"] == 100
    assert row["file_size_bytes"] == 4096
    conn.close()


def test_the_migration_creates_the_transcript_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)

    conn = connect(db_path)
    ensure_schema(conn)

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "transcripts" in tables
    assert "transcript_segments" in tables
    conn.close()


def test_the_migration_keeps_the_job_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)

    conn = connect(db_path)
    ensure_schema(conn)

    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'jobs'"
        )
    }
    assert "idx_jobs_status_created_at" in indexes
    assert "idx_jobs_created_at" in indexes
    assert "idx_jobs_lease" in indexes
    assert "idx_jobs_worker_id" in indexes
    conn.close()


def test_the_migration_leaves_no_placeholder_table(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)

    conn = connect(db_path)
    ensure_schema(conn)

    tables = [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    assert "jobs_phase3" not in tables
    assert tables.count("jobs") == 1
    conn.close()


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)

    conn = connect(db_path)
    ensure_schema(conn)
    ensure_schema(conn)
    ensure_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == (
        SCHEMA_VERSION
    )
    conn.close()


def test_a_fresh_database_is_created_with_the_new_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"

    with session(db_path) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (
            SCHEMA_VERSION
        )
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"jobs", "transcripts", "transcript_segments"} <= tables


def test_the_new_job_states_are_accepted_after_the_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)

    conn = connect(db_path)
    ensure_schema(conn)
    conn.execute(
        "UPDATE jobs SET status = 'extracting_audio', stage = 'extracting_audio'"
    )
    conn.execute("UPDATE jobs SET status = 'transcribing', stage = 'transcribing'")
    conn.close()


# --- persistence -----------------------------------------------------------


def test_a_transcript_is_stored_with_its_segments(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="b" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )

    record = save_transcript(
        db, job_id="b" * 32, transcript=_transcript("one", "two", "three")
    )

    assert record.job_id == "b" * 32
    assert record.segment_count == 3
    assert record.language == "en"
    assert record.model_name == "fake-model"

    stored = get_transcript(db, "b" * 32)
    assert stored is not None
    assert stored.id == record.id
    assert count_transcript_segments(db, record.id) == 3


def test_the_segments_come_back_in_order(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="c" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    record = save_transcript(
        db, job_id="c" * 32, transcript=_transcript("first", "second", "third")
    )

    segments = list_transcript_segments(db, record.id)
    assert [segment.text for segment in segments] == ["first", "second", "third"]
    assert [segment.segment_index for segment in segments] == [0, 1, 2]
    assert [segment.start_seconds for segment in segments] == [0.0, 1.0, 2.0]


def test_the_segments_are_paginated(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="d" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    record = save_transcript(
        db, job_id="d" * 32, transcript=_transcript("a", "b", "c", "d", "e")
    )

    page = list_transcript_segments(db, record.id, limit=2, offset=0)
    assert [segment.text for segment in page] == ["a", "b"]
    page = list_transcript_segments(db, record.id, limit=2, offset=2)
    assert [segment.text for segment in page] == ["c", "d"]
    page = list_transcript_segments(db, record.id, limit=2, offset=4)
    assert [segment.text for segment in page] == ["e"]


def test_a_transcript_needs_a_real_job(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        save_transcript(db, job_id="does-not-exist", transcript=_transcript("hello"))


def test_a_segment_needs_a_real_transcript(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO transcript_segments (
                id, transcript_id, segment_index, start_seconds, end_seconds, text
            ) VALUES ('x', 'no-such-transcript', 0, 0.0, 1.0, 'hello')
            """
        )


def test_a_job_can_only_have_one_transcript(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="e" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    save_transcript(db, job_id="e" * 32, transcript=_transcript("one"))

    # The uniqueness is enforced by the schema itself, so even a writer that
    # does not go through save_transcript cannot add a second transcript.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO transcripts (
                id, job_id, language, model_name, audio_sample_rate,
                audio_channels, duration_seconds, segment_count, created_at
            ) VALUES ('another-id', ?, 'en', 'fake', 16000, 1, 1.0, 0, '2026-10-20T00:00:00+00:00')
            """,
            ("e" * 32,),
        )


def test_saving_the_same_transcript_twice_replaces_it(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="f" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    first = save_transcript(db, job_id="f" * 32, transcript=_transcript("one", "two"))
    second = save_transcript(db, job_id="f" * 32, transcript=_transcript("three"))

    assert second.id != first.id
    stored = get_transcript(db, "f" * 32)
    assert stored is not None
    assert stored.id == second.id
    assert stored.segment_count == 1

    # The old segments are gone: the retry replaced the transcript rather than
    # adding a second one.
    assert count_transcript_segments(db, first.id) == 0
    assert count_transcript_segments(db, second.id) == 1


def test_a_failure_while_storing_the_segments_rolls_everything_back(
    db: sqlite3.Connection,
) -> None:
    create_job(
        db,
        job_id="g" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    db.execute(
        """
        CREATE TRIGGER refuse_segments BEFORE INSERT ON transcript_segments
        BEGIN SELECT RAISE(ABORT, 'no segments today'); END
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        save_transcript(db, job_id="g" * 32, transcript=_transcript("one", "two"))

    # Nothing at all was written: no half-finished transcript is visible.
    assert get_transcript(db, "g" * 32) is None
    assert (
        db.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0
    )


def test_a_transcript_can_be_deleted(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="h" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    record = save_transcript(db, job_id="h" * 32, transcript=_transcript("one"))

    delete_transcript(db, "h" * 32)

    assert get_transcript(db, "h" * 32) is None
    assert count_transcript_segments(db, record.id) == 0


def test_deleting_a_job_deletes_its_transcript(db: sqlite3.Connection) -> None:
    create_job(
        db,
        job_id="i" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    save_transcript(db, job_id="i" * 32, transcript=_transcript("one"))

    db.execute("DELETE FROM jobs WHERE id = ?", ("i" * 32,))

    assert get_transcript(db, "i" * 32) is None


def test_the_transcript_survives_a_restart(settings) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="j" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        save_transcript(conn, job_id="j" * 32, transcript=_transcript("persisted"))

    # A brand new connection, as after the application is restarted.
    with session(settings.resolved_database_path) as conn:
        stored = get_transcript(conn, "j" * 32)
        assert stored is not None
        assert stored.segment_count == 1
        segments = list_transcript_segments(conn, stored.id)
        assert segments[0].text == "persisted"


def test_two_connections_can_write_transcripts_at_the_same_time(
    settings,
) -> None:
    with session(settings.resolved_database_path) as conn:
        for index in range(4):
            create_job(
                conn,
                job_id=f"{index:032d}",
                source_type=SourceType.UPLOAD,
                source_reference="clip.mp4",
            )

    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def writer(index: int) -> None:
        try:
            barrier.wait()
            with session(settings.resolved_database_path) as conn:
                save_transcript(
                    conn,
                    job_id=f"{index:032d}",
                    transcript=_transcript(f"job {index}"),
                )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with session(settings.resolved_database_path) as conn:
        for index in range(4):
            stored = get_transcript(conn, f"{index:032d}")
            assert stored is not None
            assert stored.segment_count == 1


def test_the_migration_does_not_destroy_existing_sources(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "clipforge.db"
    _phase2_database(db_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "clip.mp4").write_bytes(b"the source video")

    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()

    # The database still points at the file, and the file is untouched.
    conn = connect(db_path)
    row = conn.execute("SELECT source_path FROM jobs").fetchone()
    assert row["source_path"] == "sources/clip.mp4"
    conn.close()
    assert (sources / "clip.mp4").read_bytes() == b"the source video"
