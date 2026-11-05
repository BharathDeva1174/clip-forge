import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ClipPanel from "@/components/ClipPanel";

const JOB_ID = "a".repeat(32);
const CANDIDATE_ID = "c".repeat(32);

const QUEUED = {
  id: "k".repeat(32),
  job_id: JOB_ID,
  analysis_id: null,
  candidate_id: CANDIDATE_ID,
  status: "queued",
  stage: "queued",
  progress: 0,
  start_seconds: 2,
  end_seconds: 8,
  duration_seconds: 6,
  actual_duration_seconds: null,
  output_size_bytes: null,
  width: null,
  height: null,
  video_codec: null,
  audio_codec: null,
  attempt_count: 0,
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:00:00+00:00",
  started_at: null,
  completed_at: null,
  error: null,
  media_url: null,
};

const COMPLETED = {
  ...QUEUED,
  status: "completed",
  stage: "completed",
  progress: 100,
  actual_duration_seconds: 6.02,
  output_size_bytes: 123456,
  width: 320,
  height: 240,
  video_codec: "h264",
  media_url: `http://localhost:8000/api/v1/clips/${QUEUED.id}/media`,
};

const FAILED = {
  ...QUEUED,
  status: "failed",
  stage: "failed",
  progress: 0,
  error: { code: "clip_render_failed", message: "The clip could not be rendered." },
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Call = { url: string; method: string };

function stubApi(
  answers: {
    post?: () => Response;
    get?: () => Response;
    retry?: () => Response;
    clips?: () => Response;
  } = {},
) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ url, method });
    if (method === "POST" && url.includes("/retry")) {
      if (answers.retry) {
        return answers.retry();
      }
      return json({ clip: QUEUED });
    }
    if (method === "POST" && answers.post) {
      return answers.post();
    }
    if (url.includes("/jobs/") && url.includes("/clips")) {
      // The list of the job's clips, which is how an existing clip is found.
      return answers.clips ? answers.clips() : json({ items: [], total: 0 });
    }
    if (answers.get) {
      return answers.get();
    }
    return json(QUEUED);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ClipPanel", () => {
  it("offers to render a clip", async () => {
    stubApi();

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Render clip" })).toBeDefined(),
    );
  });

  it("shows a clip that has already been rendered", async () => {
    stubApi({ clips: () => json({ items: [COMPLETED], total: 1 }) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);

    await waitFor(() => expect(screen.getByText(/Clip ready/)).toBeDefined());
    // The preview comes through the API's media route, not from a path.
    const video = document.querySelector("video");
    expect(video?.getAttribute("src")).toBe(
      `http://localhost:8000/api/v1/clips/${QUEUED.id}/media`,
    );
    expect(
      screen.getByRole("link", { name: "Download" }).getAttribute("href"),
    ).toBe(`http://localhost:8000/api/v1/clips/${QUEUED.id}/media`);
  });

  it("does not offer to render a clip that already exists", async () => {
    stubApi({ clips: () => json({ items: [COMPLETED], total: 1 }) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);

    await waitFor(() => expect(screen.getByText(/Clip ready/)).toBeDefined());
    expect(screen.queryByRole("button", { name: "Render clip" })).toBeNull();
  });

  it("ignores a clip that belongs to another candidate", async () => {
    stubApi({
      clips: () =>
        json({ items: [{ ...COMPLETED, candidate_id: "d".repeat(32) }], total: 1 }),
    });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Render clip" })).toBeDefined(),
    );
  });

  it("requests the clip and shows the queued state", async () => {
    const { calls } = stubApi({ post: () => json(QUEUED, 202) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );

    await waitFor(() => expect(screen.getByText(/Queued/)).toBeDefined());
    expect(
      calls.some(
        (call) =>
          call.method === "POST" && call.url.includes("/clips"),
      ),
    ).toBe(true);
  });

  it("does not show a progress percentage it cannot know", async () => {
    stubApi({ post: () => json(QUEUED, 202) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );

    await waitFor(() => expect(screen.getByText(/Queued/)).toBeDefined());
    // The stage is shown, not a made-up percentage.
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("polls while the clip is rendering and cleans up on unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => json(QUEUED));
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(
      <ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(0));

    const before = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);

    unmount();
    const afterUnmount = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(6000);
    // No request is made after the component is gone.
    expect(fetchMock.mock.calls.length).toBe(afterUnmount);
  });

  it("shows a finished clip with a preview and a download", async () => {
    stubApi({ post: () => json(COMPLETED, 202) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );

    await waitFor(() => expect(screen.getByText(/Clip ready/)).toBeDefined());
    const video = document.querySelector("video");
    expect(video).not.toBeNull();
    // The media comes from the API's route, never from a file path.
    expect(video?.getAttribute("src")).toBe(
      `http://localhost:8000/api/v1/clips/${QUEUED.id}/media`,
    );
    const link = screen.getByRole("link", { name: "Download" });
    expect(link.getAttribute("href")).toBe(
      `http://localhost:8000/api/v1/clips/${QUEUED.id}/media`,
    );
  });

  it("shows a failure with its code and offers to render again", async () => {
    stubApi({ post: () => json(FAILED, 202) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );

    await waitFor(() =>
      expect(
        screen.getByText("The clip could not be rendered."),
      ).toBeDefined(),
    );
    expect(screen.getByText("clip_render_failed")).toBeDefined();
    expect(
      screen.getByRole("button", { name: "Render again" }),
    ).toBeDefined();
  });

  it("retries a failed clip", async () => {
    const { calls } = stubApi({
      post: () => json(FAILED, 202),
      get: () => json(FAILED),
    });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Render again" })).toBeDefined(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Render again" }));

    await waitFor(() =>
      expect(
        calls.filter((call) => call.url.includes("/retry")).length,
      ).toBeGreaterThan(0),
    );
  });

  it("shows a failure of the request itself", async () => {
    stubApi({
      post: () =>
        json(
          {
            error: {
              status: 409,
              code: "integration_not_configured",
              message: "No integration is selected.",
            },
          },
          409,
        ),
    });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );

    await waitFor(() =>
      expect(screen.getByText("No integration is selected.")).toBeDefined(),
    );
    expect(screen.getByText("integration_not_configured")).toBeDefined();
  });

  it("does not send the key or a path to the browser", async () => {
    stubApi({ post: () => json(QUEUED, 202) });

    render(<ClipPanel jobId={JOB_ID} candidateId={CANDIDATE_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Render clip" }),
    );

    await waitFor(() => expect(screen.getByText(/Queued/)).toBeDefined());
    expect(document.body.textContent).not.toContain("C:\\");
    expect(document.body.textContent).not.toContain("api_key");
  });
});
