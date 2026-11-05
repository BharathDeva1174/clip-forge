"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import HealthIndicator from "@/components/HealthIndicator";
import { getApiBaseUrl } from "@/lib/config";

const NAVIGATION = [
  {
    href: "/",
    label: "Videos",
    /** The library and the workflow of one video are the same place. */
    matches: (pathname: string) =>
      pathname === "/" || pathname.startsWith("/jobs"),
  },
  {
    href: "/status",
    label: "Status",
    matches: (pathname: string) => pathname.startsWith("/status"),
  },
  {
    href: "/integrations",
    label: "Integrations",
    matches: (pathname: string) => pathname.startsWith("/integrations"),
  },
];

/**
 * The application header.
 *
 * It is shared by every route, so the navigation is in one place and the
 * health indicator is on every page. The indicator links to the status
 * view, which says what it means.
 */
export default function AppHeader() {
  const apiBaseUrl = getApiBaseUrl();
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-10 border-b border-line/80 bg-canvas/75 backdrop-blur-xl">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-6 py-3">
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="flex size-9 items-center justify-center rounded-xl bg-gradient-to-br from-ember-400 to-ember-600 font-mono text-sm font-bold text-canvas shadow-[0_2px_10px_-2px_rgba(255,122,24,0.5)]"
          >
            CF
          </span>
          <div className="leading-tight">
            <p className="text-sm font-semibold tracking-tight text-ink">
              ClipForge
            </p>
            <p className="hidden text-[11px] text-ink-faint sm:block">
              Local-first clip studio
            </p>
          </div>
        </div>

        <nav
          aria-label="Main"
          className="flex items-center gap-0.5 rounded-xl border border-line bg-surface/60 p-0.5"
        >
          {NAVIGATION.map((item) => {
            const current = item.matches(pathname);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={current ? "page" : undefined}
                className={
                  current
                    ? "rounded-[10px] bg-raised px-3 py-1.5 text-xs font-medium text-ink shadow-[0_1px_2px_rgba(0,0,0,0.4)]"
                    : "rounded-[10px] px-3 py-1.5 text-xs text-ink-muted transition-colors hover:text-ink"
                }
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <Link href="/status" className="shrink-0">
          <HealthIndicator baseUrl={apiBaseUrl} />
        </Link>
      </div>
    </header>
  );
}
