"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import ClipEditor from "@/components/ClipEditor";
import ExportButton from "@/components/ExportButton";
import MediaPreview from "@/components/MediaPreview";
import { ApiError, listClips, listFormats, type Clip, type ClipFormat } from "@/lib/api";

/** The stages a clip passes through while it renders. */
const WORKING_STAGES = ["queued", "preparing", "rendering", "verifying"];

/** How long a duration is shown as. */
function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) {
    return "";
  }
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** A file size, in the largest unit that keeps it readable. */
function formatBytes(bytes: number | null): string {
  if (!bytes) {
    return "";
  }
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** One clip, with the short-form versions that were made from it. */
function ClipCard({
  clip,
  formats,
  selected,
  onToggle,
  onEdit,
  showEditor,
  onRevision,
  onSaved,
}: {
  clip: Clip;
  formats: ClipFormat[];
  /** Whether this clip is chosen for the batch. */
  selected: boolean;
  onToggle: (clipId: string) => void;
  /** Opens or closes the editor for this clip. */
  onEdit: (clipId: string) => void;
  /** Whether the editor is open for this clip. */
  showEditor: boolean;
  onRevision?: (revision: Clip) => void;
  onSaved?: () => void;
}) {
  return (
    <li className="rounded-xl border border-line bg-surface p-3">
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggle(clip.id)}
          disabled={clip.status !== "completed"}
          aria-label={`Choose ${clip.title ?? "this clip"} for the export`}
          className="mt-1 size-3.5 accent-ember-500"
        />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-ink">
            {clip.title ?? "Untitled clip"}
            {clip.is_revision ? (
              <span className="ml-1 text-[10px] text-ink-faint">
                (trimmed copy)
              </span>
            ) : null}
          </p>
          <p className="mt-0.5 text-[11px] text-ink-muted">
            Clip · {formatDuration(clip.actual_duration_seconds ?? clip.duration_seconds)}
            {clip.width && clip.height ? ` · ${clip.width}×${clip.height}` : ""}
            {clip.output_size_bytes ? ` · ${formatBytes(clip.output_size_bytes)}` : ""}
          </p>
          <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
            {clip.start_seconds.toFixed(1)}s – {clip.end_seconds.toFixed(1)}s of
            the source video
          </p>
        </div>
      </div>

      {clip.media_url ? (
        <>
          <MediaPreview
            src={clip.media_url}
            label={`the clip from ${clip.start_seconds.toFixed(1)} seconds`}
            className="mt-2 max-h-40 w-full rounded"
          />
          <a
            href={clip.media_url}
            download
            className="mt-1.5 inline-block rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
          >
            Download the clip
          </a>
        </>
      ) : (
        <p className="mt-2 text-[11px] text-ink-faint">
          {clip.status === "failed"
            ? "This clip could not be rendered."
            : "This clip is still being rendered."}
        </p>
      )}

      {formats.length > 0 && (
        <div className="mt-3 border-t border-line pt-2">
          <p className="text-[10px] tracking-wider text-ink-faint uppercase">
            Short-form versions
          </p>
          <ul className="mt-1.5 space-y-2">
            {formats.map((format) => (
              <li key={format.id}>
                <p className="text-[11px] text-ink-muted">
                  {format.width}×{format.height}
                  {format.actual_duration_seconds !== null
                    ? ` · ${format.actual_duration_seconds.toFixed(2)}s`
                    : ""}
                  {format.output_size_bytes
                    ? ` · ${formatBytes(format.output_size_bytes)}`
                    : ""}
                </p>
                {format.captions_enabled && !format.captions_applied && (
                  <p className="text-[10px] text-warn">
                    Rendered without captions — no text overlapped this clip.
                  </p>
                )}
                {format.status === "failed" ? (
                  <p className="text-[10px] text-bad">
                    {format.error?.message ?? "This version could not be rendered."}
                  </p>
                ) : format.media_url ? (
                  <a
                    href={format.media_url}
                    download
                    className="mt-1 inline-block rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
                  >
                    Download {format.width}×{format.height}
                  </a>
                ) : (
                  <p className="text-[10px] text-ink-faint">
                    {format.status === "queued" ? "Queued…" : "Rendering…"}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-2">
        <button
          type="button"
          onClick={() => onEdit(clip.id)}
          aria-expanded={showEditor}
          className="rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          {showEditor ? "Hide the editor" : "Edit this clip"}
        </button>
      </div>

      {showEditor && (
        <ClipEditor
          clip={clip}
          onRevision={onRevision}
          onSaved={onSaved}
        />
      )}
    </li>
  );
}

/**
 * The gallery of the clips of one job.
 *
 * The moments live under their own candidates, with the control that makes
 * a clip. This is the other side of that: everything that has been made,
 * with its measurements, its preview and its download — and the
 * short-form versions that came out of it.
 */
export default function ClipGrid({ jobId }: { jobId: string }) {
  const [clips, setClips] = useState<Clip[]>([]);
  const [formats, setFormats] = useState<Record<string, ClipFormat[]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const latestRequest = useRef(0);

  /**
   * The short-form versions of each clip.
   *
   * They are gathered together, so one clip's failure does not lose the
   * others, and a clip that has none is simply left out.
   */
  const loadFormats = useCallback(
    async (found: Clip[], requestId: number) => {
      const entries = await Promise.all(
        found.map(async (clip) => {
          const list = await listFormats(clip.id).then(
            (formats) => formats,
            () => [] as ClipFormat[],
          );
          return [clip.id, list] as const;
        }),
      );
      if (requestId === latestRequest.current) {
        setFormats(Object.fromEntries(entries));
      }
    },
    [],
  );

  const load = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const found = await listClips(jobId).then(
      (clips) => clips,
      // A temporary failure is not a lost gallery: the clips that are
      // already shown stay, and the next poll tries again.
      () => null,
    );
    if (requestId !== latestRequest.current || found === null) {
      return;
    }
    setError(null);
    setLoading(false);
    setClips(found);
    void loadFormats(found, requestId);
  }, [jobId, loadFormats]);

  useEffect(() => {
    // The state is set in the promise's callbacks rather than in the effect
    // body, so the render is not cascaded.
    let cancelled = false;
    const requestId = ++latestRequest.current;
    void listClips(jobId).then(
      (found) => {
        if (cancelled) {
          return;
        }
        setClips(found);
        setLoading(false);
        void loadFormats(found, requestId);
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        setError(
          cause instanceof ApiError
            ? cause.message
            : "The clips could not be loaded.",
        );
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [jobId, loadFormats]);

  // While a clip is still being rendered, keep asking until it is done.
  const rendering = clips.some((clip) => WORKING_STAGES.includes(clip.status));

  useEffect(() => {
    if (!rendering) {
      return;
    }
    const timer = setInterval(() => {
      void load();
    }, 2500);
    return () => clearInterval(timer);
  }, [rendering, load]);

  if (loading) {
    return (
      <p className="mt-2 text-xs text-ink-muted" role="status">
        Loading the clips…
      </p>
    );
  }

  if (error) {
    return (
      <p role="alert" className="mt-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-[11px] text-bad">
        {error}
      </p>
    );
  }

  if (clips.length === 0) {
    return (
      <p className="mt-2 text-xs text-ink-muted">
        No clips yet. Accept a moment below and render it into one.
      </p>
    );
  }

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2">
        <ExportButton
          jobId={jobId}
          clipIds={selected}
          onError={(message) => setExportError(message)}
        />
        <p className="text-[10px] text-ink-faint">
          {selected.length === 0
            ? "Choose the clips to put in the ZIP."
            : `${selected.length} of ${clips.length} chosen.`}
        </p>
      </div>
      {exportError && (
        <p role="alert" className="mt-2 text-[11px] text-bad">
          {exportError}
        </p>
      )}
      <ul className="mt-3 grid gap-3 sm:grid-cols-2">
        {clips.map((clip) => (
          <ClipCard
            key={clip.id}
            clip={clip}
            formats={formats[clip.id] ?? []}
            selected={selected.includes(clip.id)}
            onToggle={(id) =>
              setSelected((current) =>
                current.includes(id)
                  ? current.filter((item) => item !== id)
                  : [...current, id],
              )
            }
            onEdit={(id) =>
              setEditing((current) => (current === id ? null : id))
            }
            showEditor={editing === clip.id}
            onRevision={() => void load()}
            onSaved={() => void load()}
          />
        ))}
      </ul>
    </div>
  );
}
