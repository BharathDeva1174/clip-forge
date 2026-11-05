"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  createClipRevision,
  getTranscript,
  renameClip,
  setClipCaptionEdits,
  type CaptionEdit,
  type Clip,
  type Transcript,
} from "@/lib/api";

/** The boundary of the source media, in seconds, as the user would read it. */
function formatTime(seconds: number): string {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const rest = (total - minutes * 60).toFixed(1);
  return `${minutes}:${rest.padStart(4, "0")}`;
}

type EditorState = "idle" | "busy" | "error";

/**
 * The editor for one clip.
 *
 * Three things can be changed, and they are three different kinds of
 * change:
 *
 * - the **title** is a label. It is saved on the clip and the file is not
 *   touched;
 * - the **boundaries** make a new artifact. Trimming produces a
 *   *revision*: a new clip with its own file, and the clip that was
 *   trimmed keeps its own;
 * - the **caption text** replaces the words the transcriber got wrong.
 *   It is part of the export configuration, so a changed text produces a
 *   different short-form version.
 */
export default function ClipEditor({
  clip,
  onRevision,
  onSaved,
}: {
  clip: Clip;
  /** Called with the new revision, so the grid can show it. */
  onRevision?: (revision: Clip) => void;
  /** Called when the label or the captions have been saved. */
  onSaved?: () => void;
}) {
  const [start, setStart] = useState(String(clip.start_seconds));
  const [end, setEnd] = useState(String(clip.end_seconds));
  const [title, setTitle] = useState(clip.title ?? "");
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [captions, setCaptions] = useState<CaptionEdit[]>(clip.caption_edits);
  const [state, setState] = useState<EditorState>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // The words of this clip come from the job's transcript. The clip knows
  // which job it belongs to.
  useEffect(() => {
    let cancelled = false;
    void getTranscript(clip.job_id).then(
      (found) => {
        if (!cancelled) {
          setTranscript(found);
        }
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [clip.job_id]);

  const fail = useCallback((cause: unknown, fallback: string) => {
    if (!mounted.current) {
      return;
    }
    setState("error");
    setMessage(
      cause instanceof ApiError ? cause.message : fallback,
    );
  }, []);

  const saveTitle = useCallback(async () => {
    setState("busy");
    setMessage(null);
    try {
      await renameClip(clip.id, title.trim() ? title.trim() : null);
      if (mounted.current) {
        setState("idle");
        onSaved?.();
      }
    } catch (cause) {
      fail(cause, "The title could not be saved.");
    }
  }, [clip.id, fail, onSaved, title]);

  const trim = useCallback(async () => {
    // A number field that holds something that is not a number reads as
    // empty, and an empty field is not a boundary of zero.
    if (!start.trim() || !end.trim()) {
      setState("error");
      setMessage("The start and the end must be numbers.");
      return;
    }
    const startNumber = Number(start);
    const endNumber = Number(end);
    if (!Number.isFinite(startNumber) || !Number.isFinite(endNumber)) {
      setState("error");
      setMessage("The start and the end must be numbers.");
      return;
    }
    setState("busy");
    setMessage(null);
    try {
      const revision = await createClipRevision(clip.id, {
        startSeconds: startNumber,
        endSeconds: endNumber,
      });
      if (mounted.current) {
        setState("idle");
        onRevision?.(revision);
      }
    } catch (cause) {
      fail(cause, "The clip could not be trimmed.");
    }
  }, [clip.id, end, fail, onRevision, start]);

  const saveCaptions = useCallback(async () => {
    setState("busy");
    setMessage(null);
    try {
      const stored = await setClipCaptionEdits(clip.id, captions);
      if (mounted.current) {
        setCaptions(stored);
        setState("idle");
        onSaved?.();
      }
    } catch (cause) {
      fail(cause, "The captions could not be saved.");
    }
  }, [captions, clip.id, fail, onSaved]);

  // The segments this clip covers, with the user's text where there is one.
  const covered = (transcript?.segments ?? []).filter(
    (segment) =>
      segment.end_seconds > clip.start_seconds &&
      segment.start_seconds < clip.end_seconds,
  );
  const edited = new Map(captions.map((edit) => [edit.segment_index, edit.text]));

  return (
    <div className="mt-2 rounded-lg border border-line bg-surface-raised px-3 py-3">
      {/* The title -------------------------------------------------- */}
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-0 flex-1 text-[11px] text-ink-muted">
          Title
          <input
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder={clip.source_title ?? "Untitled clip"}
            className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
          />
        </label>
        <button
          type="button"
          disabled={state === "busy"}
          onClick={() => void saveTitle()}
          className="rounded-lg border border-line-strong px-2.5 py-1.5 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
        >
          Save the title
        </button>
      </div>
      <p className="mt-1 text-[10px] text-ink-faint">
        A label only. The clip is not rendered again.
      </p>

      {/* The boundaries ---------------------------------------------- */}
      <div className="mt-4 border-t border-line pt-3">
        <p className="text-[11px] text-ink-muted">
          Trim
          <span className="ml-1 text-[10px] text-ink-faint">
            seconds from the start of the source video
          </span>
        </p>
        <div className="mt-1.5 flex flex-wrap items-end gap-2">
          <label className="text-[11px] text-ink-muted">
            Start
            <input
              type="number"
              step="0.1"
              min="0"
              value={start}
              onChange={(event) => setStart(event.target.value)}
              className="mt-1 w-24 rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
            />
          </label>
          <label className="text-[11px] text-ink-muted">
            End
            <input
              type="number"
              step="0.1"
              min="0"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
              className="mt-1 w-24 rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
            />
          </label>
          <button
            type="button"
            disabled={state === "busy"}
            onClick={() => void trim()}
            className="rounded-lg border border-ember-500/40 bg-ember-500/10 px-2.5 py-1.5 text-[11px] text-ember-300 transition-colors hover:border-ember-500 disabled:opacity-50"
          >
            Make a trimmed copy
          </button>
        </div>
        <p className="mt-1 text-[10px] text-ink-faint">
          This clip runs from {formatTime(clip.start_seconds)} to{" "}
          {formatTime(clip.end_seconds)} of the source video. A trimmed copy is
          a new clip: this one keeps its own file.
        </p>
      </div>

      {/* The caption text --------------------------------------------- */}
      {covered.length > 0 && (
        <div className="mt-4 border-t border-line pt-3">
          <p className="text-[11px] text-ink-muted">
            The words
            <span className="ml-1 text-[10px] text-ink-faint">
              what the captions will say
            </span>
          </p>
          <ul className="mt-1.5 space-y-1.5">
            {covered.map((segment) => (
              <li key={segment.index}>
                <label className="block text-[10px] text-ink-faint">
                  {formatTime(segment.start_seconds)}
                  <input
                    type="text"
                    value={
                      edited.get(segment.index) ?? segment.text
                    }
                    onChange={(event) => {
                      const next = captions.filter(
                        (edit) => edit.segment_index !== segment.index,
                      );
                      setCaptions([
                        ...next,
                        {
                          segment_index: segment.index,
                          text: event.target.value,
                        },
                      ]);
                    }}
                    className="mt-0.5 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  />
                </label>
              </li>
            ))}
          </ul>
          <button
            type="button"
            disabled={state === "busy"}
            onClick={() => void saveCaptions()}
            className="mt-2 rounded-lg border border-line-strong px-2.5 py-1.5 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
          >
            Save the words
          </button>
          <p className="mt-1 text-[10px] text-ink-faint">
            Changed words make a different short-form version. The clip
            itself is not re-rendered.
          </p>
        </div>
      )}

      {message && (
        <p role="alert" className="mt-2 text-[11px] text-bad">
          {message}
        </p>
      )}
    </div>
  );
}
