# ClipForge

**ClipForge** turns long-form videos into multiple short-form clips sized for
YouTube Shorts, Instagram Reels and TikTok. It runs locally on your own
machine: your videos never leave your disk, and the only credential it needs is
your own LLM API key.

> **Status: Phase 2 (video ingestion and background jobs).** The API accepts
> video uploads and authorised YouTube URLs, stores them, and validates them
> with FFprobe in a separate worker process. Transcription, AI highlight
> detection, clip rendering and captions are **not implemented yet** — the UI
> labels every one of them as upcoming. See
> [docs/ROADMAP.md](docs/ROADMAP.md) and [PHASE_2_REPORT.md](PHASE_2_REPORT.md).

---

## Technology stack

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4 |
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic v2 / pydantic-settings |
| Jobs | SQLite (WAL mode) with a versioned schema and atomic claiming |
| Worker | A separate Python process (`backend/worker.py`) |
| Media | FFprobe for validation (FFmpeg for rendering, from Phase 5) |
| AI (planned) | External LLM API for highlight detection, behind a provider abstraction |
| Tests | pytest + httpx (backend), Vitest + React Testing Library (frontend) |

Docker is **optional** and not required to run anything in this repository.

---

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Node.js | 20.9+ (22 LTS / 24 LTS recommended) | Next.js 16 requires >= 20.9.0 |
| npm | 10+ | Ships with Node.js |
| Python | 3.11+ | `python --version` |
| **FFmpeg / FFprobe** | 4+ | **Required from Phase 2** — media validation uses FFprobe |
| Git | any | Only needed if you want to commit changes |

Check what you have:

```powershell
node --version
npm --version
python --version
ffprobe -version
```

If FFprobe is missing, install FFmpeg and make sure `ffprobe` is on your
`PATH` (the `Gyan.FFmpeg` winget package does this for you):

```powershell
winget install Gyan.FFmpeg
```

---

## Repository layout

```
clipforge/
├── frontend/                  # Next.js app (http://localhost:3000)
│   ├── src/
│   │   ├── app/               # layout, page, global styles
│   │   ├── components/        # HealthIndicator, JobList, PipelinePreview, VideoSourcePanel
│   │   └── lib/               # api client, config, health check
│   ├── public/
│   ├── package.json
│   └── .env.example
├── backend/                   # FastAPI app (http://localhost:8000)
│   ├── app/
│   │   ├── main.py            # application entry point
│   │   ├── config.py          # environment-driven settings
│   │   ├── db.py              # SQLite schema, migrations, job repository
│   │   ├── jobs.py            # job states and allowed transitions
│   │   ├── storage.py         # storage layout and path containment
│   │   ├── media.py           # FFprobe inspection
│   │   ├── youtube.py         # strict URL validation
│   │   ├── acquisition.py     # downloader adapter
│   │   ├── uploads.py         # streaming multipart upload
│   │   ├── worker.py          # background worker
│   │   └── routers/           # health, api, jobs, videos
│   ├── worker.py              # worker entry point
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
├── docs/                      # Architecture, development, testing, roadmap, decisions
├── .env.example
├── .editorconfig
├── .gitignore
├── README.md
├── PHASE_1_REPORT.md
└── PHASE_2_REPORT.md
```

---

## Setup (Windows PowerShell)

Run every command from the repository root unless the comment says otherwise.

### 1. Backend

```powershell
cd backend

# Create and activate the virtual environment
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1

# Install runtime + test dependencies
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

# Create your local environment file
Copy-Item .env.example .env

# Confirm it works
python -m pytest
```

If PowerShell blocks the activation script, allow it for the current session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 2. Frontend

```powershell
cd frontend

# Install dependencies
npm install

# Create your local environment file
Copy-Item .env.example .env.local

# Confirm it works
npm run typecheck
npm run lint
npm test
```

---

## Configuration

Environment variables are the only configuration mechanism. There is no config
file to keep in sync with the code.

### Backend — `backend/.env`

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLIPFORGE_ENVIRONMENT` | `development` | `development` \| `test` \| `production` |
| `CLIPFORGE_DEBUG` | `false` | Verbose error output |
| `CLIPFORGE_LOG_LEVEL` | `INFO` | Log level |
| `CLIPFORGE_HOST` | `127.0.0.1` | API bind address |
| `CLIPFORGE_PORT` | `8000` | API port |
| `CLIPFORGE_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed browser origins |
| `CLIPFORGE_STORAGE_DIR` | `data` | Root of uploads, sources, scratch space and the database |
| `CLIPFORGE_DATABASE_PATH` | *(unset)* | Overrides the database location |
| `CLIPFORGE_MAX_UPLOAD_BYTES` | `2147483648` (2 GiB) | Maximum upload size |
| `CLIPFORGE_MAX_VIDEO_DURATION_SECONDS` | `14400` (4 h) | Maximum video duration |
| `CLIPFORGE_MAX_VIDEO_DIMENSION` | `7680` | Maximum width/height in pixels |
| `CLIPFORGE_ALLOWED_UPLOAD_EXTENSIONS` | `.mp4,.mov,.m4v,.webm,.mkv` | Accepted containers |
| `CLIPFORGE_ALLOWED_VIDEO_CODECS` | `h264,hevc,vp9,av1,mpeg4` | Accepted video codecs |
| `CLIPFORGE_FFPROBE_PATH` | `ffprobe` | FFprobe executable |
| `CLIPFORGE_FFPROBE_TIMEOUT_SECONDS` | `60` | FFprobe timeout |
| `CLIPFORGE_WORKER_POLL_INTERVAL_SECONDS` | `2` | How often the worker polls |
| `CLIPFORGE_WORKER_LEASE_SECONDS` | `300` | How long a claimed job stays owned |
| `CLIPFORGE_WORKER_MAX_ATTEMPTS` | `3` | Retries for a transient failure |
| `CLIPFORGE_WORKER_ACQUISITION_TIMEOUT_SECONDS` | `1800` | Download timeout |
| `CLIPFORGE_DATABASE_BUSY_TIMEOUT_SECONDS` | `30` | SQLite lock wait |
| `CLIPFORGE_DATABASE_WAL_MODE` | `true` | WAL mode (local filesystems only) |
| `CLIPFORGE_YOUTUBE_ACQUISITION_MODE` | `yt_dlp` | `yt_dlp` or `disabled` |
| `CLIPFORGE_YOUTUBE_ALLOWED_HOSTS` | YouTube hosts | Exact hostnames accepted |
| `CLIPFORGE_YOUTUBE_MAX_BYTES` | `2147483648` | Maximum download size |
| `CLIPFORGE_LLM_API_KEY` | *(unset)* | Reserved for Phase 4 — backend only |

### Frontend — `frontend/.env.local`

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | Base URL the browser calls |

**Secret rules**

- API keys live only in `backend/.env` (git-ignored) and only in
  `CLIPFORGE_LLM_*` variables.
- Never put a secret in a `NEXT_PUBLIC_*` variable: Next.js inlines those into
  the browser bundle, where anybody can read them.
- The backend stores secrets as `SecretStr`, so they are masked in logs and
  dumps, and never appear in an API response.

---

## Running the applications

**Three terminals**: the API, the worker and the frontend.

**Terminal 1 — API** (from `backend/`):

```powershell
..\.venv\Scripts\Activate.ps1
python run.py
```

`python run.py` starts Uvicorn with auto-reload on the configured host and
port. The equivalent explicit command is:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**Terminal 2 — worker** (from `backend/`):

```powershell
..\.venv\Scripts\Activate.ps1
python worker.py
```

The worker is a separate operating-system process. It uses the same
configuration and the same database as the API, and it is the only thing that
downloads or inspects media. Jobs stay in the queue until the worker is
running, so you can start it later and it will pick up whatever is waiting.

**Terminal 3 — frontend** (from `frontend/`):

```powershell
npm run dev
```

### Local URLs

| URL | What |
| --- | --- |
| http://localhost:3000 | ClipForge frontend |
| http://localhost:8000/health | Liveness contract |
| http://localhost:8000/ready | Readiness + implemented capabilities |
| http://localhost:8000/api/v1 | API name and version |
| http://localhost:8000/api/v1/jobs | Recent jobs, newest first |
| http://localhost:8000/docs | Interactive OpenAPI docs |

---

## How ingestion works

```
  browser                API                    worker                 disk
     │                    │                       │                    │
     │ POST /videos/upload│                       │                    │
     │───────────────────►│ stream to disk        │                    │
     │                    │ (limit enforced while │
     │                    │  the body arrives)    │                    │
     │                    │ FFprobe validation    │                    │
     │                    │ INSERT job (queued)   │                    │
     │◄─── 202 + job id ──│                       │                    │
     │                    │                       │                    │
     │                    │      claim job ──────►│                    │
     │                    │      (atomic)         │ FFprobe inspect ───►│
     │                    │                       │ UPDATE job         │
     │ GET /jobs/{id}     │◄── status, progress ──│                    │
```

`completed` means **ingestion and media inspection finished** — the source is
stored and verified. It does not mean clips have been generated; that arrives
in Phase 5.

### Job states

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for a worker |
| `validating` | A worker is checking the source |
| `downloading` | A worker is fetching an authorised YouTube video |
| `inspecting` | A worker is reading the media metadata |
| `completed` | The source is stored and validated |
| `failed` | The job could not be completed; `error.code` says why |
| `cancelled` | The job was cancelled |

### Supported source formats

| | Accepted |
| --- | --- |
| Containers | `.mp4`, `.mov`, `.m4v`, `.webm`, `.mkv` |
| Video codecs | `h264`, `hevc`, `vp9`, `av1`, `mpeg4` |
| Duration | Up to 4 hours by default |
| Dimensions | Up to 7680 px on a side by default |
| Size | Up to 2 GiB by default |

The extension is only a first filter. The file is then opened with FFprobe, and
a file that is not really a video is rejected with `415 unsupported_media_type`.

### Authorised YouTube URLs

- Only `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be` and
  `www.youtu.be` are accepted, compared **exactly** — `youtube.com.attacker.example`
  is not `youtube.com`.
- Only `http`/`https`, only the default ports, no credentials in the URL.
- Watch, short, embed and shorts URLs are accepted and normalised to
  `https://www.youtube.com/watch?v=<id>`.
- You must tick **"I own this video or have permission to download and reuse
  it"**. The request is refused without it.
- The downloader runs without cookies or a login, so it can only reach videos
  that are publicly available without signing in. Private, age-restricted and
  region-locked videos fail with a clear error.
- Set `CLIPFORGE_YOUTUBE_ACQUISITION_MODE=disabled` to refuse every YouTube
  submission and point users at local uploads instead.

### Storage and retention

```
backend/data/
├── uploads/    in-flight uploads; a partial file is deleted on any error
├── sources/    accepted source videos, named with a server-generated id
├── temp/       scratch space used by the worker, cleaned up after each job
└── database/   clipforge.db (SQLite)
```

- The database stores **relative** paths. Every path is resolved and checked
  for containment before it is opened, deleted or probed.
- Original filenames are never used as paths. They are kept only so you can
  recognise your own file.
- A source file is **not** deleted when processing fails. Delete
  `backend/data/sources` by hand to reclaim the space.
- The whole `data` directory is git-ignored.

---

## Tests and quality checks

Backend (from `backend/`):

```powershell
..\.venv\Scripts\Activate.ps1
python -m pytest
```

Frontend (from `frontend/`):

```powershell
npm run typecheck   # next typegen && tsc --noEmit
npm run lint        # ESLint
npm test            # Vitest (unit + component)
npm run build       # production build
```

See [docs/TESTING.md](docs/TESTING.md) for what each suite covers and how to
read failures.

---

## Troubleshooting

**`python` is not recognised** — install Python 3.11+ and tick *Add python.exe
to PATH*, or use the `py` launcher: `py -3.11 -m venv .venv`.

**`Activate.ps1` is blocked by execution policy** — run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and activate
again. Alternatively skip activation and call the interpreter directly:
`..\.venv\Scripts\python.exe -m pytest`.

**"ffprobe is not installed"** — install FFmpeg and make sure `ffprobe` is on
`PATH`, or set `CLIPFORGE_FFPROBE_PATH` to its full path.

**A job stays `queued` forever** — the worker is not running. Start it with
`python worker.py` from `backend/`. It will pick up the waiting job.

**A job stays `validating` or `downloading` forever** — the worker died. Its
lease expires after `CLIPFORGE_WORKER_LEASE_SECONDS` and the job is queued
again (or failed if it has used up its attempts). Restarting the worker
recovers it immediately.

**The upload fails with 413** — the file is larger than
`CLIPFORGE_MAX_UPLOAD_BYTES`. Raise it if you have the disk space.

**The upload fails with 415** — either the extension is not in the allowlist,
or FFprobe could not read a video from the file. Check the message; it says
which of the two it was.

**A YouTube job fails with `acquisition_failed`** — the video could not be
downloaded. It may be private, region-locked, age-restricted, or already
removed. Upload a local copy instead.

**The frontend says "Backend unreachable"** — the API is not running or is on a
different port. Check http://localhost:8000/health, then press **Retry** in
the header. The frontend reads its URL from `NEXT_PUBLIC_API_BASE_URL` at
build time: after changing it, restart `npm run dev` (or rebuild).

**CORS error in the browser console** — the browser origin is not in
`CLIPFORGE_CORS_ORIGINS`. Wildcards are rejected by design, so list the exact
origin.

**`npm run build` fails to fetch fonts** — `next/font/google` downloads Geist
at build time. On a machine without internet access, remove the `next/font`
imports from `src/app/layout.tsx` and use a system font stack instead.

**Windows path errors in the API** — make sure the API and the worker are
started from the `backend/` directory, so `CLIPFORGE_STORAGE_DIR` and `.env`
resolve relative to it.

---

## Known limitations

- **No clips are produced yet.** Ingestion stores and validates a source; the
  transcript, the highlights, the rendered clips and the captions are Phase 3
  to Phase 6.
- **YouTube acquisition depends on YouTube.** yt-dlp works against a moving
  target. A video that downloads today may not tomorrow, and videos that
  require a sign-in or a region check are refused by design.
- **No job cancellation.** The state machine defines a `cancelled` status, but
  no API can reach it yet. There is deliberately no cancel button in the UI.
- **No authentication.** The API binds `127.0.0.1` and is a local tool only.
- **The local development server is not a hardened upload gateway.** It is
  Uvicorn behind no proxy, with no rate limiting and no request-size limit
  beyond the configured upload cap.
- **`npm audit` reports advisories** in the dev-only ESLint plugin chain
  (`braces` via `micromatch` via `fast-glob`). There is no patched `braces`
  release; the only "fix" npm offers is downgrading `eslint-config-next` to
  v14, which breaks the Next.js 16 lint setup. The affected code only runs
  during linting and is not part of the shipped application.
- **The frontend has no routing, no job detail page and no clip editor** —
  those are Phases 7 and 8.
- **No end-to-end browser automation.** The Phase 2 integration check was done
  by hand in a real browser; browser automation is scheduled for Phase 9.

---

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Boundaries, request flow, job model, storage, claiming, recovery |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Copy-pasteable PowerShell commands |
| [docs/TESTING.md](docs/TESTING.md) | Suites, commands, how to read failures |
| [docs/ROADMAP.md](docs/ROADMAP.md) | The ten phases and their status |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Architectural decisions and reasoning |
| [PHASE_1_REPORT.md](PHASE_1_REPORT.md) | Phase 1: what was built, what was verified |
| [PHASE_2_REPORT.md](PHASE_2_REPORT.md) | Phase 2: what was built, what was verified |

---

## Legal note

YouTube ingestion only works for videos you own or have permission to download
and reuse. It respects the copyright of the source material, the access
permissions of the account, and YouTube's terms of service. The downloader is
never given cookies or a login, so it can only reach publicly available
material. Downloading or re-uploading video without authorisation is not
something ClipForge will do.
