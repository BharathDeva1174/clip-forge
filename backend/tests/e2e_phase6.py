"""The Phase 6 end-to-end workflow check.

This is a script, not a test. It starts the real API process and the real
worker process and drives the whole chain:

    upload -> transcribe -> analyse -> clip -> format -> verify -> play

Everything is real: the real FFmpeg, the real FFprobe, the real SQLite
database and the real worker process. The only stand-in is the AI provider,
which is a **real local HTTP server** that speaks the Chat Completions
dialect, so the check needs no credential and reaches no external service.

The formatting step itself needs no AI at all, and that is the point the
check makes: the same clip can be formatted with the provider switched off.

It exits non-zero if any step fails, and it only touches a throwaway storage
directory.
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

    root = Path(tempfile.gettempdir()) / "clipforge-e2e-phase6"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    storage = root / "storage"
    storage.mkdir()
    database = storage / "database" / "clipforge.db"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print("ClipForge Phase 6 end-to-end workflow check")
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

            heading("2. upload and transcribe")
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            check("the upload is accepted", response.status_code == 202)
            job_id = response.json()["id"]
            job = wait_for(client, f"/api/v1/jobs/{job_id}", {"completed", "failed"})
            check("the job completed", job["status"] == "completed", job["status"])
            check("it has a transcript", job["has_transcript"] is True)

            heading("3. the analysis and the clip")
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
            analysis = wait_for(
                client, f"/api/v1/jobs/{job_id}/highlights", {"completed", "failed"}
            )
            check("the analysis completed", analysis["status"] == "completed", analysis["status"])
            check("it produced candidates", analysis["candidate_count"] >= 1)
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
            print(
                f"    clip: {clip_state['width']}x{clip_state['height']}, "
                f"{clip_state['actual_duration_seconds']}s"
            )

            heading("4. request a formatted version")
            requested = client.post(
                f"/api/v1/clips/{clip_id}/formats",
                json={
                    "settings": {
                        "width": 720,
                        "height": 1280,
                        "layout": "center_crop",
                        "padding_colour": "#000000",
                        "captions": {
                            "enabled": True,
                            "font_family": "Arial",
                            "font_size": 42,
                            "text_colour": "#FFFFFF",
                            "outline_colour": "#000000",
                            "outline_thickness": 3,
                            "shadow": 0,
                            "alignment": 2,
                            "margin_vertical": 90,
                            "max_characters_per_line": 28,
                            "max_lines": 3,
                        },
                    }
                },
            )
            check("the request returns at once", requested.status_code == 202, str(requested.status_code))
            check("it starts as queued", requested.json()["status"] == "queued")
            format_id = requested.json()["id"]

            heading("5. the worker formats it")
            formatted = wait_for(
                client, f"/api/v1/formats/{format_id}", {"completed", "failed"}
            )
            check("the formatted clip completed", formatted["status"] == "completed", formatted["status"])
            print(
                f"    output: {formatted['width']}x{formatted['height']}, "
                f"{formatted['actual_duration_seconds']}s, "
                f"{formatted['output_size_bytes']} bytes, "
                f"{formatted['video_codec']}/{formatted['pixel_format']}"
            )
            check("the output is portrait 9:16", (formatted["width"], formatted["height"]) == (720, 1280))
            check(
                "the duration matches the clip",
                abs(
                    (formatted["actual_duration_seconds"] or 0)
                    - (clip_state["actual_duration_seconds"] or 0)
                )
                <= 0.5,
            )
            check("the captions were burned in", formatted["captions_applied"] is True)
            print(f"    captions: {formatted['caption_count']}")

            heading("6. the file is real, and the captions are in it")
            media = client.get(f"/api/v1/formats/{format_id}/media")
            check("the media route answers", media.status_code == 200)
            check("it is a video", media.headers["content-type"].startswith("video/mp4"))
            check("it has content", len(media.content) > 1024, f"{len(media.content)} bytes")

            ffprobe = shutil.which("ffprobe")
            ffmpeg = shutil.which("ffmpeg")
            if ffprobe and ffmpeg:
                served = root / "served.mp4"
                served.write_bytes(media.content)
                probe = subprocess.run(
                    [
                        ffprobe, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(served),
                    ],
                    capture_output=True, timeout=60, check=False,
                )
                payload = json.loads(probe.stdout.decode("utf-8", "replace"))
                streams = payload.get("streams", [])
                video = next(
                    (s for s in streams if s.get("codec_type") == "video"), {}
                )
                check(
                    "the served bytes are a portrait H.264 video",
                    (video.get("width"), video.get("height")) == (720, 1280)
                    and video.get("codec_name") == "h264",
                    f"{video.get('width')}x{video.get('height')} {video.get('codec_name')}",
                )

                # The caption text has to be in the picture, not just in a
                # subtitle file. The clip and the formatted clip are compared
                # frame by frame: the frames where a caption is on screen
                # have to differ.
                clip_media = client.get(f"/api/v1/clips/{clip_id}/media")
                original = root / "original.mp4"
                original.write_bytes(clip_media.content)

                def _frames(path: Path) -> list[str]:
                    out = subprocess.run(
                        [ffmpeg, "-v", "error", "-i", str(path),
                         "-f", "framehash", "-hash", "md5", "-"],
                        capture_output=True, timeout=180, check=False,
                    )
                    return [
                        line.split(",")[-1].strip()
                        for line in out.stdout.decode("utf-8", "replace").splitlines()
                        if line[:1].isdigit()
                    ]

                differing = 0
                try:
                    before = _frames(original)
                    after = _frames(served)
                    differing = sum(1 for a, b in zip(before, after) if a != b)
                except Exception:
                    differing = -1
                check(
                    "the captions are burned into the picture",
                    differing != 0,
                    f"{differing} frames differ from the uncaptioned clip",
                )

            heading("7. a second format with different settings")
            other = client.post(
                f"/api/v1/clips/{clip_id}/formats",
                json={
                    "settings": {
                        "width": 1080,
                        "height": 1920,
                        "layout": "fit_pad",
                        "padding_colour": "#101010",
                        "captions": {"enabled": False},
                    }
                },
            )
            check("it is queued", other.status_code == 202, str(other.status_code))
            check(
                "it is a different artifact",
                other.json()["id"] != format_id,
            )
            second = wait_for(
                client, f"/api/v1/formats/{other.json()['id']}", {"completed", "failed"}
            )
            check("the second format completed", second["status"] == "completed", second["status"])
            check(
                "it has the other resolution",
                (second["width"], second["height"]) == (1080, 1920),
                f"{second['width']}x{second['height']}",
            )
            check("it was rendered without captions", second["captions_applied"] is False)
            listed = client.get(f"/api/v1/clips/{clip_id}/formats").json()
            check("both artifacts are kept", listed["total"] == 2, str(listed["total"]))

            heading("8. the original clip is untouched")
            check(
                "the clip is still completed",
                client.get(f"/api/v1/clips/{clip_id}").json()["status"] == "completed",
            )
            original_media = client.get(f"/api/v1/clips/{clip_id}/media")
            check("the original is still playable", original_media.status_code == 200)
            check(
                "the original is still landscape",
                client.get(f"/api/v1/clips/{clip_id}").json()["width"] == 640,
            )

            heading("9. a failed format is retried")
            # A format whose source clip is gone fails with a stable code.
            from app.db import create_format, session  # noqa: PLC0415
            import json as _json  # noqa: PLC0415

            with session(database) as conn:
                conn.execute(
                    "UPDATE clips SET output_path = 'clips/gone.mp4' WHERE id = ?",
                    (clip_id,),
                )
                create_format(
                    conn,
                    clip_id=clip_id,
                    job_id=job_id,
                    config_fingerprint="f" * 32,
                    settings_json=_json.dumps({"width": 720, "height": 1280}),
                    format_id="e" * 32,
                )
            failed = wait_for(
                client, f"/api/v1/formats/{'e' * 32}", {"completed", "failed"}
            )
            check("the format failed", failed["status"] == "failed", failed["status"])
            check(
                "it failed with a stable code",
                (failed.get("error") or {}).get("code") == "format_source_missing",
                str((failed.get("error") or {}).get("code")),
            )
            retried = client.post(f"/api/v1/formats/{'e' * 32}/retry")
            check("it can be retried", retried.status_code == 200, str(retried.status_code))
            check("the retry is queued", retried.json()["format"]["status"] == "queued")
            check("the worker is still running", worker.poll() is None)

            heading("10. nothing is left behind")
            temp = storage / "temp"
            leftovers = list(temp.glob("format-*")) if temp.is_dir() else []
            check("no temporary format directory is left", leftovers == [], str(leftovers))
            formats_dir = storage / "formats"
            rendered = list(formats_dir.glob("*.mp4")) if formats_dir.is_dir() else []
            check("the finished formats are on disk", len(rendered) >= 2, str(len(rendered)))
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
