"use client";

/**
 * The pipeline stepper, shared by the dashboard and every job page.
 *
 * Five numbered steps with connecting lines, in the order the worker does
 * them. `done` fills the circle with the ok colour, `active` warms it with
 * the accent, `waiting` keeps it quiet. The component never invents
 * progress: the caller passes the state of each step from real backend
 * fields.
 */

export type StepState = "done" | "active" | "waiting";

export type Step = {
  label: string;
  state: StepState;
  /** A short explanation, shown as the step's title attribute. */
  hint?: string;
};

const CIRCLE_STYLES: Record<StepState, string> = {
  done: "border-ok/60 bg-ok/15 text-ok",
  active: "border-ember-500/60 bg-ember-500/15 text-ember-300",
  waiting: "border-line-strong bg-raised text-ink-faint",
};

const LINE_STYLES: Record<StepState, string> = {
  done: "bg-ok/40",
  active: "bg-line-strong",
  waiting: "bg-line",
};

export default function Stepper({ steps }: { steps: Step[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-y-2">
      {steps.map((step, index) => (
        <li key={step.label} className="flex items-center">
          {index > 0 && (
            <span
              aria-hidden="true"
              className={`mx-1.5 h-px w-5 sm:w-7 ${LINE_STYLES[step.state]}`}
            />
          )}
          <span
            title={step.hint}
            className="inline-flex items-center gap-2 rounded-full border border-line bg-raised/50 py-1 pl-1.5 pr-3"
          >
            <span
              aria-hidden="true"
              className={`numeric flex size-5 shrink-0 items-center justify-center rounded-full border font-mono text-[10px] ${CIRCLE_STYLES[step.state]}`}
            >
              {step.state === "done" ? "✓" : index + 1}
            </span>
            <span
              className={`text-[11px] font-medium ${
                step.state === "waiting" ? "text-ink-faint" : "text-ink"
              }`}
            >
              {step.label}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}
