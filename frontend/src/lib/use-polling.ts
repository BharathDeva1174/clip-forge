"use client";

import { useEffect, useRef } from "react";

/**
 * Ask something again and again, but only while it can still change.
 *
 * The rules this hook enforces, so that every view follows the same ones:
 *
 * - there is **one** interval, never one per render;
 * - it runs only while `active` is true, so a terminal resource stops
 *   being asked about;
 * - it is cleared when the component goes away, so leaving a view does not
 *   leave a request running;
 * - the callback is always the current one, so an interval never calls a
 *   stale closure.
 *
 * The callback is not called immediately: the caller does that itself, in
 * the effect that loads the first answer.
 */

export interface PollingOptions {
  /** Whether the thing being watched can still change. */
  active: boolean;
  /** How long to wait between two requests, in milliseconds. */
  intervalMs: number;
}

export function usePolling(
  callback: () => void,
  { active, intervalMs }: PollingOptions,
): void {
  const latest = useRef(callback);

  useEffect(() => {
    latest.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setInterval(() => {
      latest.current();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
}
