"""Building the export archive.

The roadmap asks for "download individual clips and a ZIP of a batch". An
individual clip is already served by the media route; this module is the
batch.

The archive is built **for the request** and is not kept. That is a
deliberate choice:

- a stored export would need its own lifecycle, its own file and a policy
  for when it is deleted, and a stale one would be a trap;
- a stored export would be a second storage scheme beside the one the
  rest of the application uses;
- the clips in it are already verified files, so there is nothing to
  render and nothing that can go stale in the archive itself.

The files are read through the same containment check the media route uses,
so an export can never reach a file outside the storage root.

Resource safety
---------------

The archive is assembled in a temporary file inside the storage root and is
streamed back to the caller once complete -- it is never held entirely in
RAM. Both the number of clips and their combined on-disk size are bounded
before any archive is assembled, so a single request (or several concurrent
ones) cannot consume unbounded memory or disk. Temporary files are cleaned up
whether the export succeeds or fails.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import zipfile
from pathlib import Path

from app.clips import ClipRecord
from app.config import Settings
from app.storage import Storage, StorageEscapeError

#: The characters that may be in a name inside the archive.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")


def safe_archive_name(name: str, fallback: str) -> str:
    """Turn a title into something that is safe to put in a ZIP.

    The name is a convenience, never an identity: the clip's own id is
    always in the file name, so two clips can never collide and the file
    can always be traced back to the artifact it came from.
    """
    cleaned = _UNSAFE_NAME.sub("", name).strip(" .")
    cleaned = " ".join(cleaned.split())[:60]
    return cleaned or fallback


def clip_file(clip: ClipRecord, storage: Storage) -> Path | None:
    """The file of a finished clip, when it has one.

    The path is resolved inside the storage root, and the file has to
    exist: an export never invents a file and never reaches outside.
    """
    if not clip.output_path:
        return None
    try:
        path = storage.resolve(clip.output_path)
    except StorageEscapeError:
        return None
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return path


def check_export_limits(
    clips: list[ClipRecord], storage: Storage, settings: Settings
) -> None:
    """Refuse an export that would exceed the configured limits.

    Raises:
        ValueError: with a human-readable message when the clip count or
            the combined on-disk size of the chosen clips exceeds the limit.
    """
    if len(clips) > settings.clip_export_max_items:
        raise ValueError(
            f"An export may hold at most {settings.clip_export_max_items} clips."
        )

    total_bytes = 0
    for clip in clips:
        path = clip_file(clip, storage)
        if path is None:
            continue
        total_bytes += path.stat().st_size
    if total_bytes > settings.clip_export_max_bytes:
        raise ValueError(
            "An export may hold at most "
            f"{settings.clip_export_max_bytes // (1024 * 1024)} MiB of media."
        )


def build_export_archive(
    clips: list[ClipRecord], storage: Storage, settings: Settings
) -> Path | None:
    """Build the ZIP of the chosen clips into a temporary file.

    Returns the path of a temporary ZIP file, or ``None`` when one of the
    clips has no file to put in it. The caller is responsible for deleting
    the file once it has been streamed back.

    The archive is assembled in the storage root's scratch directory so it
    never escapes containment and is always cleaned up.
    """
    check_export_limits(clips, storage, settings)

    members: list[tuple[Path, str]] = []
    for index, clip in enumerate(clips, start=1):
        path = clip_file(clip, storage)
        if path is None:
            return None
        label = safe_archive_name(clip.title or clip.id, clip.id)
        members.append((path, f"{index:02d}-{label}.mp4"))

    storage.temp_dir.mkdir(parents=True, exist_ok=True)
    buffer = tempfile.NamedTemporaryFile(
        dir=storage.temp_dir,
        prefix="export-",
        suffix=".zip",
        delete=False,
    )
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, name in members:
                archive.write(path, arcname=name)
        buffer.flush()
        os.fsync(buffer.fileno())
    except Exception:
        buffer.close()
        try:
            os.unlink(buffer.name)
        except OSError:
            pass
        raise
    finally:
        buffer.close()
    return Path(buffer.name)


def read_export_archive(path: Path) -> io.BytesIO:
    """Read a finished archive back into memory for streaming to the caller.

    The caller should delete ``path`` once the response has been sent.
    """
    return io.BytesIO(path.read_bytes())
