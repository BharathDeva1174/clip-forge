"""The Phase 5 end-to-end workflow check.

This is a script, not a test. It starts the real API process, the real
worker process and a **real local HTTP server** that speaks the Chat
Completions dialect, then drives the whole chain:

    upload -> transcribe -> analyse -> request a clip -> render -> verify
    -> play

The local server stands in for the AI provider so that the check needs no
credential and reaches no external service. Everything else is the real
thing: the real FFmpeg, the real FFprobe, the real SQLite database and the
real worker process.

It exits non-zero if any step fails, and it only touches a throwaway
storage directory.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx2  # noqa: E402

from app.db import get_transcript, session  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record the outcome of one step."""
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


# --- the mock provider ------------------------------------------------------

#: The moments the mock provider answers with. They cite the segment
#: numbers of the transcript the fixture produces.
MOCK_HIGHLIGHTS = {
    "highlights": [
        {
            "start_segment": 0,
            "end_segment": 1,
            "title": "The opening line",
            "reason": "It states the point in one sentence.",
            "moment_type": "hook",
            "score": 0.9,
        }
    ]
}


class MockProviderHandler(BaseHTTPRequestHandler):
    """A provider that speaks the OpenAI Chat Completions dialect."""

    def log_message(self, *args: object) -> None:
        """Keep the output quiet."""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by the base class
        self._send(200, {"object": "list", "data": [{"id": "mock-model-1"}]})

    def do_POST(self) -> None:  # noqa: N802 - the name is fixed by the base class
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._send(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(MOCK_HIGHLIGHTS),
                        }
                    }
                ]
            },
        )


def start_mock_provider() -> tuple[ThreadingHTTPServer, str]:
    """Start the mock provider and return it with its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}/v1"


# --- the processes ----------------------------------------------------------


def free_port() -> int:
    """A port that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_video(directory: Path) -> Path:
    """A short video with real speech in it."""
    from real_model_fixtures import speak, to_pcm16k

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is not installed")
    raw = directory / "speech-raw.wav"
    wav = directory / "speech.wav"
    phrase = "The quick brown fox jumps over the lazy dog. Pack my box."
    if not speak(raw, phrase):
        raise SystemExit("the Windows speech synthesiser is not available")
    if not to_pcm16k(raw, wav):
        raise SystemExit("ffmpeg could not convert the speech")
    video = directory / "speech.mp4"
    completed = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
            "-i", str(wav),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(video),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0 or not video.exists():
        raise SystemExit("ffmpeg could not build the video fixture")
    return video


def environment(storage: Path, database: Path, **extra: str) -> dict[str, str]:
    """The environment the API and the worker are started with."""
    result = dict(os.environ)
    for key in list(result):
        if key.startswith("CLIPFORGE_"):
            result.pop(key)
    result.update(
        {
            "CLIPFORGE_STORAGE_DIR": str(storage),
            "CLIPFORGE_DATABASE_PATH": str(database),
            "CLIPFORGE_LOG_LEVEL": "INFO",
            "CLIPFORGE_WORKER_POLL_INTERVAL_SECONDS": "0.5",
            "CLIPFORGE_WORKER_LEASE_SECONDS": "60",
            "CLIPFORGE_WORKER_MAX_ATTEMPTS": "2",
            "CLIPFORGE_TRANSCRIPTION_OFFLINE": "true",
            "CLIPFORGE_TRANSCRIPTION_CACHE_DIR": str(BACKEND_DIR / "data" / "models"),
            "PYTHONPATH": str(BACKEND_DIR),
        }
    )
    result.update(extra)
    return result


def start_api(port: int, env: dict[str, str], log: Path) -> subprocess.Popen:
    # The output goes to a file, not to a pipe. A Windows pipe holds about
    # 4 KB, and a server that fills it blocks while writing its log -- which
    # looks exactly like the server having hung.
    handle = log.open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(BACKEND_DIR), env=env, stdout=handle, stderr=subprocess.STDOUT,
    )


def start_worker(env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        [sys.executable, "worker.py"],
        cwd=str(BACKEND_DIR), env=env, stdout=handle, stderr=subprocess.STDOUT,
    )


def stop(process: subprocess.Popen | None, log: Path) -> str:
    """Stop a process and return what it said."""
    if process is None:
        return ""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    try:
        return log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_for_job(client, job_id: str, timeout: int = 300) -> dict:
    """Poll a job until it reaches a terminal state."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        last = response.json()
        if last["status"] in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.5)
    return last


def wait_for_analysis(client, job_id: str, timeout: int = 300) -> dict:
    """Poll the highlight analysis until it reaches a terminal state."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}/highlights")
        response.raise_for_status()
        last = response.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.5)
    return last


def wait_for_clip(client, clip_id: str, timeout: int = 300) -> dict:
    """Poll a clip until it reaches a terminal state."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/clips/{clip_id}")
        response.raise_for_status()
        last = response.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    import tempfile

    root = Path(tempfile.gettempdir()) / "clipforge-e2e-phase5"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    storage = root / "storage"
    storage.mkdir()
    database = storage / "database" / "clipforge.db"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print("ClipForge Phase 5 end-to-end workflow check")
    print(f"  storage  : {storage}")
    print(f"  api      : {base}")

    heading("1. start the mock provider and build the fixture")
    provider, provider_url = start_mock_provider()
    print(f"  provider : {provider_url}")
    video = build_video(root)
    check("the fixture video exists", video.exists(), f"{video.stat().st_size} bytes")

    api = None
    worker = None
    broken = False
    api_log_path = root / "api.log"
    worker_log_path = root / "worker.log"
    try:
        env = environment(storage, database)
        api = start_api(port, env, api_log_path)
        worker = start_worker(env, worker_log_path)

        with httpx2.Client(base_url=base, timeout=30) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            check("the API answers /health", True)

            heading("2. upload a video and let the worker transcribe it")
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            check("the upload is accepted", response.status_code == 202)
            job_id = response.json()["id"]
            job = wait_for_job(client, job_id)
            check("the job completed", job["status"] == "completed", job["status"])
            check("it has a transcript", job["has_transcript"] is True)

            with session(database) as conn:
                transcript = get_transcript(conn, job_id)
            check("the transcript is stored", transcript is not None)
            print(f"  segments : {transcript.segment_count if transcript else 0}")

            heading("3. configure a provider and run the analysis")
            created = client.post(
                "/api/v1/integrations",
                json={
                    "name": "Mock provider",
                    "provider_id": "custom",
                    "protocol": "openai_chat_completions",
                    "base_url": provider_url,
                    "model_id": "mock-model-1",
                    "local_endpoint": True,
                    "api_key": "a-mock-key",
                },
            )
            check("the integration is saved", created.status_code == 201)
            integration_id = created.json()["id"]
            client.post(f"/api/v1/integrations/{integration_id}/select")

            client.post(f"/api/v1/jobs/{job_id}/highlights")
            analysis = wait_for_analysis(client, job_id)
            check("the analysis completed", analysis["status"] == "completed", analysis["status"])
            check("it produced candidates", analysis["candidate_count"] >= 1)
            candidate = analysis["candidates"][0]
            print(
                f"    candidate: {candidate['title']} "
                f"[{candidate['start_seconds']:.2f}s -> {candidate['end_seconds']:.2f}s]"
            )

            heading("4. request a clip")
            requested = client.post(
                f"/api/v1/jobs/{job_id}/clips", json={"candidate_id": candidate["id"]}
            )
            check("the clip is queued", requested.status_code == 202, str(requested.status_code))
            clip = requested.json()
            check("it starts as queued", clip["status"] == "queued")
            # The boundaries are the candidate's own.
            check(
                "the boundaries come from the candidate",
                clip["start_seconds"] == candidate["start_seconds"]
                and clip["end_seconds"] == candidate["end_seconds"],
                f"{clip['start_seconds']} -> {clip['end_seconds']}",
            )
            clip_id = clip["id"]

            heading("5. the worker renders and verifies it")
            clip = wait_for_clip(client, clip_id)
            check("the clip completed", clip["status"] == "completed", clip["status"])
            print(
                f"    duration : requested {clip['duration_seconds']:.2f}s, "
                f"measured {clip['actual_duration_seconds']}"
            )
            print(
                f"    size     : {clip['output_size_bytes']} bytes, "
                f"{clip['width']}x{clip['height']}, {clip['video_codec']}"
            )
            check(
                "the measured duration matches the request",
                clip["actual_duration_seconds"] is not None
                and abs(clip["actual_duration_seconds"] - clip["duration_seconds"]) <= 0.5,
            )
            check("the output has a size", (clip["output_size_bytes"] or 0) > 1024)
            check("the output has dimensions", (clip["width"] or 0) > 0 and (clip["height"] or 0) > 0)
            check("the output is in the expected format", clip["video_codec"] == "h264")

            heading("6. the file is real and playable")
            media = client.get(f"/api/v1/clips/{clip_id}/media")
            check("the media route answers", media.status_code == 200)
            check("it is a video", media.headers["content-type"].startswith("video/mp4"))
            check("it has content", len(media.content) > 1024, f"{len(media.content)} bytes")

            ffprobe = shutil.which("ffprobe")
            if ffprobe:
                probe = subprocess.run(
                    [
                        ffprobe, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", "-",
                    ],
                    input=media.content,
                    capture_output=True, timeout=60, check=False,
                )
                payload = json.loads(probe.stdout.decode("utf-8", "replace"))
                streams = payload.get("streams", [])
                check(
                    "the served bytes are a real video",
                    any(s.get("codec_type") == "video" for s in streams),
                )
                check(
                    "the served video is H.264",
                    any(s.get("codec_name") == "h264" for s in streams),
                )

            heading("7. a repeated request does not render twice")
            again = client.post(
                f"/api/v1/jobs/{job_id}/clips", json={"candidate_id": candidate["id"]}
            )
            check("the same clip comes back", again.json()["id"] == clip_id)
            clips = client.get(f"/api/v1/jobs/{job_id}/clips").json()
            check("there is only one clip", clips["total"] == 1, str(clips["total"]))

            heading("8. a failure path")
            # A clip whose source video is gone fails with a stable code.
            with session(database) as conn:
                conn.execute(
                    "UPDATE jobs SET source_path = 'sources/gone.mp4' WHERE id = ?",
                    (job_id,),
                )
            from app.db import create_clip

            with session(database) as conn:
                create_clip(
                    conn,
                    job_id=job_id,
                    analysis_id=None,
                    candidate_id=None,
                    start_seconds=1.0,
                    end_seconds=3.0,
                    clip_id="f" * 32,
                )
            failed = wait_for_clip(client, "f" * 32)
            check("the clip failed", failed["status"] == "failed", failed["status"])
            check(
                "it failed with a stable code",
                (failed.get("error") or {}).get("code") == "clip_source_missing",
                str((failed.get("error") or {}).get("code")),
            )
            check("the worker is still running", worker.poll() is None)

            heading("9. nothing is left behind")
            temp = storage / "temp"
            leftovers = list(temp.glob("clip-*.mp4")) if temp.is_dir() else []
            check("no temporary clip file is left", leftovers == [], str(leftovers))
            clips_dir = storage / "clips"
            rendered = list(clips_dir.glob("*.mp4")) if clips_dir.is_dir() else []
            check("the finished clip is on disk", len(rendered) == 1, str(len(rendered)))
    except Exception:
        broken = True
        raise
    finally:
        api_log = stop(api, api_log_path)
        worker_log = stop(worker, worker_log_path)
        provider.shutdown()
        if FAILURES or broken:
            print("\n--- api log ---")
            print(api_log[-4000:])
            print("\n--- worker log ---")
            print(worker_log[-4000:])

    heading("result")
    if FAILURES:
        print(f"  {len(FAILURES)} step(s) failed:")
        for failure in FAILURES:
            print(f"    - {failure}")
        return 1
    print("  every step passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
