import HealthIndicator from "@/components/HealthIndicator";
import JobList from "@/components/JobList";
import PipelinePreview from "@/components/PipelinePreview";
import VideoSourcePanel from "@/components/VideoSourcePanel";
import { getApiBaseUrl } from "@/lib/config";

const WORKING_TODAY = [
  "Video upload and authorised YouTube ingestion",
  "Persistent SQLite job records with a separate worker process",
  "Real media inspection with FFprobe before a source is accepted",
  "Frontend job list with live status and progress",
];

const ON_THE_ROADMAP = [
  "Audio extraction and speech-to-text transcription (Phase 3)",
  "LLM highlight detection over the transcript (Phase 4)",
  "FFmpeg clip rendering, 9:16 conversion and burned-in captions (Phases 5–6)",
  "Job tracking, clip editing and export (Phases 7–8)",
];

export default function Home() {
  const apiBaseUrl = getApiBaseUrl();

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-10 border-b border-line bg-canvas/85 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3.5">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="flex size-9 items-center justify-center rounded-lg bg-gradient-to-br from-ember-400 to-ember-600 font-mono text-sm font-bold text-canvas"
            >
              CF
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold tracking-tight text-ink">
                ClipForge
              </p>
              <p className="text-[11px] text-ink-faint">Local-first clip studio</p>
            </div>
          </div>
          <HealthIndicator baseUrl={apiBaseUrl} />
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6 pb-20">
        <section className="relative overflow-hidden pt-16 pb-12">
          <div aria-hidden="true" className="hero-glow pointer-events-none absolute inset-0 -z-10" />
          <p className="text-[11px] font-semibold tracking-[0.2em] text-ember-400 uppercase">
            Phase 2 · Ingestion
          </p>
          <h1 className="mt-5 max-w-3xl text-4xl leading-[1.1] font-semibold tracking-tight text-ink sm:text-5xl">
            Turn one long video into a week of short-form clips.
          </h1>
          <p className="mt-6 max-w-2xl text-base leading-relaxed text-ink-muted">
            ClipForge finds the moments worth keeping in a long recording and
            renders vertical, captioned clips for YouTube Shorts, Instagram Reels
            and TikTok — running entirely on your own machine, with your own API
            key.
          </p>
          <ul className="mt-8 flex flex-wrap gap-2.5 text-xs text-ink-muted">
            {["Runs on your machine", "No cloud account", "Your API key stays local"].map(
              (claim) => (
                <li
                  key={claim}
                  className="rounded-full border border-line bg-surface px-3 py-1.5"
                >
                  {claim}
                </li>
              ),
            )}
          </ul>
        </section>

        <VideoSourcePanel />

        <div className="mt-14">
          <JobList />
        </div>

        <section aria-labelledby="pipeline-heading" className="mt-14">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2
                id="pipeline-heading"
                className="text-xl font-semibold tracking-tight text-ink"
              >
                How ClipForge works
              </h2>
              <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-ink-muted">
                The pipeline below is the plan for the coming phases. Only the
                first step is next; nothing below it runs yet.
              </p>
            </div>
            <span className="text-[11px] tracking-wider text-ink-faint uppercase">
              Phases 2–6
            </span>
          </div>
          <div className="mt-6">
            <PipelinePreview />
          </div>
        </section>

        <section aria-labelledby="status-heading" className="mt-14">
          <h2
            id="status-heading"
            className="text-xl font-semibold tracking-tight text-ink"
          >
            Build status
          </h2>
          <div className="mt-6 grid gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-line bg-surface p-6">
              <h3 className="text-sm font-semibold text-ok">Working today</h3>
              <ul className="mt-4 space-y-2.5">
                {WORKING_TODAY.map((item) => (
                  <li key={item} className="flex gap-3 text-sm text-ink-muted">
                    <span aria-hidden="true" className="mt-1.5 size-1.5 shrink-0 rounded-full bg-ok" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-2xl border border-line bg-surface p-6">
              <h3 className="text-sm font-semibold text-ink-muted">
                On the roadmap
              </h3>
              <ul className="mt-4 space-y-2.5">
                {ON_THE_ROADMAP.map((item) => (
                  <li key={item} className="flex gap-3 text-sm text-ink-muted">
                    <span aria-hidden="true" className="mt-1.5 size-1.5 shrink-0 rounded-full bg-line-strong" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-6 text-xs text-ink-faint">
          <p>ClipForge · Phase 2 ingestion · API at {apiBaseUrl}</p>
          <p>Local-first. Nothing leaves your machine unless you configure it to.</p>
        </div>
      </footer>
    </div>
  );
}
