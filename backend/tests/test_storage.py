"""Tests for the storage layer and path containment."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import Storage, StorageEscapeError, sanitise_filename


def test_the_layout_is_created(storage: Storage) -> None:
    storage.ensure_layout()
    for directory in (
        storage.root,
        storage.uploads_dir,
        storage.sources_dir,
        storage.temp_dir,
        storage.database_dir,
    ):
        assert directory.is_dir()


def test_a_relative_path_resolves_inside_the_root(storage: Storage) -> None:
    resolved = storage.resolve("sources/clip.mp4")
    assert resolved == storage.sources_dir / "clip.mp4"
    assert storage.contains(resolved)


@pytest.mark.parametrize(
    "candidate",
    [
        "../outside.mp4",
        "../../etc/passwd",
        "sources/../../outside.mp4",
        "sources/../../../outside.mp4",
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        "",
    ],
)
def test_paths_that_escape_the_root_are_refused(storage: Storage, candidate: str) -> None:
    with pytest.raises(StorageEscapeError):
        storage.resolve(candidate)


def test_a_symlink_out_of_the_root_is_refused(storage: Storage, tmp_path: Path) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"secret")
    link = storage.sources_dir / "link.mp4"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this filesystem")

    with pytest.raises(StorageEscapeError):
        storage.resolve("sources/link.mp4")


def test_relative_to_root_returns_a_posix_path(storage: Storage) -> None:
    path = storage.sources_dir / "clip.mp4"
    assert storage.relative_to_root(path) == "sources/clip.mp4"


def test_relative_to_root_refuses_paths_outside_the_root(
    storage: Storage, tmp_path: Path
) -> None:
    with pytest.raises(StorageEscapeError):
        storage.relative_to_root(tmp_path / "elsewhere.mp4")


def test_generated_source_names_are_unpredictable(storage: Storage) -> None:
    names = {storage.new_source_name(".mp4") for _ in range(50)}
    assert len(names) == 50
    assert all(name.endswith(".mp4") for name in names)
    # The name is server generated; the client's filename never appears in it.
    assert "client" not in "".join(names)


def test_generated_source_names_keep_the_extension(storage: Storage) -> None:
    assert storage.new_source_name("MP4").endswith(".mp4")
    assert storage.new_source_name(".mov").endswith(".mov")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("clip.mp4", "clip.mp4"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\evil.mp4", "evil.mp4"),
        ("/absolute/path/video.mp4", "video.mp4"),
        ("with\x00null.mp4", "with_null.mp4"),
        ("", "upload"),
        (None, "upload"),
    ],
)
def test_filenames_are_reduced_to_a_safe_display_form(raw, expected: str) -> None:
    assert sanitise_filename(raw) == expected
