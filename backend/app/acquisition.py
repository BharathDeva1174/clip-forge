"""Acquisition of remote source media.

Acquisition is deliberately kept behind a narrow interface so that the rest of
the application never depends on a particular downloader.

Security notes:

* The only URL that ever reaches an acquirer is a **canonical URL produced by**
  :func:`app.youtube.parse_youtube_url`, which has already passed exact
  hostname validation. There is no fallback that fetches an arbitrary URL.
* The downloader is started with an argument list and never through a shell,
  so nothing in a filename or URL can become a command.
* Downloads are bounded by size and by time, and the child process is
  terminated when the timeout expires or the worker shuts down.
* User-facing error messages are generic. Detail is kept in the worker log,
  where it can be truncated and reviewed, and never stored in the job record.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from app.errors import ErrorCode
from app.youtube import YouTubeVideo

logger = logging.getLogger("clipforge.acquisition")

#: How much of the downloader's output is kept for the server-side log.
_MAX_OUTPUT_BYTES = 4000


class AcquisitionError(Exception):
    """The remote source could not be acquired."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SourceAcquirer(Protocol):
    """Fetches an authorised remote source into a local directory."""

    name: str

    def available(self) -> bool:
        """Whether this acquirer can run in the current environment."""
        ...

    def acquire(
        self,
        video: YouTubeVideo,
        destination_dir: Path,
        *,
        max_bytes: int,
        timeout_seconds: int,
        on_tick: object | None = None,
    ) -> Path:
        """Download the video and return the path of the downloaded file."""
        ...


class DisabledAcquirer:
    """Refuses every acquisition.

    Used when no acquisition mechanism is configured. It fails loudly and
    points the user at local uploads instead of pretending to work.
    """

    name = "disabled"

    def available(self) -> bool:
        return False

    def acquire(
        self,
        video: YouTubeVideo,
        destination_dir: Path,
        *,
        max_bytes: int,
        timeout_seconds: int,
        on_tick: object | None = None,
    ) -> Path:
        raise AcquisitionError(
            ErrorCode.YOUTUBE_ACQUISITION_UNAVAILABLE.value,
            "Downloading from YouTube is not enabled in this installation. "
            "Upload a local copy of the video instead.",
        )


class YtDlpAcquirer:
    """Acquires a public YouTube video with yt-dlp.

    yt-dlp is started with ``python -m yt_dlp`` so it always uses the
    interpreter that is running the worker. It is given a canonical,
    already-validated URL and no credentials, so it can only reach videos that
    are publicly accessible without signing in.
    """

    name = "yt-dlp"

    def __init__(self, *, python_executable: str | None = None) -> None:
        self.python_executable = python_executable or sys.executable
        self._process: subprocess.Popen[bytes] | None = None

    def available(self) -> bool:
        """Whether the yt-dlp package is importable."""
        try:
            return importlib.util.find_spec("yt_dlp") is not None
        except (ImportError, ValueError):
            return False

    def acquire(
        self,
        video: YouTubeVideo,
        destination_dir: Path,
        *,
        max_bytes: int,
        timeout_seconds: int,
        on_tick: object | None = None,
        on_progress: object | None = None,
    ) -> Path:
        """Download the video into ``destination_dir`` and return its path."""
        if not self.available():
            raise AcquisitionError(
                ErrorCode.YOUTUBE_ACQUISITION_UNAVAILABLE.value,
                "Downloading from YouTube is not enabled in this installation. "
                "Upload a local copy of the video instead.",
            )

        destination_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.python_executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-warnings",
            # Progress is emitted to stderr so the worker can report it to
            # the job record while the download runs.
            "--progress",
            "--no-color",
            "--no-simulate",
            "--max-filesize",
            str(max_bytes),
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            # YouTube serves video and audio as separate streams, so the best
            # progressive format often does not exist. Ask for the best video
            # plus the best audio and merge them, with a plain single format
            # as the fallback.
            "--format",
            "bv*[height<=1080]+ba/b",
            "--merge-output-format",
            "mp4",
            "--paths",
            f"home:{destination_dir}",
            "--output",
            "%(id)s.%(ext)s",
            video.canonical_url,
        ]

        logger.info(
            "acquiring youtube video %s into %s", video.video_id, destination_dir.name
        )

        # A retry reuses this directory (its name is the job id). Anything in
        # here belongs to a dead previous attempt -- the lease guarantees a
        # single live downloader per job -- so clear it before starting, or a
        # stale .part could collide with this attempt's download.
        destination_dir.mkdir(parents=True, exist_ok=True)
        for stale in destination_dir.iterdir():
            try:
                stale.unlink()
            except OSError:
                # A file still held open by a truly orphaned child cannot be
                # removed on Windows; yt-dlp overwrites it anyway.
                pass

        #: A child process's stdout/stderr must go to a file, never a pipe:
        #: a pipe fills after about 4 KB and the child blocks on write, which
        #: looks exactly like a hung download (see AGENTS.md). Redirect to
        #: log files in the destination directory instead.
        stdout_log = destination_dir / "yt-dlp.stdout.log"
        stderr_log = destination_dir / "yt-dlp.stderr.log"
        stdout_handle = open(stdout_log, "wb")
        stderr_handle = open(stderr_log, "wb")
        try:
            process = subprocess.Popen(  # noqa: S603 - argument list, never a shell
                args,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
            )
        except OSError as exc:
            stdout_handle.close()
            stderr_handle.close()
            raise AcquisitionError(
                ErrorCode.ACQUISITION_FAILED.value,
                "The downloader could not be started.",
            ) from exc

        # Close our handle copies: the child inherited duplicates and keeps
        # writing to them. Closing ours lets us reopen the log files for
        # reading while the download runs, which we use to parse progress.
        stdout_handle.close()
        stderr_handle.close()

        self._process = process
        started = time.monotonic()
        last_progress = 0
        try:
            while True:
                try:
                    process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() - started > timeout_seconds:
                    self.terminate()
                    raise AcquisitionError(
                        ErrorCode.ACQUISITION_TIMEOUT.value,
                        "The download did not finish in time.",
                    )
                if callable(on_tick):
                    on_tick()
                if callable(on_progress):
                    pct = _parse_yt_dlp_progress(stdout_log)
                    if pct is not None and pct > last_progress:
                        # yt-dlp's first HLS fragment briefly reports 100.0%
                        # from a tiny estimated total. A stream that really
                        # finished is followed by the next stream or by exit,
                        # so a 100 seen while the child is still running is
                        # that artefact -- latching it would reject every
                        # later percentage and freeze the job's progress for
                        # the rest of the download.
                        if pct >= 100 and process.poll() is None:
                            continue
                        last_progress = pct
                        on_progress(pct)
        finally:
            # Every exit path -- normal exit, timeout, cancellation, any
            # error raised by the callbacks -- must not leave the child
            # running: an orphan keeps downloading into the scratch
            # directory and collides with the next attempt's files.
            if process.poll() is None:
                self.terminate()
            self._process = None

        stderr = _read_bounded_path(stderr_log)
        if process.returncode != 0:
            logger.warning(
                "yt-dlp exited with %s for video %s: %s",
                process.returncode,
                video.video_id,
                stderr,
            )
            raise AcquisitionError(
                ErrorCode.ACQUISITION_FAILED.value,
                "The video could not be downloaded. It may be private, "
                "region-locked, or not available for download.",
            )

        downloaded = self._find_downloaded_file(destination_dir)
        if downloaded is None:
            raise AcquisitionError(
                ErrorCode.ACQUISITION_FAILED.value,
                "The downloader finished without producing a file.",
            )
        size = downloaded.stat().st_size
        if size == 0:
            downloaded.unlink(missing_ok=True)
            raise AcquisitionError(
                ErrorCode.ACQUISITION_FAILED.value,
                "The downloaded file was empty.",
            )
        if size > max_bytes:
            downloaded.unlink(missing_ok=True)
            raise AcquisitionError(
                ErrorCode.ACQUISITION_FAILED.value,
                "The downloaded file is larger than the configured limit.",
            )
        return downloaded

    def terminate(self) -> None:
        """Stop a running download, if there is one."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _find_downloaded_file(destination_dir: Path) -> Path | None:
        # The progress and diagnostic logs live in the same directory as the
        # download; they are not media, whatever their size.
        log_names = ("yt-dlp.stdout.log", "yt-dlp.stderr.log")
        candidates = [
            path
            for path in destination_dir.iterdir()
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl", ".temp"))
            and path.name not in log_names
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_size)


def _read_bounded(stream: object) -> str:
    """Read at most a bounded amount of output from a subprocess pipe."""
    if stream is None:
        return ""
    try:
        data = stream.read(_MAX_OUTPUT_BYTES)  # type: ignore[union-attr]
    except (OSError, ValueError, AttributeError):
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace").strip()
    return str(data).strip()


def _read_bounded_path(path: Path) -> str:
    """Read at most a bounded amount of output from a log file."""
    try:
        data = path.read_bytes()[:_MAX_OUTPUT_BYTES]
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", "replace").strip()


#: yt-dlp progress lines look like ``[download]  45.2% of ~123.45MiB ...``.
_YTDLP_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def _parse_yt_dlp_progress(log_path: Path) -> int | None:
    """Return the latest download percentage from a yt-dlp stderr log.

    Returns ``None`` when the log cannot be read or contains no progress line.
    The percentage is rounded down to an integer.
    """
    try:
        data = log_path.read_bytes()
    except (OSError, ValueError):
        return None
    text = data.decode("utf-8", "replace")
    last: int | None = None
    for match in _YTDLP_PROGRESS_RE.finditer(text):
        last = int(float(match.group(1)))
    return last


def build_acquirer(mode: str) -> SourceAcquirer:
    """Return the acquirer for the configured mode."""
    if mode == "yt_dlp":
        acquirer = YtDlpAcquirer()
        if acquirer.available():
            return acquirer
        logger.warning(
            "youtube acquisition mode is 'yt_dlp' but the yt-dlp package is not "
            "installed; YouTube submissions will be refused"
        )
        return DisabledAcquirer()
    return DisabledAcquirer()
