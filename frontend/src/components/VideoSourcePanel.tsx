"use client";

import { useCallback, useRef, useState } from "react";
import Link from "next/link";

import Button from "@/components/ui/Button";
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
    <section aria-labelledby="video-source-heading" className="card p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="eyebrow">Step 1</p>
          <h2
            id="video-source-heading"
            className="mt-2 text-lg font-semibold tracking-tight text-ink"
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
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        {/* Local file ------------------------------------------------------ */}
        <form
          className="group flex flex-col gap-3 rounded-xl border border-dashed border-line-strong bg-raised/60 p-5 transition-colors hover:border-ember-500/50"
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
          <label
            htmlFor="video-file"
            className="flex cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-line bg-canvas/60 px-4 py-5 text-center transition-colors group-hover:border-ember-500/40"
          >
            <span
              aria-hidden="true"
              className="text-base text-ink-faint transition-colors group-hover:text-ember-400"
            >
              ⬆
            </span>
            <span className="text-xs text-ink-muted">
              Choose a file — or drop it here
            </span>
          </label>
          <input
            ref={fileInput}
            id="video-file"
            type="file"
            accept={ACCEPTED_TYPES}
            aria-label="Local video file"
            aria-describedby="video-file-help"
            className="sr-only"
          />
          <p id="video-file-help" className="sr-only">
            Choose a video file and press Upload to queue it for processing.
          </p>
          <Button
            type="submit"
            variant="primary"
            size="small"
            disabled={upload.phase === "uploading"}
            className="mt-1 self-start"
          >
            {upload.phase === "uploading" ? "Uploading…" : "Upload"}
          </Button>

          {upload.phase === "uploading" && (
            <div
              className="h-1 overflow-hidden rounded-full bg-line"
              role="progressbar"
              aria-valuenow={Math.round(upload.progress * 100)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="Upload progress"
            >
              <div
                className="h-full rounded-full bg-gradient-to-r from-ember-500 to-ember-400 transition-[width] duration-300"
                style={{ width: `${Math.round(upload.progress * 100)}%` }}
              />
            </div>
          )}
          {upload.phase === "uploading" && (
            <p className="numeric text-xs text-ink-muted" role="status">
              Uploading… {Math.round(upload.progress * 100)}%
            </p>
          )}
          {upload.phase === "done" && (
            <p className="text-xs text-ok" role="status">
              Queued as job {upload.job.id.slice(0, 8)}…
            </p>
          )}
          {upload.phase === "done" && (
            <Link
              href={`/jobs/${upload.job.id}`}
              className="self-start rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
            >
              Open the uploaded video
            </Link>
          )}
          {upload.phase === "error" && (
            <p className="text-xs text-bad" role="alert">
              {upload.message}
            </p>
          )}
        </form>

        {/* YouTube link ---------------------------------------------------- */}
        <form
          className="flex flex-col gap-3 rounded-xl border border-dashed border-line-strong bg-raised/60 p-5 transition-colors focus-within:border-ember-500/50"
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
          <Button
            type="submit"
            variant="primary"
            size="small"
            disabled={link.phase === "submitting" || !linkUrl || !authorized}
            className="mt-1 self-start"
          >
            {link.phase === "submitting" ? "Submitting…" : "Add link"}
          </Button>

          {link.phase === "done" && (
            <p className="text-xs text-ok" role="status">
              Queued as job {link.job.id.slice(0, 8)}…
            </p>
          )}
          {link.phase === "done" && (
            <Link
              href={`/jobs/${link.job.id}`}
              className="self-start rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
            >
              Open the linked video
            </Link>
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
