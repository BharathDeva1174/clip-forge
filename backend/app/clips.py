"""The clip domain model.

A clip is a rendered video: one highlight moment, cut out of the source
video, verified, and playable on its own.

The rules live in this module, and they perform no I/O, so they can be
applied and tested on their own. The lifecycle mirrors the job lifecycle of
:mod:`app.jobs` -- a small, explicit set of statuses, a table of legal
transitions, and a rule that the terminal statuses are terminal.

Three things this module insists on, because they are the ones that go
wrong in practice:

**A clip is not completed because FFmpeg exited with zero.** The output has
to be verified independently first. That is the renderer's job, but the
*rule* is here: only the verification step may move a clip to ``completed``.

**The boundaries come from the persisted candidate.** A clip is cut from
the transcript segment range the candidate cites, and the candidate's own
validated times. Nothing else may set them, and in particular no
model-generated number reaches here without having been through
:mod:`app.highlights` first.

**A stale worker may not overwrite a newer attempt.** Every write that
changes a clip's state is conditional on the clip still being owned by the
worker making it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class ClipStatus(StrEnum):
    """The lifecycle of a clip."""

    QUEUED = "queued"
    PREPARING = "preparing"
    RENDERING = "rendering"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class ClipStage(StrEnum):
    """The step a clip is on.

    The stage is the finer-grained name of the status. It is what the
    interface shows, because "rendering" says more than "not finished".
    """

    QUEUED = "queued"
    PREPARING = "preparing"
    RENDERING = "rendering"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


#: The statuses a clip never leaves on its own.
TERMINAL_CLIP_STATUSES: frozenset[ClipStatus] = frozenset(
    {ClipStatus.COMPLETED, ClipStatus.FAILED}
)

#: The statuses in which a worker owns the clip.
ACTIVE_CLIP_STATUSES: frozenset[ClipStatus] = frozenset(
    {
        ClipStatus.PREPARING,
        ClipStatus.RENDERING,
        ClipStatus.VERIFYING,
    }
)

#: The legal transitions.
#:
#: ``completed`` and ``failed`` are terminal. The only way out of a
#: terminal clip is the explicit retry, which is not a transition but a
#: deliberate reset.
ALLOWED_CLIP_TRANSITIONS: dict[ClipStatus, frozenset[ClipStatus]] = {
    ClipStatus.QUEUED: frozenset(
        {ClipStatus.PREPARING, ClipStatus.RENDERING, ClipStatus.FAILED}
    ),
    ClipStatus.PREPARING: frozenset(
        {ClipStatus.RENDERING, ClipStatus.VERIFYING, ClipStatus.FAILED}
    ),
    ClipStatus.RENDERING: frozenset(
        {ClipStatus.VERIFYING, ClipStatus.FAILED}
    ),
    ClipStatus.VERIFYING: frozenset(
        {ClipStatus.COMPLETED, ClipStatus.FAILED}
    ),
    ClipStatus.COMPLETED: frozenset(),
    ClipStatus.FAILED: frozenset(),
}

#: The progress reported for each stage.
#:
#: FFmpeg does not report a reliable percentage, so the progress is
#: stage-based and honest: it says which step is running, not how far
#: through it is.
CLIP_STAGE_PROGRESS: dict[ClipStage, int] = {
    ClipStage.QUEUED: 0,
    ClipStage.PREPARING: 10,
    ClipStage.RENDERING: 40,
    ClipStage.VERIFYING: 85,
    ClipStage.COMPLETED: 100,
    ClipStage.FAILED: 0,
}


class ClipTransitionError(RuntimeError):
    """Raised when a clip is asked to move to a status it cannot reach."""

    def __init__(self, current: ClipStatus, new: ClipStatus) -> None:
        super().__init__(f"cannot move a clip from {current.value} to {new.value}")
        self.current = current
        self.new = new


def can_transition(current: ClipStatus, new: ClipStatus) -> bool:
    """Whether ``current -> new`` is an allowed transition."""
    return new in ALLOWED_CLIP_TRANSITIONS.get(current, frozenset())


def assert_transition(current: ClipStatus, new: ClipStatus) -> None:
    """Raise :class:`ClipTransitionError` if the transition is not allowed."""
    if not can_transition(current, new):
        raise ClipTransitionError(current, new)


@dataclass(frozen=True, slots=True)
class ClipRecord:
    """A clip, as it is stored."""

    id: str
    job_id: str
    analysis_id: str | None
    candidate_id: str | None
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    status: ClipStatus
    stage: ClipStage
    progress: int
    output_path: str | None
    output_size_bytes: int | None
    actual_duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    attempt_count: int
    worker_id: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    error_code: str | None
    error_message: str | None
    #: The clip this one was trimmed from, when it is a revision.
    parent_clip_id: str | None = None
    #: The user's own label for the clip, when there is one.
    title: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether the clip has reached a status it never leaves."""
        return self.status in TERMINAL_CLIP_STATUSES

    @property
    def is_playable(self) -> bool:
        """Whether there is a verified output to play."""
        return self.status is ClipStatus.COMPLETED and self.output_path is not None

    @property
    def is_revision(self) -> bool:
        """Whether this clip was trimmed by hand rather than chosen by the AI.

        A revision points at the clip it came from. It never points at a
        candidate: the AI's proposal is left exactly as the model made it,
        and the user's edit is a separate artifact with its own identity.
        """
        return self.parent_clip_id is not None


class ClipInputError(ValueError):
    """A clip request that cannot be rendered as asked.

    This is raised before any expensive work starts. It carries a stable
    code so the API can report it without inventing a message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_boundaries(
    start_seconds: object,
    end_seconds: object,
    *,
    source_duration_seconds: float | None,
    min_duration_seconds: float,
    max_duration_seconds: float,
    duration_tolerance_seconds: float = 0.0,
) -> tuple[float, float]:
    """Check a requested range and return it as two floats.

    The rules, in the order they are applied:

    1. both values are real numbers -- a boolean is not a number, and
       neither is a string that only looks like one;
    2. both are finite;
    3. the start is not negative;
    4. the end is after the start;
    5. the duration is at least the minimum and at most the maximum;
    6. the range lies inside the source, within the tolerance.

    Raises:
        ClipInputError: with a stable code.
    """
    start = _read_number(start_seconds, "start")
    end = _read_number(end_seconds, "end")

    if start < 0:
        raise ClipInputError(
            "clip_invalid_boundaries", "The start of a clip cannot be negative."
        )
    if end <= start:
        raise ClipInputError(
            "clip_invalid_boundaries",
            "The end of a clip must be after its start.",
        )

    duration = end - start
    if duration < min_duration_seconds:
        raise ClipInputError(
            "clip_duration_too_short",
            f"A clip must be at least {min_duration_seconds:.1f} seconds long.",
        )
    if duration > max_duration_seconds:
        raise ClipInputError(
            "clip_duration_too_long",
            f"A clip cannot be longer than {max_duration_seconds:.0f} seconds.",
        )

    if source_duration_seconds is not None:
        if source_duration_seconds <= 0:
            raise ClipInputError(
                "clip_source_invalid", "The source video has no usable duration."
            )
        if end > source_duration_seconds + duration_tolerance_seconds:
            raise ClipInputError(
                "clip_source_too_short",
                "The clip would run past the end of the source video.",
            )

    return start, end


def _read_number(value: object, name: str) -> float:
    """Read a number, refusing anything that is not one."""
    if isinstance(value, bool):
        raise ClipInputError(
            "clip_invalid_boundaries", f"The {name} of a clip must be a number."
        )
    if not isinstance(value, (int, float)):
        raise ClipInputError(
            "clip_invalid_boundaries", f"The {name} of a clip must be a number."
        )
    number = float(value)
    if not math.isfinite(number):
        raise ClipInputError(
            "clip_invalid_boundaries",
            f"The {name} of a clip must be a finite number.",
        )
    return number
