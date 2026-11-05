import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  getJob,
  getReadiness,
  getTranscript,
  getFullTranscript,
  isKnownStatus,
  isTerminal,
  listJobs,
  submitUpload,
  submitYouTube,
} from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const SAMPLE_JOB = {
  id: "a".repeat(32),
  source_type: "upload" as const,
  source_reference: "clip.mp4",
  status: "queued" as const,
  progress: 0,
  stage: "queued" as const,
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:00:00+00:00",
  started_at: null,
  completed_at: null,
  attempt_count: 0,
  error: null,
  file_size_bytes: 1024,
  video_duration_seconds: null,
  video_width: null,
  video_height: null,
  video_codec: null,
  has_transcript: false,
  analysis_status: null,
  candidate_count: 0,
  clip_count: 0,
  format_count: 0,
};

describe("submitYouTube", () => {
  it("sends the url and the authorisation confirmation as JSON", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(SAMPLE_JOB, 202));
    vi.stubGlobal("fetch", fetchMock);

    const job = await submitYouTube("https://youtu.be/dQw4w9WgXcQ", true);

    expect(job.id).toBe(SAMPLE_JOB.id);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/v1/videos/youtube");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      url: "https://youtu.be/dQw4w9WgXcQ",
      authorized: true,
    });
  });

  it("reports the API error code and message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              status: 400,
              code: "invalid_youtube_url",
              message: "Only YouTube URLs are accepted.",
            },
          },
          400,
        ),
      ),
    );

    await expect(submitYouTube("https://example.com/x", true)).rejects.toMatchObject({
      status: 400,
      code: "invalid_youtube_url",
      message: "Only YouTube URLs are accepted.",
    });
  });

  it("reports a network failure clearly", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    await expect(submitYouTube("https://youtu.be/dQw4w9WgXcQ", true)).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});

describe("listJobs", () => {
  it("asks for the job list", async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () =>
        jsonResponse({ items: [SAMPLE_JOB], total: 1, limit: 50, offset: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listJobs();

    expect(result.items).toHaveLength(1);
    expect(result.total).toBe(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/v1/jobs?limit=50");
  });

  it("falls back to a generic error for a non-JSON answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("not json", { status: 500 })),
    );

    await expect(listJobs()).rejects.toMatchObject({
      status: 500,
      code: "http_error",
    });
  });
});

describe("submitUpload", () => {
  function installFakeXhr() {
    const instances: FakeXhr[] = [];

    class FakeXhr {
      method = "";
      url = "";
      responseType = "";
      response: unknown = null;
      status = 202;
      body: unknown = null;
      upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;

      constructor() {
        instances.push(this);
      }
      open(method: string, url: string) {
        this.method = method;
        this.url = url;
      }
      send(body: unknown) {
        this.body = body;
      }
    }

    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    return instances;
  }

  it("sends the file as multipart form data", async () => {
    const instances = installFakeXhr();

    const promise = submitUpload(new File(["data"], "clip.mp4"));
    const xhr = instances[0];

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toContain("/api/v1/videos/upload");
    expect(xhr.body).toBeInstanceOf(FormData);
    expect((xhr.body as FormData).get("file")).toBeInstanceOf(File);

    xhr.response = SAMPLE_JOB;
    xhr.onload?.();

    await expect(promise).resolves.toMatchObject({ id: SAMPLE_JOB.id });
  });

  it("reports a rejected upload with its error code", async () => {
    const instances = installFakeXhr();

    const promise = submitUpload(new File(["data"], "clip.mp4"));
    const xhr = instances[0];
    xhr.status = 415;
    xhr.response = {
      error: {
        status: 415,
        code: "unsupported_media_type",
        message: "That file type is not supported.",
      },
    };
    xhr.onload?.();

    await expect(promise).rejects.toMatchObject({
      status: 415,
      code: "unsupported_media_type",
    });
  });

  it("reports a network failure", async () => {
    const instances = installFakeXhr();

    const promise = submitUpload(new File(["data"], "clip.mp4"));
    instances[0].onerror?.();

    await expect(promise).rejects.toMatchObject({ code: "network_error" });
  });

  it("reports upload progress", async () => {
    const instances = installFakeXhr();
    const fractions: number[] = [];

    const promise = submitUpload(new File(["data"], "clip.mp4"), (fraction) =>
      fractions.push(fraction),
    );
    instances[0].upload.onprogress?.({
      lengthComputable: true,
      loaded: 50,
      total: 100,
    } as ProgressEvent);
    instances[0].response = SAMPLE_JOB;
    instances[0].onload?.();

    await promise;
    expect(fractions).toEqual([0.5]);
  });
});

describe("isTerminal", () => {
  it("knows which statuses are terminal", () => {
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("queued")).toBe(false);
    expect(isTerminal("inspecting")).toBe(false);
  });
});

describe("isKnownStatus", () => {
  it("knows the statuses the backend defines", () => {
    expect(isKnownStatus("completed")).toBe(true);
    expect(isKnownStatus("transcribing")).toBe(true);
    expect(isKnownStatus("unknown")).toBe(false);
    expect(isKnownStatus("something-new")).toBe(false);
  });
});

describe("the job list is normalised", () => {
  it("keeps the counts the backend reported", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        items: [
          {
            ...SAMPLE_JOB,
            status: "completed",
            has_transcript: true,
            analysis_status: "completed",
            candidate_count: 3,
            clip_count: 2,
            format_count: 1,
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listJobs();

    expect(result.items[0].candidate_count).toBe(3);
    expect(result.items[0].clip_count).toBe(2);
    expect(result.items[0].format_count).toBe(1);
    expect(result.items[0].analysis_status).toBe("completed");
    expect(result.items[0].has_transcript).toBe(true);
  });

  it("does not crash on a malformed entry", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        items: [{ id: "a".repeat(32), nonsense: true }],
        total: 1,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listJobs();

    expect(result.items).toHaveLength(1);
    // The status was not sent, so the interface does not invent one.
    expect(result.items[0].status).toBe("unknown");
    expect(result.items[0].progress).toBe(0);
    expect(result.items[0].has_transcript).toBe(false);
    expect(result.items[0].clip_count).toBe(0);
  });

  it("drops an entry that cannot be addressed", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ items: [{ status: "queued" }], total: 1 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listJobs();

    expect(result.items).toHaveLength(0);
  });

  it("copes with an answer that is not a list", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ items: "nope" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await listJobs();

    expect(result.items).toEqual([]);
    expect(result.total).toBe(0);
  });
});

describe("getJob is normalised", () => {
  it("keeps a progress figure inside its bounds", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ ...SAMPLE_JOB, progress: 9000 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const job = await getJob(SAMPLE_JOB.id);

    expect(job.progress).toBe(100);
  });

  it("does not invent a transcript", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...SAMPLE_JOB }));
    vi.stubGlobal("fetch", fetchMock);

    const job = await getJob(SAMPLE_JOB.id);

    expect(job.has_transcript).toBe(false);
    expect(job.analysis_status).toBeNull();
  });

  it("keeps a well-formed error envelope", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        ...SAMPLE_JOB,
        status: "failed",
        error: { code: "invalid_media", message: "No video stream." },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const job = await getJob(SAMPLE_JOB.id);

    expect(job.error?.code).toBe("invalid_media");
  });

  it("ignores a malformed error envelope", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ ...SAMPLE_JOB, error: { code: 42 } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const job = await getJob(SAMPLE_JOB.id);

    expect(job.error).toBeNull();
  });
});

describe("getTranscript is normalised", () => {
  it("copes with an answer that has no segments", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ job_id: "a".repeat(32), status: "ready" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const transcript = await getTranscript("a".repeat(32));

    expect(transcript.segments).toEqual([]);
    expect(transcript.status).toBe("ready");
    expect(transcript.segment_count).toBe(0);
  });

  it("keeps the segments it was given", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        job_id: "a".repeat(32),
        status: "ready",
        segment_count: 1,
        segments: [
          {
            index: 0,
            start_seconds: 0,
            end_seconds: 1.5,
            text: "hello world",
            no_speech_probability: 0.01,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const transcript = await getTranscript("a".repeat(32));

    expect(transcript.segments).toHaveLength(1);
    expect(transcript.segments[0].text).toBe("hello world");
  });
});

describe("getFullTranscript walks the pagination", () => {
  it("fetches the pages until the reported total has been read", async () => {
    const segment = (index: number) => ({
      index,
      start_seconds: index,
      end_seconds: index + 1,
      text: `segment ${index}`,
      no_speech_probability: 0.01,
    });
    const fetchMock = vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.includes("offset=500")) {
        return jsonResponse({
          job_id: "a".repeat(32),
          status: "ready",
          segment_count: 502,
          limit: 500,
          offset: 500,
          segments: [segment(500), segment(501)],
        });
      }
      return jsonResponse({
        job_id: "a".repeat(32),
        status: "ready",
        segment_count: 502,
        limit: 500,
        offset: 0,
        segments: Array.from({ length: 500 }, (_, i) => segment(i)),
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const transcript = await getFullTranscript("a".repeat(32));

    expect(transcript.segments).toHaveLength(502);
    expect(transcript.segments[501].text).toBe("segment 501");
    expect(transcript.segment_count).toBe(502);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops after one page when the answer is complete already", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        job_id: "a".repeat(32),
        status: "ready",
        segment_count: 1,
        segments: [
          {
            index: 0,
            start_seconds: 0,
            end_seconds: 1.5,
            text: "hello world",
            no_speech_probability: 0.01,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const transcript = await getFullTranscript("a".repeat(32));

    expect(transcript.segments).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not loop for ever when segment_count overstates the transcript", async () => {
    const emptyPage = jsonResponse({
      job_id: "a".repeat(32),
      status: "ready",
      segment_count: 5000,
      limit: 500,
      offset: 0,
      segments: [],
    });
    const fetchMock = vi.fn(async () => emptyPage);
    vi.stubGlobal("fetch", fetchMock);

    const transcript = await getFullTranscript("a".repeat(32));

    expect(transcript.segments).toHaveLength(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not duplicate a segment when the server ignores the offset", async () => {
    const samePage = () =>
      jsonResponse({
        job_id: "a".repeat(32),
        status: "ready",
        segment_count: 2,
        limit: 500,
        offset: 0,
        segments: [
          {
            index: 0,
            start_seconds: 0,
            end_seconds: 1.5,
            text: "hello world",
            no_speech_probability: 0.01,
          },
        ],
      });
    const fetchMock = vi.fn(async () => samePage());
    vi.stubGlobal("fetch", fetchMock);

    const transcript = await getFullTranscript("a".repeat(32));

    expect(transcript.segments).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("getReadiness is normalised", () => {
  it("reads the capabilities", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        status: "ready",
        service: "clipforge-api",
        version: "1.0.0",
        config: "loaded",
        capabilities: { video_ingestion: true, clip_rendering: false },
        transcription_model_cached: true,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const readiness = await getReadiness();

    expect(readiness.status).toBe("ready");
    expect(readiness.capabilities.video_ingestion).toBe(true);
    expect(readiness.capabilities.clip_rendering).toBe(false);
    expect(readiness.transcription_model_cached).toBe(true);
  });

  it("does not claim a capability the backend did not mention", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ status: "ready", capabilities: { video_ingestion: true } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const readiness = await getReadiness();

    expect(readiness.capabilities.video_ingestion).toBe(true);
    expect(Object.keys(readiness.capabilities)).toHaveLength(1);
    expect(readiness.config).toBe("loaded");
  });
});
