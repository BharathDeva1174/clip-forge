"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, isTerminal, listJobs, type Job } from "@/lib/api";
import { JOBS_CHANGED_EVENT } from "@/lib/jobs-event";

/** How often the list refreshes while a job is still running. */
const POLL_INTERVAL_MS = 3000;

type ListState = {
  jobs: Job[];
  loading: boolean;
  error: string | null;
};

const INITIAL_STATE: ListState = { jobs: [], loading: true, error: null };

const STATUS_STYLES: Record<Job["status"], string> = {
  queued: "border-line-strong bg-surface text-ink-muted",
  validating: "border-warn/40 bg-warn/10 text-warn",
  downloading: "border-warn/40 bg-warn/10 text-warn",
  inspecting: "border-warn/40 bg-warn/10 text-warn",
  completed: "border-ok/40 bg-ok/10 text-ok",
  failed: "border-bad/40 bg-bad/10 text-bad",
  cancelled: "border-line-strong bg-surface text-ink-faint",
};

const STATUS_LABELS: Record<Job["status"], string> = {
  queued: "Queued",
  validating: "Validating",
  downloading: "Downloading",
  inspecting: "Inspecting",
  completed: "Ingested",
  failed: "Failed",
  cancelled: "Cancelled",
};

export default function JobList() {
  const [state, setState] = useState<ListState>(INITIAL_STATE);
  const latestRequest = useRef(0);

  const refresh = useCallback(async () => {
    // A newer request always wins, so a slow answer cannot overwrite a
    // fresher one. The state is only touched after the request has settled.
    const requestId = ++latestRequest.current;
    const outcome = await listJobs().then(
      (result) => ({ result, failure: null as string | null }),
      (cause: unknown) => ({
        result: null,
        failure:
          cause instanceof ApiError
            ? cause.message
            : "The job list could not be loaded.",
      }),
    );
    if (requestId === latestRequest.current) {
      setState((current) => ({
        jobs: outcome.result ? outcome.result.items : current.jobs,
        loading: false,
        error: outcome.result ? null : outcome.failure,
      }));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A form elsewhere on the page asks for a refresh when it queues a job.
  useEffect(() => {
    const handler = () => {
      void refresh();
    };
    window.addEventListener(JOBS_CHANGED_EVENT, handler);
    return () => window.removeEventListener(JOBS_CHANGED_EVENT, handler);
  }, [refresh]);

  const hasActiveJobs = state.jobs.some((job) => !isTerminal(job.status));

  useEffect(() => {
    // Poll only while something is actually running, and stop as soon as every
    // job has reached a terminal state.
    if (!hasActiveJobs) {
      return;
    }
    const timer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [hasActiveJobs, refresh]);

  return (
    <section aria-labelledby="jobs-heading">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2
            id="jobs-heading"
            className="text-xl font-semibold tracking-tight text-ink"
          >
            Ingestion jobs
          </h2>
          <p className="mt-1.5 text-sm text-ink-muted">
            Every source is validated by the background worker before it is
            accepted. “Ingested” means the video was stored and checked — the
            clips come later.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Refresh
        </button>
      </div>

      {state.error && (
        <p
          role="alert"
          className="mt-4 rounded-lg border border-bad/40 bg-bad/10 px-4 py-3 text-xs text-bad"
        >
          {state.error}
        </p>
      )}

      {state.loading && state.jobs.length === 0 && (
        <p className="mt-4 text-sm text-ink-muted" role="status">
          Loading jobs…
        </p>
      )}

      {!state.loading && state.jobs.length === 0 && !state.error && (
        <div className="mt-4 rounded-2xl border border-dashed border-line-strong bg-surface px-6 py-10 text-center">
          <p className="text-sm font-medium text-ink">No jobs yet</p>
          <p className="mt-1 text-xs text-ink-muted">
            Upload a video or add a YouTube link and it will show up here.
          </p>
        </div>
      )}

      {state.jobs.length > 0 && (
        <ul className="mt-4 space-y-2">
          {state.jobs.map((job) => (
            <li
              key={job.id}
              className="rounded-xl border border-line bg-surface px-4 py-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm text-ink">
                    {job.source_type === "youtube"
                      ? job.source_reference
                      : job.source_reference || "Uploaded video"}
                  </p>
                  <p className="mt-0.5 font-mono text-[11px] text-ink-faint">
                    {job.id.slice(0, 8)} · {job.source_type}
                  </p>
                </div>
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${STATUS_STYLES[job.status]}`}
                >
                  {STATUS_LABELS[job.status]}
                </span>
              </div>

              {!isTerminal(job.status) && (
                <div
                  className="mt-3 h-1.5 overflow-hidden rounded-full bg-line"
                  role="progressbar"
                  aria-valuenow={job.progress}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`Progress for job ${job.id.slice(0, 8)}`}
                >
                  <div
                    className="h-full rounded-full bg-ember-500 transition-[width] duration-500"
                    style={{ width: `${job.progress}%` }}
                  />
                </div>
              )}

              {job.error && (
                <p className="mt-2 text-xs text-bad">
                  {job.error.message}
                  <span className="ml-2 font-mono text-ink-faint">
                    {job.error.code}
                  </span>
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
