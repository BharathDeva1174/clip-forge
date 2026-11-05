"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import FormatPanel from "@/components/FormatPanel";
import MediaPreview from "@/components/MediaPreview";
import {
  ApiError,
  CLIP_STAGE_LABELS,
  getClip,
  listClips,
  requestClip,
  retryClip,
  type Clip,
} from "@/lib/api";

/** How often a rendering clip is asked about. */
const POLL_INTERVAL_MS = 2500;

type State =
  | { phase: "idle" }
  | { phase: "requesting" }
  | { phase: "working"; clip: Clip }
  | { phase: "error"; message: string; code: string | null };

/** The stages a clip passes through while it renders. */
const WORKING_STAGES = ["queued", "preparing", "rendering", "verifying"];

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/**
 * The render control for one highlight candidate.
 *
 * It asks the API for a clip, follows it while it renders, and shows the
 * result. The progress is the stage, not a percentage: FFmpeg does not
 * report a reliable one, and a made-up percentage would be a lie.
 */
export default function ClipPanel({
  jobId,
  candidateId,
}: {
  jobId: string;
  candidateId: string;
}) {
  const [state, setState] = useState<State>({ phase: "idle" });
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const clip = state.phase === "working" ? state.clip : null;
  const rendering =
    clip !== null && WORKING_STAGES.includes(clip.status);

  // Look for a clip that has already been made for this candidate.
  //
  // A clip is rendered once and kept, so opening the workflow again has to
  // show the clip that is already there rather than offering to render it
  // all over again. A temporary failure is not a lost clip: the panel falls
  // back to offering the render.
  useEffect(() => {
    let cancelled = false;
    void listClips(jobId).then(
      (clips) => {
        if (cancelled) {
          return;
        }
        const existing = clips.find(
          (candidate) => candidate.candidate_id === candidateId,
        );
        if (existing) {
          setState({ phase: "working", clip: existing });
        }
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [jobId, candidateId]);

  // While the clip is rendering, keep asking about it.
  useEffect(() => {
    if (!clip || !rendering) {
      return;
    }
    const timer = setInterval(() => {
      void getClip(clip.id)
        .then((fresh) => {
          if (mounted.current) {
            setState({ phase: "working", clip: fresh });
          }
        })
        // A temporary failure is not a lost clip: the next poll tries again.
        .catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [clip, rendering]);

  const start = useCallback(async () => {
    setBusy(true);
    setState({ phase: "requesting" });
    try {
      const created = await requestClip(jobId, candidateId);
      if (!created || typeof created.id !== "string") {
        throw new ApiError(0, "unknown", "The clip could not be requested.");
      }
      if (mounted.current) {
        setState({ phase: "working", clip: created });
      }
    } catch (cause) {
      if (mounted.current) {
        setState({
          phase: "error",
          message:
            cause instanceof ApiError
              ? cause.message
              : "The clip could not be requested.",
          code: cause instanceof ApiError ? cause.code : null,
        });
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }, [jobId, candidateId]);

  const retry = useCallback(async () => {
    if (!clip) {
      return;
    }
    setBusy(true);
    try {
      const again = await retryClip(clip.id);
      if (!again || typeof again.id !== "string") {
        // The answer was not a clip. It is shown as a failure rather than
        // being rendered as a broken one.
        throw new ApiError(0, "unknown", "The clip could not be rendered again.");
      }
      if (mounted.current) {
        setState({ phase: "working", clip: again });
      }
    } catch (cause) {
      if (mounted.current) {
        setState({
          phase: "error",
          message:
            cause instanceof ApiError
              ? cause.message
              : "The clip could not be rendered again.",
          code: cause instanceof ApiError ? cause.code : null,
        });
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }, [clip]);

  if (state.phase === "error") {
    return (
      <div
        className="mt-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2"
        role="alert"
      >
        <p className="text-[11px] text-bad">{state.message}</p>
        {state.code && (
          <p className="mt-1 font-mono text-[10px] text-ink-faint">{state.code}</p>
        )}
        <button
          type="button"
          onClick={() => void start()}
          className="mt-1.5 rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Try again
        </button>
      </div>
    );
  }

  if (clip === null) {
    return (
      <div className="mt-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void start()}
          className="rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
        >
          {busy ? "Requesting…" : "Render clip"}
        </button>
      </div>
    );
  }

  if (clip.status === "failed") {
    return (
      <div
        className="mt-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2"
        role="alert"
      >
        <p className="text-[11px] text-bad">
          {clip.error?.message ?? "The clip could not be rendered."}
        </p>
        {clip.error?.code && (
          <p className="mt-1 font-mono text-[10px] text-ink-faint">
            {clip.error.code}
          </p>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => void retry()}
          className="mt-1.5 rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
        >
          {busy ? "Requesting…" : "Render again"}
        </button>
      </div>
    );
  }

  if (clip.status !== "completed") {
    return (
      <div className="mt-2 rounded-lg border border-line bg-surface-raised px-3 py-2">
        <p className="text-[11px] text-ink-muted" role="status">
          {CLIP_STAGE_LABELS[clip.stage] ?? clip.stage}…
        </p>
        <p className="mt-0.5 text-[10px] text-ink-faint">
          The clip is being cut from the source video.
        </p>
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-lg border border-ok/30 bg-ok/5 px-3 py-2">
      <p className="text-[11px] text-ink-muted">
        Clip ready · {formatDuration(clip.duration_seconds)}
        {clip.width && clip.height ? ` · ${clip.width}×${clip.height}` : ""}
        {clip.actual_duration_seconds !== null
          ? ` · measured ${clip.actual_duration_seconds.toFixed(2)}s`
          : ""}
      </p>
      {clip.media_url && (
        <>
          {/* The browser plays the file through the API's media route, so
              the frontend never needs to know where it is. */}
          <MediaPreview
            src={clip.media_url}
            label="the rendered clip"
            className="mt-2 w-full max-w-sm rounded"
          />
          <a
            href={clip.media_url}
            download
            className="mt-1.5 inline-block rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
          >
            Download
          </a>
        </>
      )}

      {/* The short-form version is a separate artifact. The clip above is
          not changed by it. */}
      <FormatPanel clipId={clip.id} />
    </div>
  );
}
