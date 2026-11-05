"""The normalisation rules, one test per case.

The Phase 3 rules are covered in ``test_transcript_model.py``. This file
walks the whole matrix of things a speech engine can produce, so that every
case has a named, deterministic test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from app.transcripts import (
    MAX_SEGMENT_TEXT_LENGTH,
    MAX_TIMESTAMP_OVERSHOOT_SECONDS,
    TranscriptValidationError,
    validate_segments,
)


@dataclass(frozen=True)
class Segment:
    """A segment as a speech engine would return it."""

    start: float
    end: float
    text: str
    avg_logprob: float | None = -0.25
    no_speech_prob: float | None = 0.02


# --- impossible timestamps --------------------------------------------------


def test_a_negative_start_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=-0.5, end=1.0, text="x")])


def test_a_nan_timestamp_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=0.0, end=float("nan"), text="x")])


def test_an_infinite_timestamp_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=0.0, end=float("inf"), text="x")])


def test_a_segment_that_ends_before_it_starts_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=2.0, end=1.0, text="x")])


def test_a_timestamp_that_is_not_a_number_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start="zero", end=1.0, text="x")])  # type: ignore[arg-type]


def test_a_timestamp_further_than_the_window_past_the_audio_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments(
            [
                Segment(
                    start=0.0,
                    end=6.7 + MAX_TIMESTAMP_OVERSHOOT_SECONDS + 1,
                    text="x",
                )
            ],
            duration_seconds=6.7,
        )


def test_the_whole_transcript_is_rejected_rather_than_stored_with_a_hole() -> None:
    """One impossible timestamp is enough to refuse the transcript."""
    with pytest.raises(TranscriptValidationError):
        validate_segments(
            [
                Segment(start=0.0, end=1.0, text="good speech"),
                Segment(start=-1.0, end=2.0, text="impossible"),
            ],
            duration_seconds=5.0,
        )


# --- timestamps at or past the end of the audio -----------------------------


def test_a_segment_that_starts_exactly_at_the_end_of_the_audio_is_dropped() -> None:
    segments = validate_segments(
        [Segment(start=5.0, end=6.0, text="past the end")], duration_seconds=5.0
    )
    assert segments == []


def test_a_segment_that_starts_beyond_the_end_of_the_audio_is_dropped() -> None:
    segments = validate_segments(
        [Segment(start=7.0, end=10.0, text="hallucinated")], duration_seconds=6.7
    )
    assert segments == []


def test_a_segment_that_ends_past_the_audio_is_pulled_back() -> None:
    segments = validate_segments(
        [Segment(start=3.36, end=10.0, text="real speech")], duration_seconds=6.7
    )
    assert len(segments) == 1
    assert segments[0].start_seconds == 3.36
    assert segments[0].end_seconds == 6.7


def test_a_segment_that_begins_inside_and_ends_past_the_audio_keeps_its_start() -> None:
    """The clamp only moves the end; the start is a real position."""
    segments = validate_segments(
        [Segment(start=1.0, end=35.0, text="x")], duration_seconds=10.0
    )
    assert segments[0].start_seconds == 1.0
    assert segments[0].end_seconds == 10.0


def test_a_segment_inside_the_window_overshoot_is_accepted() -> None:
    segments = validate_segments(
        [Segment(start=0.0, end=6.7 + MAX_TIMESTAMP_OVERSHOOT_SECONDS, text="x")],
        duration_seconds=6.7,
    )
    assert len(segments) == 1
    assert segments[0].end_seconds == 6.7


# --- text -------------------------------------------------------------------


def test_a_segment_with_no_text_is_dropped() -> None:
    segments = validate_segments(
        [
            Segment(start=0.0, end=1.0, text=""),
            Segment(start=1.0, end=2.0, text="   \n\t "),
            Segment(start=2.0, end=3.0, text="real speech"),
        ],
        duration_seconds=3.0,
    )
    assert [segment.text for segment in segments] == ["real speech"]


def test_text_that_is_not_a_string_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=0.0, end=1.0, text=123)])  # type: ignore[arg-type]


def test_a_segment_with_an_absurdly_long_text_is_dropped() -> None:
    """One malformed result cannot use an unbounded amount of memory."""
    segments = validate_segments(
        [Segment(start=0.0, end=1.0, text="x" * (MAX_SEGMENT_TEXT_LENGTH + 1))],
        duration_seconds=1.0,
    )
    assert segments == []


def test_a_segment_with_a_long_but_plausible_text_is_kept() -> None:
    text = "word " * 100
    segments = validate_segments(
        [Segment(start=0.0, end=1.0, text=text)], duration_seconds=1.0
    )
    assert len(segments) == 1


# --- duplicates, overlaps and very short segments ---------------------------


def test_duplicate_segments_are_kept_in_a_stable_order() -> None:
    """A duplicate is not a fabrication, so it is not silently removed."""
    raw = [
        Segment(start=0.0, end=1.0, text="a"),
        Segment(start=0.0, end=1.0, text="a"),
    ]

    first = validate_segments(raw, duration_seconds=1.0)
    second = validate_segments(raw, duration_seconds=1.0)

    assert [segment.text for segment in first] == ["a", "a"]
    assert [segment.index for segment in first] == [0, 1]
    assert [segment.text for segment in second] == ["a", "a"]


def test_overlapping_segments_are_kept_and_ordered() -> None:
    segments = validate_segments(
        [
            Segment(start=1.0, end=3.0, text="second"),
            Segment(start=0.0, end=2.0, text="first"),
        ],
        duration_seconds=3.0,
    )
    assert [segment.text for segment in segments] == ["first", "second"]
    assert segments[0].index == 0
    assert segments[1].index == 1


def test_a_zero_duration_segment_is_dropped() -> None:
    """A segment with no span cannot be placed in the audio."""
    segments = validate_segments(
        [Segment(start=1.0, end=1.0, text="quick")], duration_seconds=3.0
    )
    assert segments == []


def test_a_zero_duration_segment_is_dropped_even_without_a_duration() -> None:
    """The rule does not depend on knowing how long the audio is."""
    segments = validate_segments([Segment(start=1.0, end=1.0, text="quick")])
    assert segments == []


def test_a_very_short_segment_is_kept() -> None:
    segments = validate_segments(
        [Segment(start=1.0, end=1.02, text="hm")], duration_seconds=3.0
    )
    assert len(segments) == 1


# --- the number of segments -------------------------------------------------


def test_the_number_of_segments_is_bounded() -> None:
    raw = [Segment(start=0.0, end=1.0, text=f"segment {index}") for index in range(50)]

    segments = validate_segments(raw, duration_seconds=1.0, max_segments=10)

    assert len(segments) == 10
    assert [segment.index for segment in segments] == list(range(10))


def test_valid_output_is_not_modified() -> None:
    """The rules normalise; they do not rewrite what the engine said."""
    raw = [Segment(start=0.0, end=1.25, text="Hello there.")]

    segments = validate_segments(raw, duration_seconds=10.0)

    assert segments[0].text == "Hello there."
    assert segments[0].start_seconds == 0.0
    assert segments[0].end_seconds == 1.25
    assert segments[0].average_log_probability == -0.25
    assert segments[0].no_speech_probability == 0.02


# --- malformed output -------------------------------------------------------


def test_output_that_is_not_a_list_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments(None)


def test_output_that_cannot_be_iterated_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments(42)


def test_a_segment_without_timestamps_is_rejected() -> None:
    class Bare:
        text = "hello"

    with pytest.raises(TranscriptValidationError):
        validate_segments([Bare()])


def test_a_segment_without_text_is_treated_as_no_speech() -> None:
    class Bare:
        start = 0.0
        end = 1.0

    assert validate_segments([Bare()], duration_seconds=1.0) == []


# --- the timestamps the database will accept --------------------------------


def test_the_stored_timestamps_satisfy_the_database_constraints() -> None:
    """Everything that survives normalisation can be written to SQLite."""
    raw = [
        Segment(start=0.0, end=1.0, text="one"),
        Segment(start=1.0, end=2.0, text="two"),
        Segment(start=5.0, end=9.0, text="past the end"),
        Segment(start=3.0, end=3.0, text="instant"),
    ]

    segments = validate_segments(raw, duration_seconds=5.0)

    for segment in segments:
        assert math.isfinite(segment.start_seconds)
        assert math.isfinite(segment.end_seconds)
        assert segment.start_seconds >= 0
        assert segment.end_seconds >= 0
        assert segment.end_seconds >= segment.start_seconds
        assert segment.text.strip()
