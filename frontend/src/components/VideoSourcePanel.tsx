"use client";

import { useCallback, useRef, useState } from "react";

import { ApiError, submitUpload, submitYouTube, type Job } from "@/lib/api";
import { notifyJobsChanged } from "@/lib/jobs-event";

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading"; progress: number }
  | { phase: "done"; job: Job }
  | { phase: "error"; message: string };

type LinkState =
  | { phase: "idle" }
  | { phase: "submitting" }
  | { phase: "done"; job: Job }
  | { phase: "error"; message: string };

const ACCEPTED_TYPES = "video/mp4,video/quicktime,video/webm,video/x-matroska";

export default function VideoSourcePanel({
  onSubmitted,
}: {
  onSubmitted?: (job: Job) => void;
}) {
  const [upload, setUpload] = useState<UploadState>({ phase: "idle" });
  const [link, setLink] = useState<LinkState>({ phase: "idle" });
  const [linkUrl, setLinkUrl] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const handleUpload = useCallback(
    async (file: File) => {
      setUpload({ phase: "uploading", progress: 0 });
      try {
        const job = await submitUpload(file, (progress) =>
          setUpload({ phase: "uploading", progress }),
        );
        setUpload({ phase: "done", job });
        notifyJobsChanged();
        onSubmitted?.(job);
      } catch (error) {
        setUpload({
          phase: "error",
          message:
            error instanceof ApiError
              ? error.message
              : "The upload could not be completed.",
        });
      }
    },
    [onSubmitted],
  );

  const handleLink = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setLink({ phase: "submitting" });
      try {
        const job = await submitYouTube(linkUrl, authorized);
        setLink({ phase: "done", job });
        setLinkUrl("");
        setAuthorized(false);
        notifyJobsChanged();
        onSubmitted?.(job);
      } catch (error) {
        setLink({
          phase: "error",
          message:
            error instanceof ApiError
              ? error.message
              : "The link could not be submitted.",
        });
      }
    },
    [authorized, linkUrl, onSubmitted],
  );

  return (
    <section
      aria-labelledby="video-source-heading"
      className="rounded-2xl border border-line bg-surface p-6 sm:p-8"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2
            id="video-source-heading"
            className="text-lg font-semibold tracking-tight text-ink"
          >
            Add a video
          </h2>
          <p
            id="video-source-note"
            className="mt-1.5 max-w-xl text-sm leading-relaxed text-ink-muted"
          >
            Upload a local video, or point at a YouTube video you own or have
            permission to reuse. The file is stored on this machine and checked
            before anything else happens to it.
          </p>
        </div>
        <span className="rounded-full border border-line-strong px-2.5 py-1 text-[10px] font-semibold tracking-wider text-ink-faint uppercase">
          Phase 2
        </span>
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        {/* Local file ------------------------------------------------------ */}
        <form
          className="flex flex-col gap-3 rounded-xl border border-dashed border-line-strong bg-raised p-5"
          onSubmit={(event) => {
            event.preventDefault();
            const file = fileInput.current?.files?.[0];
            if (file) void handleUpload(file);
          }}
        >
          <label
            htmlFor="video-file"
            className="text-sm font-medium text-ink"
          >
            Local video file
          </label>
          <p className="text-xs text-ink-faint">
            MP4, MOV, M4V, WebM or MKV. The file never leaves your machine.
          </p>
          <input
            ref={fileInput}
            id="video-file"
            type="file"
            accept={ACCEPTED_TYPES}
            aria-label="Local video file"
            aria-describedby="video-file-help"
            className="block w-full text-xs text-ink-faint file:mr-3 file:rounded-md file:border-0 file:bg-line-strong file:px-3 file:py-1.5 file:text-xs file:text-ink-muted"
          />
          <p id="video-file-help" className="sr-only">
            Choose a video file and press Upload to queue it for processing.
          </p>
          <button
            type="submit"
            disabled={upload.phase === "uploading"}
            className="mt-1 self-start rounded-lg bg-ember-500 px-4 py-2 text-xs font-semibold text-canvas transition-colors hover:bg-ember-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {upload.phase === "uploading" ? "Uploading…" : "Upload"}
          </button>

          {upload.phase === "uploading" && (
            <p className="text-xs text-ink-muted" role="status">
              Uploading… {Math.round(upload.progress * 100)}%
            </p>
          )}
          {upload.phase === "done" && (
            <p className="text-xs text-ok" role="status">
              Queued as job {upload.job.id.slice(0, 8)}…
            </p>
          )}
          {upload.phase === "error" && (
            <p className="text-xs text-bad" role="alert">
              {upload.message}
            </p>
          )}
        </form>

        {/* YouTube link ---------------------------------------------------- */}
        <form
          className="flex flex-col gap-3 rounded-xl border border-dashed border-line-strong bg-raised p-5"
          onSubmit={handleLink}
        >
          <label htmlFor="youtube-url" className="text-sm font-medium text-ink">
            YouTube link
          </label>
          <p className="text-xs text-ink-faint">
            Only for videos you are authorised to access and permitted to
            download.
          </p>
          <input
            id="youtube-url"
            type="url"
            value={linkUrl}
            onChange={(event) => setLinkUrl(event.target.value)}
            placeholder="https://www.youtube.com/watch?v=…"
            aria-label="YouTube link"
            aria-describedby="youtube-url-help"
            className="block w-full rounded-lg border border-line bg-canvas px-3 py-2 text-xs text-ink placeholder:text-ink-faint/70"
          />
          <p id="youtube-url-help" className="sr-only">
            The address of a YouTube video you have permission to download.
          </p>
          <label className="flex items-start gap-2 text-xs text-ink-muted">
            <input
              type="checkbox"
              checked={authorized}
              onChange={(event) => setAuthorized(event.target.checked)}
              className="mt-0.5 size-3.5 accent-ember-500"
            />
            I own this video or have permission to download and reuse it.
          </label>
          <button
            type="submit"
            disabled={link.phase === "submitting" || !linkUrl || !authorized}
            className="mt-1 self-start rounded-lg bg-ember-500 px-4 py-2 text-xs font-semibold text-canvas transition-colors hover:bg-ember-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {link.phase === "submitting" ? "Submitting…" : "Add link"}
          </button>

          {link.phase === "done" && (
            <p className="text-xs text-ok" role="status">
              Queued as job {link.job.id.slice(0, 8)}…
            </p>
          )}
          {link.phase === "error" && (
            <p className="text-xs text-bad" role="alert">
              {link.message}
            </p>
          )}
        </form>
      </div>
    </section>
  );
}
