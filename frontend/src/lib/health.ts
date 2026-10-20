/**
 * Backend health check.
 *
 * The logic lives here, apart from the React component, so it can be tested
 * without a DOM and so the component stays a thin renderer.
 */

export type HealthStatus =
  | { state: "checking" }
  | { state: "online"; service: string }
  | { state: "offline"; reason: string };

export type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

export const HEALTH_TIMEOUT_MS = 5000;

/**
 * Ask the backend for its health contract.
 *
 * Never throws: an unreachable backend is a normal state for this app, so the
 * result is always a `HealthStatus`.
 */
export async function fetchBackendHealth(
  baseUrl: string,
  fetchImpl: FetchLike = (input, init) => fetch(input, init),
  timeoutMs: number = HEALTH_TIMEOUT_MS,
): Promise<HealthStatus> {
  const url = `${baseUrl.replace(/\/+$/, "")}/health`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchImpl(url, {
      headers: { accept: "application/json" },
      signal: controller.signal,
    });

    if (!response.ok) {
      return {
        state: "offline",
        reason: `The backend answered with HTTP ${response.status}.`,
      };
    }

    const payload: unknown = await response.json();
    if (!isHealthPayload(payload)) {
      return {
        state: "offline",
        reason: "The backend answered with an unexpected health payload.",
      };
    }

    return { state: "online", service: payload.service };
  } catch {
    return {
      state: "offline",
      reason: `Could not reach the backend at ${baseUrl}. Is it running?`,
    };
  } finally {
    clearTimeout(timer);
  }
}

function isHealthPayload(
  value: unknown,
): value is { status: string; service: string } {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.status === "string" &&
    typeof candidate.service === "string"
  );
}
