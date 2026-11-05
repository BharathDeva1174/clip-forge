import type { Metadata } from "next";

import ServiceStatusPanel from "@/components/ServiceStatusPanel";

export const metadata: Metadata = {
  title: "Status",
};

/**
 * The service status.
 *
 * The header carries a small indicator that says whether the backend is
 * there. This page says what that actually means: whether the
 * configuration is good, whether the speech model is already cached, and
 * which parts of the pipeline are available.
 */
export default function StatusPage() {
  return (
    <div className="pt-10">
      <div className="pb-8">
        <p className="text-[11px] font-semibold tracking-[0.2em] text-ember-400 uppercase">
          Status
        </p>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight text-ink">
          Is ClipForge ready?
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-muted">
          What this build can do, and whether everything it needs is in
          place. Everything here runs on this machine.
        </p>
      </div>
      <ServiceStatusPanel />
    </div>
  );
}
