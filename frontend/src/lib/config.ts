/**
 * Public runtime configuration for the frontend.
 *
 * Only values that are safe to ship to the browser live here. Secrets must
 * never be placed in a `NEXT_PUBLIC_*` variable: Next.js inlines those into
 * the client bundle, so anything in them is readable by anyone using the app.
 *
 * `NEXT_PUBLIC_*` values are read from `frontend/.env.local` at build time, so
 * the frontend has to be rebuilt after changing them.
 */

export const DEFAULT_API_BASE_URL = "http://localhost:8000";

/** Base URL of the ClipForge API, without a trailing slash. */
export function getApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return (configured || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
}
