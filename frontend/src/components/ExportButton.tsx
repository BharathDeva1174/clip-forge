"use client";

import { useCallback, useRef, useState } from "react";

import { ApiError, exportClips } from "@/lib/api";

/**
 * The download of a batch of clips.
 *
 * The archive is built by the backend for this request and is not kept, so
 * there is no stale copy to think about. The button is disabled while the
 * download is in flight, so a second click cannot start a second one.
 */
export default function ExportButton({
  jobId,
  clipIds,
  onError,
}: {
  jobId: string;
  /** The clips that are chosen. */
  clipIds: string[];
  /** Called with a message when the export fails. */
  onError?: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  const download = useCallback(async () => {
    if (clipIds.length === 0 || busy) {
      return;
    }
    setBusy(true);
    try {
      const blob = await exportClips(jobId, clipIds);
      if (!mounted.current) {
        return;
      }
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `clipforge-${jobId.slice(0, 8)}-clips.zip`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (cause) {
      if (mounted.current) {
        onError?.(
          cause instanceof ApiError
            ? cause.message
            : "The export could not be downloaded.",
        );
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }, [busy, clipIds, jobId, onError]);

  return (
    <button
      type="button"
      disabled={busy || clipIds.length === 0}
      onClick={() => void download()}
      className="rounded-lg border border-ember-500/40 bg-ember-500/10 px-2.5 py-1 text-[11px] text-ember-300 transition-colors hover:border-ember-500 disabled:opacity-50"
    >
      {busy
        ? "Preparing the ZIP…"
        : `Download ${clipIds.length} ${clipIds.length === 1 ? "clip" : "clips"} as a ZIP`}
    </button>
  );
}
