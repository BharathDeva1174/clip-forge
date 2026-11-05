import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import JobWorkflow from "@/components/JobWorkflow";

const JOB_ID = "a".repeat(32);

const BASE_JOB = {
  id: JOB_ID,
  source_type: "upload" as const,
  source_reference: "clip.mp4",
  status: "completed" as const,
  progress: 100,
  stage: "ready" as const,
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:05:00+00:00",
  started_at: "2026-10-20T10:00:10+00:00",
  completed_at: "2026-10-20T10:05:00+00:00",
  attempt_count: 1,
  error: null,
  file_size_bytes: 1024,
  video_duration_seconds: 12.5,
  video_width: 640,
  video_height: 360,
  video_codec: "h264",
  has_transcript: true,
  analysis_status: "completed" as const,
  candidate_count: 2,
  clip_count: 1,
  format_count: 0,
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function errorEnvelope(status: number, code: string, message: string) {
  return json({ error: { status, code, message } }, status);
}

const TRANSCRIPT = {
  job_id: JOB_ID,
  status: "ready",
  job_status: "completed",
  job_stage: "ready",
  language: "en",
  model_name: "tiny.en",
  audio_sample_rate: 16000,
  audio_channels: 1,
  duration_seconds: 2,
  created_at: "2026-10-20T10:05:00+00:00",
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
  limit: 500,
  offset: 0,
  error: null,
};

const NO_ANALYSIS = {
  job_id: JOB_ID,
  status: "none",
  candidate_count: 0,
  candidates: [],
  error: null,
  data_notice: "The provider receives the transcript text.",
};

type Call = { url: string };

function stubApi(
  answers: {
    job?: () => Response;
    transcript?: () => Response;
    highlights?: () => Response;
    cancel?: () => Response;
  } = {},
) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push({ url });
    if (url.includes("/cancel")) {
      return answers.cancel
        ? answers.cancel()
        : json({ job: { ...BASE_JOB, status: "cancelled" } });
    }
    if (url.includes("/highlights")) {
      return answers.highlights ? answers.highlights() : json(NO_ANALYSIS);
    }
    if (url.includes("/transcript")) {
      return answers.transcript ? answers.transcript() : json(TRANSCRIPT);
    }
    return answers.job ? answers.job() : json(BASE_JOB);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("JobWorkflow", () => {
  it("shows the source and the state of the job", async () => {
    stubApi();

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText("clip.mp4")).toBeDefined());
    expect(screen.getByText("Completed")).toBeDefined();
    // The facts the backend actually reported.
    expect(screen.getByText("13s")).toBeDefined();
    expect(screen.getByText("640 × 360")).toBeDefined();
    expect(screen.getByText("h264")).toBeDefined();
  });

  it("shows the transcript", async () => {
    stubApi();

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText("hello world")).toBeDefined());
    expect(screen.getByText("0:00")).toBeDefined();
    expect(screen.getByText(/tiny\.en/)).toBeDefined();
  });

  it("says what can be done next, from the real state", async () => {
    stubApi({
      job: () =>
        json({
          ...BASE_JOB,
          analysis_status: null,
          candidate_count: 0,
          clip_count: 0,
          format_count: 0,
        }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      expect(
        screen.getByText(/Find the moments worth keeping next/),
      ).toBeDefined(),
    );
  });

  it("does not offer formatting before there is a clip", async () => {
    stubApi({
      job: () =>
        json({
          ...BASE_JOB,
          analysis_status: "completed",
          candidate_count: 2,
          clip_count: 0,
          format_count: 0,
        }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    // The moment list is there, but no clip has been rendered, so the
    // interface does not claim that a short-form version can be made.
    await waitFor(() =>
      expect(screen.getByText(/Accept a moment below/)).toBeDefined(),
    );
  });

  it("says plainly when there is no transcript", async () => {
    stubApi({
      job: () => json({ ...BASE_JOB, has_transcript: false }),
      transcript: () => json({ ...TRANSCRIPT, status: "pending", segments: [] }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByText("There is no transcript for this video.")).toBeDefined(),
    );
  });

  it("shows a job that failed, with its stable code", async () => {
    stubApi({
      job: () =>
        json({
          ...BASE_JOB,
          status: "failed",
          error: {
            code: "invalid_media",
            message: "The file does not contain a video stream.",
          },
        }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      expect(
        screen.getByText(/does not contain a video stream/),
      ).toBeDefined(),
    );
    expect(screen.getByText("invalid_media")).toBeDefined();
  });

  it("says so when the job does not exist", async () => {
    stubApi({
      job: () =>
        errorEnvelope(404, "not_found", "No job was found with that id."),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText("No such video")).toBeDefined());
  });

  it("reports a network failure without pretending the job is empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("backend"),
    );
    expect(screen.queryByText("No such video")).toBeNull();
  });

  it("survives a malformed job without crashing", async () => {
    stubApi({ job: () => json({ id: JOB_ID, nonsense: true }) });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      // The short id is shown in the source card's meta line.
      expect(screen.getByText(new RegExp(JOB_ID.slice(0, 8)))).toBeDefined(),
    );
    // The status was not sent, so the interface does not invent one.
    expect(screen.getByText("Unknown status")).toBeDefined();
  });

  it("polls while the job is running and stops when it finishes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => {
      const url = String(_input);
      if (url.includes("/highlights")) {
        return json(NO_ANALYSIS);
      }
      if (url.includes("/transcript")) {
        return json(TRANSCRIPT);
      }
      return json({ ...BASE_JOB, status: "transcribing", progress: 40 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<JobWorkflow jobId={JOB_ID} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);

    // The job finishes, and the polling must stop.
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/highlights")) {
        return json(NO_ANALYSIS);
      }
      if (url.includes("/transcript")) {
        return json(TRANSCRIPT);
      }
      return json(BASE_JOB);
    });
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(screen.getByText("Completed")).toBeDefined());

    // Count only the job-detail poll: the workflow also asks the readiness
    // report whether the worker is alive, on its own cadence.
    const jobPollCalls = () =>
      fetchMock.mock.calls.filter((c) =>
        String(c[0]).endsWith(`/api/v1/jobs/${JOB_ID}`),
      ).length;
    const callsAfterTerminal = jobPollCalls();
    await vi.advanceTimersByTimeAsync(15000);
    expect(jobPollCalls()).toBe(callsAfterTerminal);
  });

  it("stops polling when the component is gone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () =>
      json({ ...BASE_JOB, status: "queued", progress: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<JobWorkflow jobId={JOB_ID} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    unmount();
    const after = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(9000);
    expect(fetchMock.mock.calls.length).toBe(after);
  });

  it("offers to cancel a job that has not finished", async () => {
    stubApi({
      job: () => json({ ...BASE_JOB, status: "transcribing", stage: "transcribing" }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Cancel this video" }),
      ).toBeDefined(),
    );
  });

  it("does not offer to cancel a job that has finished", async () => {
    stubApi();

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() => expect(screen.getByText("Completed")).toBeDefined());
    expect(
      screen.queryByRole("button", { name: "Cancel this video" }),
    ).toBeNull();
  });

  it("cancels the job", async () => {
    const { calls } = stubApi({
      job: () => json({ ...BASE_JOB, status: "queued", stage: "queued" }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Cancel this video" }),
      ).toBeDefined(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel this video" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/cancel"))).toBe(true),
    );
    await waitFor(() => expect(screen.getByText("Cancelled")).toBeDefined());
  });

  it("shows the error when the job cannot be cancelled", async () => {
    stubApi({
      job: () => json({ ...BASE_JOB, status: "queued", stage: "queued" }),
      cancel: () =>
        json(
          {
            error: {
              status: 409,
              code: "job_not_cancellable",
              message: "This job has already finished, so it cannot be cancelled.",
            },
          },
          409,
        ),
    });

    render(<JobWorkflow jobId={JOB_ID} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Cancel this video" }),
      ).toBeDefined(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel this video" }));

    // The page can also carry a worker warning box (role="alert"), so the
    // cancellation error is found by its own message, not by its role.
    await waitFor(() =>
      expect(screen.getByText(/cannot be cancelled/)).toBeDefined(),
    );
  });

  it("opens the highlights of the job", async () => {
    stubApi({
      highlights: () =>
        json({
          ...NO_ANALYSIS,
          status: "completed",
          candidate_count: 1,
          candidates: [
            {
              id: "c".repeat(32),
              position: 0,
              start_segment_index: 0,
              end_segment_index: 1,
              start_seconds: 0,
              end_seconds: 2,
              duration_seconds: 2,
              title: "The opening line",
              reason: "It states the point in one sentence.",
              moment_type: "hook",
              score: 0.9,
              decision: "proposed",
            },
          ],
        }),
    });

    render(<JobWorkflow jobId={JOB_ID} />);

    await waitFor(() =>
      expect(screen.getByText("The opening line")).toBeDefined(),
    );
  });
});
