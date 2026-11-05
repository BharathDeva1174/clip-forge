"use client";

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import Button from "@/components/ui/Button";
import StatusBadge from "@/components/ui/StatusBadge";
import Stepper from "@/components/ui/Stepper";
import ClipGrid from "@/components/ClipGrid";
import HighlightsPanel from "@/components/HighlightsPanel";
import TranscriptPanel from "@/components/TranscriptPanel";
import {
  ApiError,
  cancelJob,
  getJob,
  getReadiness,
  isKnownStatus,
  isTerminal,
  type Job,
} from "@/lib/api";
import { sourceDisplay } from "@/lib/source-display";
import { usePolling } from "@/lib/use-polling";

/** How often the workflow asks about a job that is still running. */
const POLL_INTERVAL_MS = 3000;

type WorkflowState =
  | { phase: "loading" }
  | { phase: "ready"; job: Job; refreshError?: string | null }
  | { phase: "missing" }
  | { phase: "error"; message: string; code: string | null };

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  validating: "Validating",
  downloading: "Downloading",
  inspecting: "Inspecting",
  extracting_audio: "Extracting audio",
  transcribing: "Transcribing",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting for the worker",
  validating: "Validating the source",
  downloading: "Downloading the source",
  inspecting: "Inspecting the video",
  extracting_audio: "Extracting the audio track",
  transcribing: "Transcribing the audio",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
};

/** A duration in seconds, as minutes and seconds. */
function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) {
    return "";
  }
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes > 0
    ? `${minutes}m ${String(rest).padStart(2, "0")}s`
    : `${rest}s`;
}

/** When the job was submitted, in the reader's own timezone. */
function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  return parsed.toLocaleString();
}

/**
 * What the user can do next, given what has actually happened.
 *
 * The answer is driven by the real state of the job, so nothing is offered
 * before it is possible. A job with no transcript yet has no moments to
 * find; a job with no rendered clip has nothing to format.
 */
function nextStep(job: Job): string {
  if (job.status === "failed") {
    return "This job failed. The reason is above; nothing else can be done with it until it is submitted again.";
  }
  if (job.status === "cancelled") {
    return "This job was cancelled.";
  }
  if (!isKnownStatus(job.status)) {
    return "The backend reported a status this build does not know about.";
  }
  if (!isTerminal(job.status)) {
    return "The worker is still working on it. This page keeps itself up to date.";
  }
  if (!job.has_transcript) {
    return "There is no transcript for this video, so there are no moments to find.";
  }
  if (job.analysis_status === null) {
    return "The transcript is ready. Find the moments worth keeping next.";
  }
  if (job.analysis_status === "failed") {
    return "The highlight analysis failed. You can try it again below.";
  }
  if (job.analysis_status === "pending" || job.analysis_status === "running") {
    return "The highlight analysis is running.";
  }
  if (job.candidate_count === 0) {
    return "The analysis found no moments worth clipping. That is a real answer, not a failure.";
  }
  if (job.clip_count === 0) {
    return "Accept a moment below, then render it into a clip.";
  }
  if (job.format_count === 0) {
    return "The clip is rendered. Make a short-form version of it below.";
  }
  return "Everything is done. Preview and download the outputs below.";
}

/**
 * The workflow of one video.
 *
 * It brings the stages together in the order they happen: the source and
 * the job, the transcript, the moments, the clips and the short-form
 * versions. Each stage only appears once there is something to show, and
 * each one says what can be done next.
 */
export default function JobWorkflow({ jobId }: { jobId: string }) {
  const [state, setState] = useState<WorkflowState>({ phase: "loading" });
  const [focusRange, setFocusRange] = useState<{
    start: number;
    end: number;
  } | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const latestRequest = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const outcome = await getJob(jobId).then(
      (job) => ({ job, failure: null as ApiError | null }),
      (cause: unknown) => ({
        job: null,
        failure:
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The job could not be loaded."),
      }),
    );
    if (requestId !== latestRequest.current) {
      return;
    }
    if (outcome.failure) {
      // A job that does not exist is not an error to shout about; it is
      // a dead end, and it says so.
      if (outcome.failure.status === 404) {
        setState({ phase: "missing" });
        return;
      }
      // One failed poll -- a network blip, an API restart, a sleeping
      // machine during a long download -- must not throw away a running
      // job's view or stop the polling. Keep the last known state and say
      // the refresh failed; only a job we never saw is an error page.
      setState((current) => {
        if (current.phase === "ready") {
          return { ...current, refreshError: outcome.failure!.message };
        }
        return {
          phase: "error",
          message: outcome.failure!.message,
          code: outcome.failure!.code === "unknown" ? null : outcome.failure!.code,
        };
      });
      return;
    }
    setState({ phase: "ready", job: outcome.job!, refreshError: null });
  }, [jobId]);

  useEffect(() => {
    // The state is set in the promise's callbacks rather than in the effect
    // body, so the render is not cascaded.
    let cancelled = false;
    void getJob(jobId).then(
      (job) => {
        if (!cancelled) {
          setState({ phase: "ready", job });
        }
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        const failure =
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The job could not be loaded.");
        if (failure.status === 404) {
          setState({ phase: "missing" });
          return;
        }
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
  }, [jobId]);

  const job = state.phase === "ready" ? state.job : null;
  const running =
    job !== null && !isTerminal(job.status) && isKnownStatus(job.status);

  // A job can only move while the background worker is alive, and the
  // header's "Backend online" says nothing about the worker. While this
  // job is running, ask the readiness report whether the worker is actually
  // there, and say so plainly when it is not.
  const [workerDown, setWorkerDown] = useState<boolean | null>(null);
  useEffect(() => {
    if (!running) {
      // The warning box only renders while the job is running, so there is
      // nothing to clear here -- and clearing synchronously in an effect
      // would trigger a cascading render.
      return;
    }
    let cancelled = false;
    const check = () => {
      void getReadiness().then(
        (report) => {
          if (!cancelled) {
            setWorkerDown(!report.worker.running);
          }
        },
        () => {
          if (!cancelled) {
            setWorkerDown(null);
          }
        },
      );
    };
    check();
    const timer = window.setInterval(check, 30000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running]);

  /**
   * Cancel the job.
   *
   * The server decides whether it can be cancelled; a job that has already
   * finished is refused, and the interface says so rather than pretending.
   */
  const cancel = useCallback(async () => {
    setCancelling(true);
    setCancelError(null);
    try {
      const cancelled = await cancelJob(jobId);
      if (mounted.current) {
        setState({ phase: "ready", job: cancelled });
        setCancelling(false);
      }
    } catch (cause) {
      if (mounted.current) {
        setCancelling(false);
        setCancelError(
          cause instanceof ApiError
            ? cause.message
            : "The job could not be cancelled.",
        );
      }
    }
  }, [jobId]);
  usePolling(() => void refresh(), {
    active: running,
    intervalMs: POLL_INTERVAL_MS,
  });

  if (state.phase === "loading") {
    return (
      <p className="text-sm text-ink-muted" role="status">
        Loading this video…
      </p>
    );
  }

  if (state.phase === "missing") {
    return (
      <div className="rounded-2xl border border-dashed border-line-strong bg-surface px-6 py-10 text-center">
        <p className="text-sm font-medium text-ink">No such video</p>
        <p className="mt-1 text-xs text-ink-muted">
          Nothing is stored under that id. It may have been removed, or the
          link may be wrong.
        </p>
        <Link
          href="/"
          className="mt-4 inline-block rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Back to your videos
        </Link>
      </div>
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

  // The state is the ready state: the three cases above have all returned.
  const ready = state.job;
  const source = sourceDisplay(ready.source_type, ready.source_reference);

  const facts: { label: string; value: string }[] = [];
  if (ready.video_duration_seconds !== null) {
    facts.push({
      label: "Length",
      value: formatDuration(ready.video_duration_seconds),
    });
  }
  if (ready.video_width !== null && ready.video_height !== null) {
    facts.push({
      label: "Size",
      value: `${ready.video_width} × ${ready.video_height}`,
    });
  }
  if (ready.video_codec) {
    facts.push({ label: "Codec", value: ready.video_codec });
  }
  if (ready.created_at) {
    facts.push({
      label: "Submitted",
      value: formatTimestamp(ready.created_at),
    });
  }

  const stageStates: { label: string; state: "done" | "next" | "waiting" }[] =
    [
      { label: "Source", state: "done" },
      {
        label: "Transcript",
        state: ready.has_transcript ? "done" : "waiting",
      },
      {
        label: "Moments",
        state:
          ready.analysis_status === "completed" && ready.candidate_count > 0
            ? "done"
            : ready.analysis_status === "failed"
              ? "waiting"
              : "next",
      },
      {
        label: "Clips",
        state: ready.clip_count > 0 ? "done" : "next",
      },
      {
        label: "Short-form versions",
        state: ready.format_count > 0 ? "done" : "next",
      },
    ];

  return (
    <div className="space-y-6">
      <nav aria-label="Workflow" className="card px-5 py-4">
        <Stepper
          steps={stageStates.map((stage) => ({
            label: stage.label,
            state: stage.state === "next" ? "active" : stage.state,
            hint:
              stage.state === "done"
                ? "Done."
                : stage.state === "next"
                  ? "Happening now, or next."
                  : "Not there yet.",
          }))}
        />
        <p className="mt-3 text-[11px] text-ink-faint">
          Each stage only opens once the backend has something to show.
        </p>
      </nav>
      {/* The source and the job ------------------------------------------- */}
      <section
        aria-labelledby="source-heading"
        className="card p-5"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1
                id="source-heading"
                className="text-lg font-semibold tracking-tight text-ink"
              >
                {source.title}
              </h1>
              {source.detail && (
                <span className="numeric rounded border border-line bg-raised px-1.5 py-0.5 font-mono text-[11px] text-ink-faint">
                  {source.detail}
                </span>
              )}
            </div>
            <p className="numeric mt-1 font-mono text-[11px] text-ink-faint">
              {ready.id.slice(0, 8)}
              <span className="mx-1.5 text-line-strong">·</span>
              {source.kind}
              {ready.source_reference && source.detail && (
                <>
                  <span className="mx-1.5 text-line-strong">·</span>
                  <a
                    href={ready.source_reference}
                    target="_blank"
                    rel="noreferrer"
                    className="transition-colors hover:text-ember-300"
                  >
                    {ready.source_reference}
                  </a>
                </>
              )}
            </p>
          </div>
          <StatusBadge
            status={ready.status as Parameters<typeof StatusBadge>[0]["status"]}
            label={STATUS_LABELS[ready.status] ?? "Unknown status"}
          />
        </div>

        {!isTerminal(ready.status) && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              disabled={cancelling}
              onClick={() => void cancel()}
              className="text-xs"
            >
              {cancelling ? "Cancelling…" : "Cancel this video"}
            </Button>
            <p className="text-[10px] text-ink-faint">
              A cancelled video is not processed. Nothing that has already
              been made is lost.
            </p>
          </div>
        )}

        {cancelError && (
          <p role="alert" className="mt-2 text-[11px] text-bad">
            {cancelError}
          </p>
        )}

        {state.refreshError && (
          <p role="status" className="mt-3 text-[11px] text-ink-muted">
            Couldn&apos;t refresh just now — showing the last known state, and
            retrying every few seconds.
          </p>
        )}

        {workerDown === true && running && (
          <div
            role="alert"
            className="mt-3 rounded-lg border border-line-strong p-3 text-xs text-bad"
          >
            The background worker is not running, so this job will not move:
            start it with <span className="font-mono">python worker.py</span>{" "}
            from <span className="font-mono">backend/</span>. See the{" "}
            <Link href="/status" className="underline">
              Status
            </Link>{" "}
            page.
          </div>
        )}

        {running && (
          <div
            className="mt-4 h-1.5 overflow-hidden rounded-full bg-line"
            role="progressbar"
            aria-valuenow={ready.progress}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={`Progress for job ${ready.id.slice(0, 8)}`}
          >
            <div
              className="h-full rounded-full bg-gradient-to-r from-ember-500 to-ember-400 shadow-[0_0_8px_rgba(255,122,24,0.5)] transition-[width] duration-500"
              style={{ width: `${ready.progress}%` }}
            />
          </div>
        )}

        {running && (
          <p className="mt-2 text-[11px] text-ink-faint" role="status">
            {STAGE_LABELS[ready.stage] ?? "Working"} — {ready.progress}%
            {ready.stage === "downloading" && (
              <> · a large video can take 10–15 minutes to download</>
            )}
          </p>
        )}

        {ready.error && (
          <div className="mt-3 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2">
            <p className="text-xs text-bad">{ready.error.message}</p>
            <p className="mt-0.5 font-mono text-[11px] text-ink-faint">
              {ready.error.code}
            </p>
          </div>
        )}

        {facts.length > 0 && (
          <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
            {facts.map((fact) => (
              <div key={fact.label}>
                <dt className="text-[10px] tracking-wider text-ink-faint uppercase">
                  {fact.label}
                </dt>
                <dd className="mt-0.5 truncate text-xs text-ink-muted">
                  {fact.value}
                </dd>
              </div>
            ))}
          </dl>
        )}

        <p className="mt-4 border-l-2 border-ember-500/50 pl-3 text-xs leading-relaxed text-ink-muted">
          {nextStep(ready)}
        </p>
      </section>

      {/* The transcript ---------------------------------------------------- */}
      <section
        aria-labelledby="transcript-heading"
        className="rounded-2xl border border-line bg-surface p-5"
      >
        <h2
          id="transcript-heading"
          className="text-sm font-semibold text-ink"
        >
          2 · Transcript
        </h2>
        {ready.has_transcript ? (
          <TranscriptPanel job={ready} focusRange={focusRange} />
        ) : (
          <p className="mt-2 text-xs text-ink-muted">
            {running
              ? "The transcript is not ready yet. It appears here once the worker has stored it."
              : "There is no transcript for this video."}
          </p>
        )}
      </section>

      {/* The moments, the clips and the short-form versions ---------------- */}
      <section
        aria-labelledby="highlights-heading"
        className="rounded-2xl border border-line bg-surface p-5"
      >
        <h2 id="highlights-heading" className="text-sm font-semibold text-ink">
          3 · Moments
        </h2>
        <p className="mt-1 text-xs text-ink-muted">
          A moment is a place in the transcript. Accept one, render it into a
          clip, and then make a portrait version of it with the captions
          burned in.
        </p>
        {ready.status === "completed" && ready.has_transcript ? (
          <HighlightsPanel
            job={ready}
            onFocusRange={(range) => setFocusRange(range)}
          />
        ) : (
          <p className="mt-2 text-xs text-ink-muted">
            {running
              ? "The moments are found once the transcript is ready."
              : "The moments are found from the transcript."}
          </p>
        )}
      </section>

      {/* The clips and the short-form versions ----------------------------- */}
      <section
        aria-labelledby="clips-heading"
        className="rounded-2xl border border-line bg-surface p-5"
      >
        <h2 id="clips-heading" className="text-sm font-semibold text-ink">
          4 · Clips and short-form versions
        </h2>
        <p className="mt-1 text-xs text-ink-muted">
          Everything that has been rendered from this video, with its
          measurements, its preview and its download.
        </p>
        <ClipGrid jobId={ready.id} />
      </section>
    </div>
  );
}
