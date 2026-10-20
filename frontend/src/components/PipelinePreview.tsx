type PipelineStep = {
  id: number;
  title: string;
  body: string;
  phase: string;
  next?: boolean;
};

const STEPS: PipelineStep[] = [
  {
    id: 1,
    title: "Ingest",
    body: "Point ClipForge at a local video file, or a YouTube URL you are authorised to access.",
    phase: "Phase 2",
    next: true,
  },
  {
    id: 2,
    title: "Transcribe",
    body: "Pull the audio track out of the video and turn the speech into a timed transcript.",
    phase: "Phase 3",
  },
  {
    id: 3,
    title: "Find highlights",
    body: "An LLM reads the transcript and proposes the moments worth turning into clips.",
    phase: "Phase 4",
  },
  {
    id: 4,
    title: "Render clips",
    body: "FFmpeg cuts each highlighted moment into a standalone clip on local disk.",
    phase: "Phase 5",
  },
  {
    id: 5,
    title: "Format & caption",
    body: "Convert to 9:16, burn in synchronised subtitles and export for each platform.",
    phase: "Phase 6",
  },
];

export default function PipelinePreview() {
  return (
    <ol className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      {STEPS.map((step) => (
        <li
          key={step.id}
          className="relative flex flex-col gap-3 rounded-xl border border-line bg-surface p-5"
        >
          <div className="flex items-center justify-between">
            <span
              aria-hidden="true"
              className="flex size-7 items-center justify-center rounded-full border border-line-strong font-mono text-xs text-ink-muted"
            >
              {step.id}
            </span>
            {step.next ? (
              <span className="rounded-full border border-ember-500/40 bg-ember-500/10 px-2 py-0.5 text-[10px] font-semibold tracking-wider text-ember-300 uppercase">
                Next up
              </span>
            ) : (
              <span className="text-[10px] font-semibold tracking-wider text-ink-faint uppercase">
                {step.phase}
              </span>
            )}
          </div>
          <div>
            <h3 className="text-sm font-semibold text-ink">{step.title}</h3>
            <p className="mt-1.5 text-[13px] leading-relaxed text-ink-muted">
              {step.body}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
