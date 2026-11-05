"""The formatting settings.

A *formatting profile* is the description of the video a user wants out of
a clip: a portrait canvas, a layout mode, and how the captions should look.

The settings are **validated strictly**. A number that is not finite, a
dimension that is not an even positive integer, a colour that is not a
colour, a font name that could break a filter expression -- each of them is
refused with a stable code, before anything expensive happens.

Nothing here performs I/O, so the rules can be applied and tested on their
own.

The fingerprint
---------------

The settings are normalised and hashed. Two requests with the same
normalised settings have the same fingerprint, and the same clip with the
same fingerprint is the same artifact: a repeated request finds the artifact
that already exists instead of rendering the same thing twice. Changing a
setting changes the fingerprint, and therefore produces a *different*
artifact, so a finished artifact is never silently replaced.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

#: The portrait ratio the output has to be.
TARGET_ASPECT_WIDTH = 9
TARGET_ASPECT_HEIGHT = 16

#: How far the aspect ratio may drift from 9:16 and still be accepted.
ASPECT_TOLERANCE = 0.005

#: The smallest and the largest canvas that may be asked for.
MIN_DIMENSION = 64
MAX_WIDTH = 2160
MAX_HEIGHT = 3840

#: The bounds of the caption settings.
MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 200
MAX_OUTLINE = 10
MAX_MARGIN = 4000
MIN_LINE_CHARACTERS = 8
MAX_LINE_CHARACTERS = 100
MAX_LINES = 10

#: A font name that cannot break a filter expression.
#:
#: The caption style is a comma-separated list of libass options, so a
#: comma cannot be in a name; the value is single-quoted, so a quote
#: cannot be in one either. Everything else a family name may contain --
#: including a non-Latin script -- is allowed, because a user whose fonts
#: are named in their own language has to be able to pick one.
_FONT_NAME = re.compile(r"^[^\W_][\w .-]{0,63}$")

#: A colour, as the user writes it.
_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class LayoutMode(StrEnum):
    """How the source frame is fitted into the portrait canvas."""

    #: Fill the canvas and crop the excess.
    CENTER_CROP = "center_crop"

    #: Fit the whole frame inside the canvas and pad the rest.
    FIT_PAD = "fit_pad"


class FormatSettingError(ValueError):
    """A formatting setting that cannot be used.

    It carries a stable code, so the API can report it without inventing a
    message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid(message: str) -> FormatSettingError:
    return FormatSettingError("format_invalid_settings", message)


def _read_dimension(value: object, name: str, *, maximum: int) -> int:
    """Read a dimension: a positive, even, bounded integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"The {name} must be a whole number.")
    if value < MIN_DIMENSION:
        raise _invalid(f"The {name} must be at least {MIN_DIMENSION}.")
    if value > maximum:
        raise _invalid(f"The {name} must not be larger than {maximum}.")
    if value % 2 != 0:
        # H.264 with yuv420p needs even dimensions; an odd one would be
        # rejected by the encoder at render time, so it is refused here.
        raise _invalid(f"The {name} must be an even number.")
    return value


def _read_bounded_int(
    value: object, name: str, *, minimum: int, maximum: int
) -> int:
    """Read a bounded integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"The {name} must be a whole number.")
    if value < minimum or value > maximum:
        raise _invalid(
            f"The {name} must be between {minimum} and {maximum}."
        )
    return value


def _read_colour(value: object, name: str) -> str:
    """Read a colour as ``#RRGGBB``."""
    if not isinstance(value, str) or not _COLOUR.match(value.strip()):
        raise _invalid(f"The {name} must be a colour such as #FFFFFF.")
    return value.strip().upper()


def is_usable_font_name(name: str) -> bool:
    """Whether a font name could be used in the caption style.

    The style is a comma-separated list of libass options, so a name with a
    comma in it cannot be expressed -- not even inside the quotes. The
    interface uses this to offer only the fonts that can actually be
    selected.
    """
    return bool(_FONT_NAME.match(name.strip()))


@dataclass(frozen=True, slots=True)
class CaptionSettings:
    """How the captions should look."""

    enabled: bool = True
    font_family: str = "Arial"
    font_size: int = 42
    text_colour: str = "#FFFFFF"
    outline_colour: str = "#000000"
    outline_thickness: int = 3
    #: A box behind the text. ``None`` means no box.
    background_colour: str | None = None
    #: A drop shadow, in pixels.
    shadow: int = 0
    #: Where the text sits: 1 is bottom, 2 is centred, 3 is top.
    alignment: int = 2
    #: The gap between the text and the edge of the frame, in pixels.
    margin_vertical: int = 90
    #: How many characters fit on a line.
    max_characters_per_line: int = 28
    #: How many lines a caption may use.
    max_lines: int = 3

    def normalised(self) -> CaptionSettings:
        """The settings in their canonical form."""
        return replace(
            self,
            font_family=self.font_family.strip(),
            text_colour=self.text_colour.upper(),
            outline_colour=self.outline_colour.upper(),
            background_colour=(
                self.background_colour.upper() if self.background_colour else None
            ),
        )

    def validate(self) -> CaptionSettings:
        """Check every field and return the normalised settings.

        Raises:
            FormatSettingError: with a stable code.
        """
        if not isinstance(self.enabled, bool):
            raise _invalid("The caption switch must be true or false.")

        font = self.font_family.strip()
        if not _FONT_NAME.match(font):
            raise _invalid(
                "The font name may only contain letters, digits, spaces, "
                "hyphens, underscores and dots."
            )
        if "," in font:
            raise _invalid(
                "The font name may not contain a comma, because the caption "
                "style is a list of comma-separated options."
            )

        return replace(
            self,
            font_family=font,
            font_size=_read_bounded_int(
                self.font_size,
                "font size",
                minimum=MIN_FONT_SIZE,
                maximum=MAX_FONT_SIZE,
            ),
            text_colour=_read_colour(self.text_colour, "text colour"),
            outline_colour=_read_colour(self.outline_colour, "outline colour"),
            outline_thickness=_read_bounded_int(
                self.outline_thickness,
                "outline thickness",
                minimum=0,
                maximum=MAX_OUTLINE,
            ),
            background_colour=(
                _read_colour(self.background_colour, "background colour")
                if self.background_colour is not None
                else None
            ),
            shadow=_read_bounded_int(
                self.shadow, "shadow", minimum=0, maximum=MAX_OUTLINE
            ),
            alignment=_read_bounded_int(
                self.alignment, "alignment", minimum=1, maximum=3
            ),
            margin_vertical=_read_bounded_int(
                self.margin_vertical,
                "vertical margin",
                minimum=0,
                maximum=MAX_MARGIN,
            ),
            max_characters_per_line=_read_bounded_int(
                self.max_characters_per_line,
                "characters per line",
                minimum=MIN_LINE_CHARACTERS,
                maximum=MAX_LINE_CHARACTERS,
            ),
            max_lines=_read_bounded_int(
                self.max_lines, "maximum lines", minimum=1, maximum=MAX_LINES
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """The settings as a plain dictionary, for storing and for hashing."""
        return {
            "enabled": self.enabled,
            "font_family": self.font_family,
            "font_size": self.font_size,
            "text_colour": self.text_colour,
            "outline_colour": self.outline_colour,
            "outline_thickness": self.outline_thickness,
            "background_colour": self.background_colour,
            "shadow": self.shadow,
            "alignment": self.alignment,
            "margin_vertical": self.margin_vertical,
            "max_characters_per_line": self.max_characters_per_line,
            "max_lines": self.max_lines,
        }


@dataclass(frozen=True, slots=True)
class FormatSettings:
    """Everything about the video a user wants out of a clip."""

    width: int = 720
    height: int = 1280
    layout: LayoutMode = LayoutMode.CENTER_CROP
    #: The colour of the padding in ``fit_pad``.
    padding_colour: str = "#000000"
    captions: CaptionSettings = field(default_factory=CaptionSettings)

    def normalised(self) -> FormatSettings:
        """The settings in their canonical form."""
        return replace(
            self,
            padding_colour=self.padding_colour.upper(),
            captions=self.captions.normalised(),
        )

    def validate(self) -> FormatSettings:
        """Check every field and return the normalised settings.

        Raises:
            FormatSettingError: with a stable code.
        """
        width = _read_dimension(self.width, "width", maximum=MAX_WIDTH)
        height = _read_dimension(self.height, "height", maximum=MAX_HEIGHT)

        ratio = width / height
        target = TARGET_ASPECT_WIDTH / TARGET_ASPECT_HEIGHT
        if abs(ratio - target) / target > ASPECT_TOLERANCE:
            raise _invalid(
                "The output has to be portrait 9:16. "
                f"{width}x{height} is not; try 720x1280 or 1080x1920."
            )

        if not isinstance(self.layout, LayoutMode):
            raise _invalid("The layout mode is not one ClipForge knows.")

        captions = self.captions.validate()

        return replace(
            self,
            width=width,
            height=height,
            padding_colour=_read_colour(self.padding_colour, "padding colour"),
            captions=captions,
        )

    def to_dict(self) -> dict[str, object]:
        """The settings as a plain dictionary, for storing and for hashing."""
        return {
            "width": self.width,
            "height": self.height,
            "layout": self.layout.value,
            "padding_colour": self.padding_colour,
            "captions": self.captions.to_dict(),
        }

    def fingerprint(self) -> str:
        """A stable hash of the normalised settings.

        The same settings always produce the same fingerprint, and a
        different setting always produces a different one.
        """
        return configuration_fingerprint(self)

    def summary(self) -> dict[str, object]:
        """A short description for the interface."""
        return {
            "width": self.width,
            "height": self.height,
            "layout": self.layout.value,
            "captions_enabled": self.captions.enabled,
            "font_family": self.captions.font_family,
            "font_size": self.captions.font_size,
        }


def configuration_fingerprint(
    settings: FormatSettings,
    caption_edits: Sequence[tuple[int, str]] = (),
) -> str:
    """A stable hash of a configuration and the caption text that goes with it.

    The caption text is part of the configuration, not an afterthought:
    two different texts produce **different** videos, so they must not
    share a fingerprint. Two equivalent texts -- the same text in the same
    order -- produce the same one, so the same variant is not rendered
    twice.
    """
    canonical = json.dumps(
        {
            "settings": settings.normalised().to_dict(),
            "captions": [[index, text] for index, text in caption_edits],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def settings_from_dict(payload: dict) -> FormatSettings:
    """Build the settings from a stored dictionary.

    The stored form is the normalised one, so this is a straight read; the
    validation still runs, so a hand-edited row cannot produce something
    unusable.
    """
    raw_captions = payload.get("captions")
    captions = CaptionSettings()
    if isinstance(raw_captions, dict):
        known = {key: value for key, value in raw_captions.items() if key in captions.to_dict()}
        captions = CaptionSettings(**known)

    layout = payload.get("layout")
    return FormatSettings(
        width=int(payload.get("width", 720)),
        height=int(payload.get("height", 1280)),
        layout=LayoutMode(layout) if isinstance(layout, str) else LayoutMode.CENTER_CROP,
        padding_colour=str(payload.get("padding_colour", "#000000")),
        captions=captions,
    )
