"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import Button from "@/components/ui/Button";
import StatusBadge from "@/components/ui/StatusBadge";
import {
  ApiError,
  deleteJob,
  isKnownStatus,
  isTerminal,
  listJobs,
  type Job,
} from "@/lib/api";
import { sourceDisplay } from "@/lib/source-display";
import { JOBS_CHANGED_EVENT, notifyJobsChanged } from "@/lib/jobs-event";
import { usePolling } from "@/lib/use-polling";

/** How often the list refreshes while a job is still running. */
const POLL_INTERVAL_MS = 3000;

type ListState = {
  jobs: Job[];
  loading: boolean;
  error: string | null;
};

const INITIAL_STATE: ListState = { jobs: [], loading: true, error: null };

/** The stage a job is on, in the words the pipeline uses. */
const STAGE_LABELS: Record<string, string> = {
  queued: "Queued",
  validating: "Validating the source",
  downloading: "Downloading the source",
  inspecting: "Inspecting the video",
  extracting_audio: "Extracting the audio track",
  transcribing: "Transcribing the audio",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
};

/**
 * The label for a job's badge.
 *
 * A finished job is only called *Transcribed* when a transcript really has
 * been stored. A job that was finished before transcripts existed is still
 * completed, but it has no transcript, and the badge must not claim one.
 */
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

function statusLabel(job: Job): string {
  if (job.status === "completed") {
    return job.has_transcript ? "Transcribed" : "Completed";
  }
  return STATUS_LABELS[job.status] ?? "Unknown status";
}

/** When the job was submitted, in the reader's own timezone. */
function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  return parsed.toLocaleString();
}

/** One produced artifact, as a small count that reads at a glance. */
function ArtifactStat({ label, value, tone }: { label: string; value: string; tone?: "ok" | "muted" }) {
  return (
    <span
      className={`inline-flex items-baseline gap-1.5 rounded-md border border-line bg-raised/40 px-2 py-0.5 ${
        tone === "ok" ? "text-ok" : "text-ink-muted"
      }`}
    >
      <span className="numeric text-[11px] font-semibold text-ink">{value}</span>
      <span className="text-[10px] text-ink-faint">{label}</span>
    </span>
  );
}

/** What the job has produced, as structured counts instead of a run-on line. */
function ArtifactCounts({ job }: { job: Job }) {
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <ArtifactStat
        label={job.has_transcript ? "transcript" : "no transcript"}
        value={job.has_transcript ? "✓" : "—"}
        tone={job.has_transcript ? "ok" : "muted"}
      />
      {job.analysis_status === "failed" ? (
        <ArtifactStat label="analysis failed" value="—" />
      ) : job.analysis_status === "pending" || job.analysis_status === "running" ? (
        <ArtifactStat label="analysing…" value="…" />
      ) : job.analysis_status === "completed" ? (
        <ArtifactStat
          label={job.candidate_count === 1 ? "moment" : "moments"}
          value={String(job.candidate_count)}
        />
      ) : (
        <ArtifactStat label="not analysed" value="—" />
      )}
      <ArtifactStat
        label={job.clip_count === 1 ? "clip" : "clips"}
        value={String(job.clip_count)}
      />
      <ArtifactStat
        label={job.format_count === 1 ? "short-form" : "short-form"}
        value={String(job.format_count)}
      />
    </span>
  );
}

/**
 * The library of every video that has been submitted.
 *
 * It is a summary, not a workspace: it says what each job is and what it
 * has produced, and every entry leads to the workflow, where the
 * transcript, the moments, the clips and the formatted outputs live.
 */
export default function JobList() {
  const [state, setState] = useState<ListState>(INITIAL_STATE);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
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

  const hasActiveJobs = state.jobs.some(
    (job) => !isTerminal(job.status) && isKnownStatus(job.status),
  );

  // Poll only while something is actually running, and stop as soon as
  // every job has reached a terminal state.
  usePolling(() => void refresh(), {
    active: hasActiveJobs,
    intervalMs: POLL_INTERVAL_MS,
  });

  const runningCount = state.jobs.filter(
    (job) => !isTerminal(job.status) && isKnownStatus(job.status),
  ).length;

  /**
   * The library, in three sections. A video is *in progress* while the
   * worker could still touch it, *processed* once it finished, and
   * *failed or cancelled* otherwise. The sections are rendered only when
   * they have something in them.
   */
  const sections: { key: string; label: string; jobs: Job[] }[] = [
    {
      key: "active",
      label: "In progress",
      jobs: state.jobs.filter(
        (job) => !isTerminal(job.status) && isKnownStatus(job.status),
      ),
    },
    {
      key: "processed",
      label: "Processed",
      jobs: state.jobs.filter((job) => job.status === "completed"),
    },
    {
      key: "ended",
      label: "Failed or cancelled",
      jobs: state.jobs.filter(
        (job) =>
          job.status === "failed" ||
          job.status === "cancelled" ||
          (isTerminal(job.status) &&
            job.status !== "completed" &&
            !isKnownStatus(job.status)),
      ),
    },
  ].filter((section) => section.jobs.length > 0);

  const confirmAndDelete = useCallback(
    async (jobId: string) => {
      setDeletingId(jobId);
      const outcome = await deleteJob(jobId).then(
        () => ({ ok: true as const }),
        (cause: unknown) => ({
          ok: false as const,
          failure:
            cause instanceof ApiError
              ? cause.message
              : "The video could not be deleted.",
        }),
      );
      setDeletingId(null);
      setConfirmingId(null);
      if (outcome.ok) {
        notifyJobsChanged();
        void refresh();
      } else {
        setState((current) => ({ ...current, error: outcome.failure }));
      }
    },
    [refresh],
  );

  return (
    <section aria-labelledby="jobs-heading">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="eyebrow">Library</p>
          <h2
            id="jobs-heading"
            className="mt-2 text-xl font-semibold tracking-tight text-ink"
          >
            Your videos
          </h2>
          <p className="mt-1.5 text-sm text-ink-muted">
            Every source is validated by the background worker before it is
            accepted. Open one to follow it through the transcript, the
            moments, the clips and the short-form versions.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          {runningCount > 0 && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-warn/30 bg-warn/10 px-2.5 py-1 text-[11px] font-medium text-warn">
              <span aria-hidden="true" className="size-1.5 animate-pulse rounded-full bg-warn" />
              {runningCount} running
            </span>
          )}
          <Button onClick={() => void refresh()}>Refresh</Button>
        </div>
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
          <p className="text-sm font-medium text-ink">No videos yet</p>
          <p className="mt-1 text-xs text-ink-muted">
            Upload a video or add a YouTube link above, and it will show up
            here.
          </p>
        </div>
      )}

      {sections.map((section) => (
        <div key={section.key} className="mt-6 first:mt-5">
          <div className="flex items-baseline gap-2">
            <h3 className="text-xs font-semibold tracking-wide text-ink-muted">
              {section.label}
            </h3>
            <span className="numeric text-[11px] text-ink-faint">
              {section.jobs.length}
            </span>
          </div>
          <ul className="mt-2.5 space-y-2.5">
            {section.jobs.map((job) => {
              const display = sourceDisplay(job.source_type, job.source_reference);
              const active = !isTerminal(job.status) && isKnownStatus(job.status);
              const confirming = confirmingId === job.id;
              const deleting = deletingId === job.id;
              return (
                <li
                  key={job.id}
                  className={`card px-4 py-3.5 ${
                    confirming
                      ? "border-bad/40"
                      : "card-interactive"
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-medium text-ink">
                          {display.title}
                        </p>
                        {display.detail && (
                          <span className="numeric hidden rounded border border-line bg-raised px-1.5 py-0.5 font-mono text-[10px] text-ink-faint sm:inline">
                            {display.detail}
                          </span>
                        )}
                      </div>
                      <p className="numeric mt-1 font-mono text-[11px] text-ink-faint">
                        {job.id.slice(0, 8)}
                        <span className="mx-1.5 text-line-strong">·</span>
                        {display.kind}
                        {job.created_at
                          ? <>
                              <span className="mx-1.5 text-line-strong">·</span>
                              {formatTimestamp(job.created_at)}
                            </>
                          : null}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <StatusBadge status={job.status as Parameters<typeof StatusBadge>[0]["status"]} label={statusLabel(job)} />
                      <Link
                        href={`/jobs/${job.id}`}
                        className="rounded-lg border border-line-strong bg-raised/60 px-2.5 py-1 text-[11px] font-medium text-ink-muted transition-colors hover:border-ember-500/60 hover:text-ink"
                      >
                        Open workflow
                      </Link>
                      {!active && (
                        <Button
                          variant="danger"
                          size="small"
                          disabled={deleting}
                          onClick={() => setConfirmingId(confirming ? null : job.id)}
                        >
                          {deleting ? "Deleting…" : "Delete"}
                        </Button>
                      )}
                    </div>
                  </div>

                  {confirming && (
                    <div className="mt-3 rounded-lg border border-bad/30 bg-bad/5 px-3 py-2.5">
                      <p className="text-xs text-ink">
                        Delete this video and everything it produced — the
                        transcript, the moments, the clips and the short-form
                        versions, including the files on this machine?
                      </p>
                      <p className="mt-1 text-[11px] text-bad">
                        This cannot be undone.
                      </p>
                      <div className="mt-2.5 flex items-center gap-2">
                        <Button
                          variant="danger"
                          size="small"
                          disabled={deleting}
                          onClick={() => void confirmAndDelete(job.id)}
                        >
                          {deleting ? "Deleting…" : "Delete for good"}
                        </Button>
                        <Button
                          variant="secondary"
                          size="small"
                          disabled={deleting}
                          onClick={() => setConfirmingId(null)}
                        >
                          Keep it
                        </Button>
                      </div>
                    </div>
                  )}

                  {active && (
                    <div className="mt-3">
                      <div
                        className="h-1.5 overflow-hidden rounded-full bg-line"
                        role="progressbar"
                        aria-valuenow={job.progress}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-label={`Progress for job ${job.id.slice(0, 8)}`}
                      >
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-ember-500 to-ember-400 shadow-[0_0_8px_rgba(255,122,24,0.5)] transition-[width] duration-500"
                          style={{ width: `${job.progress}%` }}
                        />
                      </div>
                      <p className="mt-1.5 text-[11px] text-ink-faint">
                        <span className="numeric text-ink-muted">{job.progress}%</span>
                        <span className="mx-1.5 text-line-strong">·</span>
                        {STAGE_LABELS[job.stage] ?? "Working"}
                        {job.status === "transcribing" && (
                          <> (an estimate rather than an exact figure)</>
                        )}
                        {job.stage === "downloading" && (
                          <>
                            {" "}
                            · a large video can take 10–15 minutes to download
                          </>
                        )}
                      </p>
                    </div>
                  )}

                  <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                    <ArtifactCounts job={job} />
                  </div>

                  {job.error && (
                    <p className="mt-2.5 flex flex-wrap items-baseline gap-x-2 rounded-lg border border-bad/25 bg-bad/5 px-2.5 py-1.5 text-xs text-bad">
                      <span>{job.error.message}</span>
                      <span className="numeric font-mono text-[10px] text-bad/60">
                        {job.error.code}
                      </span>
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </section>
  );
}
