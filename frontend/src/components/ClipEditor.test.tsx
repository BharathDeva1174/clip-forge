import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ClipEditor from "@/components/ClipEditor";

const CLIP_ID = "k".repeat(32);
const JOB_ID = "a".repeat(32);

const CLIP = {
  id: CLIP_ID,
  job_id: JOB_ID,
  analysis_id: null,
  candidate_id: "c".repeat(32),
  status: "completed" as const,
  stage: "completed",
  progress: 100,
  start_seconds: 1,
  end_seconds: 4,
  duration_seconds: 3,
  actual_duration_seconds: 3,
  output_size_bytes: 4096,
  width: 320,
  height: 240,
  video_codec: "h264",
  audio_codec: "aac",
  attempt_count: 1,
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:05:00+00:00",
  started_at: null,
  completed_at: "2026-10-20T10:05:00+00:00",
  error: null,
  media_url: `http://localhost:8000/api/v1/clips/${CLIP_ID}/media`,
  parent_clip_id: null,
  is_revision: false,
  title: "The opening line",
  source_title: "The opening line",
  caption_edits: [],
};

const TRANSCRIPT = {
  job_id: JOB_ID,
  status: "ready",
  job_status: "completed",
  job_stage: "ready",
  language: "en",
  model_name: "tiny.en",
  audio_sample_rate: 16000,
  audio_channels: 1,
  duration_seconds: 4,
  created_at: "2026-10-20T10:05:00+00:00",
  segment_count: 2,
  segments: [
    {
      index: 0,
      start_seconds: 0,
      end_seconds: 2,
      text: "The first thing that was said.",
      no_speech_probability: 0.01,
    },
    {
      index: 1,
      start_seconds: 2,
      end_seconds: 4,
      text: "The second thing that was said.",
      no_speech_probability: 0.01,
    },
  ],
  limit: 500,
  offset: 0,
  error: null,
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Call = { url: string; method: string; body: unknown };

function stubApi(
  answers: {
    transcript?: () => Response;
    revision?: () => Response;
    title?: () => Response;
    captions?: () => Response;
  } = {},
) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    if (url.includes("/transcript")) {
      return answers.transcript ? answers.transcript() : json(TRANSCRIPT);
    }
    if (url.includes("/revisions")) {
      return answers.revision ? answers.revision() : json(CLIP, 202);
    }
    if (url.includes("/captions")) {
      return answers.captions ? answers.captions() : json({ clip_id: CLIP_ID, edits: [] });
    }
    if (method === "PATCH") {
      return answers.title ? answers.title() : json(CLIP);
    }
    return json(CLIP);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ClipEditor", () => {
  it("shows the title and the boundaries of the clip", async () => {
    stubApi();

    render(<ClipEditor clip={CLIP} />);

    const title = screen.getByLabelText("Title") as HTMLInputElement;
    expect(title.value).toBe("The opening line");
    const start = screen.getByLabelText("Start") as HTMLInputElement;
    expect(start.value).toBe("1");
    const end = screen.getByLabelText("End") as HTMLInputElement;
    expect(end.value).toBe("4");
  });

  it("says the boundaries are on the source video", async () => {
    stubApi();

    render(<ClipEditor clip={CLIP} />);

    expect(
      screen.getByText(/seconds from the start of the source video/),
    ).toBeDefined();
  });

  it("saves the title without rendering the clip again", async () => {
    const { calls } = stubApi();

    render(<ClipEditor clip={CLIP} />);
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "My best moment" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save the title" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.method === "PATCH" && call.url.includes(CLIP_ID),
        ),
      ).toBe(true),
    );
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch?.body).toEqual({ title: "My best moment" });
    // It is a label. Nothing is rendered again.
    expect(calls.some((call) => call.url.includes("/revisions"))).toBe(false);
  });

  it("trims the clip into a new revision", async () => {
    const { calls } = stubApi();

    render(<ClipEditor clip={CLIP} />);
    fireEvent.change(screen.getByLabelText("Start"), { target: { value: "1.5" } });
    fireEvent.change(screen.getByLabelText("End"), { target: { value: "3" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Make a trimmed copy" }),
    );

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/revisions"))).toBe(true),
    );
    const revision = calls.find((call) => call.url.includes("/revisions"));
    expect(revision?.body).toEqual({
      start_seconds: 1.5,
      end_seconds: 3,
    });
  });

  it("refuses a boundary that is not a number before asking the server", async () => {
    const { calls } = stubApi();

    render(<ClipEditor clip={CLIP} />);
    fireEvent.change(screen.getByLabelText("Start"), { target: { value: "abc" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Make a trimmed copy" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("must be numbers"),
    );
    expect(calls.some((call) => call.url.includes("/revisions"))).toBe(false);
  });

  it("shows the error when the server refuses a trim", async () => {
    stubApi({
      revision: () =>
        json(
          {
            error: {
              status: 400,
              code: "clip_source_too_short",
              message: "The clip would run past the end of the source video.",
            },
          },
          400,
        ),
    });

    render(<ClipEditor clip={CLIP} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Make a trimmed copy" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "past the end of the source video",
      ),
    );
  });

  it("shows the words the clip covers", async () => {
    stubApi();

    render(<ClipEditor clip={CLIP} />);

    await waitFor(() =>
      expect(
        screen.getByDisplayValue("The first thing that was said."),
      ).toBeDefined(),
    );
    expect(
      screen.getByDisplayValue("The second thing that was said."),
    ).toBeDefined();
  });

  it("saves the words the user changed", async () => {
    const { calls } = stubApi();

    render(<ClipEditor clip={CLIP} />);
    const field = await screen.findByDisplayValue(
      "The first thing that was said.",
    );
    fireEvent.change(field, {
      target: { value: "What was actually said." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save the words" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/captions"))).toBe(true),
    );
    const saved = calls.find((call) => call.url.includes("/captions"));
    expect(saved?.method).toBe("PUT");
    expect(saved?.body).toEqual({
      edits: [{ segment_index: 0, text: "What was actually said." }],
    });
  });

  it("shows the error when the words cannot be saved", async () => {
    stubApi({
      captions: () =>
        json(
          {
            error: {
              status: 400,
              code: "clip_caption_edit_invalid",
              message: "The transcript has no segment 9.",
            },
          },
          400,
        ),
    });

    render(<ClipEditor clip={CLIP} />);
    const field = await screen.findByDisplayValue(
      "The first thing that was said.",
    );
    fireEvent.change(field, { target: { value: "Changed." } });
    fireEvent.click(screen.getByRole("button", { name: "Save the words" }));

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "no segment 9",
      ),
    );
  });

  it("does not offer the words when the job has no transcript", async () => {
    stubApi({ transcript: () => json({ ...TRANSCRIPT, segments: [] }) });

    render(<ClipEditor clip={CLIP} />);

    await waitFor(() =>
      expect(screen.queryByText(/what the captions will say/)).toBeNull(),
    );
  });

  it("tells the user that a title is not a re-render", async () => {
    stubApi();

    render(<ClipEditor clip={CLIP} />);

    expect(
      screen.getByText(/The clip is not rendered again/),
    ).toBeDefined();
  });

  it("tells the user that a trimmed copy is a new clip", async () => {
    stubApi();

    render(<ClipEditor clip={CLIP} />);

    expect(screen.getByText(/a new clip: this one keeps its own file/)).toBeDefined();
  });
});
