import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ServiceStatusPanel from "@/components/ServiceStatusPanel";

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const READY = {
  status: "ready",
  service: "clipforge-api",
  version: "1.0.0",
  config: "loaded",
  capabilities: {
    video_ingestion: true,
    background_worker: true,
    database: true,
    persistent_storage: true,
    audio_extraction: true,
    transcription: true,
    llm_highlight_detection: true,
    clip_rendering: true,
    vertical_formatting: true,
    subtitle_generation: true,
  },
  transcription_model_cached: true,
  worker: {
    running: true,
    worker_id: "worker-1",
    last_seen: "2026-10-21T10:00:00+00:00",
    seconds_since_seen: 3,
  },
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ServiceStatusPanel", () => {
  it("says the backend is ready", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(READY)));

    render(<ServiceStatusPanel />);

    await waitFor(() => expect(screen.getByText("Ready")).toBeDefined());
    expect(screen.getByText(/clipforge-api/)).toBeDefined();
    expect(screen.getByText("Loaded")).toBeDefined();
    expect(screen.getByText("Already on this machine")).toBeDefined();
  });

  it("lists the capabilities that exist", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(READY)));

    render(<ServiceStatusPanel />);

    await waitFor(() =>
      expect(screen.getByText("Clip rendering")).toBeDefined(),
    );
    expect(screen.getByText("Vertical formatting")).toBeDefined();
    expect(screen.getByText("Burned-in captions")).toBeDefined();
    // Every capability is available in this build.
    expect(screen.getAllByText("Available")).toHaveLength(10);
  });

  it("says the worker is running", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(READY)));

    render(<ServiceStatusPanel />);

    await waitFor(() =>
      expect(screen.getByText(/Running — seen 3s ago/)).toBeDefined(),
    );
  });

  it("says plainly that the worker is not running", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          ...READY,
          worker: {
            running: false,
            worker_id: null,
            last_seen: null,
            seconds_since_seen: null,
          },
        }),
      ),
    );

    render(<ServiceStatusPanel />);

    await waitFor(() =>
      expect(
        screen.getByText(/Not running — nothing will be processed/),
      ).toBeDefined(),
    );
  });

  it("says when a capability is not available", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          ...READY,
          capabilities: { ...READY.capabilities, clip_rendering: false },
        }),
      ),
    );

    render(<ServiceStatusPanel />);

    await waitFor(() =>
      expect(screen.getByText("Clip rendering")).toBeDefined(),
    );
    expect(screen.getAllByText("Unavailable")).toHaveLength(1);
  });

  it("says the model still has to be fetched", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ ...READY, transcription_model_cached: false })),
    );

    render(<ServiceStatusPanel />);

    await waitFor(() =>
      expect(screen.getByText(/Not downloaded yet/)).toBeDefined(),
    );
  });

  it("reports a failure without pretending everything is fine", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    render(<ServiceStatusPanel />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("backend"),
    );
    expect(screen.queryByText("Ready")).toBeNull();
  });

  it("can be asked again after a failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockImplementation(async () => json(READY));
    vi.stubGlobal("fetch", fetchMock);

    render(<ServiceStatusPanel />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(screen.getByText("Ready")).toBeDefined());
  });

  it("survives a malformed answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ nonsense: true })));

    render(<ServiceStatusPanel />);

    await waitFor(() => expect(screen.getByText("Ready")).toBeDefined());
    // A capability the backend did not mention is not claimed to exist.
    expect(screen.getAllByText("Unavailable")).toHaveLength(10);
  });
});
