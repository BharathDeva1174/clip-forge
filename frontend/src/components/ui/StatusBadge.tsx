"use client";

/**
 * A status badge whose colour and text come from one place.
 *
 * Reusing this everywhere keeps the library's status vocabulary consistent
 * (queued, processing, ready, failed, cancelled, unknown) instead of every
 * component re-inventing the same border/background/text triple.
 *
 * Every badge carries a small status dot, so the state is readable even
 * where the label is truncated — and colour, not just words, separates a
 * running job from a finished one.
 */

type StatusVariant =
  | "default"
  | "queued"
  | "validating"
  | "downloading"
  | "inspecting"
  | "extracting_audio"
  | "transcribing"
  | "ready"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown";

const VARIANTS: Record<StatusVariant, { badge: string; dot: string }> = {
  default: { badge: "border-line-strong bg-surface text-ink-faint", dot: "bg-ink-faint" },
  queued: { badge: "border-line-strong bg-surface text-ink-muted", dot: "bg-ink-faint" },
  validating: { badge: "border-warn/30 bg-warn/10 text-warn", dot: "bg-warn" },
  downloading: { badge: "border-warn/30 bg-warn/10 text-warn", dot: "bg-warn" },
  inspecting: { badge: "border-warn/30 bg-warn/10 text-warn", dot: "bg-warn" },
  extracting_audio: { badge: "border-warn/30 bg-warn/10 text-warn", dot: "bg-warn" },
  transcribing: { badge: "border-warn/30 bg-warn/10 text-warn", dot: "bg-warn" },
  ready: { badge: "border-ok/30 bg-ok/10 text-ok", dot: "bg-ok" },
  completed: { badge: "border-ok/30 bg-ok/10 text-ok", dot: "bg-ok" },
  failed: { badge: "border-bad/30 bg-bad/10 text-bad", dot: "bg-bad" },
  cancelled: { badge: "border-line-strong bg-surface text-ink-faint", dot: "bg-ink-faint" },
  unknown: { badge: "border-line-strong bg-surface text-ink-faint", dot: "bg-ink-faint" },
};

/** Variants whose work is still moving get a gently pulsing dot. */
const LIVE: ReadonlySet<string> = new Set([
  "validating",
  "downloading",
  "inspecting",
  "extracting_audio",
  "transcribing",
]);

function titleCase(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export default function StatusBadge({
  status,
  label,
}: {
  status: StatusVariant;
  label?: string;
}) {
  const variant = VARIANTS[status] ?? VARIANTS.default;
  const live = LIVE.has(status);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${variant.badge}`}
    >
      <span
        aria-hidden="true"
        className={`size-1.5 shrink-0 rounded-full ${variant.dot} ${
          live ? "animate-pulse" : ""
        }`}
      />
      {label ?? titleCase(status)}
    </span>
  );
}
