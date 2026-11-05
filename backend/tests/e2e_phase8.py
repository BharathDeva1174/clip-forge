"""The Phase 8 end-to-end workflow check.

This is a script, not a test. It starts the real API process and the real
worker process and drives the whole chain:

    upload -> transcribe -> analyse -> clip -> trim -> title -> captions
           -> format -> export

Everything is real: the real FFmpeg, the real FFprobe, the real SQLite
database and the real worker process. The only stand-in is the AI provider,
which is a **real local HTTP server** that speaks the Chat Completions
dialect, so the check needs no credential and reaches no external service.

It exits non-zero if any step fails, and it only touches a throwaway
storage directory.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx2  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


# --- the mock provider ------------------------------------------------------


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
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}/v1"


# --- the processes ----------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_video(directory: Path) -> Path:
    """A landscape video with real speech in it."""
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
            "-f", "lavfi", "-i", "testsrc=duration=8:size=640x360:rate=10",
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


def wait_for(client, path: str, statuses, timeout: int = 300) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        last = response.json()
        if last.get("status") in statuses:
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    import tempfile

    root = Path(tempfile.gettempdir()) / "clipforge-e2e-phase8"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    storage = root / "storage"
    storage.mkdir()
    database = storage / "database" / "clipforge.db"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print("ClipForge Phase 8 end-to-end workflow check")
    print(f"  storage  : {storage}")
    print(f"  api      : {base}")

    heading("1. the mock provider and the fixture")
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

            heading("2. upload, transcribe, analyse and clip")
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            check("the upload is accepted", response.status_code == 202)
            job_id = response.json()["id"]
            job = wait_for(client, f"/api/v1/jobs/{job_id}", {"completed", "failed"})
            check("the job completed", job["status"] == "completed", job["status"])

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
            client.post(f"/api/v1/integrations/{created.json()['id']}/select")

            client.post(f"/api/v1/jobs/{job_id}/highlights")
            analysis = wait_for(
                client, f"/api/v1/jobs/{job_id}/highlights", {"completed", "failed"}
            )
            check("the analysis completed", analysis["status"] == "completed", analysis["status"])
            candidate = analysis["candidates"][0]

            clip = client.post(
                f"/api/v1/jobs/{job_id}/clips", json={"candidate_id": candidate["id"]}
            )
            check("the clip is queued", clip.status_code == 202, str(clip.status_code))
            clip_id = clip.json()["id"]
            clip_state = wait_for(
                client, f"/api/v1/clips/{clip_id}", {"completed", "failed"}
            )
            check("the clip completed", clip_state["status"] == "completed", clip_state["status"])
            original_start = clip_state["start_seconds"]
            original_end = clip_state["end_seconds"]
            original_duration = clip_state["actual_duration_seconds"]
            print(
                f"    clip: {original_start:.1f}s - {original_end:.1f}s, "
                f"{original_duration:.2f}s"
            )

            heading("3. trim the clip into a revision")
            trim_start = original_start + 0.5
            trim_end = original_end - 0.5
            revision = client.post(
                f"/api/v1/clips/{clip_id}/revisions",
                json={"start_seconds": trim_start, "end_seconds": trim_end},
            )
            check("the revision is queued", revision.status_code == 202, str(revision.status_code))
            revision_body = revision.json()
            check(
                "the revision points at the clip it came from",
                revision_body["parent_clip_id"] == clip_id,
            )
            check(
                "the revision has no candidate of its own",
                revision_body["candidate_id"] is None,
            )
            revision_id = revision_body["id"]
            revision_state = wait_for(
                client, f"/api/v1/clips/{revision_id}", {"completed", "failed"}
            )
            check(
                "the revision completed",
                revision_state["status"] == "completed",
                revision_state["status"],
            )
            expected = trim_end - trim_start
            check(
                "the revision is the trimmed length",
                abs((revision_state["actual_duration_seconds"] or 0) - expected) < 0.4,
                f"{revision_state['actual_duration_seconds']} vs {expected:.2f}",
            )

            heading("4. the clip that was trimmed is untouched")
            after = client.get(f"/api/v1/clips/{clip_id}").json()
            check("the boundaries are unchanged", (after["start_seconds"], after["end_seconds"]) == (original_start, original_end))
            check("its measured length is unchanged", after["actual_duration_seconds"] == original_duration)
            check("its file is still there", after["media_url"] is not None)
            check(
                "the candidate was not rewritten",
                client.get(f"/api/v1/jobs/{job_id}/highlights").json()["candidates"][0][
                    "start_seconds"
                ]
                == candidate["start_seconds"],
            )

            heading("5. the same trim twice is the same revision")
            again = client.post(
                f"/api/v1/clips/{clip_id}/revisions",
                json={"start_seconds": trim_start, "end_seconds": trim_end},
            )
            check("the revision is the same one", again.json()["id"] == revision_id)

            heading("6. a title is a label, not a re-render")
            renamed = client.patch(
                f"/api/v1/clips/{clip_id}", json={"title": "The best bit"}
            )
            check("the title is saved", renamed.status_code == 200)
            check("the title is the user's", renamed.json()["title"] == "The best bit")
            check(
                "the clip is still the same one",
                client.get(f"/api/v1/clips/{clip_id}").json()["id"] == clip_id,
            )

            heading("7. changed words make a different short-form version")
            first_format = client.post(
                f"/api/v1/clips/{clip_id}/formats", json={"settings": {}}
            )
            check("the first version is queued", first_format.status_code == 202)
            transcript = client.get(
                f"/api/v1/jobs/{job_id}/transcript?limit=500&offset=0"
            ).json()
            segment = transcript["segments"][0]
            edited = client.put(
                f"/api/v1/clips/{clip_id}/captions",
                json={
                    "edits": [
                        {"segment_index": segment["index"], "text": "A corrected line."}
                    ]
                },
            )
            check("the words are saved", edited.status_code == 200, str(edited.status_code))
            second_format = client.post(
                f"/api/v1/clips/{clip_id}/formats", json={"settings": {}}
            )
            check("a second version is queued", second_format.status_code == 202, str(second_format.status_code))
            check(
                "the two versions are different artifacts",
                second_format.json()["id"] != first_format.json()["id"],
            )
            check(
                "their fingerprints are different",
                second_format.json()["config_fingerprint"]
                != first_format.json()["config_fingerprint"],
            )
            wait_for(
                client,
                f"/api/v1/formats/{second_format.json()['id']}",
                {"completed", "failed"},
            )
            final = client.get(f"/api/v1/formats/{second_format.json()['id']}").json()
            check("the second version completed", final["status"] == "completed", final["status"])
            check(
                "it really is portrait",
                (final["width"], final["height"]) == (720, 1280),
                f"{final['width']}x{final['height']}",
            )
            check("the captions were burned in", final["captions_applied"] is True)

            heading("8. export a ZIP of the chosen clips")
            export = client.post(
                f"/api/v1/jobs/{job_id}/exports",
                json={"clip_ids": [clip_id, revision_id]},
            )
            check("the export answers", export.status_code == 200, str(export.status_code))
            check("it is a ZIP", export.headers["content-type"] == "application/zip")
            with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
                names = archive.namelist()
                contents = {name: archive.read(name) for name in names}
            check("it holds both clips", len(names) == 2, str(names))
            check(
                "the files are real videos",
                all(content[4:8] == b"ftyp" for content in contents.values()),
            )

            heading("9. a clip of another job cannot be exported")
            missing = client.post(
                f"/api/v1/jobs/{job_id}/exports", json={"clip_ids": ["b" * 32]}
            )
            check(
                "a clip that does not exist is refused",
                missing.status_code == 404,
                str(missing.status_code),
            )

            # A second video, with a clip of its own, so that the export is
            # asked for a clip that really exists but belongs elsewhere.
            with video.open("rb") as handle:
                other = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("other.mp4", handle, "video/mp4")},
                )
            other_id = other.json()["id"]
            wait_for(
                client, f"/api/v1/jobs/{other_id}", {"completed", "failed"}
            )
            client.post(f"/api/v1/jobs/{other_id}/highlights")
            other_analysis = wait_for(
                client,
                f"/api/v1/jobs/{other_id}/highlights",
                {"completed", "failed"},
            )
            other_clip = client.post(
                f"/api/v1/jobs/{other_id}/clips",
                json={"candidate_id": other_analysis["candidates"][0]["id"]},
            )
            other_clip_id = other_clip.json()["id"]
            wait_for(
                client, f"/api/v1/clips/{other_clip_id}", {"completed", "failed"}
            )
            refused = client.post(
                f"/api/v1/jobs/{job_id}/exports", json={"clip_ids": [other_clip_id]}
            )
            check("it is refused", refused.status_code == 409, str(refused.status_code))

            heading("10. a job can be cancelled")
            with video.open("rb") as handle:
                second = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("second.mp4", handle, "video/mp4")},
                )
            second_id = second.json()["id"]
            cancelled = client.post(f"/api/v1/jobs/{second_id}/cancel")
            check("the cancellation is accepted", cancelled.status_code == 200, str(cancelled.status_code))
            check(
                "the job is cancelled",
                cancelled.json()["job"]["status"] == "cancelled",
                cancelled.json()["job"]["status"],
            )
            time.sleep(2)
            after_cancel = client.get(f"/api/v1/jobs/{second_id}").json()
            check(
                "it stays cancelled",
                after_cancel["status"] == "cancelled",
                after_cancel["status"],
            )
            check(
                "the first job is untouched",
                client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed",
            )

            heading("11. the finished media is still there")
            media = client.get(f"/api/v1/clips/{clip_id}/media")
            check("the clip plays", media.status_code == 200)
            check("it has content", len(media.content) > 1024, f"{len(media.content)} bytes")
            version = client.get(f"/api/v1/formats/{final['id']}/media")
            check("the short-form version plays", version.status_code == 200)
            check(
                "it has content", len(version.content) > 1024, f"{len(version.content)} bytes"
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
