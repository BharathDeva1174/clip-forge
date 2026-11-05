import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import FormatPanel from "@/components/FormatPanel";

const CLIP_ID = "c".repeat(32);

const QUEUED = {
  id: "f".repeat(32),
  clip_id: CLIP_ID,
  job_id: "a".repeat(32),
  status: "queued",
  stage: "queued",
  progress: 0,
  settings: { width: 720, height: 1280 },
  config_fingerprint: "abc",
  output_size_bytes: null,
  actual_duration_seconds: null,
  width: null,
  height: null,
  video_codec: null,
  audio_codec: null,
  pixel_format: null,
  captions_enabled: true,
  captions_applied: false,
  caption_count: 0,
  attempt_count: 0,
  created_at: "2026-10-21T10:00:00+00:00",
  updated_at: "2026-10-21T10:00:00+00:00",
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
  output_size_bytes: 204800,
  actual_duration_seconds: 4.02,
  width: 720,
  height: 1280,
  video_codec: "h264",
  audio_codec: "aac",
  pixel_format: "yuv420p",
  captions_applied: true,
  caption_count: 2,
  media_url: `http://localhost:8000/api/v1/formats/${QUEUED.id}/media`,
};

const FAILED = {
  ...QUEUED,
  status: "failed",
  stage: "failed",
  error: { code: "format_render_failed", message: "The clip could not be rendered." },
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
    list?: () => Response;
    post?: () => Response;
    fonts?: () => Response;
  } = {},
) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });

    if (url.includes("/fonts")) {
      return answers.fonts
        ? answers.fonts()
        : json({ items: ["Arial", "Segoe UI"], default: "Arial" });
    }
    if (method === "POST") {
      return answers.post ? answers.post() : json(QUEUED, 202);
    }
    return answers.list ? answers.list() : json({ items: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("FormatPanel", () => {
  it("offers the short-form version behind a control", async () => {
    stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);

    const button = await screen.findByRole("button", {
      name: "Make a short-form version",
    });
    expect(button).toBeDefined();
  });

  it("shows the default settings when it is opened", async () => {
    stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    const resolution = screen.getByLabelText("Resolution") as HTMLSelectElement;
    expect(resolution.value).toBe("720x1280");
    const layout = screen.getByLabelText("Layout") as HTMLSelectElement;
    expect(layout.value).toBe("center_crop");
    const captions = screen.getByLabelText("Burn the captions in") as HTMLInputElement;
    expect(captions.checked).toBe(true);
  });

  it("changes the resolution and the layout", async () => {
    const { calls } = stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    fireEvent.change(screen.getByLabelText("Resolution"), {
      target: { value: "1080x1920" },
    });
    fireEvent.change(screen.getByLabelText("Layout"), {
      target: { value: "fit_pad" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Render the short-form version" }),
    );

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST")).toBe(true),
    );
    const post = calls.find((call) => call.method === "POST");
    const body = post?.body as { settings: Record<string, unknown> };
    expect(body.settings.width).toBe(1080);
    expect(body.settings.layout).toBe("fit_pad");
  });

  it("hides the caption controls when the captions are off", async () => {
    stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    expect(screen.getByLabelText("Font")).toBeDefined();

    fireEvent.click(screen.getByLabelText("Burn the captions in"));

    expect(screen.queryByLabelText("Font")).toBeNull();
  });

  it("shows the caption styling controls", async () => {
    stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    expect(screen.getByLabelText("Font size")).toBeDefined();
    expect(screen.getByLabelText("Text colour")).toBeDefined();
    expect(screen.getByLabelText("Outline colour")).toBeDefined();
    expect(screen.getByLabelText("Outline thickness")).toBeDefined();
    expect(screen.getByLabelText("Position")).toBeDefined();
  });

  it("offers the fonts this machine has", async () => {
    stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    const font = screen.getByLabelText("Font") as HTMLSelectElement;
    await waitFor(() => expect(font.options.length).toBeGreaterThan(1));
    expect(font.options[0].text).toBe("Arial");
  });

  it("prevents a duplicate submission while one is in flight", async () => {
    const { calls } = stubApi();

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    const button = screen.getByRole("button", {
      name: "Render the short-form version",
    });
    fireEvent.click(button);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Requesting…" }),
      ).toBeDefined(),
    );
    expect(
      calls.filter((call) => call.method === "POST").length,
    ).toBeLessThanOrEqual(1);
  });

  it("shows a queued artifact and its stage", async () => {
    stubApi({ list: () => json({ items: [QUEUED] }) });

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    await waitFor(() => expect(screen.getByText(/queued/)).toBeDefined());
  });

  it("shows a finished artifact with a preview and a download", async () => {
    stubApi({ list: () => json({ items: [COMPLETED] }) });

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    // The measured length only appears on the finished artifact.
    await waitFor(() => expect(screen.getByText(/4\.02s/)).toBeDefined());
    const video = document.querySelector("video");
    expect(video?.getAttribute("src")).toBe(
      `http://localhost:8000/api/v1/formats/${QUEUED.id}/media`,
    );
    expect(
      screen.getByRole("link", { name: "Download" }).getAttribute("href"),
    ).toBe(`http://localhost:8000/api/v1/formats/${QUEUED.id}/media`);
  });

  it("says when a clip was rendered without captions", async () => {
    stubApi({
      list: () =>
        json({ items: [{ ...COMPLETED, captions_applied: false, caption_count: 0 }] }),
    });

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    await waitFor(() =>
      expect(screen.getByText(/rendered without captions/i)).toBeDefined(),
    );
  });

  it("shows a failure and offers to render again", async () => {
    stubApi({ list: () => json({ items: [FAILED] }) });

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    await waitFor(() =>
      expect(
        screen.getByText("The clip could not be rendered."),
      ).toBeDefined(),
    );
    expect(screen.getByText("format_render_failed")).toBeDefined();
    expect(screen.getByRole("button", { name: "Render again" })).toBeDefined();
  });

  it("retries a failed artifact", async () => {
    const { calls } = stubApi({
      list: () => json({ items: [FAILED] }),
      post: () => json({ format: QUEUED }),
    });

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Render again" }));

    await waitFor(() =>
      expect(
        calls.some((call) => call.url.includes("/retry")),
      ).toBe(true),
    );
  });

  it("shows an error when the request is refused", async () => {
    stubApi({
      post: () =>
        json(
          {
            error: {
              status: 400,
              code: "format_invalid_settings",
              message: "The output has to be portrait 9:16.",
            },
          },
          400,
        ),
    });

    render(<FormatPanel clipId={CLIP_ID} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Make a short-form version" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Render the short-form version" }),
    );

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert.textContent).toContain(
        "The output has to be portrait 9:16.",
      );
    });
  });

  it("survives an answer that is not a list", async () => {
    // The server may answer with something else entirely. A surprising
    // response must not take the panel down.
    stubApi({ list: () => json({ items: undefined }) });

    render(<FormatPanel clipId={CLIP_ID} />);

    const button = await screen.findByRole("button", {
      name: "Make a short-form version",
    });
    expect(button).toBeDefined();
    fireEvent.click(button);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Render the short-form version" }),
      ).toBeDefined(),
    );
  });

  it("stops polling when the component is gone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => json({ items: [QUEUED] }));
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<FormatPanel clipId={CLIP_ID} />);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(0));

    unmount();
    const after = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(9000);
    expect(fetchMock.mock.calls.length).toBe(after);
  });
});
