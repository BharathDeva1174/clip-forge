"""The end-to-end workflow check.

This is a script, not a test: it starts the real API process, the real
worker process, uses a real SQLite database, real FFmpeg and the real local
speech model, and drives everything over HTTP.

Run it from ``backend/``::

    ..\\.venv\\Scripts\\python.exe tests/e2e_workflow.py

It prints what it did and what it observed, and it exits non-zero if any
step fails. It only ever touches a throwaway storage directory.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx2 as httpx  # noqa: E402

from real_model_fixtures import speak, to_pcm16k  # noqa: E402

#: The phrase the fixture video says.
PHRASE = "The quick brown fox jumps over the lazy dog."

#: The model the check uses. It must already be in the cache.
MODEL = "tiny.en"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record the outcome of one step."""
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


def free_port() -> int:
    """A port that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_fixture(directory: Path) -> Path:
    """Build a short video with real speech in it."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is not installed")
    raw = directory / "speech-raw.wav"
    wav = directory / "speech.wav"
    if not speak(raw, PHRASE):
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
        capture_output=True,
        timeout=300,
        check=False,
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
            "CLIPFORGE_WORKER_MAX_ATTEMPTS": "1",
            "CLIPFORGE_TRANSCRIPTION_MODEL": MODEL,
            "CLIPFORGE_TRANSCRIPTION_CACHE_DIR": str(
                BACKEND_DIR / "data" / "models"
            ),
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
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def start_worker(env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        [sys.executable, "worker.py"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def stop(process: subprocess.Popen | None) -> None:
    """Stop a process and collect what it said."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def child_pids(parent_pid: int) -> list[int]:
    """The direct children of a process."""
    if os.name == "nt":
        script = (
            f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={parent_pid}')"
            ".ProcessId"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, check=False,
        )
        return [int(part) for part in completed.stdout.split() if part.strip().isdigit()]
    completed = subprocess.run(
        ["ps", "-o", "pid=", "--ppid", str(parent_pid)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return [int(part) for part in completed.stdout.split() if part.strip().isdigit()]


def wait_for_job(client: httpx.Client, job_id: str, timeout: int = 300) -> dict:
    """Poll a job until it reaches a terminal state."""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        last = response.json()
        if last["status"] in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    root = Path(
        os.environ.get("CLIPFORGE_E2E_DIR", Path(os.environ["TEMP"]) / "clipforge-e2e")
    )
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    storage = root / "storage"
    storage.mkdir()
    database = storage / "database" / "clipforge.db"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print(f"ClipForge end-to-end workflow check")
    print(f"  storage : {storage}")
    print(f"  database: {database}")
    print(f"  api     : {base}")

    heading("1. build the fixture")
    video = build_fixture(root)
    check("the fixture video exists", video.exists(), f"{video.stat().st_size} bytes")

    api = None
    worker = None
    api_log_path = root / "api.log"
    worker_log_path = root / "worker.log"
    try:
        env = environment(storage, database)
        api = start_api(port, env, api_log_path)
        worker = start_worker(env, worker_log_path)

        with httpx.Client(base_url=base, timeout=30) as client:
            deadline = time.monotonic() + 60
            ready = False
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").status_code == 200:
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            check("the API answers /health", ready)

            heading("2. upload a video through the real HTTP API")
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            check("the upload is accepted", response.status_code == 202, str(response.status_code))
            job_id = response.json()["id"]
            print(f"  job id  : {job_id}")
            print(f"  status  : {response.json()['status']} / {response.json()['stage']}")

            heading("3. watch the pipeline")
            job = wait_for_job(client, job_id)
            print(f"  status  : {job['status']} / {job['stage']} ({job['progress']}%)")
            check("the job completed", job["status"] == "completed", (job.get("error") or {}).get("code", ""))
            check("the job has a transcript", job.get("has_transcript") is True)

            heading("4. the transcript")
            transcript = client.get(f"/api/v1/jobs/{job_id}/transcript")
            check("the transcript endpoint answers", transcript.status_code == 200)
            body = transcript.json()
            print(f"  status  : {body['status']}")
            print(f"  language: {body['language']}   model: {body['model_name']}")
            print(f"  audio   : {body['audio_sample_rate']} Hz, {body['audio_channels']} channel(s)")
            print(f"  duration: {body['duration_seconds']}s")
            print(f"  segments: {body['segment_count']}")
            for segment in body["segments"]:
                print(
                    f"    [{segment['start_seconds']:6.2f}s -> {segment['end_seconds']:6.2f}s] "
                    f"{segment['text']}"
                )
            check("the transcript is ready", body["status"] == "ready")
            check("the audio is 16 kHz mono", body["audio_sample_rate"] == 16000 and body["audio_channels"] == 1)
            check("there is at least one segment", body["segment_count"] >= 1)
            text = " ".join(s["text"] for s in body["segments"]).lower()
            check(
                "the expected phrase is in the transcript",
                "fox" in text or "quick" in text or "dog" in text,
                text[:120],
            )
            length = body["duration_seconds"] or 0
            check(
                "no segment claims speech outside the audio",
                all(
                    s["start_seconds"] >= 0 and s["end_seconds"] <= length + 0.01
                    for s in body["segments"]
                ),
            )

            heading("5. the temporary artefacts are gone")
            temp = storage / "temp"
            leftovers = list(temp.glob("audio-*")) if temp.is_dir() else []
            check("no extracted audio is left behind", leftovers == [], str(leftovers))
            check(
                "the source video is still there",
                list((storage / "sources").glob("*.mp4")) != [],
            )

            heading("6. a legacy job without a transcript")
            # A job that was completed before transcripts existed.
            legacy_id = "legacy00000000000000000000000"
            from app.db import create_job, session  # noqa: PLC0415
            from app.jobs import JobStage, JobStatus, SourceType  # noqa: PLC0415

            with session(database) as conn:
                create_job(
                    conn,
                    job_id=legacy_id,
                    source_type=SourceType.UPLOAD,
                    source_reference="old-clip.mp4",
                )
                conn.execute(
                    "UPDATE jobs SET status = ?, stage = ?, progress = 100 WHERE id = ?",
                    (JobStatus.COMPLETED.value, JobStage.READY.value, legacy_id),
                )

            legacy = client.get(f"/api/v1/jobs/{legacy_id}")
            check("the legacy job is answered", legacy.status_code == 200)
            check(
                "the legacy job does not claim a transcript",
                legacy.json()["has_transcript"] is False,
            )
            legacy_transcript = client.get(f"/api/v1/jobs/{legacy_id}/transcript")
            check("the legacy transcript endpoint answers", legacy_transcript.status_code == 200)
            check(
                "the legacy transcript is pending, not ready",
                legacy_transcript.json()["status"] == "pending",
            )
            check(
                "the legacy transcript has no segments",
                legacy_transcript.json()["segments"] == [],
            )

        # --- a controlled timeout ------------------------------------------------
        heading("7. a controlled timeout")
        stop(worker)
        worker = None
        stop(api)
        api = None

        env = environment(
            storage, database, CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS="1"
        )
        api = start_api(port, env, api_log_path)
        worker = start_worker(env, worker_log_path)
        with httpx.Client(base_url=base, timeout=30) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            timeout_job = response.json()["id"]
            print(f"  job id  : {timeout_job}")
            job = wait_for_job(client, timeout_job)
            code = (job.get("error") or {}).get("code")
            print(f"  status  : {job['status']}   error: {code}")
            check("the job reached a terminal state", job["status"] in ("failed", "completed"), job["status"])
            check("the failure is the timeout", code == "transcription_timeout", str(code))
            check(
                "the job is not left transcribing",
                job["status"] != "transcribing",
            )
            transcript = client.get(f"/api/v1/jobs/{timeout_job}/transcript")
            check(
                "a timed-out job has no transcript",
                transcript.status_code == 409,
                str(transcript.status_code),
            )
            check("the worker is still running", worker.poll() is None)

            heading("8. the worker carries on")
            stop(worker)
            worker = None
            env = environment(storage, database)
            worker = start_worker(env, worker_log_path)
            with video.open("rb") as handle:
                response = client.post(
                    "/api/v1/videos/upload",
                    files={"file": ("speech.mp4", handle, "video/mp4")},
                )
            second_job = response.json()["id"]
            print(f"  job id  : {second_job}")
            job = wait_for_job(client, second_job)
            print(f"  status  : {job['status']} / {job['stage']}")
            check("the next job completed", job["status"] == "completed", job["status"])
            check("the worker is still running", worker.poll() is None)

        heading("9. nothing is left behind")
        worker_pid = worker.pid if worker is not None else None
        stop(worker)
        worker = None
        stop(api)
        api = None
        time.sleep(2)
        temp = storage / "temp"
        leftovers = list(temp.glob("audio-*")) if temp.is_dir() else []
        check("no extracted audio is left behind", leftovers == [], str(leftovers))
        if worker_pid is not None:
            orphans = child_pids(worker_pid)
            check("no inference process is left behind", orphans == [], str(orphans))
    finally:
        stop(worker)
        stop(api)

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
