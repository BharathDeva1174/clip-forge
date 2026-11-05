import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import HighlightsPanel from "@/components/HighlightsPanel";
import type { Job } from "@/lib/api";

const JOB: Job = {
  id: "a".repeat(32),
  source_type: "upload",
  source_reference: "clip.mp4",
  status: "completed",
  progress: 100,
  stage: "ready",
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:05:00+00:00",
  started_at: null,
  completed_at: "2026-10-20T10:05:00+00:00",
  attempt_count: 1,
  error: null,
  file_size_bytes: 1024,
  video_duration_seconds: 120,
  video_width: 320,
  video_height: 240,
  video_codec: "h264",
  has_transcript: true,
  analysis_status: "completed",
  candidate_count: 2,
  clip_count: 1,
  format_count: 0,
};

const CANDIDATE = {
  id: "c".repeat(32),
  position: 0,
  start_segment_index: 1,
  end_segment_index: 3,
  start_seconds: 12,
  end_seconds: 45,
  duration_seconds: 33,
  title: "The surprising part",
  reason: "It lands the point in one line.",
  moment_type: "insight",
  score: 0.82,
  decision: "proposed",
};

function analysis(overrides: Record<string, unknown> = {}) {
  return {
    job_id: JOB.id,
    analysis_id: "a".repeat(32),
    status: "completed",
    provider_name: "Groq",
    model_id: "llama-3.3-70b-versatile",
    model_name: null,
    prompt_version: "1",
    candidate_count: 1,
    candidates: [CANDIDATE],
    error: null,
    data_notice: "The provider receives the transcript text.",
    ...overrides,
  };
}

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Call = { url: string; method: string; body: unknown };

function stubApi(
  answers: {
    get?: () => Response;
    post?: () => Response;
    patch?: () => Response;
  } = {},
) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });
    if (method === "POST" && answers.post) {
      return answers.post();
    }
    if (method === "PATCH" && answers.patch) {
      return answers.patch();
    }
    if (answers.get) {
      return answers.get();
    }
    return json(analysis({ status: "none", candidate_count: 0, candidates: [] }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("HighlightsPanel", () => {
  it("offers to start an analysis when there is none", async () => {
    stubApi();

    render(<HighlightsPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Find highlights" })).toBeDefined(),
    );
    // The notice says what the provider receives.
    expect(screen.getByText(/transcript is sent to the provider/i)).toBeDefined();
  });

  it("shows the candidates when the analysis is finished", async () => {
    stubApi({ get: () => json(analysis()) });

    render(<HighlightsPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByText("The surprising part")).toBeDefined(),
    );
    expect(screen.getByText("It lands the point in one line.")).toBeDefined();
    expect(screen.getByText(/0:12/)).toBeDefined();
    expect(screen.getByText(/Groq/)).toBeDefined();
  });

  it("says that an empty answer is a real answer", async () => {
    stubApi({
      get: () => json(analysis({ candidate_count: 0, candidates: [] })),
    });

    render(<HighlightsPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByText(/did not find a moment worth clipping/i)).toBeDefined(),
    );
  });

  it("shows a pending analysis and keeps asking", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () =>
      json(analysis({ status: "running", candidate_count: 0, candidates: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<HighlightsPanel job={JOB} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(6000);

    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("shows a failure with its stable code", async () => {
    stubApi({
      get: () =>
        json(
          {
            error: {
              status: 409,
              code: "ai_rate_limited",
              message: "The provider is rate limiting this account.",
            },
          },
          409,
        ),
    });

    render(<HighlightsPanel job={JOB} />);

    await waitFor(() =>
      expect(
        screen.getByText("The provider is rate limiting this account."),
      ).toBeDefined(),
    );
    expect(screen.getByText("ai_rate_limited")).toBeDefined();
  });

  it("starts an analysis when asked", async () => {
    const { calls } = stubApi({
      post: () => json(analysis({ status: "pending", candidate_count: 0, candidates: [] })),
    });

    render(<HighlightsPanel job={JOB} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Find highlights" })).toBeDefined(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Find highlights" }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST")).toBe(true),
    );
  });

  it("accepts a candidate", async () => {
    const { calls } = stubApi({
      get: () => json(analysis()),
      patch: () => json(analysis({ candidates: [{ ...CANDIDATE, decision: "accepted" }] })),
    });

    render(<HighlightsPanel job={JOB} />);
    await waitFor(() =>
      expect(screen.getByText("The surprising part")).toBeDefined(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "PATCH")).toBe(true),
    );
    expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
      decision: "accepted",
    });
  });

  it("rejects a candidate", async () => {
    const { calls } = stubApi({
      get: () => json(analysis()),
      patch: () => json(analysis({ candidates: [{ ...CANDIDATE, decision: "rejected" }] })),
    });

    render(<HighlightsPanel job={JOB} />);
    await waitFor(() =>
      expect(screen.getByText("The surprising part")).toBeDefined(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() =>
      expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
        decision: "rejected",
      }),
    );
  });

  it("points at the transcript interval a candidate covers", async () => {
    const focused: { start: number; end: number }[] = [];
    stubApi({ get: () => json(analysis()) });

    render(
      <HighlightsPanel
        job={JOB}
        onFocusRange={(range) => focused.push(range)}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("The surprising part")).toBeDefined(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Show in transcript" }));

    expect(focused).toEqual([{ start: 1, end: 3 }]);
  });

  it("never presents a candidate as a rendered clip", async () => {
    stubApi({ get: () => json(analysis()) });

    render(<HighlightsPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByText("The surprising part")).toBeDefined(),
    );
    expect(screen.getByText(/It is not a rendered clip/)).toBeDefined();
  });

  it("does not leak a transcript or a key in an error", async () => {
    stubApi({
      get: () =>
        json(
          {
            error: {
              status: 500,
              code: "internal_error",
              message: "The analysis could not be completed.",
            },
          },
          500,
        ),
    });

    render(<HighlightsPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "The analysis could not be completed.",
      ),
    );
    expect(document.body.textContent).not.toContain("sk-");
  });
});
