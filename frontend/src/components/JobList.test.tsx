import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import JobList from "@/components/JobList";

const BASE_JOB = {
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

function jobList(items: unknown[]) {
  return new Response(
    JSON.stringify({ items, total: items.length, limit: 50, offset: 0 }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("JobList", () => {
  it("shows an empty state when there are no jobs", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jobList([])));

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("No videos yet")).toBeDefined());
  });

  it("renders the status and progress of a job", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([{ ...BASE_JOB, status: "inspecting", progress: 70 }]),
      ),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("Inspecting")).toBeDefined());
    const progress = screen.getByRole("progressbar");
    expect(progress.getAttribute("aria-valuenow")).toBe("70");
  });

  it("does not show a progress bar for a finished job", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "completed",
            progress: 100,
            has_transcript: true,
          },
        ]),
      ),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("Transcribed")).toBeDefined());
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("does not claim a transcript that was never stored", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "completed",
            progress: 100,
            has_transcript: false,
          },
        ]),
      ),
    );

    render(<JobList />);

    // The job is finished, but it has no transcript, so it is not called
    // transcribed.
    await waitFor(() => expect(screen.getByText("Completed")).toBeDefined());
    expect(screen.queryByText("Transcribed")).toBeNull();
  });

  it("shows the error of a failed job", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "failed",
            error: {
              code: "invalid_media",
              message: "The file does not contain a video stream.",
            },
          },
        ]),
      ),
    );

    render(<JobList />);

    await waitFor(() =>
      expect(screen.getByText(/does not contain a video stream/)).toBeDefined(),
    );
    expect(screen.getByText("invalid_media")).toBeDefined();
  });

  it("shows an error message when the API cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    render(<JobList />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("backend"),
    );
  });

  it("polls while a job is running and stops when it finishes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => jobList([{ ...BASE_JOB, status: "queued" }]));
    vi.stubGlobal("fetch", fetchMock);

    render(<JobList />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);

    // Every job is terminal now, so the polling must stop.
    fetchMock.mockImplementation(async () =>
      jobList([
        {
          ...BASE_JOB,
          status: "completed",
          progress: 100,
          has_transcript: true,
        },
      ]),
    );
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(screen.getByText("Transcribed")).toBeDefined());

    const callsAfterTerminal = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(15000);
    expect(fetchMock.mock.calls.length).toBe(callsAfterTerminal);
  });

  it("refreshes on demand", async () => {
    const fetchMock = vi.fn(async () => jobList([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<JobList />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("shows the audio extraction stage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "extracting_audio",
            stage: "extracting_audio",
            progress: 80,
          },
        ]),
      ),
    );

    render(<JobList />);

    // The status badge says "Extracting audio"; the stage caption and the
    // percentage are rendered beside each other in the running row.
    await waitFor(() => expect(screen.getByText(/Extracting audio/)).toBeDefined());
    expect(screen.getByText("Extracting the audio track")).toBeDefined();
    expect(screen.getByText("80%")).toBeDefined();
  });

  it("shows the transcription stage and marks the progress as an estimate", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "transcribing",
            stage: "transcribing",
            progress: 91,
          },
        ]),
      ),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("Transcribing")).toBeDefined());
    expect(screen.getByText(/an estimate rather than an exact figure/)).toBeDefined();
    const progress = screen.getByRole("progressbar");
    expect(progress.getAttribute("aria-valuenow")).toBe("91");
  });

  it("links every entry to its workflow", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "completed",
            progress: 100,
            has_transcript: true,
          },
        ]),
      ),
    );

    render(<JobList />);

    await waitFor(() =>
      expect(screen.getByText("Transcribed")).toBeDefined(),
    );
    const link = screen.getByRole("link", { name: "Open workflow" });
    expect(link.getAttribute("href")).toBe(`/jobs/${BASE_JOB.id}`);
  });

  it("shows when the video was submitted", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "completed",
            progress: 100,
            has_transcript: true,
            created_at: "2026-10-20T10:00:00+00:00",
          },
        ]),
      ),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("Transcribed")).toBeDefined());
    // The time the backend reported is shown, and it is a real date.
    const row = screen.getByText(/5808261e|a{8}/);
    expect(row.textContent).toMatch(/\d{1,2}\/\d{1,2}\/\d{4}/);
  });

  it("says what the job has produced", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "completed",
            progress: 100,
            has_transcript: true,
            analysis_status: "completed",
            candidate_count: 3,
            clip_count: 2,
            format_count: 1,
          },
        ]),
      ),
    );

    render(<JobList />);

    // The artifacts are shown as structured counts, not a run-on line.
    await waitFor(() => expect(screen.getByText("transcript")).toBeDefined());
    expect(screen.getByText("3")).toBeDefined();
    const moments = screen.getByText("moments");
    expect(moments.textContent).toBe("moments");
    expect(screen.getByText("2")).toBeDefined();
    expect(screen.getByText("clips")).toBeDefined();
    expect(screen.getByText("short-form")).toBeDefined();
  });

  it("does not claim an analysis that has not happened", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          {
            ...BASE_JOB,
            status: "completed",
            progress: 100,
            has_transcript: true,
            analysis_status: null,
            candidate_count: 0,
            clip_count: 0,
            format_count: 0,
          },
        ]),
      ),
    );

    render(<JobList />);

    // The counts say "not analysed" rather than "0 moments", which would
    // claim the analysis ran and found nothing.
    await waitFor(() => expect(screen.getByText("not analysed")).toBeDefined());
  });
});

describe("JobList sections and deletion", () => {
  it("groups jobs into in-progress, processed and failed sections", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([
          { ...BASE_JOB, id: "1".repeat(32), status: "queued", stage: "queued" },
          {
            ...BASE_JOB,
            id: "2".repeat(32),
            status: "completed",
            has_transcript: true,
          },
          {
            ...BASE_JOB,
            id: "3".repeat(32),
            status: "failed",
            error: { code: "invalid_media", message: "Not a video." },
          },
        ]),
      ),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("In progress")).toBeDefined());
    expect(screen.getByText("Processed")).toBeDefined();
    expect(screen.getByText("Failed or cancelled")).toBeDefined();

    // Each video sits in the section its state names: the running one has a
    // progress bar, the processed one a "Transcribed" badge, the failed one
    // an error.
    const inProgress = screen.getByText("In progress").closest("div")
      ?.parentElement as HTMLElement;
    expect(inProgress.textContent).toContain("1".repeat(8));
    const processed = screen.getByText("Processed").closest("div")
      ?.parentElement as HTMLElement;
    expect(processed.textContent).toContain("Transcribed");
    const ended = screen.getByText("Failed or cancelled").closest("div")
      ?.parentElement as HTMLElement;
    expect(ended.textContent).toContain("Not a video.");
  });

  it("renders one section when every job is in the same state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jobList([{ ...BASE_JOB, status: "completed", has_transcript: true }]),
      ),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("Processed")).toBeDefined());
    expect(screen.queryByText("In progress")).toBeNull();
    expect(screen.queryByText("Failed or cancelled")).toBeNull();
  });

  it("deletes a finished job after an explicit confirmation", async () => {
    const fetchMock = vi.fn(async (input: unknown, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jobList([{ ...BASE_JOB, status: "failed" }]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<JobList />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete" })).toBeDefined(),
    );

    // First press only asks; nothing is deleted yet.
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(
      screen.getByText(/Delete this video and everything it produced/),
    ).toBeDefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Delete for good" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [url, init] = fetchMock.mock.calls[1];
    expect(String(url)).toContain(`/api/v1/jobs/${BASE_JOB.id}`);
    expect(init?.method).toBe("DELETE");
  });

  it("can back out of the deletion", async () => {
    const fetchMock = vi.fn(async () =>
      jobList([{ ...BASE_JOB, status: "failed" }]),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<JobList />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete" })).toBeDefined(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));

    expect(
      screen.queryByText(/Delete this video and everything it produced/),
    ).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
