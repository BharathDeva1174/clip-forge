"use client";

import Stepper from "@/components/ui/Stepper";

/**
 * The workflow indicator on the dashboard.
 *
 * It states the pipeline in the order it happens — source, transcript,
 * moments, clips, short-form versions — with every step quiet, because on
 * the dashboard there is no single job to attach a state to. It does not
 * claim progress where the backend has none.
 */

/** One step in the workflow, in pipeline order. */
const STEPS: { label: string; hint: string }[] = [
  { label: "Source", hint: "A video you uploaded or linked." },
  { label: "Transcript", hint: "The speech turned into timed text." },
  { label: "Moments", hint: "Highlights the model found worth keeping." },
  { label: "Clips", hint: "Each accepted moment, rendered to a file." },
  {
    label: "Short-form versions",
    hint: "The portrait 9:16 copies with captions burned in.",
  },
];

export default function WorkflowIndicator() {
  return (
    <nav
      aria-label="Workflow"
      className="card mt-8 mb-8 px-5 py-4"
    >
      <Stepper
        steps={STEPS.map((step) => ({
          label: step.label,
          state: "waiting" as const,
          hint: step.hint,
        }))}
      />
      <p className="mt-3 text-[11px] text-ink-faint">
        Each video moves through these stages as the worker finishes them.
        Open a video from the library below to see where it is.
      </p>
    </nav>
  );
}
