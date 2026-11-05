"use client";

import { useState } from "react";

/**
 * A video preview that says so when it cannot be played.
 *
 * The file is served through the API's media route, so the frontend never
 * knows where it is. A preview that fails — the file has gone, the route
 * is broken, the browser cannot decode it — has to say that, rather than
 * showing an empty black rectangle that looks like a video with nothing in
 * it.
 */
export default function MediaPreview({
  src,
  label,
  className = "mt-2 w-full max-w-sm rounded",
}: {
  /** The media route to play. */
  src: string;
  /** What this preview shows, for a screen reader. */
  label: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);

  if (failed) {
    return (
      <p
        role="status"
        className="mt-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[11px] text-warn"
      >
        The preview of {label} could not be loaded. The download link still
        works if the file is there.
      </p>
    );
  }

  return (
    <video
      controls
      preload="metadata"
      src={src}
      aria-label={label}
      onError={() => setFailed(true)}
      className={className}
    />
  );
}
