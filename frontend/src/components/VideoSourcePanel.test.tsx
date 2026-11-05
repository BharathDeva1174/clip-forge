import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import VideoSourcePanel from "@/components/VideoSourcePanel";

const JOB_ID = "a".repeat(32);

const JOB = {
  id: JOB_ID,
  source_type: "upload" as const,
  source_reference: "clip.mp4",
  status: "queued" as const,
  progress: 0,
  stage: "queued" as const,
  created_at: "2026-10-21T10:00:00+00:00",
  updated_at: "2026-10-21T10:00:00+00:00",
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

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("VideoSourcePanel", () => {
  it("explains what it accepts", () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(JOB, 202)));

    render(<VideoSourcePanel />);

    expect(
      screen.getByText(/MP4, MOV, M4V, WebM or MKV/),
    ).toBeDefined();
    expect(
      screen.getByText(/authorised to access and permitted to download/),
    ).toBeDefined();
  });

  it("gives the file input and the link an accessible name", () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(JOB, 202)));

    render(<VideoSourcePanel />);

    expect(screen.getByLabelText("Local video file")).toBeDefined();
    expect(screen.getByLabelText("YouTube link")).toBeDefined();
    expect(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    ).toBeDefined();
  });

  it("will not submit a link that has not been authorised", () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(JOB, 202)));

    render(<VideoSourcePanel />);

    fireEvent.change(screen.getByLabelText("YouTube link"), {
      target: { value: "https://www.youtube.com/watch?v=abc" },
    });

    // The button stays disabled until the box is ticked.
    expect(screen.getByRole("button", { name: "Add link" })).toHaveProperty(
      "disabled",
      true,
    );

    fireEvent.click(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    );

    expect(screen.getByRole("button", { name: "Add link" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("keeps the returned job id and offers the workflow", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(JOB, 202)));

    render(<VideoSourcePanel />);
    fireEvent.change(screen.getByLabelText("YouTube link"), {
      target: { value: "https://www.youtube.com/watch?v=abc" },
    });
    fireEvent.click(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));

    await waitFor(() =>
      expect(screen.getByText(/Queued as job/)).toBeDefined(),
    );
    // The id the backend returned is kept, and it leads to the workflow.
    expect(
      screen.getByRole("link", { name: "Open the linked video" }).getAttribute("href"),
    ).toBe(`/jobs/${JOB_ID}`);
  });

  it("says the submission was queued, not that it is finished", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(JOB, 202)));

    render(<VideoSourcePanel />);
    fireEvent.change(screen.getByLabelText("YouTube link"), {
      target: { value: "https://www.youtube.com/watch?v=abc" },
    });
    fireEvent.click(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));

    await waitFor(() =>
      expect(screen.getByText(/Queued as job/)).toBeDefined(),
    );
    // It is queued. The interface does not claim the video is ready.
    expect(screen.queryByText(/processed/)).toBeNull();
    expect(screen.queryByText(/complete/)).toBeNull();
  });

  it("shows an actionable error when the submission is refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(
          {
            error: {
              status: 403,
              code: "youtube_not_authorized",
              message: "You did not confirm that you may download this video.",
            },
          },
          403,
        ),
      ),
    );

    render(<VideoSourcePanel />);
    fireEvent.change(screen.getByLabelText("YouTube link"), {
      target: { value: "https://www.youtube.com/watch?v=abc" },
    });
    fireEvent.click(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));

    await waitFor(() =>
      expect(
        screen.getByText(/You did not confirm that you may download/),
      ).toBeDefined(),
    );
  });

  it("shows a failure when the backend cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    render(<VideoSourcePanel />);
    fireEvent.change(screen.getByLabelText("YouTube link"), {
      target: { value: "https://www.youtube.com/watch?v=abc" },
    });
    fireEvent.click(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("backend"),
    );
  });

  it("prevents a second submission while one is in flight", async () => {
    let openTheGate: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      openTheGate = resolve;
    });
    const fetchMock = vi.fn(async () => {
      await gate;
      return json(JOB, 202);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<VideoSourcePanel />);
    fireEvent.change(screen.getByLabelText("YouTube link"), {
      target: { value: "https://www.youtube.com/watch?v=abc" },
    });
    fireEvent.click(
      screen.getByLabelText(
        "I own this video or have permission to download and reuse it.",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Submitting…" })).toBeDefined(),
    );
    expect(
      screen.getByRole("button", { name: "Submitting…" }),
    ).toHaveProperty("disabled", true);

    openTheGate();
    await waitFor(() =>
      expect(screen.getByText(/Queued as job/)).toBeDefined(),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
