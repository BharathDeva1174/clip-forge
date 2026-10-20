"""Tests for the SQLite layer: schema versioning and atomic claiming."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import (
    MIGRATIONS,
    SCHEMA_VERSION,
    connect,
    create_job,
    ensure_schema,
    get_job,
    list_jobs,
    migrate,
    session,
)
from app.jobs import SourceType


def _create_at_version(db_path: Path, version: int) -> sqlite3.Connection:
    """Build a database that only has the migrations up to ``version``."""
    conn = connect(db_path, wal_mode=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    for number, statements in MIGRATIONS:
        if number > version:
            break
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (number, "2026-01-01T00:00:00+00:00"),
        )
    return conn


def test_a_fresh_database_is_created_at_the_current_version(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"

    with session(db_path) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

    assert version == SCHEMA_VERSION


def test_an_older_database_is_upgraded_and_its_data_survives(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"

    conn = _create_at_version(db_path, 1)
    create_job(
        conn,
        job_id="a" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    conn.close()

    with session(db_path) as upgraded:
        version = upgraded.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        job = get_job(upgraded, "a" * 32)

    assert version == SCHEMA_VERSION
    assert job is not None
    assert job.source_reference == "clip.mp4"


def test_migrating_twice_is_harmless(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"

    conn = connect(db_path, wal_mode=False)
    migrate(conn)
    migrate(conn)

    applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    conn.close()

    assert applied == len(MIGRATIONS)


def test_the_schema_rejects_an_impossible_status(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"

    with session(db_path) as conn:
        create_job(
            conn,
            job_id="b" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE jobs SET status = 'nonsense' WHERE id = ?", ("b" * 32,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE jobs SET source_type = 'ftp' WHERE id = ?", ("b" * 32,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE jobs SET stage = 'nonsense' WHERE id = ?", ("b" * 32,))


def test_jobs_are_listed_newest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "clipforge.db"

    with session(db_path) as conn:
        for index in range(5):
            create_job(
                conn,
                job_id=f"{index:032x}",
                source_type=SourceType.UPLOAD,
                source_reference=f"clip{index}.mp4",
            )
            conn.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (f"2026-01-0{index + 1}T00:00:00+00:00", f"{index:032x}"),
            )

        items = list_jobs(conn, limit=3)

    assert [item.id for item in items] == [f"{4:032x}", f"{3:032x}", f"{2:032x}"]


def test_the_database_file_is_created_inside_the_storage_root(tmp_path: Path) -> None:
    db_path = tmp_path / "storage" / "database" / "clipforge.db"

    with session(db_path) as conn:
        conn.execute("SELECT 1")

    assert db_path.exists()
