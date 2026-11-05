"""Finding the fonts this machine actually has.

A caption that asks for a font nobody has is a caption nobody can read, and
the user has no way to find out which fonts exist. So the fonts are detected:
the TrueType and OpenType files in the system font directories are read, and
the family name is taken out of each file's own ``name`` table.

That is a small amount of binary parsing, and it is worth it: a name taken
from the file's metadata is the name libass will actually resolve.

If the font directories cannot be read -- on a locked-down machine, or on a
platform that does not use them -- the list is simply empty and the
interface falls back to a default. Nothing here is allowed to fail the
application.
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path

logger = logging.getLogger("clipforge.fonts")

#: The directories the fonts are looked for in.


def _home_font_dirs() -> list[Path]:
    """Font directories under the user's home directory.

    ``Path.home()`` raises when HOME/USERPROFILE is not set, so guard it.
    """
    try:
        home = Path.home()
    except RuntimeError:
        return []
    return [home / ".fonts", home / ".local/share/fonts"]


FONT_DIRECTORIES: tuple[Path, ...] = (
    Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    *_home_font_dirs(),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
)

#: The font used when nothing else is known to exist.
DEFAULT_FONT_FAMILY = "Arial"

#: The extensions that hold a font.
_FONT_SUFFIXES = (".ttf", ".otf", ".ttc")

#: The name table ids that hold a family name.
_FAMILY_NAME_IDS = (16, 1)


def font_directories() -> tuple[Path, ...]:
    """The directories that are looked in, and that exist."""
    return tuple(
        directory for directory in FONT_DIRECTORIES if directory.is_dir()
    )


def list_font_families(limit: int = 200) -> list[str]:
    """The family names of the fonts this machine has.

    The list is sorted and de-duplicated. It is a convenience, not a
    contract: a font that is not listed may still work, and a font that is
    listed may still be a poor choice for a phone screen.
    """
    families: set[str] = set()
    for directory in font_directories():
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() not in _FONT_SUFFIXES:
                continue
            name = read_font_family(entry)
            if name:
                families.add(name)
            if len(families) >= limit * 4:
                break
    return sorted(families)[:limit]


def read_font_family(path: Path) -> str | None:
    """The family name inside a font file, when it can be read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 12:
        return None

    try:
        offset = 0
        tag = data[0:4]
        if tag == b"ttcf":
            # A collection: the first font is enough.
            offset = struct.unpack_from(">I", data, 12)[0]
            tag = data[offset : offset + 4]
        if tag not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            return None
        table_count = struct.unpack_from(">H", data, offset + 4)[0]
        tables: dict[bytes, int] = {}
        for index in range(table_count):
            entry = offset + 12 + index * 16
            name = data[entry : entry + 4]
            table_offset = struct.unpack_from(">I", data, entry + 8)[0]
            tables[name] = table_offset
    except (struct.error, IndexError, ValueError):
        return None

    name_offset = tables.get(b"name")
    if name_offset is None:
        return None

    try:
        count, string_offset = struct.unpack_from(">HH", data, name_offset + 2)
        records = []
        for index in range(count):
            entry = name_offset + 6 + index * 12
            platform, encoding, _language, name_id, length, string_at = (
                struct.unpack_from(">HHHHHH", data, entry)
            )
            # The string offset is relative to the start of the name table's
            # string storage, which is itself relative to the table.
            records.append(
                (
                    name_id,
                    platform,
                    encoding,
                    name_offset + string_offset + string_at,
                    length,
                )
            )
    except (struct.error, IndexError, ValueError):
        return None

    best: str | None = None
    for name_id, platform, encoding, start, length in records:
        if name_id not in _FAMILY_NAME_IDS:
            continue
        if start + length > len(data):
            continue
        raw = data[start : start + length]
        try:
            if platform == 3 or (platform == 0 and encoding != 0):
                text = raw.decode("utf-16-be", "ignore")
            else:
                text = raw.decode("latin-1", "ignore")
        except (UnicodeDecodeError, LookupError):
            continue
        text = text.strip().split("\x00")[0].strip()
        if not text:
            continue
        # The typographic family (16) is preferred over the legacy one (1).
        if name_id == 16:
            return text
        if best is None:
            best = text
    return best
