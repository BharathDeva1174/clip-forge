# ClipForge

**ClipForge** turns long-form videos into multiple short-form clips sized for
YouTube Shorts, Instagram Reels and TikTok. It runs locally on your own
machine: your videos never leave your disk, and the only credential it needs is
your own LLM API key.

> **Status: Phase 1 (project foundation).** The repository currently contains the
> backend API skeleton, its health/readiness endpoints, the frontend shell and
> its live backend status indicator. Video ingestion, transcription, AI
> highlight detection, clip rendering and captions are **not implemented yet** —
> the UI labels every one of them as upcoming. See [docs/ROADMAP.md](docs/ROADMAP.md)
> and [PHASE_1_REPORT.md](PHASE_1_REPORT.md).

---

## Technology stack

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4 |
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic v2 / pydantic-settings |
| Storage (planned) | SQLite for job metadata, local filesystem for media |
| Media (planned) | FFmpeg for audio extraction, clipping, 9:16 conversion and burn-in |
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
| Git | any | Only needed if you want to commit changes |
| FFmpeg | 4+ | Not required yet; needed from Phase 3 (audio) / Phase 5 (rendering) |

Check what you have:

```powershell
node --version
npm --version
python --version
```

---

## Repository layout

```
clipforge/
├── frontend/                  # Next.js app (http://localhost:3000)
│   ├── src/
│   │   ├── app/               # layout, page, global styles
│   │   ├── components/        # HealthIndicator, PipelinePreview, VideoSourcePanel
│   │   └── lib/               # config + health-check logic
│   ├── public/
│   ├── tests live next to the code as *.test.ts(x)
│   ├── package.json
│   └── .env.example
├── backend/                   # FastAPI app (http://localhost:8000)
│   ├── app/
│   │   ├── main.py            # application entry point
│   │   ├── config.py          # environment-driven settings
│   │   ├── logging_config.py
│   │   └── routers/           # health, api
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
├── docs/                      # Architecture, development, testing, roadmap, decisions
├── .env.example
├── .editorconfig
├── .gitignore
├── README.md
└── PHASE_1_REPORT.md
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

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | `frontend/.env.local` | `http://localhost:8000` | Base URL the browser calls |
| `CLIPFORGE_ENVIRONMENT` | `backend/.env` | `development` | `development` \| `test` \| `production` |
| `CLIPFORGE_DEBUG` | `backend/.env` | `false` | Verbose error output |
| `CLIPFORGE_LOG_LEVEL` | `backend/.env` | `INFO` | Log level |
| `CLIPFORGE_HOST` | `backend/.env` | `127.0.0.1` | API bind address |
| `CLIPFORGE_PORT` | `backend/.env` | `8000` | API port |
| `CLIPFORGE_CORS_ORIGINS` | `backend/.env` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed browser origins |
| `CLIPFORGE_STORAGE_DIR` | `backend/.env` | `data` | Media/SQLite root (Phase 2+) |
| `CLIPFORGE_LLM_API_KEY` | `backend/.env` | *(unset)* | Reserved for Phase 4 — backend only |
| `CLIPFORGE_LLM_BASE_URL` | `backend/.env` | *(unset)* | Reserved for Phase 4 |
| `CLIPFORGE_LLM_MODEL` | `backend/.env` | *(unset)* | Reserved for Phase 4 |

**Secret rules**

- API keys live only in `backend/.env` (git-ignored) and only in
  `CLIPFORGE_LLM_*` variables.
- Never put a secret in a `NEXT_PUBLIC_*` variable: Next.js inlines those into
  the browser bundle, where anybody can read them.
- The backend stores secrets as `SecretStr`, so they are masked in logs and
  dumps, and never appear in an API response.

---

## Running the applications

Two terminals.

**Terminal 1 — backend** (from `backend/`):

```powershell
..\.venv\Scripts\Activate.ps1
python run.py
```

`python run.py` starts Uvicorn with auto-reload on the configured host and
port. The equivalent explicit command is:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**Terminal 2 — frontend** (from `frontend/`):

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
| http://localhost:8000/docs | Interactive OpenAPI docs |

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

**Port 8000 or 3000 already in use** — find and stop the process, or change
`CLIPFORGE_PORT` in `backend/.env` and `NEXT_PUBLIC_API_BASE_URL` in
`frontend/.env.local` to match.

**The frontend says "Backend unreachable"** — the API is not running or is on a
different port. Check `http://localhost:8000/health` in a browser, then press
**Retry** in the header. The frontend reads its URL from
`NEXT_PUBLIC_API_BASE_URL` at build time: after changing it, restart
`npm run dev` (or rebuild).

**CORS error in the browser console** — the browser origin is not in
`CLIPFORGE_CORS_ORIGINS`. Wildcards are rejected by design, so list the exact
origin.

**`npm run build` fails to fetch fonts** — `next/font/google` downloads Geist
at build time. On a machine without internet access, remove the `next/font`
imports from `src/app/layout.tsx` and use a system font stack instead.

**Windows path errors in the API** — make sure the backend is started from the
`backend/` directory, so `CLIPFORGE_STORAGE_DIR` and `.env` resolve relative
to it.

---

## Known limitations

- No video processing exists yet: no upload, no YouTube ingestion, no
  transcription, no LLM analysis, no rendering, no captions, no export.
- No database. Job metadata storage and the filesystem layout arrive in later
  phases.
- No authentication. The API is intended for local use only and binds to
  `127.0.0.1` by default.
- The frontend has no routing, no job list and no clip editor — those are
  Phases 7 and 8.
- `npm audit` reports advisories in the dev-only ESLint plugin chain
  (`braces` via `micromatch` via `fast-glob`). There is no patched `braces`
  release; the only "fix" npm offers is downgrading `eslint-config-next` to
  v14, which breaks the Next.js 16 lint setup. The affected code only runs
  during linting and is not part of the shipped application.
- The health check fires once per page load and on demand. There is no
  polling.

---

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Boundaries, request flow, planned pipeline and storage |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Copy-pasteable PowerShell commands |
| [docs/TESTING.md](docs/TESTING.md) | Suites, commands, how to read failures |
| [docs/ROADMAP.md](docs/ROADMAP.md) | The ten phases and their status |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Architectural decisions and reasoning |
| [PHASE_1_REPORT.md](PHASE_1_REPORT.md) | What was built, what was verified, what is left |

---

## Legal note

Any YouTube ingestion added in a later phase must respect the copyright of the
source material, the access permissions of the account, and YouTube's terms of
service. Downloading or re-uploading video without authorisation is not
something ClipForge will do.
