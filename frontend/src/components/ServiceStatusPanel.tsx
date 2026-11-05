"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, getReadiness, type Readiness } from "@/lib/api";

type StatusState =
  | { phase: "loading" }
  | { phase: "ready"; readiness: Readiness }
  | { phase: "error"; message: string; code: string | null };

/**
 * The capabilities, in the order they happen.
 *
 * The order is the pipeline's, so the list reads as a journey rather than
 * as a set of flags.
 */
const CAPABILITY_LABELS: { key: string; label: string; note: string }[] = [
  {
    key: "video_ingestion",
    label: "Video ingestion",
    note: "Uploads and authorised YouTube links.",
  },
  {
    key: "background_worker",
    label: "Background worker",
    note: "A separate process does the work, with leases and recovery.",
  },
  {
    key: "database",
    label: "Job database",
    note: "SQLite, with versioned migrations.",
  },
  {
    key: "persistent_storage",
    label: "Local storage",
    note: "The source videos, the clips and the formatted outputs.",
  },
  {
    key: "audio_extraction",
    label: "Audio extraction",
    note: "FFmpeg pulls the audio track out.",
  },
  {
    key: "transcription",
    label: "Local transcription",
    note: "Whisper, running on this machine. No audio leaves it.",
  },
  {
    key: "llm_highlight_detection",
    label: "Highlight detection",
    note: "Your own AI provider finds the moments worth keeping.",
  },
  {
    key: "clip_rendering",
    label: "Clip rendering",
    note: "FFmpeg renders each accepted moment into its own file.",
  },
  {
    key: "vertical_formatting",
    label: "Vertical formatting",
    note: "A 9:16 version of a clip, cropped or padded to fit.",
  },
  {
    key: "subtitle_generation",
    label: "Burned-in captions",
    note: "The transcript, re-timed to the clip and drawn onto the picture.",
  },
];

/**
 * What this build of ClipForge can do, and whether it is ready.
 *
 * The header carries a small indicator, which answers "is the backend
 * there?". This page answers the question behind it: is the configuration
 * good, is the speech model already on this machine, and which parts of
 * the pipeline are actually available.
 */
export default function ServiceStatusPanel() {
  const [state, setState] = useState<StatusState>({ phase: "loading" });
  const latestRequest = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const outcome = await getReadiness().then(
      (readiness) => ({ readiness, failure: null as ApiError | null }),
      (cause: unknown) => ({
        readiness: null,
        failure:
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The status could not be read."),
      }),
    );
    if (requestId !== latestRequest.current) {
      return;
    }
    if (outcome.failure) {
      setState({
        phase: "error",
        message: outcome.failure.message,
        code: outcome.failure.code === "unknown" ? null : outcome.failure.code,
      });
      return;
    }
    setState({ phase: "ready", readiness: outcome.readiness! });
  }, []);

  useEffect(() => {
    let cancelled = false;
    void getReadiness().then(
      (readiness) => {
        if (!cancelled) {
          setState({ phase: "ready", readiness });
        }
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        const failure =
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The status could not be read.");
        setState({
          phase: "error",
          message: failure.message,
          code: failure.code === "unknown" ? null : failure.code,
        });
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.phase === "loading") {
    return (
      <p className="text-sm text-ink-muted" role="status">
        Reading the status…
      </p>
    );
  }

  if (state.phase === "error") {
    return (
      <div className="rounded-2xl border border-bad/40 bg-bad/10 px-5 py-4">
        <p role="alert" className="text-sm text-bad">
          {state.message}
        </p>
        {state.code && (
          <p className="mt-1 font-mono text-[11px] text-ink-faint">
            {state.code}
          </p>
        )}
        <button
          type="button"
          onClick={() => void refresh()}
          className="mt-3 rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Try again
        </button>
      </div>
    );
  }

  const readiness = state.readiness;
  const ready = readiness.status === "ready" && readiness.config === "loaded";

  return (
    <div className="space-y-6">
      <section
        aria-labelledby="service-heading"
        className="rounded-2xl border border-line bg-surface p-5"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2
              id="service-heading"
              className="text-sm font-semibold text-ink"
            >
              The backend
            </h2>
            <p className="mt-1 text-xs text-ink-muted">
              {readiness.service} · version {readiness.version || "unknown"}
            </p>
          </div>
          <span
            className={`shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
              ready
                ? "border-ok/40 bg-ok/10 text-ok"
                : "border-warn/40 bg-warn/10 text-warn"
            }`}
          >
            {ready ? "Ready" : "Not ready"}
          </span>
        </div>

        <dl className="mt-4 grid gap-x-4 gap-y-3 sm:grid-cols-2">
          <div>
            <dt className="text-[10px] tracking-wider text-ink-faint uppercase">
              Configuration
            </dt>
            <dd className="mt-0.5 text-xs text-ink-muted">
              {readiness.config === "loaded" ? "Loaded" : "Invalid"}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] tracking-wider text-ink-faint uppercase">
              Background worker
            </dt>
            <dd className="mt-0.5 text-xs text-ink-muted">
              {readiness.worker.running
                ? `Running${
                    readiness.worker.seconds_since_seen !== null
                      ? ` — seen ${Math.round(readiness.worker.seconds_since_seen)}s ago`
                      : ""
                  }`
                : "Not running — nothing will be processed until it is started"}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] tracking-wider text-ink-faint uppercase">
              Speech model
            </dt>
            <dd className="mt-0.5 text-xs text-ink-muted">
              {readiness.transcription_model_cached
                ? "Already on this machine"
                : "Not downloaded yet — the first transcription needs a network connection"}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] tracking-wider text-ink-faint uppercase">
              Storage
            </dt>
            <dd className="mt-0.5 text-xs text-ink-muted">
              On this machine only
            </dd>
          </div>
        </dl>
      </section>

      <section
        aria-labelledby="capabilities-heading"
        className="rounded-2xl border border-line bg-surface p-5"
      >
        <h2
          id="capabilities-heading"
          className="text-sm font-semibold text-ink"
        >
          What this build can do
        </h2>
        <ul className="mt-4 space-y-2.5">
          {CAPABILITY_LABELS.map((capability) => {
            const available = readiness.capabilities[capability.key] === true;
            return (
              <li key={capability.key} className="flex gap-3">
                <span
                  aria-hidden="true"
                  className={`mt-1.5 size-1.5 shrink-0 rounded-full ${
                    available ? "bg-ok" : "bg-line-strong"
                  }`}
                />
                <div className="min-w-0">
                  <p className="text-xs text-ink">
                    {capability.label}
                    <span className="sr-only">
                      {available ? " — available" : " — not available"}
                    </span>
                  </p>
                  <p className="mt-0.5 text-[11px] text-ink-faint">
                    {capability.note}
                  </p>
                </div>
                <span
                  className={`ml-auto shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${
                    available
                      ? "border-ok/40 bg-ok/10 text-ok"
                      : "border-line-strong bg-surface text-ink-faint"
                  }`}
                >
                  {available ? "Available" : "Unavailable"}
                </span>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
