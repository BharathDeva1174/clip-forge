import VideoSourcePanel from "@/components/VideoSourcePanel";
import JobList from "@/components/JobList";
import WorkflowIndicator from "@/components/ui/WorkflowIndicator";

/**
 * The dashboard.
 *
 * It is the way in: add a video, and then the library below shows every
 * video that has been submitted and what has become of it. Each entry
 * leads to the workflow, where the transcript, the moments, the clips and
 * the short-form versions live.
 */

/** The three promises of the product, as small facts rather than slogans. */
const FACTS = [
  "Runs entirely on this machine",
  "Offline speech-to-text",
  "Your own AI provider key",
  "9:16 output, captions burned in",
];

export default function Home() {
  return (
    <>
      <section className="mb-10 mt-10">
        <div className="max-w-3xl">
          <p className="eyebrow">ClipForge</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tighter text-ink sm:text-5xl sm:leading-[1.05]">
            Turn one long video into{" "}
            <span className="bg-gradient-to-r from-ember-300 via-ember-400 to-ember-500 bg-clip-text text-transparent">
              short-form clips.
            </span>
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-relaxed text-ink-muted">
            ClipForge finds the moments worth keeping in a long recording and
            renders vertical, captioned clips for YouTube Shorts, Instagram
            Reels and TikTok — running entirely on your own machine, with your
            own API key.
          </p>
          <ul className="mt-5 flex flex-wrap gap-x-5 gap-y-2">
            {FACTS.map((fact) => (
              <li
                key={fact}
                className="flex items-center gap-2 text-xs text-ink-faint"
              >
                <span
                  aria-hidden="true"
                  className="size-1 rounded-full bg-ember-500/80"
                />
                {fact}
              </li>
            ))}
          </ul>
        </div>
      </section>

      <WorkflowIndicator />

      <VideoSourcePanel />

      <div className="mt-12">
        <JobList />
      </div>
    </>
  );
}
