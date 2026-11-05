import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import AppHeader from "@/components/AppHeader";
import { getApiBaseUrl } from "@/lib/config";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "ClipForge — Long videos into short-form clips",
    template: "%s · ClipForge",
  },
  description:
    "ClipForge is a local-first app that turns long-form videos into short-form clips for YouTube Shorts, Instagram Reels and TikTok.",
  applicationName: "ClipForge",
};

export const viewport: Viewport = {
  themeColor: "#0a0b0d",
  colorScheme: "dark",
};

/**
 * The application shell.
 *
 * The header, the navigation and the footer are here rather than in each
 * page, so every route looks like the same product and the navigation is
 * in one place. A page only decides what is in the middle.
 */
export default function RootLayout({ children }: LayoutProps<"/">) {
  const apiBaseUrl = getApiBaseUrl();

  return (
    <html
      lang="en"
      /*
       * suppressHydrationWarning: browser extensions — security scanners
       * in particular — tag elements with their own attributes
       * (bis_skin_checked and friends) before React hydrates, and the
       * dev overlay then reports a hydration mismatch that the app did
       * not cause. The warning is suppressed at the two roots the
       * extensions most often touch; the app itself injects nothing.
       */
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body suppressHydrationWarning className="min-h-full bg-canvas font-sans text-ink">
        <div className="flex min-h-full flex-col">
          <AppHeader />

          <main className="mx-auto w-full max-w-6xl flex-1 px-6 pb-20">
            {children}
          </main>

          <footer className="border-t border-line">
            <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-6 text-xs text-ink-faint">
              <p>ClipForge · API at {apiBaseUrl}</p>
              <p>
                Local-first. Nothing leaves your machine unless you configure it
                to.
              </p>
            </div>
          </footer>
        </div>
      </body>
    </html>
  );
}
