"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  getFullTranscript,
  isTerminal,
  type Job,
  type Transcript,
} from "@/lib/api";

/** How often the panel refreshes while the job is still running. */
const POLL_INTERVAL_MS = 3000;

/**
 * How many segments are shown before the transcript is collapsed.
 *
 * A long recording produces hundreds of segments; showing them all pushed
 * the moments, the clips and the short-form versions a full screen-scroll
 * out of reach. The rest is one press away, and a candidate that asks to be
 * traced back to its words opens the transcript by itself.
 */
const COLLAPSED_SEGMENT_COUNT = 30;

type PanelState = {
  transcript: Transcript | null;
  loading: boolean;
  error: string | null;
  errorCode: string | null;
};

const INITIAL_STATE: PanelState = {
  transcript: null,
  loading: true,
  error: null,
  errorCode: null,
};

/** Format a number of seconds as `m:ss`. */
function formatTimestamp(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** Describe what the job is doing, in the words of the pipeline. */
function describeStage(job: Job): string {
  switch (job.stage) {
    case "extracting_audio":
      return "Extracting the audio track";
    case "transcribing":
      return "Transcribing the audio";
    case "inspecting":
      return "Inspecting the video";
    case "downloading":
      return "Downloading the video";
    case "validating":
      return "Validating the source";
    case "queued":
      return "Waiting for the worker";
    default:
      return "Processing";
  }
}

export default function TranscriptPanel({
  job,
  focusRange,
}: {
  job: Job;
  /**
   * The segment range a highlight candidate covers. The segments in the
   * range are marked, so a candidate can be traced back to the words it
   * came from.
   */
  focusRange?: { start: number; end: number } | null;
}) {
  const [state, setState] = useState<PanelState>(INITIAL_STATE);
  const [openedByUser, setOpenedByUser] = useState(false);
  const latestRequest = useRef(0);

  // The transcript is open when the user opened it — and a candidate asking
  // to be traced back to its words opens it too, without any state sync:
  // the focus range itself is the "open" instruction.
  const expanded = openedByUser || (focusRange !== undefined && focusRange !== null);

  const refresh = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const outcome = await getFullTranscript(job.id).then(
      (result) => ({ result, failure: null as ApiError | null }),
      (cause: unknown) => ({
        result: null,
        failure:
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unknown", "The transcript could not be loaded."),
      }),
    );
    if (requestId === latestRequest.current) {
      setState({
        transcript: outcome.result,
        loading: false,
        error: outcome.failure ? outcome.failure.message : null,
        errorCode: outcome.failure ? outcome.failure.code : null,
      });
    }
  }, [job.id]);

  // The job list refreshes on its own, so the panel follows it: whenever the
  // job record changes, the transcript is read again. This also covers the
  // first load, because the effect runs when the panel is mounted.
  useEffect(() => {
    void refresh();
  }, [job.updated_at, refresh]);

  // While the job is still running the panel keeps asking, so the transcript
  // appears as soon as it has been stored.
  useEffect(() => {
    if (isTerminal(job.status)) {
      return;
    }
    const timer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [job.status, refresh]);

  if (state.loading && !state.transcript) {
    return (
      <p className="mt-3 text-xs text-ink-muted" role="status">
        Loading the transcript…
      </p>
    );
  }

  if (state.error) {
    return (
      <div
        className="mt-3 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2"
        role="alert"
      >
        <p className="text-xs text-bad">{state.error}</p>
        {state.errorCode && (
          <p className="mt-1 font-mono text-[11px] text-ink-faint">
            {state.errorCode}
          </p>
        )}
      </div>
    );
  }

  const transcript = state.transcript;

  if (!transcript) {
    return null;
  }

  if (transcript.status === "pending") {
    // A job that is finished but has no transcript was finished before
    // transcripts existed. Saying "no transcript yet" would be wrong: it is
    // not going to arrive.
    if (isTerminal(job.status) && !job.has_transcript) {
      return (
        <p className="mt-3 text-xs text-ink-muted" role="status">
          This job was finished before transcripts were recorded, so it has
          none.
        </p>
      );
    }
    return (
      <p className="mt-3 text-xs text-ink-muted" role="status">
        No transcript yet — {describeStage(job).toLowerCase()}.
      </p>
    );
  }

  if (transcript.status === "failed") {
    return (
      <p className="mt-3 text-xs text-bad" role="alert">
        The transcript could not be produced.
        {transcript.error && (
          <span className="ml-2 font-mono text-ink-faint">
            {transcript.error.code}
          </span>
        )}
      </p>
    );
  }

  if (transcript.segments.length === 0) {
    return (
      <p className="mt-3 text-xs text-ink-muted">
        The audio was transcribed but no speech was found in it.
      </p>
    );
  }

  const collapsed =
    !expanded && transcript.segments.length > COLLAPSED_SEGMENT_COUNT;
  const visibleSegments = collapsed
    ? transcript.segments.slice(0, COLLAPSED_SEGMENT_COUNT)
    : transcript.segments;
  const hiddenCount = transcript.segments.length - visibleSegments.length;

  return (
    <div className="mt-3 rounded-lg border border-line bg-surface-raised px-3 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-faint">
        <span className="numeric">
          {transcript.segment_count} segment
          {transcript.segment_count === 1 ? "" : "s"}
        </span>
        {transcript.language && <span>Language: {transcript.language}</span>}
        {transcript.model_name && <span>Model: {transcript.model_name}</span>}
        {transcript.duration_seconds !== null && (
          <span className="numeric">
            Audio: {formatTimestamp(transcript.duration_seconds)}
          </span>
        )}
      </div>

      <ol
        className={`mt-3 space-y-2 ${
          expanded ? "max-h-[28rem] overflow-y-auto pr-1" : ""
        }`}
      >
        {visibleSegments.map((segment) => {
          const inFocus =
            focusRange !== undefined &&
            focusRange !== null &&
            segment.index >= focusRange.start &&
            segment.index <= focusRange.end;
          return (
            <li
              key={segment.index}
              className={`flex gap-3 rounded px-1 py-0.5 ${
                inFocus ? "bg-ember-500/10 ring-1 ring-ember-500/30" : ""
              }`}
            >
              <span className="numeric shrink-0 pt-0.5 font-mono text-[11px] text-ember-300/90">
                {formatTimestamp(segment.start_seconds)}
              </span>
              {/* A long segment, or one long word, must not push the layout
                  sideways. */}
              <span className="min-w-0 break-words text-xs leading-relaxed text-ink">
                {segment.text}
              </span>
            </li>
          );
        })}
      </ol>

      {hiddenCount > 0 && (
        <button
          type="button"
          onClick={() => setOpenedByUser(true)}
          className="mt-3 rounded-lg border border-line-strong bg-raised/60 px-3 py-1.5 text-[11px] font-medium text-ink-muted transition-colors hover:border-ember-500/60 hover:text-ink"
        >
          Show {hiddenCount} more segment{hiddenCount === 1 ? "" : "s"}
        </button>
      )}
    </div>
  );
}
