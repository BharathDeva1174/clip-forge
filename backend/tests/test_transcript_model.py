"""Tests for the transcript rules.

These are the checks that decide whether a piece of engine output may become
a stored transcript. They are deliberately strict: a transcript that claims to
be complete has to be complete.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.transcripts import (
    MAX_TIMESTAMP_OVERSHOOT_SECONDS,
    Transcript,
    TranscriptValidationError,
    assert_storable,
    normalise_text,
    validate_segments,
)


@dataclass(frozen=True)
class Segment:
    """A segment as an engine would return it."""

    start: float
    end: float
    text: str
    avg_logprob: float | None = -0.25
    no_speech_prob: float | None = 0.02


# --- conversion ------------------------------------------------------------


def test_engine_output_becomes_the_internal_schema() -> None:
    segments = validate_segments(
        [
            Segment(start=0.0, end=1.5, text="Hello there."),
            Segment(start=1.5, end=3.0, text="General Kenobi."),
        ],
        duration_seconds=3.0,
    )

    assert len(segments) == 2
    assert segments[0].index == 0
    assert segments[1].index == 1
    assert segments[0].start_seconds == 0.0
    assert segments[0].end_seconds == 1.5
    assert segments[0].text == "Hello there."
    assert segments[0].no_speech_probability == 0.02


def test_the_segments_are_ordered_and_numbered_from_zero() -> None:
    segments = validate_segments(
        [
            Segment(start=2.0, end=3.0, text="second"),
            Segment(start=0.0, end=1.0, text="first"),
            Segment(start=1.0, end=2.0, text="middle"),
        ],
        duration_seconds=3.0,
    )

    assert [segment.text for segment in segments] == ["first", "middle", "second"]
    assert [segment.index for segment in segments] == [0, 1, 2]


def test_the_ordering_is_deterministic_for_identical_input() -> None:
    raw = [
        Segment(start=0.0, end=1.0, text="a"),
        Segment(start=0.0, end=1.0, text="b"),
    ]

    first = validate_segments(raw, duration_seconds=1.0)
    second = validate_segments(raw, duration_seconds=1.0)

    assert [s.text for s in first] == [s.text for s in second]


# --- timestamps ------------------------------------------------------------


def test_a_negative_start_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=-0.5, end=1.0, text="x")])


def test_a_segment_that_ends_before_it_starts_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=2.0, end=1.0, text="x")])


def test_a_timestamp_that_is_not_a_number_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start="zero", end=1.0, text="x")])  # type: ignore[arg-type]


def test_a_timestamp_that_is_not_finite_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments(
            [Segment(start=0.0, end=float("inf"), text="x")]
        )


def test_a_segment_past_the_end_of_the_audio_is_pulled_back() -> None:
    """Whisper regularly reports the last segment past the end of the audio.

    The timestamp is an artefact of the model's 30-second window, so it is
    normalised rather than rejected.
    """
    segments = validate_segments(
        [Segment(start=0.0, end=10.0, text="x")], duration_seconds=6.7
    )
    assert len(segments) == 1
    assert segments[0].end_seconds == 6.7


def test_a_segment_within_the_window_overshoot_is_accepted() -> None:
    segments = validate_segments(
        [Segment(start=0.0, end=6.7 + MAX_TIMESTAMP_OVERSHOOT_SECONDS, text="x")],
        duration_seconds=6.7,
    )
    assert len(segments) == 1
    assert segments[0].end_seconds == 6.7


def test_a_segment_far_past_the_end_of_the_audio_is_rejected() -> None:
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


def test_a_segment_that_lies_entirely_past_the_audio_is_dropped() -> None:
    """A hallucination over trailing silence has no honest timestamp."""
    segments = validate_segments(
        [Segment(start=7.0, end=10.0, text="hallucinated")], duration_seconds=6.7
    )
    assert segments == []


def test_a_segment_that_ends_past_the_audio_keeps_its_real_span() -> None:
    segments = validate_segments(
        [Segment(start=3.36, end=10.0, text="real speech")], duration_seconds=6.7
    )
    assert len(segments) == 1
    assert segments[0].start_seconds == 3.36
    assert segments[0].end_seconds == 6.7


def test_a_transcript_without_a_duration_still_validates() -> None:
    segments = validate_segments([Segment(start=0.0, end=99.0, text="x")])
    assert len(segments) == 1


# --- text ------------------------------------------------------------------


def test_unicode_and_punctuation_are_preserved() -> None:
    text = "Bonjour — ça va ? 你好世界! Ω≈ç√"
    segments = validate_segments([Segment(start=0.0, end=1.0, text=text)])
    assert segments[0].text == text


def test_non_english_text_is_not_discarded() -> None:
    segments = validate_segments(
        [Segment(start=0.0, end=1.0, text="मुझे पानी पीना है")]
    )
    assert segments[0].text == "मुझे पानी पीना है"


def test_whitespace_is_normalised() -> None:
    segments = validate_segments(
        [Segment(start=0.0, end=1.0, text="  hello \n\t world  ")]
    )
    assert segments[0].text == "hello world"


def test_normalise_text_keeps_the_meaningful_characters() -> None:
    assert normalise_text("  Ça va,  amigos?  ") == "Ça va, amigos?"


def test_an_empty_result_is_skipped_rather_than_stored() -> None:
    segments = validate_segments(
        [
            Segment(start=0.0, end=1.0, text=""),
            Segment(start=1.0, end=2.0, text="   \n  "),
            Segment(start=2.0, end=3.0, text="real speech"),
        ],
        duration_seconds=3.0,
    )

    assert len(segments) == 1
    assert segments[0].text == "real speech"
    # The remaining segment is renumbered, so the index is contiguous.
    assert segments[0].index == 0


def test_text_that_is_not_a_string_is_rejected() -> None:
    with pytest.raises(TranscriptValidationError):
        validate_segments([Segment(start=0.0, end=1.0, text=123)])  # type: ignore[arg-type]


def test_a_missing_text_is_treated_as_no_speech() -> None:
    class Bare:
        start = 0.0
        end = 1.0

    segments = validate_segments([Bare()])
    assert segments == []


# --- storage guard ---------------------------------------------------------


def _transcript(*segments: Segment) -> Transcript:
    return Transcript(
        language="en",
        model_name="fake",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=10.0,
        segments=tuple(validate_segments(list(segments), duration_seconds=10.0)),
    )


def test_a_good_transcript_can_be_stored() -> None:
    assert_storable(_transcript(Segment(start=0.0, end=1.0, text="hello")))


def test_a_transcript_without_a_sample_rate_cannot_be_stored() -> None:
    transcript = Transcript(
        language="en",
        model_name="fake",
        audio_sample_rate=0,
        audio_channels=1,
        duration_seconds=1.0,
        segments=(),
    )
    with pytest.raises(ValueError):
        assert_storable(transcript)


def test_a_transcript_with_an_empty_segment_cannot_be_stored() -> None:
    transcript = Transcript(
        language="en",
        model_name="fake",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=1.0,
        segments=(
            # Bypasses validate_segments on purpose.
            type(
                "S",
                (),
                {
                    "index": 0,
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "text": "   ",
                    "average_log_probability": None,
                    "no_speech_probability": None,
                },
            )(),
        ),
    )
    with pytest.raises(ValueError):
        assert_storable(transcript)
