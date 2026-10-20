"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchBackendHealth, type HealthStatus } from "@/lib/health";

const CHECKING: HealthStatus = { state: "checking" };

/**
 * Shows whether the ClipForge API is reachable.
 *
 * The check runs once on mount and can be retried manually. There is no
 * polling: the backend is local, and the user is usually waiting for it to
 * start rather than for a status to change.
 */
export default function HealthIndicator({ baseUrl }: { baseUrl: string }) {
  const [status, setStatus] = useState<HealthStatus>(CHECKING);
  // Guards against an older, slower request overwriting a newer result.
  const latestRequest = useRef(0);

  const runCheck = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const result = await fetchBackendHealth(baseUrl);
    if (requestId === latestRequest.current) {
      setStatus(result);
    }
  }, [baseUrl]);

  useEffect(() => {
    void runCheck();
  }, [runCheck]);

  const retry = () => {
    setStatus(CHECKING);
    void runCheck();
  };

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-col items-end gap-1.5"
    >
      <div className="flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium">
        {status.state === "checking" && (
          <>
            <span
              aria-hidden="true"
              className="size-2 animate-pulse rounded-full bg-warn"
            />
            <span className="text-ink-muted">Checking backend…</span>
          </>
        )}

        {status.state === "online" && (
          <>
            <span
              aria-hidden="true"
              className="size-2 rounded-full bg-ok shadow-[0_0_8px] shadow-ok/60"
            />
            <span className="text-ink">Backend online</span>
            <span className="hidden text-ink-faint sm:inline">
              {status.service}
            </span>
          </>
        )}

        {status.state === "offline" && (
          <>
            <span
              aria-hidden="true"
              className="size-2 rounded-full bg-bad shadow-[0_0_8px] shadow-bad/60"
            />
            <span className="text-ink">Backend unreachable</span>
            <button
              type="button"
              onClick={retry}
              className="rounded-full border border-line-strong px-2 py-0.5 text-[11px] text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ember-500"
            >
              Retry
            </button>
          </>
        )}
      </div>

      {status.state === "offline" && (
        <p className="max-w-xs text-right text-[11px] leading-snug text-ink-faint">
          {status.reason}
        </p>
      )}
    </div>
  );
}
