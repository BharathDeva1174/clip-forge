"""Application-owned filesystem layout.

Everything the application writes lives under a single storage root:

    <storage_root>/
        uploads/    in-flight uploads (partial files)
        sources/    accepted source videos
        temp/       scratch space used by the worker
        database/   the SQLite database

The database stores paths *relative* to that root. Every path is resolved and
checked for containment before it is opened, deleted or probed, so a stored or
supplied path can never escape the storage root.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path, PurePosixPath

#: Characters that are stripped from any filename that is displayed or stored.
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]+")

#: A Windows drive specification such as ``C:``.
_DRIVE_PART = re.compile(r"^[A-Za-z]:$")


class StorageEscapeError(ValueError):
    """Raised when a path would resolve outside the storage root."""


def sanitise_filename(name: str | None, *, max_length: int = 200) -> str:
    """Reduce a client-supplied filename to a safe, display-only form.

    The result is never used as a path. It is stored so the user can recognise
    their own file.
    """
    if not name:
        return "upload"
    # Keep only the last path component, then strip anything unsafe.
    base = os.path.basename(name.replace("\\", "/"))
    base = _UNSAFE_NAME_CHARS.sub("_", base).strip(" .")
    if not base:
        return "upload"
    return base[:max_length]


class Storage:
    """Resolves and creates paths inside the application storage root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.uploads_dir = self.root / "uploads"
        self.sources_dir = self.root / "sources"
        self.temp_dir = self.root / "temp"
        self.database_dir = self.root / "database"

    def ensure_layout(self) -> None:
        """Create the storage directories if they do not exist yet."""
        for directory in (
            self.root,
            self.uploads_dir,
            self.sources_dir,
            self.temp_dir,
            self.database_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        """Resolve a stored relative path, refusing anything outside the root.

        Raises:
            StorageEscapeError: if the path is absolute, contains ``..``, or
                resolves outside the storage root (including via a symlink).
        """
        if not relative_path:
            raise StorageEscapeError("empty path")
        candidate = PurePosixPath(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise StorageEscapeError("path is not relative to the storage root")
        if any(_DRIVE_PART.match(part) for part in candidate.parts):
            raise StorageEscapeError("path contains a drive specification")
        target = (self.root / Path(*candidate.parts)).resolve()
        if not self.contains(target):
            raise StorageEscapeError("path resolves outside the storage root")
        return target

    def contains(self, target: Path) -> bool:
        """Return whether ``target`` is inside the storage root."""
        root = os.path.normcase(str(self.root))
        resolved = os.path.normcase(str(Path(target).resolve()))
        return resolved == root or resolved.startswith(root + os.sep)

    def relative_to_root(self, path: Path) -> str:
        """Return the POSIX-style path of ``path`` relative to the storage root."""
        resolved = Path(path).resolve()
        if not self.contains(resolved):
            raise StorageEscapeError("path is outside the storage root")
        return resolved.relative_to(self.root).as_posix()

    def new_source_name(self, extension: str) -> str:
        """Generate an unpredictable, server-owned filename for a source file.

        The extension is taken from the allowlist, never from the client.
        """
        ext = extension.lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        return f"{uuid.uuid4().hex}{ext}"

    def new_temp_path(self, *, suffix: str = "") -> Path:
        """Return a fresh path inside the scratch directory."""
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        return self.temp_dir / f"{uuid.uuid4().hex}{suffix}"
