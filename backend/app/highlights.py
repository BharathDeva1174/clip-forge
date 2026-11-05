"""The highlight candidate model, and the rules for what may become one.

A candidate is a moment the model believes is worth clipping. The model's
answer is **untrusted input**: it is text produced by a system that was
given a transcript, which is itself text that a video's speaker chose.

Two consequences shape this module.

**The model selects segments; the boundaries come from the database.** The
model is asked which transcript segments make a good moment. The times are
then read from the *persisted* segments, so a candidate can never claim a
timestamp the transcript does not support. When the model also proposes
times, they are treated as advisory: they are checked against the
segments, and a disagreement is a reason to drop the candidate, never a
reason to store the model's number.

**An invalid candidate is dropped, not repaired.** There is no invented
timestamp, no clamped range, no "close enough". A candidate that cannot be
trusted is not stored, and the rules that decide this live in one module
that performs no I/O, so they can be tested on their own.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

#: The version of the candidate schema the prompt asks for.
HIGHLIGHT_SCHEMA_VERSION = "1"

#: The version of the prompt itself.
HIGHLIGHT_PROMPT_VERSION = "1"

#: The longest title a candidate may carry.
MAX_TITLE_LENGTH = 120

#: The longest rationale a candidate may carry.
MAX_REASON_LENGTH = 400

#: The most candidates one analysis may produce.
MAX_CANDIDATES = 50

#: The shortest moment that is still a moment.
MIN_CANDIDATE_SECONDS = 1.0

#: The longest moment. A "short-form clip" that is longer than this is
#: not a clip, and a model that proposes one has misunderstood the task.
MAX_CANDIDATE_SECONDS = 180.0

#: How far a proposed time may sit outside the segments it cites.
TIME_TOLERANCE_SECONDS = 2.0

#: How much two candidates may overlap before the later one is dropped.
OVERLAP_RATIO = 0.8

#: The decisions a user can make about a candidate.
DECISION_PROPOSED = "proposed"
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISIONS = (DECISION_PROPOSED, DECISION_ACCEPTED, DECISION_REJECTED)

#: The kinds of moment the prompt asks about.
MOMENT_TYPES = (
    "hook",
    "insight",
    "story",
    "payoff",
    "instruction",
    "opinion",
    "demonstration",
    "other",
)

#: Control characters are not allowed in the text a model produces.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Whitespace runs inside a title or a rationale.
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SegmentReference:
    """One transcript segment, as the model is allowed to refer to it."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class HighlightCandidate:
    """A validated moment."""

    id: str
    position: int
    start_segment_index: int
    end_segment_index: int
    start_seconds: float
    end_seconds: float
    title: str
    reason: str
    moment_type: str | None = None
    #: The model's own ranking score. It is **not** a calibrated
    #: probability, and the interface says so.
    score: float | None = None
    decision: str = DECISION_PROPOSED

    @property
    def duration_seconds(self) -> float:
        """How long the moment is."""
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass(frozen=True, slots=True)
class HighlightCandidateRecord:
    """A stored candidate, with the job it belongs to.

    The job is reached through the analysis, because a candidate belongs to
    an analysis and an analysis belongs to a job.
    """

    id: str
    job_id: str
    analysis_id: str
    position: int
    start_segment_index: int
    end_segment_index: int
    start_seconds: float
    end_seconds: float
    title: str
    reason: str
    moment_type: str | None
    score: float | None
    decision: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HighlightAnalysis:
    """One highlight analysis, as it is stored.

    The record keeps the configuration the analysis was queued against, so
    that an integration edited or deleted in the meantime cannot silently
    change what runs, and so that a finished result can be explained.
    """

    id: str
    job_id: str
    integration_id: str | None
    provider_id: str
    protocol: str
    base_url: str
    model_id: str
    model_name: str | None
    prompt_version: str
    schema_version: str
    transcript_id: str | None
    source_duration_seconds: float | None
    status: str
    error_code: str | None
    error_message: str | None
    attempt_count: int
    worker_id: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class HighlightValidationError(Exception):
    """The model's answer could not be used at all."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalise_text(value: str) -> str:
    """Clean the text a model produced.

    Whitespace is collapsed and control characters are removed. Nothing
    else is changed: the words are the model's, and the user reads them.
    """
    cleaned = _CONTROL_CHARACTERS.sub(" ", value)
    return _WHITESPACE_RUN.sub(" ", cleaned).strip()


def extract_candidate_list(payload: object) -> list[object] | None:
    """The list of candidates inside a parsed answer.

    Both a bare list and ``{"highlights": [...]}`` are accepted, because
    models wrap their JSON in different ways. Anything else returns
    ``None``, which means the answer was not a list of candidates at all --
    as opposed to an empty list, which is a real answer.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("highlights", "candidates", "moments"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def validate_candidates(
    raw: object,
    segments: list[SegmentReference],
    *,
    source_duration_seconds: float | None,
    max_candidates: int = MAX_CANDIDATES,
) -> list[HighlightCandidate]:
    """Turn the model's answer into validated candidates.

    The rules, in the order they are applied:

    1. the referenced segment indices must exist and belong to this
       transcript;
    2. the start must not be after the end;
    3. the proposed times, when the model gave any, must be finite and must
       overlap the segments the candidate cites;
    4. the boundaries are read from the **persisted segments**, never from
       the model;
    5. the moment must be inside the source duration and within the
       product's length limits;
    6. the title must be a non-empty string, and the rationale a string;
    7. a score that is not a number in range is discarded, because it is
       an annotation rather than the moment itself;
    8. a candidate that repeats, or that substantially overlaps, an
       accepted one is dropped;
    9. at most ``max_candidates`` are kept.

    Returns:
        The accepted candidates, in the order the model ranked them.

    Raises:
        HighlightValidationError: when the answer is not a list of
            candidates at all.
    """
    if raw is None:
        raise HighlightValidationError("The model returned nothing.")

    entries = extract_candidate_list(raw)
    if entries is None:
        raise HighlightValidationError(
            "The model's answer was not a list of candidates."
        )
    if not entries:
        # An empty list is a real answer: the model found no good moments.
        return []

    by_index = {segment.index: segment for segment in segments}
    accepted: list[HighlightCandidate] = []

    for entry in entries:
        if len(accepted) >= max_candidates:
            break
        if not isinstance(entry, dict):
            continue
        candidate = _build_candidate(
            entry,
            by_index=by_index,
            source_duration_seconds=source_duration_seconds,
            position=len(accepted),
        )
        if candidate is None:
            continue
        if _overlaps_an_accepted(candidate, accepted):
            continue
        accepted.append(candidate)

    if not accepted and entries:
        # The model answered, but nothing in the answer could be trusted.
        raise HighlightValidationError(
            "The model's answer contained no usable candidates."
        )
    return accepted


def _build_candidate(
    entry: dict[str, Any],
    *,
    by_index: dict[int, SegmentReference],
    source_duration_seconds: float | None,
    position: int,
) -> HighlightCandidate | None:
    """Validate one candidate, or return ``None`` when it cannot be used."""
    start_index = _read_index(entry, "start_segment")
    end_index = _read_index(entry, "end_segment")
    if start_index is None or end_index is None:
        return None

    start_segment = by_index.get(start_index)
    end_segment = by_index.get(end_index)
    if start_segment is None or end_segment is None:
        # The model cited a segment that is not in this transcript.
        return None
    if start_index > end_index:
        return None

    # The boundaries come from the database, never from the model.
    start_seconds = start_segment.start_seconds
    end_seconds = end_segment.end_seconds

    proposed_start = _read_number(entry, "start_seconds")
    proposed_end = _read_number(entry, "end_seconds")
    if not _proposed_times_agree(
        proposed_start,
        proposed_end,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    ):
        return None

    if source_duration_seconds is not None:
        if start_seconds > source_duration_seconds + TIME_TOLERANCE_SECONDS:
            return None
        if end_seconds > source_duration_seconds + TIME_TOLERANCE_SECONDS:
            return None

    duration = end_seconds - start_seconds
    if duration < MIN_CANDIDATE_SECONDS or duration > MAX_CANDIDATE_SECONDS:
        return None

    title = normalise_text(_read_text(entry, "title"))
    if not title:
        return None
    title = title[:MAX_TITLE_LENGTH]

    reason = normalise_text(_read_text(entry, "reason"))[:MAX_REASON_LENGTH]

    moment_type = _read_text(entry, "moment_type")
    if moment_type:
        moment_type = moment_type.lower()[:40]
        if moment_type not in MOMENT_TYPES:
            moment_type = "other"

    score = _read_number(entry, "score")
    if score is None:
        score = _read_number(entry, "confidence")
    if score is not None and not (0.0 <= score <= 1.0):
        # A score outside the documented range is discarded rather than
        # clamped: it is an annotation, and the moment does not depend on it.
        score = None

    return HighlightCandidate(
        id="",
        position=position,
        start_segment_index=start_index,
        end_segment_index=end_index,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        title=title,
        reason=reason,
        moment_type=moment_type or None,
        score=score,
    )


def _proposed_times_agree(
    proposed_start: float | None,
    proposed_end: float | None,
    *,
    start_seconds: float,
    end_seconds: float,
) -> bool:
    """Whether the model's proposed times overlap the segments it cited.

    The proposed times are advisory. They are checked, because a model that
    says a moment is at 00:00 when it cites a segment at 03:20 has not
    understood the task. They are never stored.
    """
    if proposed_start is None and proposed_end is None:
        return True
    if proposed_start is not None and not (
        start_seconds - TIME_TOLERANCE_SECONDS
        <= proposed_start
        <= end_seconds + TIME_TOLERANCE_SECONDS
    ):
        return False
    if proposed_end is not None and not (
        start_seconds - TIME_TOLERANCE_SECONDS
        <= proposed_end
        <= end_seconds + TIME_TOLERANCE_SECONDS
    ):
        return False
    if proposed_start is not None and proposed_end is not None:
        if proposed_end <= proposed_start:
            return False
    return True


def _overlaps_an_accepted(
    candidate: HighlightCandidate, accepted: list[HighlightCandidate]
) -> bool:
    """Whether a candidate repeats or substantially overlaps an earlier one."""
    for other in accepted:
        if (
            candidate.start_segment_index == other.start_segment_index
            and candidate.end_segment_index == other.end_segment_index
        ):
            return True
        overlap = min(candidate.end_seconds, other.end_seconds) - max(
            candidate.start_seconds, other.start_seconds
        )
        if overlap <= 0:
            continue
        shorter = min(
            candidate.duration_seconds, other.duration_seconds
        )
        if shorter > 0 and overlap / shorter >= OVERLAP_RATIO:
            return True
    return False


def _read_index(entry: dict[str, Any], key: str) -> int | None:
    """Read a segment index, which must be a whole number."""
    value = entry.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _read_number(entry: dict[str, Any], key: str) -> float | None:
    """Read a number, when it is a finite one."""
    value = entry.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _read_text(entry: dict[str, Any], key: str) -> str:
    """Read a string, treating anything else as absent."""
    value = entry.get(key)
    return value if isinstance(value, str) else ""


#: The JSON schema the model is asked to follow, when it supports it.
HIGHLIGHT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["highlights"],
    "properties": {
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "start_segment",
                    "end_segment",
                    "title",
                    "reason",
                    "moment_type",
                    "score",
                ],
                "properties": {
                    "start_segment": {
                        "type": "integer",
                        "description": "The index of the first segment of the moment.",
                    },
                    "end_segment": {
                        "type": "integer",
                        "description": "The index of the last segment of the moment.",
                    },
                    "title": {
                        "type": "string",
                        "description": "A short title, at most 12 words.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One sentence on why this moment works.",
                    },
                    "moment_type": {
                        "type": "string",
                        "enum": list(MOMENT_TYPES),
                    },
                    "score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "How strongly this moment is recommended. This is a "
                            "ranking, not a probability."
                        ),
                    },
                },
            },
        }
    },
}
