"use client";

/**
 * A centred empty state: a heading, a one-line explanation, and an optional
 * action. One component so every empty screen looks like the same product.
 */

type Props = {
  heading: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
};

export default function EmptyState({
  heading,
  description,
  action,
  className,
}: Props) {
  return (
    <div
      className={`rounded-2xl border border-dashed border-line-strong bg-surface px-6 py-10 text-center ${className ?? ""}`}
    >
      <p className="text-sm font-medium text-ink">{heading}</p>
      {description && (
        <p className="mt-1 text-xs text-ink-muted">{description}</p>
      )}
      {action && (
        <div className="mt-4">{action}</div>
      )}
    </div>
  );
}
