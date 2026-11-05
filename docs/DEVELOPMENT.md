# Development guide

Windows PowerShell, exact commands. Every command says which directory to run
it in.

| Directory | What lives there |
| --- | --- |
| repository root (`clipforge\`) | Docs, `.gitignore`, `.editorconfig`, `.env.example` |
| `backend\` | The FastAPI application, the worker and their tests |
| `frontend\` | The Next.js application and its tests |

Commands are shown for PowerShell. In `cmd.exe` use `copy` instead of
`Copy-Item`, and in bash use `cp`.

---

## 1. First-time setup

### 1.1 Check the toolchain

```powershell
node --version
npm --version
python --version
ffprobe -version
```

Requirements: Node.js 20.9 or newer (22 LTS / 24 LTS recommended), npm 10 or
newer, Python 3.11 or newer, and **FFmpeg** (FFprobe is required from Phase 2
onwards).

If FFprobe is missing:

```powershell
winget install Gyan.FFmpeg
```

Then reopen the terminal so the new `PATH` is picked up.

### 1.2 Backend

```powershell
cd clipforge\backend

python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements-dev.txt

Copy-Item .env.example .env
```

> If `Activate.ps1` is blocked, run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the same
> terminal and activate again. You can also skip activation entirely and use
> `..\.venv\Scripts\python.exe` in place of `python`.

`pip install -r requirements-dev.txt` also installs `faster-whisper`, the
local speech-to-text engine. It pulls in `ctranslate2`, `huggingface-hub`,
`tokenizers`, `onnxruntime` and `av`, so it is a noticeably larger install
than the Phase 2 one.

#### The speech model

The model weights are **not** part of the installation. The first
transcription downloads the configured model into
`backend/data/models/` and every later one runs offline. To fetch it up
front — for example on a machine that will be offline afterwards — run:

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1

python -c "from pathlib import Path; from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8', download_root='data/models'); print('model cached')"
```

| Model | Size | Use it when |
| --- | --- | --- |
| `tiny.en` | about 75 MB | Development, and a quick check that the pipeline works |
| `base.en` | about 145 MB | The sensible default for real use |
| `small.en` | about 480 MB | Accuracy matters more than time |

The model name is set with `CLIPFORGE_TRANSCRIPTION_MODEL`. Only the `.en`
models are English; the multilingual models (`base`, `small`, …) are used for
other languages.

Set `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` once the model is cached: the
engine then refuses to touch the network at all, and a missing model produces
an actionable error instead of a silent download.

### 1.3 Frontend

```powershell
cd clipforge\frontend

npm install

Copy-Item .env.example .env.local
```

### 1.4 Sanity check

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python -m pytest

cd ..\frontend
npm run typecheck
npm run lint
npm test
```

---

## 2. Running the applications

### One command

```powershell
cd clipforge
.\scripts\start-local.ps1                                  # API on 8000, frontend on 3000
.\scripts\start-local.ps1 -ApiPort 19400 -FrontendPort 14400
```

The launcher starts the API, then the worker, then the frontend, and gates each
step on real readiness — a TCP check plus `GET /health` and `/ready` — rather
than on a fixed delay. It sends each process's output to a file in
`$env:TEMP\clipforge-local-launch\`, never a pipe (a Windows pipe holds about
4 KB and a server that fills one blocks while writing its log, which looks
exactly like a hang); the previous run's logs are kept as `*.prev.log`. The
other parameters are `-BackendDir`, `-FrontendDir`, `-Python`, and `-Replace`,
which stops ClipForge processes left over from an earlier run instead of
refusing to stack on top of them.

Stop the three processes with `Ctrl+C` in the launcher's terminal, or:

```powershell
Stop-ClipForge                    # only the processes this script started
```

`scripts\test-health.ps1 -Url <url>` is the small probe the launcher uses; it
exits 0 when the URL answers.

### Three terminals

The worker is a separate process and is what actually processes the jobs — the
API only queues them.

### Terminal 1 — API

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python run.py
```

Expected output:

```
Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Equivalent explicit form (useful for changing host or port):

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`--reload` restarts the server when a `.py` file changes. Stop it with
`Ctrl+C`.

### Terminal 2 — worker

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python worker.py
```

Expected output:

```
clipforge.worker | worker <hostname>-<pid>-<id> started (storage=..., database=clipforge.db)
```

The worker polls the database every `CLIPFORGE_WORKER_POLL_INTERVAL_SECONDS`
seconds. Stop it with `Ctrl+C`; it stops claiming new work and finishes the
job it is on, or releases it back to the queue.

### Terminal 3 — frontend

```powershell
cd clipforge\frontend
npm run dev
```

Expected output:

```
- Local:  http://localhost:3000
✓ Ready in ...
```

Stop it with `Ctrl+C`.

### URLs

| URL | What |
| --- | --- |
| http://localhost:3000 | Frontend |
| http://localhost:8000/health | `{"status":"ok","service":"clipforge-api"}` |
| http://localhost:8000/ready | Readiness: capability flags, model cache, worker heartbeat |
| http://localhost:8000/api/v1 | API metadata |
| http://localhost:8000/api/v1/jobs | Recent jobs, newest first |
| http://localhost:8000/docs | OpenAPI explorer |

### Production build of the frontend

```powershell
cd clipforge\frontend
npm run build
npm run start
```

`npm run start` serves the production build on http://localhost:3000.

---

## 3. Everyday commands

### Backend (from `clipforge\backend\`)

```powershell
..\.venv\Scripts\Activate.ps1

python -m pytest                    # all tests
python -m pytest -v                 # verbose
python -m pytest tests/test_worker.py
python -m pytest -k "cors"          # by name
python -m uvicorn app.main:app --reload --port 8000
python worker.py                    # the background worker
```

Add a dependency:

```powershell
pip install <package>
# then update backend/requirements.txt with the pinned version, and
# backend/constraints.txt with the new resolution.
```

### Frontend (from `clipforge\frontend\`)

```powershell
npm run dev                         # dev server
npm run build                       # production build
npm run start                       # serve the production build
npm run lint                        # ESLint
npm run typecheck                   # next typegen && tsc --noEmit
npm test                            # Vitest, single run
npm run test:watch                  # Vitest, watch mode
npm test -- health                  # Vitest, filtered by file name
```

Add a dependency:

```powershell
npm install <package>
npm install -D <package>            # dev dependency
```

`package-lock.json` is committed. Commit it whenever `package.json` changes.

---

## 4. Configuration

### Backend — `clipforge\backend\.env`

```
CLIPFORGE_ENVIRONMENT=development
CLIPFORGE_DEBUG=false
CLIPFORGE_LOG_LEVEL=INFO
CLIPFORGE_APP_NAME=ClipForge API
CLIPFORGE_APP_VERSION=1.2.0
CLIPFORGE_HOST=127.0.0.1
CLIPFORGE_PORT=8000
CLIPFORGE_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000

CLIPFORGE_STORAGE_DIR=data
# CLIPFORGE_DATABASE_PATH=data/database/clipforge.db
CLIPFORGE_MAX_UPLOAD_BYTES=2147483648
CLIPFORGE_MAX_VIDEO_DURATION_SECONDS=14400
CLIPFORGE_MAX_VIDEO_DIMENSION=7680
CLIPFORGE_ALLOWED_UPLOAD_EXTENSIONS=.mp4,.mov,.m4v,.webm,.mkv
CLIPFORGE_ALLOWED_VIDEO_CODECS=h264,hevc,vp9,av1,mpeg4

CLIPFORGE_FFPROBE_PATH=ffprobe
CLIPFORGE_FFPROBE_TIMEOUT_SECONDS=60

CLIPFORGE_FFMPEG_PATH=ffmpeg
CLIPFORGE_FFMPEG_TIMEOUT_SECONDS=1800
CLIPFORGE_AUDIO_SAMPLE_RATE=16000
CLIPFORGE_AUDIO_CHANNELS=1
CLIPFORGE_AUDIO_MAX_DURATION_SECONDS=14400
CLIPFORGE_AUDIO_MAX_BYTES=2147483648
CLIPFORGE_AUDIO_MIN_FREE_DISK_BYTES=536870912
CLIPFORGE_AUDIO_TEMP_RETENTION=delete

CLIPFORGE_TRANSCRIPTION_MODEL=tiny.en
CLIPFORGE_TRANSCRIPTION_DEVICE=cpu
CLIPFORGE_TRANSCRIPTION_ISOLATION=process
# CLIPFORGE_TRANSCRIPTION_COMPUTE_TYPE=int8
# CLIPFORGE_TRANSCRIPTION_LANGUAGE=en
# CLIPFORGE_TRANSCRIPTION_CACHE_DIR=data/models
CLIPFORGE_TRANSCRIPTION_OFFLINE=false
CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS=3600
CLIPFORGE_TRANSCRIPTION_MAX_CONCURRENCY=1
CLIPFORGE_TRANSCRIPTION_BEAM_SIZE=5
CLIPFORGE_TRANSCRIPTION_VAD_FILTER=true
# CLIPFORGE_TRANSCRIPTION_MAX_NEW_TOKENS=
CLIPFORGE_TRANSCRIPTION_MAX_SEGMENTS=50000

CLIPFORGE_WORKER_POLL_INTERVAL_SECONDS=2
CLIPFORGE_WORKER_LEASE_SECONDS=300
CLIPFORGE_WORKER_LEASE_RENEWAL_INTERVAL_SECONDS=30
CLIPFORGE_WORKER_MAX_ATTEMPTS=3
CLIPFORGE_WORKER_ACQUISITION_TIMEOUT_SECONDS=1800

CLIPFORGE_DATABASE_BUSY_TIMEOUT_SECONDS=30
CLIPFORGE_DATABASE_WAL_MODE=true

CLIPFORGE_YOUTUBE_ACQUISITION_MODE=yt_dlp
CLIPFORGE_YOUTUBE_MAX_BYTES=2147483648
CLIPFORGE_YOUTUBE_ALLOWED_HOSTS=youtube.com,www.youtube.com,m.youtube.com,youtu.be,www.youtu.be

# AI integrations (Phase 4)
# Free-tier and reasoning models can take minutes to answer one window of a
# long transcript. The lease of a running analysis is renewed in the background
# during the provider call, so an analysis is bounded by
# CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS rather than by the worker lease.
CLIPFORGE_AI_REQUEST_TIMEOUT_SECONDS=60
CLIPFORGE_AI_MAX_RETRIES=2
CLIPFORGE_AI_MAX_REQUEST_ATTEMPTS=3
CLIPFORGE_AI_MAX_BACKOFF_SECONDS=30
CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS=900
CLIPFORGE_AI_MAX_SEGMENTS_PER_WINDOW=120
CLIPFORGE_AI_MIN_SEGMENTS_PER_WINDOW=5
CLIPFORGE_AI_WINDOW_OVERLAP_SEGMENTS=4
CLIPFORGE_AI_MAX_TRANSCRIPT_CHARACTERS=12000
CLIPFORGE_AI_MAX_CONCURRENT_REQUESTS=1

# Clip generation (Phase 5)
CLIPFORGE_CLIP_MIN_DURATION_SECONDS=1
CLIPFORGE_CLIP_MAX_DURATION_SECONDS=180
CLIPFORGE_CLIP_FFMPEG_TIMEOUT_SECONDS=300
CLIPFORGE_CLIP_MAX_CONCURRENCY=1
CLIPFORGE_CLIP_DURATION_TOLERANCE_SECONDS=0.5
CLIPFORGE_CLIP_ENCODER_PRESET=veryfast

# Formatted clips (Phase 6)
CLIPFORGE_FORMAT_FFMPEG_TIMEOUT_SECONDS=600
CLIPFORGE_FORMAT_DURATION_TOLERANCE_SECONDS=0.5
CLIPFORGE_FORMAT_ENCODER_PRESET=veryfast

# Export (Phase 8)
CLIPFORGE_CLIP_EXPORT_MAX_ITEMS=50
CLIPFORGE_CLIP_EXPORT_MAX_BYTES=1073741824
```

The full list, with comments, is in `backend/.env.example`.

### Frontend — `clipforge\frontend\.env.local`

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

> `NEXT_PUBLIC_*` values are inlined into the browser bundle at build time.
> Restart `npm run dev` (or rebuild) after changing one. Never put a secret in
> a `NEXT_PUBLIC_*` variable.

### Changing the API port

1. Set `CLIPFORGE_PORT=8010` in `backend/.env`.
2. Set `NEXT_PUBLIC_API_BASE_URL=http://localhost:8010` in
   `frontend/.env.local`.
3. Restart the API, the worker and the frontend.

### Turning YouTube ingestion off

Set `CLIPFORGE_YOUTUBE_ACQUISITION_MODE=disabled` in `backend/.env` and
restart the API. The YouTube form then answers `503
youtube_acquisition_unavailable` and tells the user to upload a local file.

### Choosing a different speech model

Set `CLIPFORGE_TRANSCRIPTION_MODEL` in `backend/.env` and restart the worker.
The new model is downloaded on its first use and cached in
`backend/data/models/`. To keep the machine offline afterwards, set
`CLIPFORGE_TRANSCRIPTION_OFFLINE=true` as well.

### Exposing the API to the local network

The API binds `127.0.0.1` and refuses any other address, because it has no
authentication. To change that on purpose:

```powershell
# backend/.env
CLIPFORGE_HOST=0.0.0.0
CLIPFORGE_ALLOW_LAN_BINDING=true
```

Restart the API. Remember that every upload and every download endpoint is
then reachable by anything that can route to this machine.

---

## 5. Working with the data directory

```
backend/data/
├── uploads/    in-flight uploads
├── sources/    accepted source videos
├── temp/       worker scratch space
├── clips/      finished clips
├── formats/    formatted (portrait) clips
├── models/     the local Whisper model cache
└── database/   clipforge.db
```

To reclaim the disk space of sources you no longer need:

```powershell
Remove-Item -Recurse -Force clipforge\backend\data\sources
```

To reclaim the space taken by the speech model — it will be downloaded again
on the next transcription:

```powershell
Remove-Item -Recurse -Force clipforge\backend\data\models
```

To start completely fresh (this deletes every job record):

```powershell
Remove-Item -Recurse -Force clipforge\backend\data
```

All three are safe: nothing outside `backend\data` is touched, and each
directory is recreated when it is next needed.

---

## 6. Troubleshooting

### `python` is not recognised

Install Python 3.11+ and enable *Add python.exe to PATH*, or use the launcher:

```powershell
py -3.11 -m venv .venv
```

### `Activate.ps1` is blocked

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Or skip activation:

```powershell
..\.venv\Scripts\python.exe -m pytest
```

### `ffprobe is not installed or not on PATH`

Install FFmpeg (`winget install Gyan.FFmpeg`) and reopen the terminal, or
point the application at it explicitly:

```
CLIPFORGE_FFPROBE_PATH=C:\path\to\ffprobe.exe
```

Every upload then fails with `415 media_tool_unavailable`, which says so
plainly instead of accepting the file.

### `ffmpeg is not installed or not on PATH`

The same installation provides `ffmpeg`. If it lives somewhere else:

```
CLIPFORGE_FFMPEG_PATH=C:\path\to\ffmpeg.exe
```

Every job then fails with `ffmpeg_not_found` after it has been inspected.
Uploads still work; only the transcription fails.

### A job stays `queued`

The worker is not running. Start it:

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python worker.py
```

### A job stays `validating`, `downloading`, `extracting_audio` or `transcribing`

The worker died while it owned the job. The lease expires after
`CLIPFORGE_WORKER_LEASE_SECONDS` (default 5 minutes) and the job is queued
again. Restarting the worker recovers it straight away.

### A job fails with `transcription_model_unavailable`

The local model is not in the cache and could not be fetched. Fetch it once
while the machine is online — see *The speech model* in §1.2 — or check
`CLIPFORGE_TRANSCRIPTION_OFFLINE`, which makes this failure permanent on
purpose.

### A job fails with `no_audio_stream`

The source has no audio track. There is nothing to transcribe, and the job
fails rather than producing an empty transcript.

### A job fails with `transcription_failed`

The model could not decode the audio, or it produced timestamps that could
not be trusted. The worker log has the detail:

```powershell
# The worker prints the reason. Look for the line that mentions the job id.
```

### The transcript is empty

The audio was transcribed but the model found no speech in it. That happens
with music, noise and silence. Check that the source really has speech by
playing it.

### `Port 8000 is already in use`

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object OwningProcess
Stop-Process -Id <OwningProcess> -Force
```

Or change `CLIPFORGE_PORT` and `NEXT_PUBLIC_API_BASE_URL` as described above.

### The frontend shows "Backend unreachable"

1. Is the API running? Open http://localhost:8000/health.
2. Does `NEXT_PUBLIC_API_BASE_URL` match the API's port?
3. Did you change `.env.local` after starting the dev server? Restart it.
4. Is the browser console showing a CORS error? Add the exact browser origin
   to `CLIPFORGE_CORS_ORIGINS`. Wildcards are rejected on purpose.

### An upload is rejected with 413

The file is larger than `CLIPFORGE_MAX_UPLOAD_BYTES`. Raise it if you have the
disk space.

### An upload is rejected with 415

Either the extension is not in `CLIPFORGE_ALLOWED_UPLOAD_EXTENSIONS`, or
FFprobe could not read a video from the file. The message says which.

### A YouTube job fails with `acquisition_failed`

The video could not be downloaded: it may be private, region-locked,
age-restricted, or removed. The downloader's own output is in the worker log,
not in the job record. Upload a local copy instead.

### A highlight analysis fails with `ai_timeout` or `ai_unavailable`

The AI provider did not answer in time, or reported a temporary failure.
Reasoning models on long transcripts can take minutes per request. Each
window is retried inside the request budget
(`CLIPFORGE_AI_MAX_REQUEST_ATTEMPTS`, with backoff capped by
`CLIPFORGE_AI_MAX_BACKOFF_SECONDS`) and the whole analysis must finish inside
`CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS`; past either bound the analysis fails
with a stable code rather than running forever. Raise the per-request timeout
(`CLIPFORGE_AI_REQUEST_TIMEOUT_SECONDS`), the deadline, or shrink the windows
(`CLIPFORGE_AI_MAX_SEGMENTS_PER_WINDOW`,
`CLIPFORGE_AI_MAX_TRANSCRIPT_CHARACTERS`) in `backend/.env`, then restart the
worker. If the analysis still fails, the provider itself may be overloaded or
unable to handle the request — check the provider service (or pick a different
integration on the Integrations page).

### A highlight analysis fails with `lease_expired`

This is no longer the ordinary symptom of a slow provider call: the worker
renews an analysis lease in the background, *during* the provider request and
during the backoff pauses, so the lease does not lapse while a call is in
flight. A `lease_expired` on an analysis now means the lease genuinely could
not be renewed — the worker was restarted or died, another worker took the
row over, or `CLIPFORGE_WORKER_LEASE_RENEWAL_INTERVAL_SECONDS` is so long
relative to `CLIPFORGE_WORKER_LEASE_SECONDS` that a single missed renewal
consumed the margin. Check that the worker is still running, then raise the
lease (for example `900`) in `backend/.env` and restart it.

### The backend will not start

Read the traceback. The usual causes are a missing dependency
(`pip install -r requirements-dev.txt`), a malformed `.env`, or an invalid
value — a non-numeric `CLIPFORGE_PORT`, a `*` in `CLIPFORGE_CORS_ORIGINS`, or
a malformed entry in `CLIPFORGE_ALLOWED_UPLOAD_EXTENSIONS` all fail fast at
startup, by design.

### Tests fail after changing `config.py`

The settings are validated on every load. If you added a required field
without a default, update `backend/.env.example` and the defaults in
`backend/app/config.py`.

### Hydration errors mentioning `bis_skin_checked`

If the browser shows React hydration errors — usually only in `npm run
dev` — and the diff is full of attributes like
`bis_skin_checked="1"` or `__processed_…="true"`, the app is not the
cause. Those attributes are injected into the page by a browser
extension (security and anti-phishing scanners do this) before React
hydrates, and the dev overlay reports them as a mismatch.

To confirm, open the same page in an extension-free window: the errors
will not appear, and no `bis_skin_checked` attributes will be in the
DOM. The fix that actually works is disabling the extension — or using
an extension-free window — while working on the app.
`suppressHydrationWarning` on the `<html>` and `<body>` roots (already
set in `frontend/src/app/layout.tsx`) quiets the injections that land
directly on those two elements, but an extension that tags every `div`
in the page cannot be silenced from the app's side.

---

## 7. Git

The repository was initialised with Git in Phase 1. `backend/.venv`,
`frontend/node_modules`, build output, `.env` files and the whole `data`
directory are ignored.

```powershell
git status
git add -A
git commit -m "..."
```

Never commit an `.env` file. `git check-ignore -v backend/.env` should print
the rule that ignores it.
