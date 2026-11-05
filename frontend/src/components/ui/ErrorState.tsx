"use client";

/**
 * A consistent error surface: a message, an optional code, and an optional
 * retry action. Never shows stack traces, paths, or internal detail.
 */

type Props = {
  message: string;
  code?: string | null;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
};

export default function ErrorState({
  message,
  code,
  onRetry,
  retryLabel = "Try again",
  className,
}: Props) {
  return (
    <div
      className={`rounded-2xl border border-bad/40 bg-bad/10 p-5 ${className ?? ""}`}
      role="alert"
    >
      <p className="text-sm text-bad">{message}</p>
      {code && (
        <p className="mt-1 font-mono text-[11px] text-ink-faint">{code}</p>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          {retryLabel}
        </button>
      )}
    </div>
  );
}
