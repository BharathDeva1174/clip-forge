"""The formatted-clip domain model.

A *formatted clip* is the portrait version of a Phase 5 clip: the same
moment, fitted into a 9:16 canvas, with the captions burned in.

It is a separate artifact from the clip it came from, on purpose. The
Phase 5 clip stays exactly as it was rendered — the user may still want the
landscape original — and a second format of the same clip is a second
artifact rather than a replacement.

The lifecycle is the same one the clips use, deliberately: a formatted clip
is the same kind of thing, and having two slightly different state machines
would be a trap. The statuses and the transition table come from
:mod:`app.clips` and are re-exported here under names that read correctly
in this context.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.clips import (
    ACTIVE_CLIP_STATUSES,
    ALLOWED_CLIP_TRANSITIONS,
    TERMINAL_CLIP_STATUSES,
    ClipStage,
    ClipStatus,
)

#: A formatted clip uses the same lifecycle as a clip.
FormatStatus = ClipStatus
FormatStage = ClipStage

#: The statuses a formatted clip never leaves on its own.
TERMINAL_FORMAT_STATUSES = TERMINAL_CLIP_STATUSES

#: The statuses in which a worker owns a formatted clip.
ACTIVE_FORMAT_STATUSES = ACTIVE_CLIP_STATUSES

#: The legal transitions.
ALLOWED_FORMAT_TRANSITIONS = ALLOWED_CLIP_TRANSITIONS


@dataclass(frozen=True, slots=True)
class FormatRecord:
    """A formatted clip, as it is stored."""

    id: str
    clip_id: str
    job_id: str
    #: The hash of the normalised settings. One clip with one fingerprint is
    #: one artifact.
    config_fingerprint: str
    #: The immutable snapshot of the settings this artifact was made with.
    settings_json: str
    status: FormatStatus
    stage: FormatStage
    progress: int
    output_path: str | None
    output_size_bytes: int | None
    actual_duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    pixel_format: str | None
    #: Whether the user asked for captions.
    captions_enabled: bool
    #: Whether captions were actually burned in. The two are separate on
    #: purpose: a clip with no usable caption text renders without them, and
    #: the artifact has to say so rather than imply otherwise.
    captions_applied: bool
    caption_count: int
    attempt_count: int
    worker_id: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    error_code: str | None
    error_message: str | None

    @property
    def is_terminal(self) -> bool:
        """Whether the artifact has reached a status it never leaves."""
        return self.status in TERMINAL_FORMAT_STATUSES

    @property
    def is_playable(self) -> bool:
        """Whether there is a verified output to play."""
        return self.status is FormatStatus.COMPLETED and self.output_path is not None
