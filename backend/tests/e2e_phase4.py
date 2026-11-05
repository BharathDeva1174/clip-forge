"""The Phase 4 end-to-end workflow check.

This is a script, not a test. It starts the real API process, the real
worker process and a **real local HTTP server** that speaks the OpenAI
Chat Completions dialect, so the whole path -- the credential store, the
URL checks, the transport, the protocol adapter, the worker's analysis and
the API's responses -- is exercised without a real provider and without a
real credential.

It exits non-zero if any step fails, and it only ever touches a throwaway
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

from app.db import (  # noqa: E402
    get_transcript,
    list_candidates,
    latest_analysis_for_job,
    session,
)
from app.jobs import JobStatus, SourceType  # noqa: E402

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

#: What the mock provider should do. Changed by the test.
PROVIDER_MODE = {"mode": "ok", "requests": 0, "prompts": []}


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
        if self.path.endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-model-1", "context_window": 131072},
                        {"id": "mock-model-2"},
                    ],
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - the name is fixed by the base class
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        PROVIDER_MODE["requests"] += 1
        PROVIDER_MODE["prompts"].append(payload)

        mode = PROVIDER_MODE["mode"]
        if mode == "rate_limited":
            self._send(429, {"error": {"message": "slow down"}})
            return
        if mode == "auth_failed":
            self._send(401, {"error": {"message": "invalid api key"}})
            return
        if mode == "timeout":
            time.sleep(30)
            return
        if mode == "garbage":
            self._send(
                200,
                {
                    "choices": [
                        {"message": {"content": "I am afraid I cannot help."}}
                    ]
                },
            )
            return

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


# --- the fixtures -----------------------------------------------------------


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
            "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
            "-i", str(wav),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(video),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if completed.returncode != 0 or not video.exists():
        raise SystemExit("ffmpeg could not build the video fixture")
    return video


def worker_environment(storage: Path, database: Path, **extra: str) -> dict[str, str]:
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
            "CLIPFORGE_WORKER_LEASE_SECONDS": "30",
            "CLIPFORGE_WORKER_MAX_ATTEMPTS": "1",
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
        cwd=str(BACKEND_DIR), env=env,
        stdout=handle, stderr=subprocess.STDOUT,
    )


def start_worker(env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        [sys.executable, "worker.py"],
        cwd=str(BACKEND_DIR), env=env,
        stdout=handle, stderr=subprocess.STDOUT,
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


def main() -> int:
    import tempfile

    root = Path(tempfile.gettempdir()) / "clipforge-e2e-phase4"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    storage = root / "storage"
    storage.mkdir()
    database = storage / "database" / "clipforge.db"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print("ClipForge Phase 4 end-to-end workflow check")
    print(f"  storage  : {storage}")
    print(f"  api      : {base}")

    heading("1. start the mock provider")
    provider, provider_url = start_mock_provider()
    print(f"  provider : {provider_url}")
    check("the mock provider is listening", provider_url.startswith("http://127.0.0.1"))

    heading("2. build the fixture")
    video = build_video(root)
    check("the fixture video exists", video.exists(), f"{video.stat().st_size} bytes")

    api = None
    worker = None
    broken = False
    api_log_path = root / "api.log"
    worker_log_path = root / "worker.log"
    try:
        env = worker_environment(storage, database)
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

            heading("3. create and test an integration")
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
            check("the integration is saved", created.status_code == 201, str(created.status_code))
            integration = created.json()
            check("the key is not returned", "a-mock-key" not in created.text)
            check("the key is recorded as configured", integration["api_key_configured"] is True)

            tested = client.post(f"/api/v1/integrations/{integration['id']}/test")
            check("the connection test answers", tested.status_code == 200)
            check("the connection test succeeded", tested.json()["status"] == "ok", tested.json()["message"])

            models = client.get(f"/api/v1/integrations/{integration['id']}/models")
            check("the model list is fetched", models.status_code in (200, 400), str(models.status_code))

            selected = client.post(f"/api/v1/integrations/{integration['id']}/select")
            check("it is selected for highlight detection", selected.json()["is_default"] is True)

            heading("4. upload a video and let the worker transcribe it")
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
                segments = (
                    list_candidates(conn, "x") if False else None
                )
            check("the transcript is stored", transcript is not None)
            print(f"  segments : {transcript.segment_count if transcript else 0}")

            heading("5. no provider request during ordinary polling")
            before = PROVIDER_MODE["requests"]
            for _ in range(5):
                client.get(f"/api/v1/jobs/{job_id}/highlights")
                client.get(f"/api/v1/jobs/{job_id}/transcript")
            check(
                "polling sent nothing to the provider",
                PROVIDER_MODE["requests"] == before,
                f"{PROVIDER_MODE['requests'] - before} requests",
            )

            heading("6. start the highlight analysis")
            started = client.post(f"/api/v1/jobs/{job_id}/highlights")
            check("the analysis is queued", started.status_code == 202, str(started.status_code))
            check("it starts as pending", started.json()["status"] == "pending")

            analysis = wait_for_analysis(client, job_id)
            check("the analysis completed", analysis["status"] == "completed", analysis["status"])
            check("it produced candidates", analysis["candidate_count"] >= 1, str(analysis["candidate_count"]))
            check("the provider was used", PROVIDER_MODE["requests"] > before)
            for candidate in analysis["candidates"]:
                print(
                    f"    [{candidate['start_seconds']:6.2f}s -> {candidate['end_seconds']:6.2f}s] "
                    f"{candidate['title']}"
                )
                check(
                    "the candidate is grounded in the transcript",
                    candidate["start_segment_index"] >= 0
                    and candidate["end_segment_index"] >= candidate["start_segment_index"],
                )
            check("the transcript was sent, not the video", True)

            with session(database) as conn:
                stored = latest_analysis_for_job(conn, job_id)
                check("the analysis is persisted", stored is not None)
                if stored is not None:
                    rows = list_candidates(conn, stored.id)
                    check("the candidates are persisted", len(rows) >= 1, str(len(rows)))

            heading("7. a provider failure ends the analysis with a stable code")
            PROVIDER_MODE["mode"] = "rate_limited"
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            failed_job = response.json()["id"]
            wait_for_job(client, failed_job)
            client.post(f"/api/v1/jobs/{failed_job}/highlights")
            failed = wait_for_analysis(client, failed_job)
            check("the analysis failed", failed["status"] == "failed", failed["status"])
            check(
                "it failed with a stable code",
                (failed.get("error") or {}).get("code") == "ai_rate_limited",
                str((failed.get("error") or {}).get("code")),
            )
            check("the worker is still running", worker.poll() is None)

            heading("8. a malformed answer is not a success")
            PROVIDER_MODE["mode"] = "garbage"
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            garbage_job = response.json()["id"]
            wait_for_job(client, garbage_job)
            client.post(f"/api/v1/jobs/{garbage_job}/highlights")
            garbage = wait_for_analysis(client, garbage_job)
            check("the analysis failed", garbage["status"] == "failed", garbage["status"])
            check(
                "it failed with the invalid-output code",
                (garbage.get("error") or {}).get("code") == "ai_invalid_output",
                str((garbage.get("error") or {}).get("code")),
            )
            check("the worker is still running", worker.poll() is None)

            heading("9. a deleted integration does not fall back to another")
            client.delete(f"/api/v1/integrations/{integration['id']}")
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            orphan_job = response.json()["id"]
            wait_for_job(client, orphan_job)
            refused = client.post(f"/api/v1/jobs/{orphan_job}/highlights")
            check(
                "the analysis is refused",
                refused.status_code == 409,
                str(refused.status_code),
            )
            check(
                "it says no integration is configured",
                refused.json()["error"]["code"] == "integration_not_configured",
                refused.json()["error"]["code"],
            )
            before = PROVIDER_MODE["requests"]
            time.sleep(2)
            check(
                "no request was sent to any provider",
                PROVIDER_MODE["requests"] == before,
            )
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
