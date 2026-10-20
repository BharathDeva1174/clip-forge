import { describe, expect, it, vi } from "vitest";

import { ApiError, isTerminal, listJobs, submitUpload, submitYouTube } from "@/lib/api";

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
