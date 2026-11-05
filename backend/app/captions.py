"""The caption timeline, and the subtitle file.

The captions come from the transcript segments that are already stored for
the source job. The transcription model is **not** called again.

The timelines
-------------

Two timelines are involved, and mixing them up is the classic bug here.

The **source timeline** is the one the transcript lives on: a segment's
``start_seconds`` is a position in the source video. The **clip timeline**
starts at zero when the clip begins.

A Phase 5 clip knows where it sits on the source timeline
(``clip.start_seconds`` and ``clip.end_seconds``, which came from the
highlight candidate, which came from the transcript segments). So the
conversion is:

    overlap_start  = max(segment_start, clip_start)
    overlap_end    = min(segment_end,   clip_end)
    relative_start = overlap_start - clip_start
    relative_end   = overlap_end   - clip_start

A segment that lies entirely outside the clip contributes nothing. A segment
that crosses a boundary contributes only the part that is inside.

The result is then constrained to the clip's **verified playable duration**,
which is the length of the rendered file, measured rather than assumed. A
caption is never before zero and never after the end of the video.

The subtitle file
----------------

The captions are written as an **SRT** file. SRT has no markup at all: the
text is text. That matters, because the text is the user's speech, and a
model or a transcript must not be able to inject styling, an extra field or
a command into the file. The *styling* comes from libass's ``force_style``
option, whose value is built from validated settings.

The file is written to a controlled path with a server-generated name, and
the renderer runs FFmpeg with that directory as its working directory, so the
path that ends up in the filter is a plain name the application chose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.formatting import CaptionSettings

#: The most caption events one clip may have.
MAX_CAPTION_EVENTS = 2000

#: The largest subtitle file that may be written, in bytes.
MAX_SUBTITLE_BYTES = 2_000_000

#: The longest single caption, in characters.
MAX_CAPTION_CHARACTERS = 2000

#: Characters that are not allowed in a caption. A control character can
# move the cursor in a terminal or confuse a renderer, and none of them is
# something a person says.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Whitespace runs inside a caption.
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One segment of the persisted transcript."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class CaptionEvent:
    """One caption, on the clip's own timeline."""

    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        """How long the caption is shown."""
        return self.end_ms - self.start_ms


def clean_caption_text(text: str) -> str:
    """Make a piece of transcript safe and presentable as a caption.

    Control characters are removed, whitespace runs are collapsed, and the
    result is trimmed. Nothing else is touched: the words are the user's.
    """
    without_controls = _CONTROL_CHARACTERS.sub(" ", text)
    collapsed = _WHITESPACE_RUN.sub(" ", without_controls)
    return collapsed.strip()


def wrap_caption(text: str, settings: CaptionSettings) -> str:
    """Break a caption into lines and cap how many it may use.

    The wrapping is done here rather than left to the renderer, so that the
    "characters per line" and "maximum lines" settings mean what they say
    and can be tested.
    """
    width = max(1, settings.max_characters_per_line)
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    if len(lines) > settings.max_lines:
        # The caption is cut short rather than allowed to cover the whole
        # screen, and the cut is marked so it is not mistaken for the
        # complete sentence.
        kept = lines[: settings.max_lines]
        kept[-1] = kept[-1].rstrip(".,;:!? ") + "…"
        lines = kept
    return "\n".join(lines)


def build_caption_events(
    segments: list[TranscriptSegment],
    *,
    clip_start: float,
    clip_end: float,
    playable_duration_seconds: float,
    settings: CaptionSettings,
) -> list[CaptionEvent]:
    """Turn the transcript into captions on the clip's own timeline.

    The rules, in the order they are applied:

    1. the overlap of the segment with the clip is computed;
    2. a segment with no overlap contributes nothing;
    3. the overlap is converted to the clip's timeline;
    4. the result is constrained to the clip's **verified playable
       duration**;
    5. the times are rounded to whole milliseconds, and an event that
       rounds to nothing is dropped;
    6. empty text contributes nothing.

    The events come back in chronological order.
    """
    if not settings.enabled:
        return []

    duration_ms = int(max(0.0, playable_duration_seconds) * 1000)
    if duration_ms <= 0:
        return []

    events: list[CaptionEvent] = []

    for segment in segments:
        if len(events) >= MAX_CAPTION_EVENTS:
            break

        start = segment.start_seconds
        end = segment.end_seconds
        if not (start == start and end == end):  # NaN
            continue
        if start in (float("inf"), float("-inf")) or end in (float("inf"), float("-inf")):
            continue

        overlap_start = max(start, clip_start)
        overlap_end = min(end, clip_end)
        if overlap_end <= overlap_start:
            continue

        relative_start = overlap_start - clip_start
        relative_end = overlap_end - clip_start

        start_ms = int(round(relative_start * 1000))
        end_ms = int(round(relative_end * 1000))

        # Never before the beginning, never after the end of the video.
        start_ms = max(0, min(start_ms, duration_ms))
        end_ms = max(0, min(end_ms, duration_ms))
        if end_ms <= start_ms:
            continue

        text = clean_caption_text(segment.text)
        if not text:
            continue
        if len(text) > MAX_CAPTION_CHARACTERS:
            text = text[: MAX_CAPTION_CHARACTERS - 1].rstrip() + "…"

        wrapped = wrap_caption(text, settings)
        events.append(CaptionEvent(start_ms=start_ms, end_ms=end_ms, text=wrapped))

    events.sort(key=lambda event: (event.start_ms, event.end_ms))
    return events


def format_srt_timestamp(milliseconds: int) -> str:
    """Format a millisecond offset as an SRT timestamp.

    The rounding is to whole milliseconds, which is the resolution SRT has.
    The hours are not wrapped at 24, so a caption can never appear to be
    before the start of the file.
    """
    total = max(0, int(milliseconds))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt(events: list[CaptionEvent]) -> str:
    """Render the caption events as an SRT document.

    SRT has no markup, so the text needs no escaping: a brace, a comma or a
    backslash in the text is just text. A blank line separates the events,
    and the text of an event cannot contain one, because the whitespace was
    collapsed when the caption was built.
    """
    blocks: list[str] = []
    for number, event in enumerate(events, start=1):
        blocks.append(
            f"{number}\n"
            f"{format_srt_timestamp(event.start_ms)} --> "
            f"{format_srt_timestamp(event.end_ms)}\n"
            f"{event.text}\n"
        )
    return "\n".join(blocks)


def write_srt(path: Path, events: list[CaptionEvent]) -> int:
    """Write the subtitle file and return how many bytes it is.

    The file is written as UTF-8, which is what libass expects.

    Raises:
        ValueError: when the document would be too large.
    """
    document = render_srt(events)
    encoded = document.encode("utf-8")
    if len(encoded) > MAX_SUBTITLE_BYTES:
        raise ValueError("the subtitle document is too large")
    path.write_bytes(encoded)
    return len(encoded)


def escape_filter_path(path: Path | str) -> str:
    """Escape a path so it can be used as an FFmpeg filter value.

    FFmpeg's filter syntax has its own escaping rules, and they are not the
    shell's. Inside a filtergraph a colon separates an option from its
    value, a comma separates filters, brackets group them, and a quote opens
    and closes a value. A Windows path has a colon in its drive letter and
    backslashes everywhere, so it has to be escaped before it can be used.

    The result is meant to be wrapped in single quotes by the caller.

    A path that contains a single quote **cannot** be escaped: FFmpeg's
    subtitle filter opens the file with the name it parsed, and a quote in
    the name simply is not there any more. Such a path is refused, and the
    renderer avoids the problem by giving FFmpeg a working directory that
    holds the file and referring to it by a plain name.
    """
    text = str(path)
    if "'" in text:
        raise ValueError(
            "this path cannot be used in an ffmpeg filter because it "
            "contains a single quote"
        )
    text = text.replace("\\", "/")
    text = text.replace(":", "\\:")
    text = text.replace(",", "\\,")
    text = text.replace("[", "\\[").replace("]", "\\]")
    return text


def colour_to_ffmpeg(colour: str) -> str:
    """Convert ``#RRGGBB`` into the form FFmpeg's pad filter wants."""
    value = colour.lstrip("#")
    return f"0x{value}"


def colour_to_libass(colour: str) -> str:
    """Convert ``#RRGGBB`` into libass's ``&HAABBGGRR`` form.

    libass stores the colours the other way round: blue first, then green,
    then red, and the alpha byte in front.
    """
    value = colour.lstrip("#")
    red = value[0:2]
    green = value[2:4]
    blue = value[4:6]
    return f"&H00{blue}{green}{red}".upper()
