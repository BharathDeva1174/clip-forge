"""The formatting settings, the caption timeline and the subtitle file.

These are the rules, tested on their own: no FFmpeg, no database, no
network.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.captions import (
    MAX_CAPTION_EVENTS,
    TranscriptSegment,
    build_caption_events,
    clean_caption_text,
    colour_to_ffmpeg,
    colour_to_libass,
    escape_filter_path,
    format_srt_timestamp,
    render_srt,
    wrap_caption,
    write_srt,
)
from app.formatting import (
    CaptionSettings,
    FormatSettingError,
    FormatSettings,
    LayoutMode,
    settings_from_dict,
)


# --- the defaults -----------------------------------------------------------


def test_the_default_settings_are_a_portrait_canvas_with_captions() -> None:
    settings = FormatSettings()

    assert settings.width == 720
    assert settings.height == 1280
    assert settings.layout is LayoutMode.CENTER_CROP
    assert settings.captions.enabled is True
    assert settings.captions.font_family == "Arial"


def test_the_default_settings_validate() -> None:
    assert FormatSettings().validate().width == 720


# --- the dimensions ---------------------------------------------------------


@pytest.mark.parametrize("width,height", [(720, 1280), (1080, 1920), (360, 640)])
def test_the_supported_portrait_resolutions_are_accepted(
    width: int, height: int
) -> None:
    settings = FormatSettings(width=width, height=height).validate()

    assert (settings.width, settings.height) == (width, height)


@pytest.mark.parametrize(
    "width,height",
    [
        (0, 1280),      # zero
        (-720, 1280),   # negative
        (721, 1280),    # odd
        (720, 1279),    # odd
        (1920, 1080),   # landscape
        (720, 720),     # square
        (4000, 8000),   # too large
        (32, 64),       # too small
    ],
)
def test_an_unusable_resolution_is_refused(width: int, height: int) -> None:
    with pytest.raises(FormatSettingError) as failure:
        FormatSettings(width=width, height=height).validate()

    assert failure.value.code == "format_invalid_settings"


def test_a_dimension_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(FormatSettingError):
        FormatSettings(width="720", height=1280).validate()  # type: ignore[arg-type]


def test_a_boolean_is_not_a_dimension() -> None:
    with pytest.raises(FormatSettingError):
        FormatSettings(width=True, height=1280).validate()  # type: ignore[arg-type]


# --- the layout -------------------------------------------------------------


def test_the_layout_mode_must_be_one_clipforge_knows() -> None:
    with pytest.raises(FormatSettingError):
        FormatSettings(layout="sideways").validate()  # type: ignore[arg-type]


def test_both_layout_modes_are_accepted() -> None:
    for mode in (LayoutMode.CENTER_CROP, LayoutMode.FIT_PAD):
        assert FormatSettings(layout=mode).validate().layout is mode


# --- the colours ------------------------------------------------------------


@pytest.mark.parametrize("colour", ["#FFFFFF", "#000000", "#1a2B3c"])
def test_a_colour_is_accepted(colour: str) -> None:
    settings = FormatSettings(
        padding_colour=colour,
        captions=CaptionSettings(text_colour=colour),
    ).validate()

    assert settings.padding_colour == colour.upper()


@pytest.mark.parametrize(
    "colour", ["FFFFFF", "#FFF", "#GGGGGG", "red", "", "#1234567", "0xFFFFFF"]
)
def test_an_invalid_colour_is_refused(colour: str) -> None:
    with pytest.raises(FormatSettingError):
        FormatSettings(padding_colour=colour).validate()


def test_the_colour_conversions_are_correct() -> None:
    # FFmpeg's pad filter wants 0xRRGGBB.
    assert colour_to_ffmpeg("#1A2B3C") == "0x1A2B3C"
    # libass stores the colours the other way round.
    assert colour_to_libass("#1A2B3C") == "&H003C2B1A"


# --- the caption settings ----------------------------------------------------


@pytest.mark.parametrize("size", [-1, 0, 500, 10.5, "42", True])
def test_an_unusable_font_size_is_refused(size: object) -> None:
    with pytest.raises(FormatSettingError):
        CaptionSettings(font_size=size).validate()  # type: ignore[arg-type]


def test_a_font_size_in_range_is_accepted() -> None:
    assert CaptionSettings(font_size=48).validate().font_size == 48


@pytest.mark.parametrize(
    "name", ["Arial", "Segoe UI", "Times New Roman", "Noto-Sans_2", "Arial.Bold"]
)
def test_a_normal_font_name_is_accepted(name: str) -> None:
    assert CaptionSettings(font_family=name).validate().font_family == name


def test_a_font_name_in_another_script_is_accepted() -> None:
    """A user whose fonts are named in their own language must be able to
    pick one. The only characters that cannot be in a font name are the ones
    that would break the caption style."""
    name = "微軟正黑體"

    assert CaptionSettings(font_family=name).validate().font_family == name


@pytest.mark.parametrize(
    "name", ["Arial; rm -rf /", "Arial,Extra", "Arial'B", "Arial}", "Arial[", ""]
)
def test_a_font_name_that_could_break_a_filter_is_refused(name: str) -> None:
    with pytest.raises(FormatSettingError):
        CaptionSettings(font_family=name).validate()


def test_the_outline_thickness_is_bounded() -> None:
    with pytest.raises(FormatSettingError):
        CaptionSettings(outline_thickness=99).validate()


def test_the_alignment_is_bounded() -> None:
    with pytest.raises(FormatSettingError):
        CaptionSettings(alignment=9).validate()


def test_the_line_settings_are_bounded() -> None:
    with pytest.raises(FormatSettingError):
        CaptionSettings(max_characters_per_line=0).validate()
    with pytest.raises(FormatSettingError):
        CaptionSettings(max_lines=99).validate()


# --- the fingerprint --------------------------------------------------------


def test_the_same_settings_have_the_same_fingerprint() -> None:
    first = FormatSettings(width=720, height=1280)
    second = FormatSettings(width=720, height=1280)

    assert first.fingerprint() == second.fingerprint()


def test_a_different_setting_has_a_different_fingerprint() -> None:
    first = FormatSettings(width=720, height=1280)
    second = FormatSettings(width=1080, height=1920)

    assert first.fingerprint() != second.fingerprint()


def test_the_fingerprint_ignores_the_order_of_the_settings() -> None:
    first = FormatSettings(captions=CaptionSettings(font_size=40, font_family="Arial"))
    second = FormatSettings(captions=CaptionSettings(font_family="Arial", font_size=40))

    assert first.fingerprint() == second.fingerprint()


def test_the_fingerprint_changes_with_the_caption_settings() -> None:
    first = FormatSettings(captions=CaptionSettings(font_size=40))
    second = FormatSettings(captions=CaptionSettings(font_size=48))

    assert first.fingerprint() != second.fingerprint()


def test_the_settings_survive_a_round_trip() -> None:
    original = FormatSettings(
        width=1080,
        height=1920,
        layout=LayoutMode.FIT_PAD,
        padding_colour="#101010",
        captions=CaptionSettings(font_family="Verdana", font_size=36),
    ).validate()

    restored = settings_from_dict(original.to_dict()).validate()

    assert restored.to_dict() == original.to_dict()
    assert restored.fingerprint() == original.fingerprint()


# --- the caption timeline ----------------------------------------------------


def _segment(start: float, end: float, text: str = "hello") -> TranscriptSegment:
    return TranscriptSegment(index=0, start_seconds=start, end_seconds=end, text=text)


def _events(segments, *, clip_start=10.0, clip_end=20.0, duration=10.0, **kwargs):
    settings = CaptionSettings(**kwargs)
    return build_caption_events(
        segments,
        clip_start=clip_start,
        clip_end=clip_end,
        playable_duration_seconds=duration,
        settings=settings,
    )


def test_a_segment_inside_the_clip_keeps_its_relative_time() -> None:
    events = _events([_segment(12.0, 15.0)])

    assert len(events) == 1
    assert events[0].start_ms == 2000
    assert events[0].end_ms == 5000


def test_a_segment_that_starts_before_the_clip_is_clipped() -> None:
    events = _events([_segment(5.0, 15.0)])

    assert len(events) == 1
    # The part before the clip is dropped: the caption starts at zero.
    assert events[0].start_ms == 0
    assert events[0].end_ms == 5000


def test_a_segment_that_ends_after_the_clip_is_clipped() -> None:
    events = _events([_segment(15.0, 30.0)])

    assert len(events) == 1
    assert events[0].start_ms == 5000
    # Never beyond the end of the clip.
    assert events[0].end_ms == 10000


def test_a_segment_that_crosses_both_boundaries_is_clipped() -> None:
    events = _events([_segment(0.0, 60.0)])

    assert len(events) == 1
    assert events[0].start_ms == 0
    assert events[0].end_ms == 10000


def test_a_segment_entirely_before_the_clip_contributes_nothing() -> None:
    assert _events([_segment(1.0, 5.0)]) == []


def test_a_segment_entirely_after_the_clip_contributes_nothing() -> None:
    assert _events([_segment(30.0, 40.0)]) == []


def test_a_segment_with_no_duration_contributes_nothing() -> None:
    assert _events([_segment(12.0, 12.0)]) == []


def test_a_segment_with_reversed_timestamps_contributes_nothing() -> None:
    assert _events([_segment(15.0, 12.0)]) == []


def test_a_segment_with_a_nan_timestamp_contributes_nothing() -> None:
    assert _events([_segment(float("nan"), 15.0)]) == []


def test_a_segment_with_an_infinite_timestamp_contributes_nothing() -> None:
    assert _events([_segment(12.0, float("inf"))]) == []


def test_empty_text_contributes_nothing() -> None:
    assert _events([_segment(12.0, 15.0, "")]) == []
    assert _events([_segment(12.0, 15.0, "   \n\t ")]) == []


def test_the_captions_never_run_past_the_playable_duration() -> None:
    # The clip is measured at 8 seconds, even though the candidate asked
    # for ten.
    events = _events([_segment(10.0, 20.0)], duration=8.0)

    assert len(events) == 1
    assert events[0].end_ms == 8000


def test_the_captions_never_start_before_zero() -> None:
    events = _events([_segment(0.0, 12.0)], clip_start=10.0, clip_end=20.0)

    assert events[0].start_ms == 0


def test_the_captions_are_in_chronological_order() -> None:
    events = _events(
        [
            _segment(18.0, 19.0, "third"),
            _segment(11.0, 12.0, "first"),
            _segment(14.0, 15.0, "second"),
        ]
    )

    assert [event.text for event in events] == ["first", "second", "third"]
    assert [event.start_ms for event in events] == sorted(
        event.start_ms for event in events
    )


def test_a_clip_with_no_transcript_has_no_captions() -> None:
    assert _events([]) == []


def test_captions_that_are_disabled_produce_nothing() -> None:
    assert _events([_segment(12.0, 15.0)], enabled=False) == []


def test_unicode_text_is_preserved() -> None:
    events = _events([_segment(12.0, 15.0, "Ça va — 你好! ¿Sí?")])

    assert events[0].text == "Ça va — 你好! ¿Sí?"


def test_a_very_long_caption_is_capped() -> None:
    events = _events([_segment(12.0, 15.0, "word " * 2000)])

    assert len(events) == 1
    assert len(events[0].text) <= 2100


def test_the_number_of_captions_is_bounded() -> None:
    segments = [
        TranscriptSegment(index=index, start_seconds=10.0 + index * 0.01, end_seconds=10.005 + index * 0.01, text="x")
        for index in range(MAX_CAPTION_EVENTS + 500)
    ]
    events = _events(segments, clip_start=10.0, clip_end=20.0, duration=10.0)

    assert len(events) <= MAX_CAPTION_EVENTS


# --- the text ---------------------------------------------------------------


def test_control_characters_are_removed() -> None:
    assert clean_caption_text("a\x00b\x07c") == "a b c"


def test_whitespace_is_collapsed() -> None:
    assert clean_caption_text("  a \n\n b  ") == "a b"


def test_a_caption_is_wrapped_to_the_line_width() -> None:
    wrapped = wrap_caption(
        "one two three four five six seven eight", CaptionSettings(max_characters_per_line=10)
    )

    assert all(len(line) <= 10 for line in wrapped.split("\n"))


def test_a_caption_is_capped_at_the_maximum_number_of_lines() -> None:
    wrapped = wrap_caption(
        " ".join(f"word{index}" for index in range(40)),
        CaptionSettings(max_characters_per_line=10, max_lines=2),
    )

    lines = wrapped.split("\n")
    assert len(lines) == 2
    # The cut is marked, so it is not mistaken for the whole sentence.
    assert lines[-1].endswith("…")


# --- the subtitle file ------------------------------------------------------


def test_the_srt_timestamp_is_formatted_in_milliseconds() -> None:
    assert format_srt_timestamp(0) == "00:00:00,000"
    assert format_srt_timestamp(1500) == "00:00:01,500"
    assert format_srt_timestamp(3_661_500) == "01:01:01,500"


def test_a_negative_timestamp_is_clamped_to_zero() -> None:
    assert format_srt_timestamp(-5000) == "00:00:00,000"


def test_the_srt_document_has_a_number_a_timing_and_the_text() -> None:
    document = render_srt(
        [
            type("E", (), {"start_ms": 0, "end_ms": 1500, "text": "Hello"})(),
            type("E", (), {"start_ms": 1500, "end_ms": 3000, "text": "World"})(),
        ]
    )

    assert "1\n00:00:00,000 --> 00:00:01,500\nHello" in document
    assert "2\n00:00:01,500 --> 00:00:03,000\nWorld" in document


def test_the_subtitle_text_cannot_inject_a_field_or_a_tag() -> None:
    """SRT has no markup, so braces and commas are just text."""
    hostile = "{\\an8}Title, with a comma\\Nand a newline"
    document = render_srt(
        [type("E", (), {"start_ms": 0, "end_ms": 1000, "text": hostile})()]
    )

    # The text is there, unchanged: it is not markup, so it cannot be a tag.
    assert hostile in document
    # And it did not split the event into two.
    assert document.count("-->") == 1


def test_the_subtitle_file_is_written_as_utf8(tmp_path: Path) -> None:
    target = tmp_path / "captions.srt"
    events = [
        type("E", (), {"start_ms": 0, "end_ms": 1000, "text": "你好 — ok"})()
    ]

    size = write_srt(target, events)

    assert size == target.stat().st_size
    assert "你好" in target.read_text(encoding="utf-8")


def test_a_subtitle_file_that_would_be_too_large_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.captions

    monkeypatch.setattr(app.captions, "MAX_SUBTITLE_BYTES", 10)
    target = tmp_path / "captions.srt"
    events = [
        type("E", (), {"start_ms": 0, "end_ms": 1000, "text": "Hello"})()
    ]

    with pytest.raises(ValueError):
        write_srt(target, events)
    assert not target.exists()


# --- the filter path --------------------------------------------------------


def test_a_windows_path_is_escaped_for_the_filter() -> None:
    escaped = escape_filter_path(Path("C:/Users/someone/captions.srt"))

    # The drive letter's colon is escaped.
    assert escaped == "C\\:/Users/someone/captions.srt"


def test_a_path_with_a_comma_or_a_bracket_is_escaped() -> None:
    escaped = escape_filter_path(Path("C:/a,b[c]/captions.srt"))

    assert "\\," in escaped
    assert "\\[" in escaped
    assert "\\]" in escaped


def test_a_backslash_becomes_a_forward_slash() -> None:
    assert escape_filter_path(Path("C:\\Users\\x\\captions.srt")) == (
        "C\\:/Users/x/captions.srt"
    )


def test_a_path_with_a_single_quote_is_refused() -> None:
    """FFmpeg's subtitle filter cannot open such a path at all."""
    with pytest.raises(ValueError):
        escape_filter_path(Path("C:/Users/O'Brien/captions.srt"))
