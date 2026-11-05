/**
 * How to talk about a job's source.
 *
 * The database stores either a local filename or a full YouTube URL, and
 * neither is a good heading: a filename is fine, but a URL like
 * `https://www.youtube.com/watch?v=0BjlBnfHcHM` is noise. These helpers
 * give every screen the same honest, short name — a YouTube video is
 * called a YouTube video and its id is shown beside it, never instead of
 * the truth (the full reference stays available as the detail line).
 */

export type SourceDisplay = {
  /** The short, human name for a heading or a row title. */
  title: string;
  /** The kind of source, lower case ("youtube" / "upload"). */
  kind: string;
  /** A short technical detail, when there is one (the YouTube id). */
  detail: string | null;
};

/** Pull the video id out of a YouTube URL, or null when there is none. */
export function youtubeVideoId(url: string): string | null {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.replace(/^www\./, "");
    if (host === "youtu.be") {
      const id = parsed.pathname.split("/").find(Boolean);
      return id ?? null;
    }
    if (host.endsWith("youtube.com")) {
      return parsed.searchParams.get("v");
    }
    return null;
  } catch {
    return null;
  }
}

/** The display name for any source reference. */
export function sourceDisplay(
  sourceType: string,
  sourceReference: string | null,
): SourceDisplay {
  const reference = sourceReference || "";
  if (sourceType === "youtube") {
    const id = youtubeVideoId(reference);
    return {
      title: id ? "YouTube video" : reference || "YouTube video",
      kind: "youtube",
      detail: id,
    };
  }
  return {
    title: reference || "Uploaded video",
    kind: "upload",
    detail: null,
  };
}
