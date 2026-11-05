/**
 * Typed client for the ClipForge API.
 *
 * The base URL comes from `NEXT_PUBLIC_API_BASE_URL`, which is safe to expose
 * to the browser because it is only an address. No secret ever lives in this
 * file or in any `NEXT_PUBLIC_*` variable.
 */

import { getApiBaseUrl } from "@/lib/config";

export type JobStatus =
  | "queued"
  | "validating"
  | "downloading"
  | "inspecting"
  | "extracting_audio"
  | "transcribing"
  | "completed"
  | "failed"
  | "cancelled"
  /**
   * The backend reported a status this build does not know about, or sent
   * none at all.
   *
   * It is a client-side state, not one of the backend's: it exists so the
   * interface can say that it does not know, rather than quietly calling
   * the job *queued*.
   */
  | "unknown";

export type JobStage =
  | "queued"
  | "validating"
  | "downloading"
  | "inspecting"
  | "extracting_audio"
  | "transcribing"
  | "ready"
  | "failed"
  | "cancelled"
  /** The stage was missing, or this build does not know it. */
  | "unknown";

export type SourceType = "upload" | "youtube";

/** The statuses the backend defines. */
export const KNOWN_JOB_STATUSES: readonly string[] = [
  "queued",
  "validating",
  "downloading",
  "inspecting",
  "extracting_audio",
  "transcribing",
  "completed",
  "failed",
  "cancelled",
];

/** The stages the backend defines. */
export const KNOWN_JOB_STAGES: readonly string[] = [
  "queued",
  "validating",
  "downloading",
  "inspecting",
  "extracting_audio",
  "transcribing",
  "ready",
  "failed",
  "cancelled",
];

export interface Job {
  id: string;
  source_type: SourceType;
  source_reference: string;
  status: JobStatus;
  progress: number;
  stage: JobStage;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  attempt_count: number;
  error: { code: string; message: string } | null;
  file_size_bytes: number | null;
  video_duration_seconds: number | null;
  video_width: number | null;
  video_height: number | null;
  video_codec: string | null;
  /** Whether a transcript has actually been stored for this job. */
  has_transcript: boolean;
  /**
   * The status of the most recent highlight analysis, when there is one.
   *
   * It is `null` when no analysis has been started, which is different from
   * an analysis that is there and has failed.
   */
  analysis_status: HighlightStatus | null;
  /** How many highlight candidates the analysis produced. */
  candidate_count: number;
  /** How many clips have been made from this job. */
  clip_count: number;
  /** How many formatted (9:16) artifacts have been made from this job. */
  format_count: number;
}

export interface JobList {
  items: Job[];
  total: number;
  limit: number;
  offset: number;
}

/** What the transcript endpoint says about a job's transcript. */
export type TranscriptStatus = "ready" | "pending" | "failed";

export interface TranscriptSegment {
  index: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  no_speech_probability: number | null;
}

export interface Transcript {
  job_id: string;
  status: TranscriptStatus;
  job_status: JobStatus;
  job_stage: JobStage;
  language: string | null;
  model_name: string | null;
  audio_sample_rate: number | null;
  audio_channels: number | null;
  duration_seconds: number | null;
  created_at: string | null;
  segment_count: number;
  segments: TranscriptSegment[];
  limit: number;
  offset: number;
  error: { code: string; message: string } | null;
}

/** A status a job never leaves. */
export const TERMINAL_STATUSES: readonly JobStatus[] = [
  "completed",
  "failed",
  "cancelled",
];

export function isTerminal(status: JobStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/**
 * Whether the interface knows the status at all.
 *
 * A status the backend has introduced since this build was made is not
 * something the interface can say anything useful about, so it is treated
 * as terminal: the dashboard stops asking about it rather than polling for
 * ever over something it cannot describe.
 */
export function isKnownStatus(status: string): boolean {
  return KNOWN_JOB_STATUSES.includes(status);
}

/** An error returned by the API, with its stable machine-readable code. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function apiUrl(path: string): string {
  return `${getApiBaseUrl()}${path}`;
}

/**
 * The address of a media file.
 *
 * The API answers with a path that is **relative to itself**, which is
 * right for a client on the same origin. The frontend usually runs on a
 * different port, so the path is resolved against the API's address here —
 * in one place, so that every preview and every download works rather than
 * quietly asking the frontend for something it does not have.
 */
export function mediaUrl(path: string | null | undefined): string | null {
  if (typeof path !== "string" || path === "") {
    return null;
  }
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  return apiUrl(path.startsWith("/") ? path : `/${path}`);
}

function errorFromPayload(status: number, payload: unknown): ApiError {
  if (typeof payload === "object" && payload !== null) {
    const envelope = (payload as { error?: unknown }).error;
    if (typeof envelope === "object" && envelope !== null) {
      const { code, message } = envelope as { code?: unknown; message?: unknown };
      if (typeof code === "string" && typeof message === "string") {
        return new ApiError(status, code, message);
      }
    }
  }
  return new ApiError(status, "http_error", `The API answered with HTTP ${status}.`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers: {
        accept: "application/json",
        ...(init?.body ? { "content-type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(
      0,
      "network_error",
      "Could not reach the ClipForge API. Is the backend running?",
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    throw errorFromPayload(response.status, payload);
  }
  return payload as T;
}

/** Queue a video file for processing. */
export function submitUpload(
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<Job> {
  const form = new FormData();
  form.append("file", file);

  return new Promise<Job>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl("/api/v1/videos/upload"));
    xhr.responseType = "json";

    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      const payload = parseMaybeJson(xhr.response);
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(normalizeJob(payload));
        return;
      }
      reject(errorFromPayload(xhr.status, payload));
    };
    xhr.onerror = () => {
      reject(
        new ApiError(
          0,
          "network_error",
          "Could not reach the ClipForge API. Is the backend running?",
        ),
      );
    };
    xhr.onabort = () => {
      reject(new ApiError(0, "aborted", "The upload was cancelled."));
    };
    xhr.send(form);
  });
}

/** Queue an authorised YouTube video for processing. */
export function submitYouTube(url: string, authorized: boolean): Promise<Job> {
  return request<Job>("/api/v1/videos/youtube", {
    method: "POST",
    body: JSON.stringify({ url, authorized }),
  }).then(normalizeJob);
}

/**
 * Make a job safe to render.
 *
 * The answer comes from another process, so it is treated as untrusted: a
 * missing or malformed field becomes a safe default rather than `undefined`
 * ending up in the interface. A status the interface does not recognise is
 * **kept** rather than rewritten into one that is not true -- the interface
 * says "unknown status" instead of claiming the job is queued.
 */
function normalizeJob(raw: unknown): Job {
  const value =
    typeof raw === "object" && raw !== null
      ? (raw as Record<string, unknown>)
      : {};
  const status =
    typeof value.status === "string" && KNOWN_JOB_STATUSES.includes(value.status)
      ? value.status
      : "unknown";
  const stage =
    typeof value.stage === "string" && KNOWN_JOB_STAGES.includes(value.stage)
      ? value.stage
      : "unknown";
  return {
    id: typeof value.id === "string" ? value.id : "",
    source_type: value.source_type === "youtube" ? "youtube" : "upload",
    source_reference:
      typeof value.source_reference === "string" ? value.source_reference : "",
    status: status as JobStatus,
    progress: boundedProgress(value.progress),
    stage: stage as JobStage,
    created_at: typeof value.created_at === "string" ? value.created_at : "",
    updated_at: typeof value.updated_at === "string" ? value.updated_at : "",
    started_at: typeof value.started_at === "string" ? value.started_at : null,
    completed_at: typeof value.completed_at === "string" ? value.completed_at : null,
    attempt_count: wholeNumber(value.attempt_count),
    error: normalizeError(value.error),
    file_size_bytes: optionalNumber(value.file_size_bytes),
    video_duration_seconds: optionalNumber(value.video_duration_seconds),
    video_width: optionalNumber(value.video_width),
    video_height: optionalNumber(value.video_height),
    video_codec: typeof value.video_codec === "string" ? value.video_codec : null,
    has_transcript: value.has_transcript === true,
    analysis_status: normalizeAnalysisStatus(value.analysis_status),
    candidate_count: wholeNumber(value.candidate_count),
    clip_count: wholeNumber(value.clip_count),
    format_count: wholeNumber(value.format_count),
  };
}

/** A progress figure that is a whole percentage, whatever was sent. */
function boundedProgress(value: unknown): number {
  const number = typeof value === "number" && Number.isFinite(value) ? value : 0;
  return Math.max(0, Math.min(100, Math.round(number)));
}

/** A count that is a non-negative whole number, whatever was sent. */
function wholeNumber(value: unknown): number {
  const number = typeof value === "number" && Number.isFinite(value) ? value : 0;
  return Math.max(0, Math.round(number));
}

/** A measurement that may be absent, whatever was sent. */
function optionalNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return value;
}

/** An error envelope, or nothing. */
function normalizeError(value: unknown): Job["error"] {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const envelope = value as { code?: unknown; message?: unknown };
  if (typeof envelope.code !== "string" || typeof envelope.message !== "string") {
    return null;
  }
  return { code: envelope.code, message: envelope.message };
}

/** The analysis status, or nothing when there is no analysis. */
function normalizeAnalysisStatus(value: unknown): HighlightStatus | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    return null;
  }
  if (
    value === "none" ||
    value === "pending" ||
    value === "running" ||
    value === "completed" ||
    value === "failed"
  ) {
    return value;
  }
  return null;
}

/** Make a clip safe to render. */
function normalizeClip(raw: unknown): Clip {
  const value =
    typeof raw === "object" && raw !== null
      ? (raw as Record<string, unknown>)
      : {};
  return {
    id: typeof value.id === "string" ? value.id : "",
    job_id: typeof value.job_id === "string" ? value.job_id : "",
    analysis_id:
      typeof value.analysis_id === "string" ? value.analysis_id : null,
    candidate_id:
      typeof value.candidate_id === "string" ? value.candidate_id : null,
    status: normalizeClipStatus(value.status),
    stage: typeof value.stage === "string" ? value.stage : "queued",
    progress: boundedProgress(value.progress),
    start_seconds: optionalNumber(value.start_seconds) ?? 0,
    end_seconds: optionalNumber(value.end_seconds) ?? 0,
    duration_seconds: optionalNumber(value.duration_seconds) ?? 0,
    actual_duration_seconds: optionalNumber(value.actual_duration_seconds),
    output_size_bytes: optionalNumber(value.output_size_bytes),
    width: optionalNumber(value.width),
    height: optionalNumber(value.height),
    video_codec:
      typeof value.video_codec === "string" ? value.video_codec : null,
    audio_codec:
      typeof value.audio_codec === "string" ? value.audio_codec : null,
    attempt_count: wholeNumber(value.attempt_count),
    created_at: typeof value.created_at === "string" ? value.created_at : "",
    updated_at: typeof value.updated_at === "string" ? value.updated_at : "",
    started_at: typeof value.started_at === "string" ? value.started_at : null,
    completed_at:
      typeof value.completed_at === "string" ? value.completed_at : null,
    error: normalizeError(value.error),
    // The media route is resolved against the API's own address, so the
    // preview and the download work from the browser.
    media_url: mediaUrl(
      typeof value.media_url === "string" ? value.media_url : null,
    ),
    parent_clip_id:
      typeof value.parent_clip_id === "string" ? value.parent_clip_id : null,
    is_revision: value.is_revision === true,
    title: typeof value.title === "string" ? value.title : null,
    source_title:
      typeof value.source_title === "string" ? value.source_title : null,
    caption_edits: Array.isArray(value.caption_edits)
      ? value.caption_edits
          .map((entry) => {
            const item = (entry ?? {}) as Record<string, unknown>;
            return {
              segment_index:
                typeof item.segment_index === "number" ? item.segment_index : 0,
              text: typeof item.text === "string" ? item.text : "",
            };
          })
          .filter((entry) => entry.text !== "")
      : [],
  };
}

/** A clip status, or the one the interface does not know. */
function normalizeClipStatus(value: unknown): Clip["status"] {
  switch (value) {
    case "queued":
    case "preparing":
    case "rendering":
    case "verifying":
    case "completed":
    case "failed":
      return value;
    default:
      return "failed";
  }
}

/** Make a formatted clip safe to render. */
function normalizeClipFormat(raw: unknown): ClipFormat {
  const value =
    typeof raw === "object" && raw !== null
      ? (raw as Record<string, unknown>)
      : {};
  return {
    id: typeof value.id === "string" ? value.id : "",
    clip_id: typeof value.clip_id === "string" ? value.clip_id : "",
    job_id: typeof value.job_id === "string" ? value.job_id : "",
    status: normalizeFormatStatus(value.status),
    stage: typeof value.stage === "string" ? value.stage : "queued",
    progress: boundedProgress(value.progress),
    settings:
      typeof value.settings === "object" && value.settings !== null
        ? (value.settings as Record<string, unknown>)
        : {},
    config_fingerprint:
      typeof value.config_fingerprint === "string" ? value.config_fingerprint : "",
    output_size_bytes: optionalNumber(value.output_size_bytes),
    actual_duration_seconds: optionalNumber(value.actual_duration_seconds),
    width: optionalNumber(value.width),
    height: optionalNumber(value.height),
    video_codec:
      typeof value.video_codec === "string" ? value.video_codec : null,
    audio_codec:
      typeof value.audio_codec === "string" ? value.audio_codec : null,
    pixel_format:
      typeof value.pixel_format === "string" ? value.pixel_format : null,
    captions_enabled: value.captions_enabled === true,
    captions_applied: value.captions_applied === true,
    caption_count: wholeNumber(value.caption_count),
    attempt_count: wholeNumber(value.attempt_count),
    created_at: typeof value.created_at === "string" ? value.created_at : "",
    updated_at: typeof value.updated_at === "string" ? value.updated_at : "",
    started_at: typeof value.started_at === "string" ? value.started_at : null,
    completed_at:
      typeof value.completed_at === "string" ? value.completed_at : null,
    error: normalizeError(value.error),
    media_url: mediaUrl(
      typeof value.media_url === "string" ? value.media_url : null,
    ),
  };
}

/** A format status, or the one the interface does not know. */
function normalizeFormatStatus(value: unknown): ClipFormat["status"] {
  switch (value) {
    case "queued":
    case "preparing":
    case "rendering":
    case "verifying":
    case "completed":
    case "failed":
      return value;
    default:
      return "failed";
  }
}

/** Fetch the most recent jobs, newest first. */
export function listJobs(limit = 50): Promise<JobList> {
  return request<JobList>(`/api/v1/jobs?limit=${limit}`).then((payload) => ({
    items: items<Job>(payload)
      .map(normalizeJob)
      // An entry with no id cannot be addressed, so it is not a job the
      // interface can offer.
      .filter((job) => job.id !== ""),
    total: typeof payload?.total === "number" ? payload.total : 0,
    limit: typeof payload?.limit === "number" ? payload.limit : limit,
    offset: typeof payload?.offset === "number" ? payload.offset : 0,
  }));
}

/** Fetch one job. */
export function getJob(id: string): Promise<Job> {
  return request<Job>(`/api/v1/jobs/${encodeURIComponent(id)}`).then(normalizeJob);
}

/**
 * Cancel a job that has not finished.
 *
 * A job that has already finished cannot be cancelled, and the server says
 * so. Cancelling an already cancelled job is harmless.
 */
export async function cancelJob(id: string): Promise<Job> {
  const payload = await request<{ job: Job }>(
    `/api/v1/jobs/${encodeURIComponent(id)}/cancel`,
    { method: "POST" },
  );
  return normalizeJob(payload?.job);
}

/**
 * Delete a finished job and everything it produced, on the server and on
 * disk. Only a terminal job (completed, failed, cancelled) can be deleted;
 * anything else is refused with `job_not_deletable`.
 */
export async function deleteJob(id: string): Promise<void> {
  await request<void>(`/api/v1/jobs/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/**
 * Fetch the transcript of a job.
 *
 * The endpoint answers with the same shape whatever state the job is in:
 * `pending` while the job is still being worked on, `ready` once the
 * transcript is stored, and an error with a stable code when the job failed.
 *
 * The answer is normalised: a transcript that arrives without its segments
 * is treated as a transcript with none, rather than as something that
 * breaks the panel that shows it.
 */
export function getTranscript(
  id: string,
  options?: { limit?: number; offset?: number },
): Promise<Transcript> {
  const limit = options?.limit ?? 500;
  const offset = options?.offset ?? 0;
  return request<Transcript>(
    `/api/v1/jobs/${encodeURIComponent(id)}/transcript?limit=${limit}&offset=${offset}`,
  ).then(normalizeTranscript);
}

/** Page size used when walking a long transcript. */
const TRANSCRIPT_PAGE_SIZE = 500;

/** Hard cap on transcript pages, so a nonsensical answer cannot loop. */
const TRANSCRIPT_MAX_PAGES = 200;

/**
 * Fetch a whole transcript, following the API's pagination.
 *
 * The transcript endpoint pages long transcripts (`segment_count` is the
 * total; `offset` asks for the next page). A reader that takes one page
 * shows a transcript that quietly stops part-way through a long video.
 * This walks the pages until the reported total has been read and merges
 * them into one normalised transcript.
 */
export async function getFullTranscript(id: string): Promise<Transcript> {
  let first: Transcript | null = null;
  const segments: Transcript["segments"] = [];
  const seenIndexes = new Set<number>();
  let offset = 0;
  for (let page = 0; page < TRANSCRIPT_MAX_PAGES; page += 1) {
    const transcript = await getTranscript(id, {
      limit: TRANSCRIPT_PAGE_SIZE,
      offset,
    });
    if (first === null) {
      first = transcript;
    }
    // Segments are deduplicated by their index, so a page that repeats what
    // an earlier page already delivered (an offset-ignoring answer) cannot
    // show one segment twice.
    let added = 0;
    for (const segment of transcript.segments) {
      if (!seenIndexes.has(segment.index)) {
        seenIndexes.add(segment.index);
        segments.push(segment);
        added += 1;
      }
    }
    if (added === 0 || segments.length >= transcript.segment_count) {
      break;
    }
    offset += transcript.segments.length;
  }
  if (first === null) {
    // The loop always runs at least once, so this is unreachable.
    throw new ApiError(0, "unknown", "The transcript could not be loaded.");
  }
  return {
    ...first,
    segments,
    segment_count: Math.max(first.segment_count, segments.length),
    limit: TRANSCRIPT_PAGE_SIZE,
    offset: 0,
  };
}

/** Make a transcript safe to render. */
function normalizeTranscript(raw: unknown): Transcript {
  const value =
    typeof raw === "object" && raw !== null
      ? (raw as Record<string, unknown>)
      : {};
  const rawSegments = value.segments;
  const segments = (
    Array.isArray(rawSegments) ? rawSegments : []
  ).map((segment) => {
    const entry = (typeof segment === "object" && segment !== null
      ? segment
      : {}) as Record<string, unknown>;
    return {
      index: typeof entry.index === "number" ? entry.index : 0,
      start_seconds:
        typeof entry.start_seconds === "number" ? entry.start_seconds : 0,
      end_seconds: typeof entry.end_seconds === "number" ? entry.end_seconds : 0,
      text: typeof entry.text === "string" ? entry.text : "",
      no_speech_probability:
        typeof entry.no_speech_probability === "number"
          ? entry.no_speech_probability
          : null,
    };
  });
  const status: TranscriptStatus =
    value.status === "ready"
      ? "ready"
      : value.status === "failed"
        ? "failed"
        : "pending";
  return {
    job_id: typeof value.job_id === "string" ? value.job_id : "",
    status,
    job_status:
      typeof value.job_status === "string"
        ? (value.job_status as JobStatus)
        : "queued",
    job_stage:
      typeof value.job_stage === "string"
        ? (value.job_stage as JobStage)
        : "queued",
    language: typeof value.language === "string" ? value.language : null,
    model_name: typeof value.model_name === "string" ? value.model_name : null,
    audio_sample_rate:
      typeof value.audio_sample_rate === "number"
        ? value.audio_sample_rate
        : null,
    audio_channels:
      typeof value.audio_channels === "number" ? value.audio_channels : null,
    duration_seconds:
      typeof value.duration_seconds === "number" ? value.duration_seconds : null,
    created_at: typeof value.created_at === "string" ? value.created_at : null,
    segment_count:
      typeof value.segment_count === "number" ? value.segment_count : segments.length,
    segments,
    limit: typeof value.limit === "number" ? value.limit : 500,
    offset: typeof value.offset === "number" ? value.offset : 0,
    error: normalizeError(value.error),
  };
}

/** A highlight candidate moment. */
export interface HighlightCandidate {
  id: string;
  position: number;
  start_segment_index: number;
  end_segment_index: number;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  title: string;
  reason: string;
  moment_type: string | null;
  /**
   * The model's own ranking. It is not a calibrated probability, and the
   * interface does not pretend it is one.
   */
  score: number | null;
  decision: "proposed" | "accepted" | "rejected";
}

export type HighlightStatus = "none" | "pending" | "running" | "completed" | "failed";

export interface HighlightAnalysis {
  job_id: string;
  analysis_id: string | null;
  status: HighlightStatus;
  provider_name: string | null;
  model_id: string | null;
  model_name: string | null;
  prompt_version: string | null;
  candidate_count: number;
  candidates: HighlightCandidate[];
  error: { code: string; message: string } | null;
  /** What the provider receives when an analysis runs. */
  data_notice: string;
}

// --- integrations -----------------------------------------------------------

export type ApiProtocol =
  | "openai_chat_completions"
  | "openai_responses"
  | "anthropic_messages"
  | "google_generate_content"
  | "system_one";

export type PricingStatus = "unknown" | "free" | "paid" | "mixed";

export type StructuredOutputMode = "none" | "json_object" | "json_schema";

export type ConnectionStatus =
  | "unknown"
  | "ok"
  | "auth_failed"
  | "model_unavailable"
  | "protocol_mismatch"
  | "rate_limited"
  | "timeout"
  | "provider_error"
  | "unsupported"
  | "invalid_configuration";

export interface ProviderPreset {
  id: string;
  name: string;
  summary: string;
  default_protocol: ApiProtocol;
  base_urls: Record<string, string>;
  models_url: string | null;
  supports_model_discovery: boolean;
  free_tier: string;
  free_tier_note: string;
  docs_url: string;
}

export interface Integration {
  id: string;
  name: string;
  provider_id: string;
  provider_name: string;
  protocol: ApiProtocol;
  protocol_supported: boolean;
  base_url: string;
  model_id: string | null;
  model_name: string | null;
  context_window: number | null;
  max_output_tokens: number | null;
  reasoning_effort: string | null;
  reasoning_effort_values: string[];
  structured_output: StructuredOutputMode;
  pricing_status: PricingStatus;
  local_endpoint: boolean;
  enabled: boolean;
  api_key_configured: boolean;
  is_default: boolean;
  role: string;
  last_tested_at: string | null;
  last_test_status: ConnectionStatus;
  last_test_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelDescriptor {
  id: string;
  name: string | null;
  context_window: number | null;
  max_output_tokens: number | null;
  structured_output: StructuredOutputMode;
  available: boolean | null;
  protocol: ApiProtocol | null;
}

export interface ConnectionTestResult {
  status: ConnectionStatus;
  code: string;
  message: string;
  model: string | null;
}

/** The provider presets a configuration can start from. */
export async function listProviderPresets(): Promise<ProviderPreset[]> {
  const payload = await request<{ items: ProviderPreset[] }>(
    "/api/v1/integrations/presets",
  );
  return payload.items;
}

/** The saved configurations. */
export async function listIntegrations(): Promise<Integration[]> {
  const payload = await request<{ items: Integration[] }>("/api/v1/integrations");
  return payload.items;
}

export interface IntegrationInput {
  name: string;
  provider_id: string;
  protocol?: ApiProtocol;
  base_url?: string;
  model_id?: string | null;
  model_name?: string | null;
  context_window?: number | null;
  max_output_tokens?: number | null;
  reasoning_effort?: string | null;
  structured_output?: StructuredOutputMode;
  pricing_status?: PricingStatus;
  local_endpoint?: boolean;
  enabled?: boolean;
  is_default?: boolean;
  api_key?: string;
}

export async function createIntegration(
  input: IntegrationInput,
): Promise<Integration> {
  return request<Integration>("/api/v1/integrations", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function updateIntegration(
  id: string,
  input: Partial<Omit<IntegrationInput, "api_key">>,
): Promise<Integration> {
  return request<Integration>(`/api/v1/integrations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

export async function deleteIntegration(id: string): Promise<void> {
  await request<void>(`/api/v1/integrations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function selectIntegration(id: string): Promise<Integration> {
  return request<Integration>(
    `/api/v1/integrations/${encodeURIComponent(id)}/select`,
    { method: "POST" },
  );
}

export async function testIntegration(
  id: string,
): Promise<ConnectionTestResult> {
  return request<ConnectionTestResult>(
    `/api/v1/integrations/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  );
}

export async function fetchModels(
  id: string,
): Promise<{ items: ModelDescriptor[]; source_url: string; note: string }> {
  return request<{ items: ModelDescriptor[]; source_url: string; note: string }>(
    `/api/v1/integrations/${encodeURIComponent(id)}/models`,
  );
}

export async function replaceApiKey(
  id: string,
  apiKey: string,
): Promise<Integration> {
  return request<Integration>(
    `/api/v1/integrations/${encodeURIComponent(id)}/key`,
    { method: "POST", body: JSON.stringify({ api_key: apiKey }) },
  );
}

export async function removeApiKey(id: string): Promise<Integration> {
  return request<Integration>(
    `/api/v1/integrations/${encodeURIComponent(id)}/key`,
    { method: "DELETE" },
  );
}

// --- highlights -------------------------------------------------------------

export async function startHighlightAnalysis(
  jobId: string,
): Promise<HighlightAnalysis> {
  return request<HighlightAnalysis>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/highlights`,
    { method: "POST" },
  );
}

export async function getHighlightAnalysis(
  jobId: string,
): Promise<HighlightAnalysis> {
  return request<HighlightAnalysis>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/highlights`,
  );
}

export async function setCandidateDecision(
  jobId: string,
  candidateId: string,
  decision: "proposed" | "accepted" | "rejected",
): Promise<HighlightAnalysis> {
  return request<HighlightAnalysis>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/highlights/${encodeURIComponent(candidateId)}`,
    { method: "PATCH", body: JSON.stringify({ decision }) },
  );
}

// --- clips ------------------------------------------------------------------

export interface Clip {
  id: string;
  job_id: string;
  analysis_id: string | null;
  candidate_id: string | null;
  status: "queued" | "preparing" | "rendering" | "verifying" | "completed" | "failed";
  stage: string;
  progress: number;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  actual_duration_seconds: number | null;
  output_size_bytes: number | null;
  width: number | null;
  height: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: { code: string; message: string } | null;
  /** Where to play or download the clip, when there is one. */
  media_url: string | null;
  /** The clip this one was trimmed from, when it is a revision. */
  parent_clip_id: string | null;
  /** Whether this clip was trimmed by hand rather than chosen by the AI. */
  is_revision: boolean;
  /** The title the clip is shown with. */
  title: string | null;
  /** The title the AI gave the moment this clip came from. */
  source_title: string | null;
  /** The caption text the user has changed, by transcript segment index. */
  caption_edits: CaptionEdit[];
}

/** A caption the user has retyped. */
export interface CaptionEdit {
  segment_index: number;
  text: string;
}

/** The stages a clip can be on, and what each one means. */
export const CLIP_STAGE_LABELS: Record<string, string> = {
  queued: "Queued",
  preparing: "Preparing",
  rendering: "Rendering",
  verifying: "Verifying",
  completed: "Ready",
  failed: "Failed",
};

/** Request a clip for a highlight candidate. */
export async function requestClip(
  jobId: string,
  candidateId: string,
): Promise<Clip> {
  return request<Clip>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/clips`,
    { method: "POST", body: JSON.stringify({ candidate_id: candidateId }) },
  ).then(normalizeClip);
}

/**
 * The items of a list response.
 *
 * A list endpoint is supposed to answer with `{items: [...]}`, but the
 * answer is only as good as the server that produced it. Returning an empty
 * list for anything else means a caller can always iterate the result, and
 * a surprising response cannot crash the interface.
 */
function items<T>(payload: unknown): T[] {
  const list = (payload as { items?: unknown } | null)?.items;
  return Array.isArray(list) ? (list as T[]) : [];
}

/** The clips of a job. */
export async function listClips(jobId: string): Promise<Clip[]> {
  const payload = await request<{ items: Clip[] }>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/clips`,
  );
  return items<Clip>(payload).map(normalizeClip);
}

/** One clip. */
export async function getClip(clipId: string): Promise<Clip> {
  return request<Clip>(`/api/v1/clips/${encodeURIComponent(clipId)}`)
  .then(normalizeClip);
}

/**
 * Trim a clip.
 *
 * The result is a **revision**: a new clip with its own file. The clip
 * that was trimmed keeps its own file and its own boundaries.
 *
 * The boundaries are on the source media timeline, the same timeline the
 * clip already uses, and the server checks them against the source's real
 * duration.
 */
export async function createClipRevision(
  clipId: string,
  options: { startSeconds: number; endSeconds: number; title?: string | null },
): Promise<Clip> {
  return request<Clip>(
    `/api/v1/clips/${encodeURIComponent(clipId)}/revisions`,
    {
      method: "POST",
      body: JSON.stringify({
        start_seconds: options.startSeconds,
        end_seconds: options.endSeconds,
        ...(options.title === undefined ? {} : { title: options.title }),
      }),
    },
  ).then(normalizeClip);
}

/** Relabel a clip. A label is not a re-render. */
export async function renameClip(
  clipId: string,
  title: string | null,
): Promise<Clip> {
  return request<Clip>(`/api/v1/clips/${encodeURIComponent(clipId)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  }).then(normalizeClip);
}

/** Replace the caption text the user has changed on a clip. */
export async function setClipCaptionEdits(
  clipId: string,
  edits: CaptionEdit[],
): Promise<CaptionEdit[]> {
  const payload = await request<{ edits: CaptionEdit[] }>(
    `/api/v1/clips/${encodeURIComponent(clipId)}/captions`,
    { method: "PUT", body: JSON.stringify({ edits }) },
  );
  return Array.isArray(payload?.edits) ? payload.edits : [];
}

/** The address of the ZIP of the chosen clips. */
export function exportArchiveUrl(jobId: string): string {
  return `${getApiBaseUrl()}/api/v1/jobs/${encodeURIComponent(jobId)}/exports`;
}

/**
 * Download a ZIP of the chosen clips.
 *
 * The archive is built for the request and is not kept, so there is no
 * stale copy and no second storage scheme to look after.
 */
export async function exportClips(
  jobId: string,
  clipIds: string[],
): Promise<Blob> {
  const response = await fetch(exportArchiveUrl(jobId), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ clip_ids: clipIds }),
  });
  if (!response.ok) {
    let message = `The export failed with HTTP ${response.status}.`;
    try {
      const payload = await response.json();
      const envelope = (payload as { error?: { message?: unknown } }).error;
      if (envelope && typeof envelope.message === "string") {
        message = envelope.message;
      }
    } catch {
      // The body was not JSON. The generic message stands.
    }
    throw new ApiError(response.status, "export_failed", message);
  }
  return response.blob();
}

/** Render a failed clip again. */
export async function retryClip(clipId: string): Promise<Clip> {
  const payload = await request<{ clip: Clip }>(
    `/api/v1/clips/${encodeURIComponent(clipId)}/retry`,
    { method: "POST" },
  );
  return normalizeClip(payload.clip);
}

// --- formatted clips --------------------------------------------------------

export type LayoutMode = "center_crop" | "fit_pad";

export interface CaptionSettings {
  enabled: boolean;
  font_family: string;
  font_size: number;
  text_colour: string;
  outline_colour: string;
  outline_thickness: number;
  background_colour: string | null;
  shadow: number;
  alignment: number;
  margin_vertical: number;
  max_characters_per_line: number;
  max_lines: number;
}

export interface FormatSettings {
  width: number;
  height: number;
  layout: LayoutMode;
  padding_colour: string;
  captions: CaptionSettings;
}

export interface ClipFormat {
  id: string;
  clip_id: string;
  job_id: string;
  status: "queued" | "preparing" | "rendering" | "verifying" | "completed" | "failed";
  stage: string;
  progress: number;
  settings: Record<string, unknown>;
  config_fingerprint: string;
  output_size_bytes: number | null;
  actual_duration_seconds: number | null;
  width: number | null;
  height: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  pixel_format: string | null;
  captions_enabled: boolean;
  captions_applied: boolean;
  caption_count: number;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: { code: string; message: string } | null;
  media_url: string | null;
}

/** The resolutions the format form offers. */
export const FORMAT_RESOLUTIONS: { label: string; width: number; height: number }[] = [
  { label: "720 × 1280", width: 720, height: 1280 },
  { label: "1080 × 1920", width: 1080, height: 1920 },
];

export const DEFAULT_CAPTION_SETTINGS: CaptionSettings = {
  enabled: true,
  font_family: "Arial",
  font_size: 42,
  text_colour: "#FFFFFF",
  outline_colour: "#000000",
  outline_thickness: 3,
  background_colour: null,
  shadow: 0,
  alignment: 2,
  margin_vertical: 90,
  max_characters_per_line: 28,
  max_lines: 3,
};

export const DEFAULT_FORMAT_SETTINGS: FormatSettings = {
  width: 720,
  height: 1280,
  layout: "center_crop",
  padding_colour: "#000000",
  captions: DEFAULT_CAPTION_SETTINGS,
};

/** Request a formatted clip for a Phase 5 clip. */
export async function requestFormat(
  clipId: string,
  settings: FormatSettings,
): Promise<ClipFormat> {
  return request<ClipFormat>(
    `/api/v1/clips/${encodeURIComponent(clipId)}/formats`,
    { method: "POST", body: JSON.stringify({ settings }) },
  ).then(normalizeClipFormat);
}

/** The formatted clips of a clip. */
export async function listFormats(clipId: string): Promise<ClipFormat[]> {
  const payload = await request<{ items: ClipFormat[] }>(
    `/api/v1/clips/${encodeURIComponent(clipId)}/formats`,
  );
  return items<ClipFormat>(payload).map(normalizeClipFormat);
}

/** One formatted clip. */
export async function getFormat(formatId: string): Promise<ClipFormat> {
  return request<ClipFormat>(`/api/v1/formats/${encodeURIComponent(formatId)}`)
  .then(normalizeClipFormat);;
}

/** Render a failed formatted clip again. */
export async function retryFormat(formatId: string): Promise<ClipFormat> {
  const payload = await request<{ format: ClipFormat }>(
    `/api/v1/formats/${encodeURIComponent(formatId)}/retry`,
    { method: "POST" },
  );
  return normalizeClipFormat(payload.format);
}

/** The fonts this machine has. */
export async function listFonts(): Promise<string[]> {
  const payload = await request<{ items: string[] }>("/api/v1/fonts");
  return items<string>(payload);
}

// --- service status ---------------------------------------------------------

/** What the worker last said about itself. */
export interface WorkerStatus {
  running: boolean;
  worker_id: string | null;
  last_seen: string | null;
  seconds_since_seen: number | null;
}

/** The readiness contract the backend reports. */
export interface Readiness {
  status: "ready" | "not_ready";
  service: string;
  version: string;
  config: "loaded" | "invalid";
  /** Which capabilities this build has, by name. */
  capabilities: Record<string, boolean>;
  /** Whether the local speech model is already on this machine. */
  transcription_model_cached: boolean;
  /** Whether the background worker is running. */
  worker: WorkerStatus;
}

/**
 * Fetch the readiness report.
 *
 * It says whether the configuration could be loaded, what this build can
 * do, whether the speech model is already cached, and whether the
 * background worker is running. The answer is normalised: a capability the
 * backend did not mention is reported as unavailable rather than as
 * working.
 */
export async function getReadiness(): Promise<Readiness> {
  const payload = await request<Record<string, unknown>>("/ready");
  const raw = (payload ?? {}) as Record<string, unknown>;
  const capabilities: Record<string, boolean> = {};
  const rawCapabilities = raw.capabilities;
  if (typeof rawCapabilities === "object" && rawCapabilities !== null) {
    for (const [name, value] of Object.entries(rawCapabilities)) {
      capabilities[name] = value === true;
    }
  }
  const rawWorker =
    typeof raw.worker === "object" && raw.worker !== null
      ? (raw.worker as Record<string, unknown>)
      : {};
  return {
    status: raw.status === "not_ready" ? "not_ready" : "ready",
    service: typeof raw.service === "string" ? raw.service : "clipforge-api",
    version: typeof raw.version === "string" ? raw.version : "",
    config: raw.config === "invalid" ? "invalid" : "loaded",
    capabilities,
    transcription_model_cached: raw.transcription_model_cached === true,
    worker: {
      running: rawWorker.running === true,
      worker_id:
        typeof rawWorker.worker_id === "string" ? rawWorker.worker_id : null,
      last_seen:
        typeof rawWorker.last_seen === "string" ? rawWorker.last_seen : null,
      seconds_since_seen:
        typeof rawWorker.seconds_since_seen === "number"
          ? rawWorker.seconds_since_seen
          : null,
    },
  };
}

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") {
    return value;
  }
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}
