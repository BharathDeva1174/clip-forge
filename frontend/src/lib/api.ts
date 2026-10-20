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
  | "completed"
  | "failed"
  | "cancelled";

export type JobStage =
  | "queued"
  | "validating"
  | "downloading"
  | "inspecting"
  | "ready"
  | "failed"
  | "cancelled";

export type SourceType = "upload" | "youtube";

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
}

export interface JobList {
  items: Job[];
  total: number;
  limit: number;
  offset: number;
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
        resolve(payload as Job);
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
  });
}

/** Fetch the most recent jobs, newest first. */
export function listJobs(limit = 50): Promise<JobList> {
  return request<JobList>(`/api/v1/jobs?limit=${limit}`);
}

/** Fetch one job. */
export function getJob(id: string): Promise<Job> {
  return request<Job>(`/api/v1/jobs/${encodeURIComponent(id)}`);
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
