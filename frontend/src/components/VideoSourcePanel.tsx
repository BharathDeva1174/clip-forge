/**
 * Video input area for the coming ingestion phase.
 *
 * The controls are rendered but permanently disabled: uploading a video is not
 * implemented yet, and the UI says so rather than pretending otherwise.
 */
export default function VideoSourcePanel() {
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
            Video ingestion arrives in Phase 2. Both controls below are
            intentionally disabled — nothing is uploaded or processed yet.
          </p>
        </div>
        <span className="rounded-full border border-line-strong px-2.5 py-1 text-[10px] font-semibold tracking-wider text-ink-faint uppercase">
          Phase 2
        </span>
      </div>

      <fieldset
        disabled
        aria-describedby="video-source-note"
        className="mt-6 grid gap-4 sm:grid-cols-2"
      >
        <legend className="sr-only">Video source (not available yet)</legend>

        <label className="flex cursor-not-allowed flex-col gap-2 rounded-xl border border-dashed border-line-strong bg-raised p-5 opacity-60">
          <span className="text-sm font-medium text-ink">Local video file</span>
          <span className="text-xs text-ink-faint">
            Drop an MP4, MOV or WebM here, or browse for one.
          </span>
          <input
            type="file"
            accept="video/*"
            aria-label="Local video file"
            className="mt-1 block w-full text-xs text-ink-faint file:mr-3 file:rounded-md file:border-0 file:bg-line-strong file:px-3 file:py-1.5 file:text-xs file:text-ink-muted"
          />
        </label>

        <label className="flex cursor-not-allowed flex-col gap-2 rounded-xl border border-dashed border-line-strong bg-raised p-5 opacity-60">
          <span className="text-sm font-medium text-ink">YouTube link</span>
          <span className="text-xs text-ink-faint">
            Only for videos you are authorised to access and permitted to
            download.
          </span>
          <input
            type="url"
            aria-label="YouTube link"
            placeholder="https://www.youtube.com/watch?v=…"
            className="mt-1 block w-full rounded-lg border border-line bg-canvas px-3 py-2 text-xs text-ink-faint placeholder:text-ink-faint/70"
          />
        </label>
      </fieldset>
    </section>
  );
}
