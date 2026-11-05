import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import TranscriptPanel from "@/components/TranscriptPanel";
import type { Job } from "@/lib/api";

const JOB: Job = {
  id: "b".repeat(32),
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
  video_duration_seconds: 12,
  video_width: 320,
  video_height: 240,
  video_codec: "h264",
  has_transcript: true,
  analysis_status: "completed",
  candidate_count: 2,
  clip_count: 1,
  format_count: 0,
};

function transcriptResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const READY_TRANSCRIPT = {
  job_id: JOB.id,
  status: "ready",
  job_status: "completed",
  job_stage: "ready",
  language: "en",
  model_name: "tiny.en",
  audio_sample_rate: 16000,
  audio_channels: 1,
  duration_seconds: 75,
  created_at: "2026-10-20T10:05:00+00:00",
  segment_count: 2,
  segments: [
    {
      index: 0,
      start_seconds: 0,
      end_seconds: 2.5,
      text: "The quick brown fox.",
      no_speech_probability: 0.01,
    },
    {
      index: 1,
      start_seconds: 65.5,
      end_seconds: 68,
      text: "Jumps over the lazy dog.",
      no_speech_probability: 0.02,
    },
  ],
  limit: 500,
  offset: 0,
  error: null,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("TranscriptPanel", () => {
  it("shows the timestamped segments of a stored transcript", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => transcriptResponse(READY_TRANSCRIPT)),
    );

    render(<TranscriptPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByText("The quick brown fox.")).toBeDefined(),
    );
    expect(screen.getByText("Jumps over the lazy dog.")).toBeDefined();
    // The timestamps are shown as minutes and seconds.
    expect(screen.getByText("0:00")).toBeDefined();
    expect(screen.getByText("1:05")).toBeDefined();
  });

  it("shows how many segments the transcript has", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => transcriptResponse(READY_TRANSCRIPT)),
    );

    render(<TranscriptPanel job={JOB} />);

    await waitFor(() => expect(screen.getByText("2 segments")).toBeDefined());
    expect(screen.getByText("Language: en")).toBeDefined();
    expect(screen.getByText("Model: tiny.en")).toBeDefined();
  });

  it("says that there is no transcript yet while the job is running", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        transcriptResponse({
          ...READY_TRANSCRIPT,
          status: "pending",
          job_status: "transcribing",
          job_stage: "transcribing",
          segments: [],
          segment_count: 0,
        }),
      ),
    );

    render(
      <TranscriptPanel
        job={{ ...JOB, status: "transcribing", stage: "transcribing", progress: 85 }}
      />,
    );

    await waitFor(() =>
      expect(screen.getByText(/no transcript yet/i)).toBeDefined(),
    );
    expect(screen.getByText(/transcribing the audio/i)).toBeDefined();
  });

  it("shows the failure of a job that could not be transcribed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        transcriptResponse(
          {
            error: {
              code: "transcription_model_unavailable",
              message: "The local transcription model could not be loaded.",
            },
          },
          409,
        ),
      ),
    );

    render(<TranscriptPanel job={{ ...JOB, status: "failed" }} />);

    await waitFor(() =>
      expect(
        screen.getByText(/local transcription model could not be loaded/),
      ).toBeDefined(),
    );
    expect(screen.getByText("transcription_model_unavailable")).toBeDefined();
  });

  it("reports a transcript that could not be loaded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    render(<TranscriptPanel job={JOB} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "Could not reach the ClipForge API",
      ),
    );
    expect(screen.getByText("network_error")).toBeDefined();
  });

  it("says so when the audio held no speech", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        transcriptResponse({
          ...READY_TRANSCRIPT,
          segments: [],
          segment_count: 0,
        }),
      ),
    );

    render(<TranscriptPanel job={JOB} />);

    await waitFor(() => expect(screen.getByText(/no speech was found/i)).toBeDefined());
  });

  it("polls while the job is still running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () =>
      transcriptResponse({
        ...READY_TRANSCRIPT,
        status: "pending",
        job_status: "transcribing",
        job_stage: "transcribing",
        segments: [],
        segment_count: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <TranscriptPanel
        job={{ ...JOB, status: "transcribing", stage: "transcribing" }}
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(6000);

    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("stops polling once the job is finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => transcriptResponse(READY_TRANSCRIPT));
    vi.stubGlobal("fetch", fetchMock);

    render(<TranscriptPanel job={JOB} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(15000);

    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("never asks for a path or a cache location", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => transcriptResponse(READY_TRANSCRIPT)),
    );

    render(<TranscriptPanel job={JOB} />);

    await waitFor(() => expect(screen.getByText(/tiny\.en/)).toBeDefined());
    expect(document.body.textContent).not.toContain("C:\\");
    expect(document.body.textContent).not.toContain("/home/");
  });

  it("shows a finished job without a transcript as a finished job", async () => {
    // A job that was completed before transcripts existed has none, and the
    // panel must not say that one is still on its way.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        transcriptResponse({
          ...READY_TRANSCRIPT,
          status: "pending",
          job_status: "completed",
          job_stage: "ready",
          segments: [],
          segment_count: 0,
        }),
      ),
    );

    render(<TranscriptPanel job={{ ...JOB, has_transcript: false }} />);

    await waitFor(() =>
      expect(screen.getByText(/finished before transcripts were recorded/i)).toBeDefined(),
    );
    expect(screen.queryByText(/no transcript yet/i)).toBeNull();
  });

  it("does not show a long segment as a broken layout", async () => {
    const longText = "supercalifragilisticexpialidocious".repeat(20);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        transcriptResponse({
          ...READY_TRANSCRIPT,
          segments: [
            {
              index: 0,
              start_seconds: 0,
              end_seconds: 2.5,
              text: longText,
              no_speech_probability: 0.01,
            },
          ],
        }),
      ),
    );

    render(<TranscriptPanel job={JOB} />);

    await waitFor(() => expect(screen.getByText(longText)).toBeDefined());
    // The text is allowed to wrap rather than push the panel sideways.
    const rendered = screen.getByText(longText);
    expect(rendered.className).toContain("break-words");
  });

  it("does not show a failed request as an empty transcript", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        transcriptResponse(
          {
            error: {
              code: "transcription_timeout",
              message: "The transcription did not finish in time.",
            },
          },
          409,
        ),
      ),
    );

    render(<TranscriptPanel job={{ ...JOB, status: "failed" }} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "did not finish in time",
      ),
    );
    // It is not dressed up as a successful, empty transcript.
    expect(screen.queryByText(/no speech was found/i)).toBeNull();
    expect(screen.getByText("transcription_timeout")).toBeDefined();
  });
});
