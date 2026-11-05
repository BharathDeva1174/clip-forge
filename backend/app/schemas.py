"""Pydantic models for the API surface."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.highlights import HighlightCandidate
from app.formatting import LayoutMode
from app.integrations import (
    ApiProtocol,
    ConnectionStatus,
    IntegrationRecord,
    IntegrationRole,
    PricingStatus,
    StructuredOutputMode,
)
from app.jobs import JobArtifacts, JobRecord, JobStage, JobStatus, SourceType
from app.transcripts import TranscriptRecord, TranscriptSegmentRecord


class JobError(BaseModel):
    """A sanitised, user-facing error attached to a job."""

    code: str
    message: str


class JobResponse(BaseModel):
    """The public representation of a job.

    It deliberately contains no filesystem path, no worker identifier and no
    lease information: the client only needs to know what happened and what to
    show the user.
    """

    id: str
    source_type: SourceType
    source_reference: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    stage: JobStage
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt_count: int
    error: JobError | None = None
    file_size_bytes: int | None = None
    video_duration_seconds: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_codec: str | None = None
    #: Whether a transcript has been stored for this job.
    #:
    #: A job that was finished before transcripts existed has none, so this
    #: flag is what lets a client say truthfully that a transcript exists
    #: rather than inferring it from the status.
    has_transcript: bool = False
    #: The status of the most recent highlight analysis, when there is one.
    analysis_status: str | None = None
    #: How many highlight candidates that analysis produced.
    candidate_count: int = 0
    #: How many clips have been made from this job.
    clip_count: int = 0
    #: How many formatted (9:16) artifacts have been made from this job.
    format_count: int = 0

    @classmethod
    def from_record(
        cls,
        job: JobRecord,
        *,
        has_transcript: bool = False,
        artifacts: JobArtifacts | None = None,
    ) -> JobResponse:
        counts = artifacts or JobArtifacts()
        return cls(
            id=job.id,
            source_type=job.source_type,
            source_reference=job.source_reference,
            status=job.status,
            progress=job.progress,
            stage=job.stage,
            created_at=_parse(job.created_at),
            updated_at=_parse(job.updated_at),
            started_at=_parse(job.started_at),
            completed_at=_parse(job.completed_at),
            attempt_count=job.attempt_count,
            error=(
                JobError(code=job.error_code, message=job.error_message)
                if job.error_code and job.error_message
                else None
            ),
            file_size_bytes=job.file_size_bytes,
            video_duration_seconds=job.video_duration_seconds,
            video_width=job.video_width,
            video_height=job.video_height,
            video_codec=job.video_codec,
            has_transcript=has_transcript,
            analysis_status=counts.analysis_status,
            candidate_count=counts.candidate_count,
            clip_count=counts.clip_count,
            format_count=counts.format_count,
        )


class JobListResponse(BaseModel):
    """A bounded page of jobs, newest first."""

    items: list[JobResponse]
    total: int
    limit: int
    offset: int


class YouTubeSubmission(BaseModel):
    """A request to ingest a YouTube video the user is authorised to use."""

    url: str = Field(
        max_length=2048,
        description="A YouTube watch, short, embed or shorts URL.",
    )
    authorized: bool = Field(
        description=(
            "Must be true. Confirm that you own this video or have permission "
            "to download and reuse it."
        )
    )


class TranscriptSegmentResponse(BaseModel):
    """One timestamped piece of the transcript.

    The times are seconds from the start of the audio, which is the same
    timeline as the source video.

    ``no_speech_probability`` is the engine's own estimate that the segment
    contains no speech. It is a hint about the audio, not a measure of how
    correct the text is.
    """

    index: int = Field(ge=0, description="The position of the segment in the transcript.")
    start_seconds: float = Field(ge=0, description="Where the segment starts.")
    end_seconds: float = Field(ge=0, description="Where the segment ends.")
    text: str
    no_speech_probability: float | None = None

    @classmethod
    def from_record(cls, segment: TranscriptSegmentRecord) -> TranscriptSegmentResponse:
        return cls(
            index=segment.segment_index,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            text=segment.text,
            no_speech_probability=segment.no_speech_probability,
        )


class TranscriptResponse(BaseModel):
    """The transcript of a job, and where the job stands.

    The status is one of:

    * ``ready`` -- the transcript is stored and the segments are included.
    * ``pending`` -- the job exists but has no transcript yet; ``job_status``
      and ``job_stage`` say what it is doing.
    * ``failed`` -- the job failed; ``error`` carries the stable error code.

    The response never contains a filesystem path, an environment value, a
    model cache location or raw subprocess output.
    """

    job_id: str
    status: Literal["ready", "pending", "failed"]
    job_status: JobStatus
    job_stage: JobStage
    language: str | None = None
    model_name: str | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    duration_seconds: float | None = None
    created_at: datetime | None = None
    segment_count: int = Field(default=0, ge=0)
    segments: list[TranscriptSegmentResponse] = Field(default_factory=list)
    limit: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    error: JobError | None = None

    @classmethod
    def from_records(
        cls,
        job: JobRecord,
        transcript: TranscriptRecord,
        segments: list[TranscriptSegmentRecord],
        *,
        limit: int,
        offset: int,
    ) -> TranscriptResponse:
        return cls(
            job_id=job.id,
            status="ready",
            job_status=job.status,
            job_stage=job.stage,
            language=transcript.language,
            model_name=transcript.model_name,
            audio_sample_rate=transcript.audio_sample_rate,
            audio_channels=transcript.audio_channels,
            duration_seconds=transcript.duration_seconds,
            created_at=_parse(transcript.created_at),
            segment_count=transcript.segment_count,
            segments=[
                TranscriptSegmentResponse.from_record(segment) for segment in segments
            ],
            limit=limit,
            offset=offset,
        )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


# --- integrations (Phase 4) -------------------------------------------------


class ProviderPresetResponse(BaseModel):
    """A provider preset: the defaults a new configuration starts from."""

    id: str
    name: str
    summary: str
    default_protocol: ApiProtocol
    #: The base URL to use for each protocol, keyed by protocol.
    base_urls: dict[str, str]
    models_url: str | None
    supports_model_discovery: bool
    #: What the provider documents about free access. It is never a claim
    #: that a model is free: that is the provider's to decide and to change.
    free_tier: str
    free_tier_note: str
    docs_url: str


class ProviderPresetListResponse(BaseModel):
    """Every preset."""

    items: list[ProviderPresetResponse]


class IntegrationResponse(BaseModel):
    """A saved provider configuration.

    It never contains the key. It says whether a key is configured, and
    nothing more.
    """

    id: str
    name: str
    provider_id: str
    provider_name: str
    protocol: ApiProtocol
    protocol_supported: bool
    base_url: str
    model_id: str | None
    model_name: str | None
    context_window: int | None
    max_output_tokens: int | None
    reasoning_effort: str | None
    reasoning_effort_values: list[str]
    structured_output: StructuredOutputMode
    pricing_status: PricingStatus
    local_endpoint: bool
    enabled: bool
    api_key_configured: bool
    is_default: bool
    role: IntegrationRole
    last_tested_at: datetime | None
    last_test_status: ConnectionStatus
    last_test_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(
        cls, record: IntegrationRecord, *, provider_name: str = ""
    ) -> IntegrationResponse:
        return cls(
            id=record.id,
            name=record.name,
            provider_id=record.provider_id,
            provider_name=provider_name,
            protocol=record.protocol,
            protocol_supported=record.protocol_is_implemented,
            base_url=record.base_url,
            model_id=record.model_id,
            model_name=record.model_name,
            context_window=record.capabilities.context_window,
            max_output_tokens=record.max_output_tokens,
            reasoning_effort=record.reasoning_effort,
            reasoning_effort_values=list(record.capabilities.reasoning_effort_values),
            structured_output=record.capabilities.structured_output,
            pricing_status=record.pricing_status,
            local_endpoint=record.local_endpoint,
            enabled=record.enabled,
            api_key_configured=record.api_key_configured,
            is_default=record.is_default,
            role=record.role,
            last_tested_at=_parse(record.last_tested_at),
            last_test_status=record.last_test_status,
            last_test_code=record.last_test_code,
            created_at=_parse(record.created_at),
            updated_at=_parse(record.updated_at),
        )


class IntegrationListResponse(BaseModel):
    """A page of saved configurations."""

    items: list[IntegrationResponse]
    total: int
    limit: int
    offset: int


class IntegrationCreateRequest(BaseModel):
    """A new configuration."""

    name: str = Field(min_length=1, max_length=120)
    provider_id: str = Field(min_length=1, max_length=60)
    protocol: ApiProtocol | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    model_id: str | None = Field(default=None, max_length=200)
    model_name: str | None = Field(default=None, max_length=200)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    reasoning_effort: str | None = Field(default=None, max_length=40)
    structured_output: StructuredOutputMode | None = None
    pricing_status: PricingStatus = PricingStatus.UNKNOWN
    local_endpoint: bool = False
    enabled: bool = True
    is_default: bool = False
    #: The key, when there is one. It goes to the credential store and is
    #: never stored here.
    api_key: str | None = Field(default=None, max_length=500)


class IntegrationUpdateRequest(BaseModel):
    """An edit.

    Every field is optional. The key is deliberately **not** here: it is
    replaced or removed through its own endpoint, so that an ordinary edit
    can never erase a stored key by accident.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    protocol: ApiProtocol | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    model_id: str | None = Field(default=None, max_length=200)
    model_name: str | None = Field(default=None, max_length=200)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    reasoning_effort: str | None = Field(default=None, max_length=40)
    structured_output: StructuredOutputMode | None = None
    pricing_status: PricingStatus | None = None
    local_endpoint: bool | None = None
    enabled: bool | None = None


class ApiKeyRequest(BaseModel):
    """A replacement key."""

    api_key: str = Field(min_length=1, max_length=500)


class ModelDescriptor(BaseModel):
    """One model as the provider lists it."""

    id: str
    name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    available: bool | None = None
    #: The protocol the model speaks, when it is known.
    protocol: ApiProtocol | None = None


class ModelListResponse(BaseModel):
    """The models a provider offers."""

    items: list[ModelDescriptor]
    source_url: str
    #: A note about what the list does and does not say.
    note: str


class ConnectionTestResponse(BaseModel):
    """The outcome of a connection test."""

    status: ConnectionStatus
    code: str
    message: str
    model: str | None = None


# --- clips (Phase 5) --------------------------------------------------------


class ClipCreateRequest(BaseModel):
    """A request for one clip.

    The only thing the client chooses is *which* highlight candidate to
    render. The boundaries come from the candidate in the database, so a
    request cannot introduce a timestamp of its own.
    """

    candidate_id: str = Field(min_length=1, max_length=64)


class ClipResponse(BaseModel):
    """A clip: its state, and what was measured about the output.

    The output path is never included. The frontend plays a finished clip
    through the media route, so it has no reason to know where the file is.
    """

    id: str
    job_id: str
    analysis_id: str | None = None
    candidate_id: str | None = None
    status: str
    stage: str
    progress: int = Field(ge=0, le=100)
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    #: The duration measured from the rendered file, which is not the same
    #: thing as the duration that was asked for.
    actual_duration_seconds: float | None = None
    output_size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    attempt_count: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: JobError | None = None
    #: Where to play or download the clip, when there is one.
    media_url: str | None = None
    #: The clip this one was trimmed from, when it is a revision.
    parent_clip_id: str | None = None
    #: Whether this clip was trimmed by hand rather than chosen by the AI.
    is_revision: bool = False
    #: The user's label for the clip, when there is one.
    title: str | None = None
    #: The title the AI gave the moment this clip came from.
    source_title: str | None = None
    #: The caption text the user has changed, by transcript segment index.
    caption_edits: list["CaptionEditModel"] = Field(default_factory=list)


class ClipListResponse(BaseModel):
    """A page of clips."""

    items: list[ClipResponse]
    total: int
    limit: int
    offset: int


class ClipRetryResponse(BaseModel):
    """The clip after a retry was requested."""

    clip: ClipResponse


# --- editing and exporting (Phase 8) -----------------------------------------


class CaptionEditModel(BaseModel):
    """A caption the user has retyped.

    The segment index is the index of the transcript segment the caption
    came from, so the server can match it against the transcript it
    already holds rather than trusting the text.
    """

    segment_index: int = Field(ge=0)
    text: str = Field(max_length=2000)


class ClipEditRequest(BaseModel):
    """A request to trim a clip.

    The boundaries are on the **source media** timeline, the same timeline
    the clip and the transcript already use. They are validated against
    the source's real duration before anything is queued.
    """

    start_seconds: float
    end_seconds: float
    #: An optional label for the new clip.
    title: str | None = Field(default=None, max_length=200)

    @field_validator("start_seconds", "end_seconds", mode="before")
    @classmethod
    def _a_boundary_is_not_a_boolean(cls, value: object) -> object:
        """Refuse a boolean before it is turned into a number.

        Pydantic would happily read ``True`` as ``1.0``, and a request
        that asked to start at one second because it passed a boolean is
        not a request anybody meant to make.
        """
        if isinstance(value, bool):
            raise ValueError("a boundary must be a number, not a boolean")
        return value


class ClipTitleRequest(BaseModel):
    """A request to relabel a clip.

    A label is not a re-render: the file is not touched.
    """

    title: str | None = Field(default=None, max_length=200)


class ClipCaptionsRequest(BaseModel):
    """A request to change the caption text of a clip.

    The whole set is replaced, so an edit that is removed is removed.
    """

    edits: list[CaptionEditModel] = Field(default_factory=list, max_length=500)


class ClipCaptionsResponse(BaseModel):
    """The caption text of a clip after an edit."""

    clip_id: str
    edits: list[CaptionEditModel] = Field(default_factory=list)


class ExportRequest(BaseModel):
    """A request for a ZIP of some of a job's clips."""

    clip_ids: list[str] = Field(min_length=1, max_length=100)


class JobCancelResponse(BaseModel):
    """The job after a cancellation was requested."""

    job: JobResponse



# --- formatted clips (Phase 6) ----------------------------------------------


class CaptionSettingsModel(BaseModel):
    """How the captions should look."""

    enabled: bool = True
    font_family: str = "Arial"
    font_size: int = 42
    text_colour: str = "#FFFFFF"
    outline_colour: str = "#000000"
    outline_thickness: int = 3
    background_colour: str | None = None
    shadow: int = 0
    alignment: int = 2
    margin_vertical: int = 90
    max_characters_per_line: int = 28
    max_lines: int = 3


class FormatSettingsModel(BaseModel):
    """The video a user wants out of a clip."""

    width: int = 720
    height: int = 1280
    layout: LayoutMode = LayoutMode.CENTER_CROP
    padding_colour: str = "#000000"
    captions: CaptionSettingsModel = Field(default_factory=CaptionSettingsModel)


class FormatCreateRequest(BaseModel):
    """A request for a formatted clip.

    The boundaries are not here: they come from the clip, which came from
    the highlight candidate, which came from the transcript.
    """

    settings: FormatSettingsModel = Field(default_factory=FormatSettingsModel)


class FormatResponse(BaseModel):
    """A formatted clip: its state, and what was measured about the output.

    The output path is never included. The frontend plays the artifact
    through the media route, so it has no reason to know where the file is.
    """

    id: str
    clip_id: str
    job_id: str
    status: str
    stage: str
    progress: int = Field(ge=0, le=100)
    #: A short description of the settings this artifact was made with.
    settings: dict[str, object] = Field(default_factory=dict)
    config_fingerprint: str
    output_size_bytes: int | None = None
    actual_duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    pixel_format: str | None = None
    #: Whether captions were asked for.
    captions_enabled: bool = False
    #: Whether captions were actually burned in. A clip with no usable
    #: caption text renders without them, and the two are reported
    #: separately so nothing is implied that did not happen.
    captions_applied: bool = False
    caption_count: int = 0
    attempt_count: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: JobError | None = None
    media_url: str | None = None


class FormatListResponse(BaseModel):
    """A page of formatted clips."""

    items: list[FormatResponse]
    total: int
    limit: int
    offset: int


class FormatRetryResponse(BaseModel):
    """The artifact after a retry was requested."""

    format: FormatResponse


class FontListResponse(BaseModel):
    """The fonts this machine has."""

    items: list[str]
    default: str


# --- highlights (Phase 4) ---------------------------------------------------


class HighlightCandidateResponse(BaseModel):
    """One candidate moment."""

    id: str
    position: int
    start_segment_index: int
    end_segment_index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    title: str
    reason: str
    moment_type: str | None = None
    #: The model's own ranking. It is not a calibrated probability.
    score: float | None = None
    decision: str

    @classmethod
    def from_candidate(cls, candidate: HighlightCandidate) -> HighlightCandidateResponse:
        return cls(
            id=candidate.id,
            position=candidate.position,
            start_segment_index=candidate.start_segment_index,
            end_segment_index=candidate.end_segment_index,
            start_seconds=candidate.start_seconds,
            end_seconds=candidate.end_seconds,
            duration_seconds=candidate.duration_seconds,
            title=candidate.title,
            reason=candidate.reason,
            moment_type=candidate.moment_type,
            score=candidate.score,
            decision=candidate.decision,
        )


class HighlightAnalysisResponse(BaseModel):
    """The state of a job's highlight analysis, and its candidates."""

    job_id: str
    analysis_id: str | None = None
    status: Literal["none", "pending", "running", "completed", "failed"]
    provider_name: str | None = None
    model_id: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    candidate_count: int = 0
    candidates: list[HighlightCandidateResponse] = Field(default_factory=list)
    error: JobError | None = None
    #: What the user is agreeing to when an analysis runs. The provider
    #: receives the transcript text and processes it under its own policies.
    data_notice: str = ""


class CandidateDecisionRequest(BaseModel):
    """Accept or reject a candidate."""

    decision: Literal["proposed", "accepted", "rejected"]
