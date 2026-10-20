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

    await waitFor(() => expect(screen.getByText("No jobs yet")).toBeDefined());
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
      vi.fn(async () => jobList([{ ...BASE_JOB, status: "completed", progress: 100 }])),
    );

    render(<JobList />);

    await waitFor(() => expect(screen.getByText("Ingested")).toBeDefined());
    expect(screen.queryByRole("progressbar")).toBeNull();
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
      jobList([{ ...BASE_JOB, status: "completed", progress: 100 }]),
    );
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(screen.getByText("Ingested")).toBeDefined());

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
});
