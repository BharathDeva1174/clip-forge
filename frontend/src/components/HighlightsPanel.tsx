"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import ClipPanel from "@/components/ClipPanel";
import {
  ApiError,
  getHighlightAnalysis,
  setCandidateDecision,
  startHighlightAnalysis,
  type HighlightAnalysis,
  type HighlightCandidate,
  type Job,
} from "@/lib/api";

type PanelState =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "loading" }
  | { phase: "ready"; analysis: HighlightAnalysis }
  | { phase: "error"; message: string; code: string | null };

/** How often the panel asks for the analysis while it is running. */
const POLL_INTERVAL_MS = 3000;

function formatTimestamp(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

const MOMENT_LABELS: Record<string, string> = {
  hook: "Hook",
  insight: "Insight",
  story: "Story",
  payoff: "Payoff",
  instruction: "Instruction",
  opinion: "Opinion",
  demonstration: "Demonstration",
  other: "Moment",
};

function CandidateCard({
  candidate,
  job,
  onDecide,
  onFocus,
  busy,
}: {
  candidate: HighlightCandidate;
  job: Job;
  onDecide: (candidate: HighlightCandidate, decision: "accepted" | "rejected") => void;
  onFocus: (candidate: HighlightCandidate) => void;
  busy: boolean;
}) {
  const decision = candidate.decision;

  return (
    <li className="card card-interactive px-3.5 py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-medium text-ink">{candidate.title}</p>
          <p className="numeric mt-0.5 font-mono text-[11px] text-ink-faint">
            {formatTimestamp(candidate.start_seconds)} –{" "}
            {formatTimestamp(candidate.end_seconds)} ·{" "}
            {candidate.duration_seconds.toFixed(1)}s
            {candidate.moment_type
              ? ` · ${MOMENT_LABELS[candidate.moment_type] ?? candidate.moment_type}`
              : ""}
          </p>
        </div>
        {candidate.score !== null && (
          <span
            className="numeric shrink-0 rounded-full border border-line bg-raised px-2 py-0.5 font-mono text-[10px] text-ink-muted"
            title="The model's own ranking. It is not a calibrated probability."
          >
            {candidate.score.toFixed(2)}
          </span>
        )}
      </div>

      {candidate.reason && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-ink-muted">
          {candidate.reason}
        </p>
      )}

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        {decision === "accepted" && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-ok/30 bg-ok/10 px-2.5 py-1 text-[11px] font-medium text-ok">
            <span aria-hidden="true" className="size-1.5 rounded-full bg-ok" />
            Accepted
          </span>
        )}
        {decision === "rejected" && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-bad/30 bg-bad/10 px-2.5 py-1 text-[11px] font-medium text-bad">
            <span aria-hidden="true" className="size-1.5 rounded-full bg-bad" />
            Rejected
          </span>
        )}
        {decision !== "accepted" && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide(candidate, "accepted")}
            className="inline-flex items-center gap-1.5 rounded-lg border border-ok/40 bg-ok/10 px-2.5 py-1 text-[11px] font-medium text-ok transition-colors hover:bg-ok/15 disabled:opacity-50"
          >
            <span aria-hidden="true">＋</span>
            Accept
          </button>
        )}
        {decision !== "rejected" && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide(candidate, "rejected")}
            className="rounded-lg border border-line-strong bg-raised/50 px-2.5 py-1 text-[11px] text-ink-muted transition-colors hover:border-bad/60 hover:text-bad disabled:opacity-50"
          >
            Reject
          </button>
        )}
        <button
          type="button"
          onClick={() => onFocus(candidate)}
          className="rounded-lg px-2 py-1 text-[11px] text-ink-faint transition-colors hover:text-ember-300"
        >
          Show in transcript
        </button>
      </div>

      <ClipPanel jobId={job.id} candidateId={candidate.id} />
    </li>
  );
}

/**
 * The highlight panel for one job.
 *
 * It shows what the analysis found, and lets the user accept or reject
 * each candidate. It never implies that a candidate is a rendered clip:
 * it is a moment in the transcript, and nothing more.
 */
export default function HighlightsPanel({
  job,
  onFocusRange,
}: {
  job: Job;
  onFocusRange?: (range: { start: number; end: number }) => void;
}) {
  const [state, setState] = useState<PanelState>({ phase: "idle" });
  const [busy, setBusy] = useState(false);
  const latestRequest = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const outcome = await getHighlightAnalysis(job.id).then(
      (analysis) => ({ analysis, failure: null as ApiError | null }),
      (cause: unknown) => ({
        analysis: null,
        failure:
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The highlight analysis could not be read."),
      }),
    );
    if (requestId === latestRequest.current) {
      if (outcome.failure) {
        setState({
          phase: "error",
          message: outcome.failure.message,
          code: outcome.failure.code === "unknown" ? null : outcome.failure.code,
        });
      } else {
        setState({ phase: "ready", analysis: outcome.analysis! });
      }
    }
  }, [job.id]);

  useEffect(() => {
    // The state is set in the promise's callbacks rather than in the effect
    // body, so the render is not cascaded.
    let cancelled = false;
    void getHighlightAnalysis(job.id).then(
      (analysis) => {
        if (cancelled) {
          return;
        }
        if (!Array.isArray(analysis?.candidates)) {
          // The answer was not an analysis. It is shown as a failure rather
          // than as an empty result.
          setState({
            phase: "error",
            message: "The highlight analysis could not be read.",
            code: null,
          });
          return;
        }
        setState({ phase: "ready", analysis });
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        const failure =
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The highlight analysis could not be read.");
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
  }, [job.id]);

  const analysis = state.phase === "ready" ? state.analysis : null;
  const running =
    analysis !== null &&
    (analysis.status === "pending" || analysis.status === "running");

  // While the worker is running, keep asking until it is finished.
  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = setInterval(() => {
      void load();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [running, load]);

  const start = async () => {
    setState({ phase: "starting" });
    try {
      const started = await startHighlightAnalysis(job.id);
      setState({ phase: "ready", analysis: started });
    } catch (cause) {
      setState({
        phase: "error",
        message:
          cause instanceof ApiError
            ? cause.message
            : "The highlight analysis could not be started.",
        code: cause instanceof ApiError ? cause.code : null,
      });
    }
  };

  const decide = async (
    candidate: HighlightCandidate,
    decision: "accepted" | "rejected",
  ) => {
    setBusy(true);
    try {
      const updated = await setCandidateDecision(job.id, candidate.id, decision);
      setState({ phase: "ready", analysis: updated });
    } catch (cause) {
      setState({
        phase: "error",
        message:
          cause instanceof ApiError
            ? cause.message
            : "That decision could not be saved.",
        code: cause instanceof ApiError ? cause.code : null,
      });
    } finally {
      setBusy(false);
    }
  };

  if (state.phase === "error") {
    return (
      <div
        className="mt-3 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2"
        role="alert"
      >
        <p className="text-xs text-bad">{state.message}</p>
        {state.code && (
          <p className="mt-1 font-mono text-[11px] text-ink-faint">{state.code}</p>
        )}
      </div>
    );
  }

  if (state.phase === "loading" || state.phase === "starting") {
    return (
      <p className="mt-3 text-xs text-ink-muted" role="status">
        {state.phase === "starting"
          ? "Starting the highlight analysis…"
          : "Loading the highlight analysis…"}
      </p>
    );
  }

  if (analysis === null || analysis.status === "none") {
    return (
      <div className="mt-3">
        <button
          type="button"
          onClick={() => void start()}
          className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Find highlights
        </button>
        <p className="mt-1.5 text-[11px] text-ink-faint">
          The transcript is sent to the provider you selected. It is
          processed under that provider&apos;s own policies, and neither the
          video nor the audio is sent.
        </p>
      </div>
    );
  }

  if (running) {
    return (
      <p className="mt-3 text-xs text-ink-muted" role="status">
        The highlight analysis is {analysis.status}. The worker is doing it.
      </p>
    );
  }

  if (analysis.status === "failed") {
    return (
      <div
        className="mt-3 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2"
        role="alert"
      >
        <p className="text-xs text-bad">
          {analysis.error?.message ?? "The highlight analysis failed."}
        </p>
        {analysis.error?.code && (
          <p className="mt-1 font-mono text-[11px] text-ink-faint">
            {analysis.error.code}
          </p>
        )}
        <button
          type="button"
          onClick={() => void start()}
          className="mt-2 rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Try again
        </button>
      </div>
    );
  }

  if (analysis.candidates.length === 0) {
    return (
      <div className="mt-3 rounded-lg border border-line bg-surface-raised px-3 py-2.5">
        <p className="text-xs text-ink-muted">
          The model looked at the whole transcript and did not find a
          moment worth clipping. That is a real answer, not a failure.
        </p>
        <p className="mt-1 text-[11px] text-ink-faint">
          {analysis.provider_name} · {analysis.model_id}
        </p>
      </div>
    );
  }

  return (
    <div className="mt-3 rounded-lg border border-line bg-surface-raised px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-ink-faint">
          {analysis.candidate_count}{" "}
          {analysis.candidate_count === 1 ? "moment" : "moments"} ·{" "}
          {analysis.provider_name} · {analysis.model_id}
        </p>
      </div>

      <ul className="mt-2 space-y-2">
        {analysis.candidates.map((candidate) => (
          <CandidateCard
            key={candidate.id}
            candidate={candidate}
            job={job}
            busy={busy}
            onDecide={(item, decision) => void decide(item, decision)}
            onFocus={(item) =>
              onFocusRange?.({
                start: item.start_segment_index,
                end: item.end_segment_index,
              })
            }
          />
        ))}
      </ul>

      <p className="mt-2 text-[11px] text-ink-faint">
        A moment is a place in the transcript. It is not a rendered clip.
      </p>
    </div>
  );
}
