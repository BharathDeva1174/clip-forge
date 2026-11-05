"""The transcript domain model.

A transcript is the durable output of Phase 3: the speech of an accepted source,
split into timestamped segments.

This module owns the *rules* about what a valid segment is. It performs no I/O
and knows nothing about SQLite or about the speech-to-text engine, so the same
validation can be applied to the output of any engine.

Validation is deliberately strict. A transcript that claims to be complete must
be complete: if a segment carries a timestamp that cannot be true, the whole
transcript is rejected rather than quietly stored with a hole in it. The one
tolerance that is allowed is documented below.

The normalisation rules
-----------------------

``validate_segments`` applies exactly these rules, in this order:

1. **The text is normalised.** Runs of whitespace become a single space and
   the result is trimmed. Case, punctuation and every other Unicode character
   are preserved.
2. **A segment with no text is dropped.** Whitespace-only text carries no
   speech, so it is skipped rather than stored.
3. **An impossible timestamp rejects the whole transcript.** A start before
   the beginning of the audio, an end before its start, a timestamp that is
   not a finite number, or a timestamp further than one 30-second window past
   the end of the audio cannot be true. The transcript is rejected instead of
   being stored with a hole in it.
4. **A timestamp inside the window allowance is pulled back.** Whisper
   routinely reports the end of the last segment past the end of the audio,
   by up to a whole 30-second window. That is an artefact of the model, so
   the timestamp is pulled back to the end of the audio.
5. **A segment that lies entirely outside the audio is dropped.** There is no
   honest timestamp for it, and inventing one would be fabricating speech.
6. **A segment with no span is dropped.** A segment whose end is the same as
   its start cannot be placed in the audio. A segment that is merely *very*
   short is kept: it still carries speech.
7. **Duplicate and overlapping segments are kept.** The order is
   deterministic: by start time, then by end time, then by the original
   position.
8. **The number of segments is bounded.** A malformed engine output cannot
   make the worker collect an unbounded number of segments.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

logger = logging.getLogger("clipforge.transcripts")

#: How far past the end of the audio a timestamp may reach before the
#: transcript is rejected outright.
#:
#: Whisper's timestamps are quantised to 20 ms and are produced from a
#: 30-second sliding window, so the engine regularly reports the end of the
#: last segment *past* the end of the audio -- by up to a whole window. That
#: is a known artefact of the model rather than a broken timestamp, so it is
#: accepted and normalised back into the audio instead of being rejected.
#:
#: A timestamp further out than this is treated as genuinely wrong.
MAX_TIMESTAMP_OVERSHOOT_SECONDS = 30.0

#: The longest text a single segment may carry.
#:
#: A speech segment is a sentence or two. Anything longer is a malformed
#: result, and accepting it would let one segment use an unbounded amount of
#: memory.
MAX_SEGMENT_TEXT_LENGTH = 2000

#: Runs of whitespace inside a segment are collapsed into a single space.
_WHITESPACE_RUN = re.compile(r"\s+")


class TranscriptValidationError(ValueError):
    """A transcript did not satisfy the rules in this module."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One validated piece of speech with its position in the audio."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str
    average_log_probability: float | None = None
    no_speech_probability: float | None = None


@dataclass(frozen=True, slots=True)
class Transcript:
    """A whole validated transcript."""

    language: str | None
    model_name: str
    audio_sample_rate: int
    audio_channels: int
    duration_seconds: float | None
    segments: tuple[TranscriptSegment, ...]


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """A transcript as it is stored in SQLite."""

    id: str
    job_id: str
    language: str | None
    model_name: str
    audio_sample_rate: int
    audio_channels: int
    duration_seconds: float | None
    segment_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class TranscriptSegmentRecord:
    """One stored transcript segment."""

    id: str
    transcript_id: str
    segment_index: int
    start_seconds: float
    end_seconds: float
    text: str
    average_log_probability: float | None
    no_speech_probability: float | None


def normalise_text(value: str) -> str:
    """Normalise the text of a segment.

    Only whitespace is touched. Case, punctuation, accents and every other
    Unicode character are preserved exactly, because the transcript is the
    user's content and must not be mangled on the way to the database.
    """
    return _WHITESPACE_RUN.sub(" ", value).strip()


def validate_segments(
    raw_segments: object,
    *,
    duration_seconds: float | None = None,
    tolerance_seconds: float = MAX_TIMESTAMP_OVERSHOOT_SECONDS,
    max_segments: int | None = None,
) -> list[TranscriptSegment]:
    """Turn raw engine output into validated, ordered segments.

    Timestamps are normalised into the audio they describe. An engine that
    reports the end of the last segment past the end of the audio -- which
    Whisper does routinely, by up to one 30-second window -- has that
    timestamp pulled back to the end of the audio. A timestamp that is out
    further than that allowance is rejected, because it cannot be trusted.

    The rules are listed in the module docstring.

    Args:
        raw_segments: An iterable of objects with ``start``, ``end`` and
            ``text`` attributes. Any other shape is rejected.
        duration_seconds: The duration of the audio the segments describe, when
            it is known. Timestamps are checked and normalised against it.
            When it is ``None`` no timestamp can be checked against the audio,
            so the transcript is only checked for internal consistency.
        tolerance_seconds: How far past the audio a timestamp may reach.
        max_segments: The largest number of segments to keep. ``None`` means
            no limit.

    Returns:
        The validated segments, ordered by start time and renumbered from
        zero. Segments whose text is empty after normalisation are dropped:
        they carry no speech and would only add noise to the transcript. A
        segment that lies entirely past the end of the audio is dropped for
        the same reason.

    Raises:
        TranscriptValidationError: when the input is not a list of segments, or
            when a segment carries an impossible timestamp or a non-string
            text.
    """
    if raw_segments is None:
        raise TranscriptValidationError("The engine returned no segment list.")
    try:
        candidates = list(raw_segments)  # type: ignore[call-overload]
    except TypeError as exc:
        raise TranscriptValidationError(
            "The engine returned something that is not a list of segments."
        ) from exc

    validated: list[TranscriptSegment] = []
    for position, candidate in enumerate(candidates):
        if max_segments is not None and len(validated) >= max_segments:
            logger.warning(
                "the transcript reached the limit of %s segments and was cut short",
                max_segments,
            )
            break
        start = _read_number(candidate, "start", position)
        end = _read_number(candidate, "end", position)

        if start < 0:
            raise TranscriptValidationError(
                f"Segment {position} starts before the beginning of the audio."
            )
        if end < start:
            raise TranscriptValidationError(
                f"Segment {position} ends before it starts."
            )
        if duration_seconds is not None:
            limit = duration_seconds + tolerance_seconds
            if start > limit or end > limit:
                raise TranscriptValidationError(
                    f"Segment {position} lies outside the audio it describes."
                )
            # Pull the timestamps back into the audio. The engine's window
            # padding is an artefact; the transcript should not claim that
            # speech happened after the audio ended.
            start = min(start, duration_seconds)
            end = min(end, duration_seconds)

        if end <= start:
            # Either the whole segment lies past the end of the audio -- in
            # practice a hallucination over trailing silence -- or it has no
            # span at all. There is no honest timestamp for either, so it is
            # dropped rather than stored with an invented one.
            continue

        text = _read_text(candidate, position)
        normalised = normalise_text(text)
        if not normalised:
            # An empty or whitespace-only result is not an error: the engine
            # found no speech in that window. It is skipped on purpose.
            continue
        if len(normalised) > MAX_SEGMENT_TEXT_LENGTH:
            # Not a speech segment. It is dropped rather than stored, so one
            # malformed result cannot use an unbounded amount of memory.
            logger.warning(
                "a segment of %s characters was dropped as malformed",
                len(normalised),
            )
            continue

        validated.append(
            TranscriptSegment(
                index=0,  # renumbered below
                start_seconds=start,
                end_seconds=end,
                text=normalised,
                average_log_probability=_read_optional_number(
                    candidate, "avg_logprob"
                ),
                no_speech_probability=_read_optional_number(
                    candidate, "no_speech_prob"
                ),
            )
        )

    # Ordering is deterministic: by start time, then by end time, then by the
    # original position, so that two engines producing the same speech always
    # produce the same transcript.
    indexed = list(enumerate(validated))
    indexed.sort(key=lambda item: (item[1].start_seconds, item[1].end_seconds, item[0]))

    return [
        TranscriptSegment(
            index=position,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            text=segment.text,
            average_log_probability=segment.average_log_probability,
            no_speech_probability=segment.no_speech_probability,
        )
        for position, (_, segment) in enumerate(indexed)
    ]


def assert_storable(transcript: Transcript) -> None:
    """Check that a transcript can be written to the database safely.

    The segments have already been validated by :func:`validate_segments`, but
    the database is the last line of defence: a transcript that reaches this
    point with duplicate indexes, impossible timestamps or empty text would
    corrupt what a client later reads back.

    Raises:
        ValueError: when the transcript must not be stored.
    """
    if transcript.audio_sample_rate <= 0:
        raise ValueError("The transcript has no usable sample rate.")
    if transcript.audio_channels <= 0:
        raise ValueError("The transcript has no usable channel count.")
    if (
        transcript.duration_seconds is not None
        and not math.isfinite(transcript.duration_seconds)
    ):
        raise ValueError("The transcript has no usable duration.")

    seen: set[int] = set()
    for segment in transcript.segments:
        if segment.index in seen:
            raise ValueError("The transcript contains a duplicate segment index.")
        seen.add(segment.index)
        if segment.index < 0:
            raise ValueError("The transcript contains a negative segment index.")
        if not math.isfinite(segment.start_seconds) or not math.isfinite(
            segment.end_seconds
        ):
            raise ValueError("The transcript contains a timestamp that is not finite.")
        if segment.start_seconds < 0:
            raise ValueError("The transcript contains a negative timestamp.")
        if segment.end_seconds < segment.start_seconds:
            raise ValueError("The transcript contains a segment that ends before it starts.")
        if not isinstance(segment.text, str) or not segment.text.strip():
            raise ValueError("The transcript contains an empty segment.")


def _read_number(candidate: object, field: str, position: int) -> float:
    value = getattr(candidate, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptValidationError(
            f"Segment {position} has no usable {field} timestamp."
        )
    number = float(value)
    if not math.isfinite(number):
        raise TranscriptValidationError(
            f"Segment {position} has a {field} timestamp that is not a finite number."
        )
    return number


def _read_text(candidate: object, position: int) -> str:
    value = getattr(candidate, "text", None)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TranscriptValidationError(
            f"Segment {position} has text that is not a string."
        )
    return value


def _read_optional_number(candidate: object, field: str) -> float | None:
    """Read an optional engine-provided number, ignoring anything unusable."""
    value = getattr(candidate, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number
