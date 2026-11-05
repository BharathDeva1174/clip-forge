"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import MediaPreview from "@/components/MediaPreview";

import {
  ApiError,
  DEFAULT_FORMAT_SETTINGS,
  FORMAT_RESOLUTIONS,
  listFormats,
  listFonts,
  requestFormat,
  retryFormat,
  type ClipFormat,
  type FormatSettings,
  type LayoutMode,
} from "@/lib/api";

/** How often a rendering artifact is asked about. */
const POLL_INTERVAL_MS = 2500;

type PanelState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "requesting" }
  | { phase: "ready"; formats: ClipFormat[] }
  | { phase: "error"; message: string; code: string | null };

/** The stages a formatted clip passes through. */
const WORKING_STAGES = ["queued", "preparing", "rendering", "verifying"];

function formatBytes(bytes: number | null): string {
  if (!bytes) {
    return "";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** A labelled control. The help text is a sibling, so the name is clean. */
function Control({
  id,
  label,
  help,
  children,
}: {
  id: string;
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="block text-[11px] text-ink-muted">
      <label htmlFor={id}>{label}</label>
      {children}
      {help && <p className="mt-1 text-[10px] text-ink-faint">{help}</p>}
    </div>
  );
}

/**
 * The formatting controls for one finished clip.
 *
 * It configures the portrait version, asks for it, follows it while it
 * renders, and shows the finished video. The original clip stays exactly
 * where it was: this panel only ever adds a second artifact.
 */
export default function FormatPanel({ clipId }: { clipId: string }) {
  const [state, setState] = useState<PanelState>({ phase: "idle" });
  const [settings, setSettings] = useState<FormatSettings>(DEFAULT_FORMAT_SETTINGS);
  const [fonts, setFonts] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const formats = await listFormats(clipId);
      if (mounted.current) {
        setState({ phase: "ready", formats });
      }
    } catch (cause) {
      if (mounted.current) {
        setState({
          phase: "error",
          message:
            cause instanceof ApiError
              ? cause.message
              : "The formatted clips could not be loaded.",
          code: cause instanceof ApiError ? cause.code : null,
        });
      }
    }
  }, [clipId]);

  useEffect(() => {
    // The state is set in the promise's callbacks rather than in the effect
    // body, so the render is not cascaded.
    let cancelled = false;
    void listFormats(clipId).then(
      (formats) => {
        if (!cancelled) {
          setState({ phase: "ready", formats });
        }
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        setState({
          phase: "error",
          message:
            cause instanceof ApiError
              ? cause.message
              : "The formatted clips could not be loaded.",
          code: cause instanceof ApiError ? cause.code : null,
        });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [clipId]);

  // While something is rendering, keep asking about it.
  const rendering =
    state.phase === "ready"
      ? (state.formats ?? []).some((format) =>
          WORKING_STAGES.includes(format.status),
        )
      : false;

  useEffect(() => {
    if (!rendering) {
      return;
    }
    const timer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [rendering, refresh]);

  useEffect(() => {
    let cancelled = false;
    void listFonts()
      .then((found) => {
        if (!cancelled) {
          setFonts(found);
        }
      })
      // A machine with no readable font directory simply offers the default.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = useCallback(async () => {
    setBusy(true);
    setState({ phase: "requesting" });
    try {
      await requestFormat(clipId, settings);
      if (mounted.current) {
        await refresh();
      }
    } catch (cause) {
      if (mounted.current) {
        setState({
          phase: "error",
          message:
            cause instanceof ApiError
              ? cause.message
              : "The formatted clip could not be requested.",
          code: cause instanceof ApiError ? cause.code : null,
        });
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }, [clipId, settings, refresh]);

  const retry = useCallback(
    async (formatId: string) => {
      setBusy(true);
      try {
        await retryFormat(formatId);
        if (mounted.current) {
          await refresh();
        }
      } catch (cause) {
        if (mounted.current) {
          setState({
            phase: "error",
            message:
              cause instanceof ApiError
                ? cause.message
                : "The formatted clip could not be rendered again.",
            code: cause instanceof ApiError ? cause.code : null,
          });
        }
      } finally {
        if (mounted.current) {
          setBusy(false);
        }
      }
    },
    [refresh],
  );

  const formats = state.phase === "ready" ? (state.formats ?? []) : [];

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        className="rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
      >
        {open ? "Hide the short-form version" : "Make a short-form version"}
      </button>

      {open && (
        <div className="mt-2 rounded-lg border border-line bg-surface-raised px-3 py-3">
          <p className="text-[11px] leading-relaxed text-ink-muted">
            A portrait 9:16 copy of this clip, with the captions burned in.
            The clip you are looking at is not changed.
          </p>

          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Control id="format-resolution" label="Resolution">
              <select
                id="format-resolution"
                value={`${settings.width}x${settings.height}`}
                onChange={(event) => {
                  const [width, height] = event.target.value
                    .split("x")
                    .map((part) => Number(part));
                  setSettings((current) => ({ ...current, width, height }));
                }}
                className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
              >
                {FORMAT_RESOLUTIONS.map((option) => (
                  <option
                    key={option.label}
                    value={`${option.width}x${option.height}`}
                  >
                    {option.label}
                  </option>
                ))}
              </select>
            </Control>

            <Control id="format-layout" label="Layout">
              <select
                id="format-layout"
                value={settings.layout}
                onChange={(event) =>
                  setSettings((current) => ({
                    ...current,
                    layout: event.target.value as LayoutMode,
                  }))
                }
                className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
              >
                <option value="center_crop">
                  Centre crop — fill the screen, cut the sides
                </option>
                <option value="fit_pad">
                  Fit and pad — show the whole frame
                </option>
              </select>
            </Control>

            {settings.layout === "fit_pad" && (
              <Control id="format-padding" label="Padding colour">
                <input
                  id="format-padding"
                  type="text"
                  value={settings.padding_colour}
                  onChange={(event) =>
                    setSettings((current) => ({
                      ...current,
                      padding_colour: event.target.value,
                    }))
                  }
                  placeholder="#000000"
                  className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                />
              </Control>
            )}

            <div className="flex items-center gap-2 text-[11px] text-ink-muted">
              <input
                id="format-captions"
                type="checkbox"
                checked={settings.captions.enabled}
                onChange={(event) =>
                  setSettings((current) => ({
                    ...current,
                    captions: {
                      ...current.captions,
                      enabled: event.target.checked,
                    },
                  }))
                }
              />
              <label htmlFor="format-captions">Burn the captions in</label>
            </div>

            {settings.captions.enabled && (
              <>
                <Control
                  id="format-font"
                  label="Font"
                  help="The fonts installed on this machine."
                >
                  <select
                    id="format-font"
                    value={settings.captions.font_family}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        captions: {
                          ...current.captions,
                          font_family: event.target.value,
                        },
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  >
                    {(fonts.length > 0 ? fonts : ["Arial"]).map((font) => (
                      <option key={font} value={font}>
                        {font}
                      </option>
                    ))}
                  </select>
                </Control>

                <Control id="format-font-size" label="Font size">
                  <input
                    id="format-font-size"
                    type="number"
                    min={8}
                    max={200}
                    value={settings.captions.font_size}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        captions: {
                          ...current.captions,
                          font_size: Number(event.target.value),
                        },
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  />
                </Control>

                <Control id="format-text-colour" label="Text colour">
                  <input
                    id="format-text-colour"
                    type="text"
                    value={settings.captions.text_colour}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        captions: {
                          ...current.captions,
                          text_colour: event.target.value,
                        },
                      }))
                    }
                    placeholder="#FFFFFF"
                    className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  />
                </Control>

                <Control id="format-outline-colour" label="Outline colour">
                  <input
                    id="format-outline-colour"
                    type="text"
                    value={settings.captions.outline_colour}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        captions: {
                          ...current.captions,
                          outline_colour: event.target.value,
                        },
                      }))
                    }
                    placeholder="#000000"
                    className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  />
                </Control>

                <Control id="format-outline" label="Outline thickness">
                  <input
                    id="format-outline"
                    type="number"
                    min={0}
                    max={10}
                    value={settings.captions.outline_thickness}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        captions: {
                          ...current.captions,
                          outline_thickness: Number(event.target.value),
                        },
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  />
                </Control>

                <Control id="format-position" label="Position">
                  <select
                    id="format-position"
                    value={settings.captions.alignment}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        captions: {
                          ...current.captions,
                          alignment: Number(event.target.value),
                        },
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-2.5 py-1.5 text-xs text-ink"
                  >
                    <option value={1}>Bottom</option>
                    <option value={2}>Middle</option>
                    <option value={3}>Top</option>
                  </select>
                </Control>
              </>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void submit()}
              className="rounded-lg border border-ember-500/50 bg-ember-500/10 px-3 py-1.5 text-[11px] text-ember-300 transition-colors hover:border-ember-500 disabled:opacity-50"
            >
              {busy ? "Requesting…" : "Render the short-form version"}
            </button>
            <span className="text-[10px] text-ink-faint">
              {settings.width}×{settings.height} ·{" "}
              {settings.layout === "center_crop" ? "centre crop" : "fit and pad"}
              {settings.captions.enabled ? " · captions on" : " · no captions"}
            </span>
          </div>

          {state.phase === "error" && (
            <p role="alert" className="mt-2 text-[11px] text-bad">
              {state.message}
              {state.code ? ` (${state.code})` : ""}
            </p>
          )}

          {formats.length > 0 && (
            <ul className="mt-3 space-y-2">
              {formats.map((format) => (
                <li
                  key={format.id}
                  className="rounded-lg border border-line bg-surface px-2.5 py-2"
                >
                  {format.status === "failed" ? (
                    <>
                      <p className="text-[11px] text-bad">
                        {format.error?.message ??
                          "The formatted clip could not be rendered."}
                      </p>
                      {format.error?.code && (
                        <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
                          {format.error.code}
                        </p>
                      )}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void retry(format.id)}
                        className="mt-1.5 rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
                      >
                        Render again
                      </button>
                    </>
                  ) : format.status === "completed" ? (
                    <>
                      <p className="text-[11px] text-ink-muted">
                        {format.width}×{format.height}
                        {format.actual_duration_seconds !== null
                          ? ` · ${format.actual_duration_seconds.toFixed(2)}s`
                          : ""}
                        {format.output_size_bytes
                          ? ` · ${formatBytes(format.output_size_bytes)}`
                          : ""}
                      </p>
                      {format.captions_enabled && !format.captions_applied && (
                        <p className="mt-0.5 text-[10px] text-warn">
                          No caption text overlapped this clip, so it was
                          rendered without captions.
                        </p>
                      )}
                      {format.captions_applied && (
                        <p className="mt-0.5 text-[10px] text-ink-faint">
                          {format.caption_count} captions burned in.
                        </p>
                      )}
                      {format.media_url && (
                        <>
                          <MediaPreview
                            src={format.media_url}
                            label="the short-form version"
                            className="mt-2 max-h-64 w-auto rounded"
                          />
                          <a
                            href={format.media_url}
                            download
                            className="mt-1.5 inline-block rounded-lg border border-line-strong px-2 py-1 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
                          >
                            Download
                          </a>
                        </>
                      )}
                    </>
                  ) : (
                    <p className="text-[11px] text-ink-muted" role="status">
                      {format.stage}…
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
