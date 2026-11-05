"use client";

/**
 * Two button sizes and three colours that cover every call in the app.
 *
 * `variant="primary"` is the one filled accent: a real ember button with a
 * soft glow, reserved for the single main action of a panel (Upload, Add
 * link, Render clip). `variant="secondary"` is the quiet outline style
 * (every secondary action). `variant="danger"` is the red-tinted style for
 * destructive and cancelling actions. `size="small"` fits dense tables and
 * cards; `size="medium"` fits page headers and forms.
 */

type Props = {
  variant?: "primary" | "secondary" | "danger";
  size?: "small" | "medium";
  disabled?: boolean;
  children: React.ReactNode;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "variant" | "size">;

export default function Button({
  variant = "secondary",
  size = "medium",
  disabled,
  className,
  ...props
}: Props) {
  const base =
    "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-45";
  const variantStyles =
    variant === "primary"
      ? "bg-gradient-to-b from-ember-400 to-ember-500 text-canvas shadow-[0_1px_2px_rgba(0,0,0,0.4),0_4px_14px_-4px_rgba(255,122,24,0.45)] hover:from-ember-300 hover:to-ember-400 hover:shadow-[0_1px_2px_rgba(0,0,0,0.4),0_6px_18px_-4px_rgba(255,122,24,0.55)] active:translate-y-px disabled:from-ember-400 disabled:to-ember-500 disabled:shadow-none"
      : variant === "danger"
        ? "border border-bad/40 bg-bad/10 text-bad hover:border-bad/70 hover:bg-bad/15"
        : "border border-line-strong bg-raised/60 text-ink-muted hover:border-ember-500/60 hover:text-ink";
  const sizeStyles =
    size === "small" ? "px-2.5 py-1 text-[11px]" : "px-3.5 py-1.5 text-xs";

  return (
    <button
      type="button"
      disabled={disabled}
      className={`${base} ${variantStyles} ${sizeStyles} ${className ?? ""}`}
      {...props}
    />
  );
}
