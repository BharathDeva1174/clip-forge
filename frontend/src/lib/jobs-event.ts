/**
 * A tiny event bus so that a form in one part of the page can ask the job
 * list in another part to refresh, without turning the whole page into a
 * client component.
 */

export const JOBS_CHANGED_EVENT = "clipforge:jobs-changed";

export function notifyJobsChanged(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new Event(JOBS_CHANGED_EVENT));
}
