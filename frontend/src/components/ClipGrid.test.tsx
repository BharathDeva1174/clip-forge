import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ClipGrid from "@/components/ClipGrid";
import MediaPreview from "@/components/MediaPreview";

const JOB_ID = "a".repeat(32);
const CLIP_ID = "k".repeat(32);
const FORMAT_ID = "f".repeat(32);

const CLIP = {
  id: CLIP_ID,
  job_id: JOB_ID,
  analysis_id: null,
  candidate_id: "c".repeat(32),
  status: "completed" as const,
  stage: "completed",
  progress: 100,
  start_seconds: 2,
  end_seconds: 8,
  duration_seconds: 6,
  actual_duration_seconds: 6.02,
  output_size_bytes: 123456,
  width: 320,
  height: 240,
  video_codec: "h264",
  audio_codec: "aac",
  attempt_count: 1,
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:05:00+00:00",
  started_at: "2026-10-20T10:00:10+00:00",
  completed_at: "2026-10-20T10:05:00+00:00",
  error: null,
  media_url: `http://localhost:8000/api/v1/clips/${CLIP_ID}/media`,
};

const FORMAT = {
  id: FORMAT_ID,
  clip_id: CLIP_ID,
  job_id: JOB_ID,
  status: "completed" as const,
  stage: "completed",
  progress: 100,
  settings: {},
  config_fingerprint: "abc",
  output_size_bytes: 204800,
  actual_duration_seconds: 6.02,
  width: 720,
  height: 1280,
  video_codec: "h264",
  audio_codec: "aac",
  pixel_format: "yuv420p",
  captions_enabled: true,
  captions_applied: true,
  caption_count: 2,
  attempt_count: 1,
  created_at: "2026-10-20T10:10:00+00:00",
  updated_at: "2026-10-20T10:12:00+00:00",
  started_at: null,
  completed_at: "2026-10-20T10:12:00+00:00",
  error: null,
  media_url: `http://localhost:8000/api/v1/formats/${FORMAT_ID}/media`,
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(
  answers: {
    clips?: () => Response;
    formats?: () => Response;
    export?: () => Response;
  } = {},
) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/formats")) {
      return answers.formats
        ? answers.formats()
        : json({ items: [FORMAT], total: 1, limit: 50, offset: 0 });
    }
    if (url.includes("/exports")) {
      return answers.export
        ? answers.export()
        : new Response(new Blob(["zip"]), {
            status: 200,
            headers: { "content-type": "application/zip" },
          });
    }
    if (url.includes("/transcript")) {
      return json({ job_id: JOB_ID, status: "ready", segments: [] });
    }
    if (init?.method === "PATCH") {
      return json(CLIP);
    }
    return answers.clips ? answers.clips() : json({ items: [CLIP], total: 1 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ClipGrid", () => {
  it("shows nothing yet when there are no clips", async () => {
    stubApi({ clips: () => json({ items: [], total: 0 }) });

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByText(/No clips yet/)).toBeDefined(),
    );
  });

  it("shows a clip with its measurements", async () => {
    stubApi();

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText(/Clip · 0:06/)).toBeDefined());
    expect(screen.getByText(/320×240/)).toBeDefined();
    expect(screen.getByText(/2\.0s – 8\.0s/)).toBeDefined();
  });

  it("plays the clip through the API's media route", async () => {
    stubApi();

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText(/Clip · 0:06/)).toBeDefined());
    const video = document.querySelector("video");
    expect(video?.getAttribute("src")).toBe(`http://localhost:8000/api/v1/clips/${CLIP_ID}/media`);
    expect(
      screen.getByRole("link", { name: "Download the clip" }).getAttribute("href"),
    ).toBe(`http://localhost:8000/api/v1/clips/${CLIP_ID}/media`);
  });

  it("shows the short-form versions of the clip", async () => {
    stubApi();

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "Download 720×1280" }),
      ).toBeDefined(),
    );
    expect(
      screen.getByRole("link", { name: "Download 720×1280" }).getAttribute("href"),
    ).toBe(`http://localhost:8000/api/v1/formats/${FORMAT_ID}/media`);
    // The measurements of the version are shown as well.
    expect(screen.getByText(/6\.02s/)).toBeDefined();
  });

  it("says when a short-form version was rendered without captions", async () => {
    stubApi({
      formats: () =>
        json({
          items: [{ ...FORMAT, captions_applied: false, caption_count: 0 }],
          total: 1,
        }),
    });

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByText(/Rendered without captions/)).toBeDefined(),
    );
  });

  it("reports a failure to load the clips", async () => {
    stubApi({
      clips: () =>
        json(
          {
            error: {
              status: 500,
              code: "internal_error",
              message: "The clips could not be read.",
            },
          },
          500,
        ),
    });

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "The clips could not be read.",
      ),
    );
  });

  it("keeps the clips it has when one refresh fails", async () => {
    stubApi();
    render(<ClipGrid jobId={JOB_ID} />);
    await waitFor(() => expect(screen.getByText(/Clip · 0:06/)).toBeDefined());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers a choice of clips for the export", async () => {
    stubApi();

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", { name: /Choose .* for the export/ }),
      ).toBeDefined(),
    );
    expect(
      screen.getByText(/Choose the clips to put in the ZIP/),
    ).toBeDefined();
  });

  it("will not download a ZIP until a clip is chosen", async () => {
    stubApi();

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByRole("checkbox")).toBeDefined(),
    );
    const button = screen.getByRole("button", { name: /Download 0 clips/ });
    expect(button).toHaveProperty("disabled", true);
  });

  it("downloads a ZIP of the chosen clips", async () => {
    const { calls } = stubApi();

    render(<ClipGrid jobId={JOB_ID} />);
    await waitFor(() => expect(screen.getByRole("checkbox")).toBeDefined());

    fireEvent.click(screen.getByRole("checkbox"));
    const button = screen.getByRole("button", { name: /Download 1 clip as a ZIP/ });
    expect(button).toHaveProperty("disabled", false);
    fireEvent.click(button);

    await waitFor(() =>
      expect(calls.some((url) => url.includes("/exports"))).toBe(true),
    );
    expect(screen.getByText(/1 of 1 chosen/)).toBeDefined();
  });

  it("shows the error when the export fails", async () => {
    stubApi({
      export: () =>
        json(
          {
            error: {
              status: 409,
              code: "clip_export_unavailable",
              message: "Only a finished clip can be exported.",
            },
          },
          409,
        ),
    });

    render(<ClipGrid jobId={JOB_ID} />);
    await waitFor(() => expect(screen.getByRole("checkbox")).toBeDefined());
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(
      screen.getByRole("button", { name: /Download 1 clip as a ZIP/ }),
    );

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "Only a finished clip can be exported.",
      ),
    );
  });

  it("opens the editor for a clip", async () => {
    stubApi();

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Edit this clip" })).toBeDefined(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit this clip" }));

    await waitFor(() => expect(screen.getByLabelText("Title")).toBeDefined());
  });

  it("does not let a clip that is not finished be chosen", async () => {
    stubApi({
      clips: () =>
        json({ items: [{ ...CLIP, status: "rendering" }], total: 1 }),
    });

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByRole("checkbox")).toBeDefined());
    expect(screen.getByRole("checkbox")).toHaveProperty("disabled", true);
  });

  it("shows a trimmed copy as one", async () => {
    stubApi({
      clips: () =>
        json({ items: [{ ...CLIP, is_revision: true }], total: 1 }),
    });

    render(<ClipGrid jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText(/trimmed copy/)).toBeDefined());
  });

  it("polls while a clip is being rendered and stops when it is done", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () =>
      json({ items: [{ ...CLIP, status: "rendering" }], total: 1 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ClipGrid jobId={JOB_ID} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);

    // The clip finishes, and the polling must stop.
    fetchMock.mockImplementation(async () => json({ items: [CLIP], total: 1 }));
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(screen.getByText(/Clip · 0:06/)).toBeDefined());

    const after = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(15000);
    expect(fetchMock.mock.calls.length).toBe(after);
  });

  it("stops polling when the component is gone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () =>
      json({ items: [{ ...CLIP, status: "rendering" }], total: 1 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<ClipGrid jobId={JOB_ID} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    unmount();
    const after = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(9000);
    expect(fetchMock.mock.calls.length).toBe(after);
  });
});

describe("MediaPreview", () => {
  it("plays the file", () => {
    render(<MediaPreview src="/api/v1/clips/x/media" label="the clip" />);

    const video = document.querySelector("video");
    expect(video?.getAttribute("src")).toBe("/api/v1/clips/x/media");
    expect(video?.getAttribute("aria-label")).toBe("the clip");
  });

  it("says so when the preview cannot be loaded", () => {
    render(<MediaPreview src="/api/v1/clips/x/media" label="the clip" />);

    fireEvent.error(document.querySelector("video")!);

    expect(screen.getByRole("status").textContent).toContain(
      "could not be loaded",
    );
    expect(document.querySelector("video")).toBeNull();
  });
});
