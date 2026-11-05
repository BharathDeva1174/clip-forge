# ClipForge

**ClipForge** turns long-form videos into multiple short-form clips sized for
YouTube Shorts, Instagram Reels and TikTok. It runs locally on your own
machine: your videos never leave your disk, and the only credential it needs is
your own LLM API key (optional).

**Status: Phase 10 — Local Launch & Polish.** All ten phases are implemented
and verified: video ingestion, transcription, AI highlight detection, clip
rendering, vertical formatting with burned-in captions, revisions, caption
overrides, and ZIP export. See [docs/ROADMAP.md](docs/ROADMAP.md) for the phase
list and the acceptance criteria each phase was verified against.

Work continued after Phase 10 on the reliability of the AI pipeline and on
cancellation: a running highlight analysis now renews its lease during the
provider call and is bounded by an analysis deadline, and cancelling a job
stops the in-flight download or render within one lease tick instead of
letting it finish. Those are decisions D51–D56 in
[docs/DECISIONS.md](docs/DECISIONS.md).

The per-phase verification reports — what each phase decided, what was
actually run, and what was left limited — are kept on the author's machine and
are **not** part of this repository. The durable conclusions live in
[docs/ROADMAP.md](docs/ROADMAP.md) and [docs/DECISIONS.md](docs/DECISIONS.md),
so nothing that is published here points at a file that is not.

**Just cloned it?** [QUICK_START.md](QUICK_START.md) walks from an empty folder
to a finished vertical clip: prerequisites, the two `pip`/`npm` installs, the
one-command launch, the first clip, and how to check the install.

---

## Technology stack

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4 |
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic v2 / pydantic-settings |
| Jobs | SQLite (WAL mode) with a versioned schema (v7) and atomic claiming |
| Worker | A separate Python process (`backend/worker.py`) |
| Media | FFprobe for validation, FFmpeg for audio extraction, rendering, formatting |
| Ingestion | `yt-dlp` for an authorised YouTube URL, behind a swappable acquirer that can be turned off |
| Speech | `faster-whisper` — Whisper models running locally, offline-capable |
| AI (optional) | User-configured provider behind a provider abstraction (OpenAI-compatible, OpenAI Responses, Anthropic) |
| Tests | pytest + httpx (backend), Vitest + React Testing Library (frontend) |

Docker is **optional** and not required to run anything in this repository.

---

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Node.js | 20.9+ (22 LTS / 24 LTS recommended) | Next.js 16 requires >= 20.9.0 |
| npm | 10+ | Ships with Node.js |
| Python | 3.11+ | `python --version` |
| **FFmpeg / FFprobe** | 4+ | Required — media validation uses FFprobe, rendering uses FFmpeg |
| Git | any | Only needed to inspect the repository |

Check what you have:

```powershell
node --version
npm --version
python --version
ffprobe -version
ffmpeg -version
```

If FFmpeg/FFprobe is missing, install FFmpeg and make sure `ffprobe` is on your
`PATH` (the Gyan.FFmpeg winget package does this):

```powershell
winget install Gyan.FFmpeg
```

Then reopen the terminal so the new `PATH` is picked up.

---

## Repository layout

```
clipforge/
├── frontend/                  # Next.js app (http://localhost:3000)
├── backend/                   # FastAPI app (http://localhost:8000) + worker
├── docs/                      # Architecture, development, testing, roadmap, decisions
├── scripts/                   # start-local.ps1 (one-command launch), test-health.ps1
├── .env.example               # Root template (points at the two app templates)
├── README.md
├── QUICK_START.md             # Clone → first finished clip
└── AGENTS.md
```

The virtual environment lives at `..\.venv` — at the repository root, not
inside `backend/`. `backend/data/` holds the media, the model cache and the
SQLite file, and is git-ignored.

---

## First-time setup (Windows PowerShell)

Run every command from the repository root unless a comment says otherwise.

### 0. Get the code

```powershell
git clone https://github.com/BharathDeva1174/clip-forge.git
cd clip-forge
```

The repository carries source and documentation only — no media, no database,
no model weights, no environment files. See [QUICK_START.md](QUICK_START.md) for
the short version of everything below.

### 1. Backend

```powershell
cd backend

# Create and activate the virtual environment (at the repo root)
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1

# Install runtime + test dependencies
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

# Create your local environment file
Copy-Item .env.example .env
```

> If PowerShell blocks the activation script, run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the same
> terminal and activate again. You can also skip activation entirely and use
> `..\.venv\Scripts\python.exe` in place of `python`.

`pip install -r requirements-dev.txt` also installs `faster-whisper`. It pulls
in ctranslate2, huggingface-hub, tokenizers, onnxruntime and av, so it is a
noticeably larger install than a plain FastAPI one.

#### The speech model

The model weights are **not** part of the installation. The first
transcription downloads the configured model into `backend/data/models/` and
every later one runs offline. To fetch it up front — for example on a machine
that will be offline afterwards — run:

```powershell
cd backend
..\.venv\Scripts\python.exe -c "from pathlib import Path; from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8', download_root='data/models'); print('model cached')"
```

| Model | Size | Use it when |
| --- | --- | --- |
| `tiny.en` | about 75 MB | Development, and a quick check that the pipeline works |
| `base.en` | about 145 MB | The sensible default for real use |
| `small.en` | about 480 MB | Accuracy matters more than time |

The model name is set with `CLIPFORGE_TRANSCRIPTION_MODEL`. Only the `.en`
models are English; the multilingual models (`base`, `small`, …) are used
for other languages.

Set `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` once the model is cached: the
engine then refuses to touch the network at all, and a missing model produces
an actionable error instead of a silent download.

### 2. Frontend

```powershell
cd frontend

npm install

Copy-Item .env.example .env.local
```

### 3. Sanity check

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest

cd ..\frontend
npm run typecheck
npm run lint
npm test
```

---

## Configuration

Environment variables are the only configuration mechanism. There is no config
file. The backend reads `backend/.env`; the frontend reads
`frontend/.env.local`. See `backend/.env.example` and `frontend/.env.example`
for the full, commented list.

Essentials:

```
CLIPFORGE_ENVIRONMENT=development
CLIPFORGE_HOST=127.0.0.1
CLIPFORGE_PORT=8000
CLIPFORGE_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
CLIPFORGE_STORAGE_DIR=data
CLIPFORGE_TRANSCRIPTION_MODEL=tiny.en
CLIPFORGE_TRANSCRIPTION_OFFLINE=false
```

The API has no authentication, so it only binds to this machine. Set
`CLIPFORGE_ALLOW_LAN_BINDING=true` to expose it to the network.

The AI provider configuration is **not** in `.env`: it is saved in the
database and managed from the Integrations page, and the key lives in the
operating system's credential store.

Resource bounds (defaults): upload 2 GiB, video duration 4 h, transcription
timeout 1 h, clip render 5 min, format render 10 min, export 50 clips / 1 GiB,
one highlight analysis 15 min (`CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS`) with
each provider window retried up to 3 times.

---

## Running the application

### One command

```powershell
cd clipforge
.\scripts\start-local.ps1                        # API on 8000, worker, frontend on 3000
.\scripts\start-local.ps1 -ApiPort 19400 -FrontendPort 14400
```

`scripts/start-local.ps1` starts the API, then the worker, then the frontend,
gates each step on readiness rather than a fixed delay, logs every process to a
file under `$env:TEMP\clipforge-local-launch\`, and provides `Stop-ClipForge` to
stop exactly the processes it started. Use `-Replace` to clear ClipForge
processes left over from an earlier run.

### Three terminals

The worker is a separate process and is what actually processes the jobs — the
API only queues them.

### Terminal 1 — API

```powershell
cd backend
..\.venv\Scripts\python.exe run.py
```

Expected output: `Uvicorn running on http://127.0.0.1:8000`.

### Terminal 2 — worker

```powershell
cd backend
..\.venv\Scripts\python.exe worker.py
```

Expected output: `clipforge.worker | worker <id> started (storage=..., database=clipforge.db)`.

### Terminal 3 — frontend

```powershell
cd frontend
npm run dev
```

Expected output: `- Local: http://localhost:3000`.

### URLs

| URL | What |
| --- | --- |
| http://localhost:3000 | Frontend |
| http://localhost:8000/health | `{"status":"ok","service":"clipforge-api"}` |
| http://localhost:8000/ready | Readiness and capability flags, worker heartbeat, model cache |
| http://localhost:8000/api/v1 | API metadata |
| http://localhost:8000/api/v1/jobs | Recent jobs, newest first |
| http://localhost:8000/docs | OpenAPI explorer |

Stop any process with `Ctrl+C`. The worker stops claiming new work and finishes
or releases the job it is on.

### Production build of the frontend

```powershell
cd frontend
npm run build
npm run start
```

`npm run start` serves the production build on http://localhost:3000.

---

## Verifying health and readiness

```powershell
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/ready` reports `config: loaded`, all capability flags, whether the
transcription model is cached, and the worker heartbeat (`running`,
`last_seen`). A capability is reported healthy only when it is implemented
**and** usable; a missing model cache is reported as `transcription_model_cached: false`,
not as a failure of the feature.

---

## Using the application

1. Open http://localhost:3000.
2. **Upload** a local video (or submit an authorised YouTube URL) on the
   dashboard. The size limit is enforced while the body arrives.
3. Watch the job progress through extraction, transcription, and (after you
   configure an integration) highlight detection.
4. On the **Integrations** page, add a preset or custom endpoint, store the
   key in the OS credential store, test the connection, and select a default
   model.
5. **Generate clips** from highlight candidates. Boundaries are server-authoritative.
6. **Format** a clip to portrait 9:16 with burned-in captions.
7. **Revise** a clip (trim) — creates a new revision, original untouched.
8. **Edit captions** — produces a distinct format fingerprint.
9. **Export** finished clips to a ZIP.

Local video stays on disk. Only transcript text is sent to a configured
remote AI endpoint, and only when you request highlight detection.

---

## Where data is stored

Everything lives under `CLIPFORGE_STORAGE_DIR` (default `backend/data`):

```
data/
├── uploads/    # in-flight uploads
├── sources/    # validated source media
├── temp/       # scratch space (audio, renders in progress)
├── clips/      # finished clips
├── formats/    # formatted (portrait) clips
├── models/     # cached Whisper weights
└── database/   # clipforge.db (SQLite)
```

Paths in the database are relative to the storage root and resolved with a
containment check.

---

## Security and privacy

- The API binds to loopback by default; LAN binding requires explicit opt-in.
- CORS lists explicit origins; wildcards are rejected at load time.
- API keys live in the OS credential store; the database holds only a reference.
- No plaintext secret fallback; missing store → `SECRET_STORE_UNAVAILABLE`.
- Media paths are contained to the storage root; client filenames are never
  used as paths.
- Subprocesses use argument arrays (`shell=False`), timeouts, and
  terminate-then-kill-reap.
- AI transport requires HTTPS for remote, refuses redirects, bounds request
  and response, and never leaks the key or the provider's error body.

---

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `Could not determine home directory` | `HOME`/`USERPROFILE` not set in the process environment (common in stripped sandboxes). Set them, e.g. `$env:USERPROFILE='C:\Users\you'`. |
| `ffprobe` / `ffmpeg` not found | Install FFmpeg and reopen the terminal so PATH updates. |
| Port already in use | Change `CLIPFORGE_PORT` / the frontend port, or stop the other process. |
| Worker not running (`/ready` shows `running: false`) | Start `python worker.py` from `backend/`. |
| Transcription model not cached | Run the cache command in §1.2, or allow the first run to download (offline mode off). |
| `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` with missing model | Set offline false, cache the model, then re-enable. |
| Database migration failure | Check logs; restore from a backup of `data/database/`. Never delete the live DB to "fix" it — use a temp storage root for tests. |
| Credential store unavailable | Ensure the OS keyring is usable (Windows Credential Manager). |
| AI connection test fails | Verify endpoint URL, key, and that the endpoint speaks one of the supported protocols. |
| Export exceeds limit | Raise `CLIPFORGE_CLIP_EXPORT_MAX_ITEMS` / `CLIPFORGE_CLIP_EXPORT_MAX_BYTES`, or export fewer/smaller clips. |
| Stale/interrupted job | Cancel it, or restart the worker; expired leases are recovered automatically. |
| Hydration errors or an "N Issues" badge in the browser, with `bis_skin_checked` / `bis_register` attributes in the diff | A browser extension is mutating the page before React hydrates — the `bis_*` attributes are the signature of Bitdefender's page scanner, bundled into extensions such as Urban VPN Proxy. Disable or remove the extension (or test in an Incognito window); the app itself has no mismatch, which is verified in an extension-free browser. |

---

## Tests

```powershell
# Backend (from backend/)
..\.venv\Scripts\python.exe -m pytest

# Frontend (from frontend/)
npm test
npm run typecheck
npm run lint
npm run build
npm run audit:check
```

E2E scripts (`backend/tests/e2e_*.py`) start real API + worker + FFmpeg +
real speech model and take minutes; run them serially, one at a time, from
`backend/`.

The ordinary suite never loads the real speech model; `test_real_model.py`
skips unless the model is already cached.

---

## Known limitations

- No repeatable browser automation is available on this install (no
  Playwright/Cypress/Selenium; the built-in preview cannot attach). UI was
  verified over HTTP and via component tests.
- Cancellation stops the work, not only the bookkeeping (decision D55): a
  cancelled job fails its queued clips, formats and analyses in the same
  transaction, and an in-flight yt-dlp download, clip render or format render
  is terminated within one lease tick. A cancel that lands inside the bounded
  (≤ 60 s) verification step is picked up at the next tick boundary, not
  mid-probe. Cancelled results are never published.
- No live paid-provider verification: every protocol-adapter and analysis test
  runs against a local deterministic mock endpoint, so the shape of a real
  vendor's answer is not asserted anywhere.
- Highlight analyses on long transcripts are bounded by
  `CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS`; a reasoning model that needs
  longer than that has to be given a bigger deadline rather than a retry.
- The ZIP is assembled into a temporary file and streamed, but the whole
  archive still occupies disk for the duration of the response.
- Dependency audit: `npm run audit:check` **currently fails**. Its accepted
  baseline is the old dev-only `braces` advisory (GHSA-vfj7-8cjw-p6xm), but
  `npm audit` now reports GHSA-g3hv-75wg-m249 — a moderate denial of service in
  `minimatch` (`<11.0.1`) that reaches the 16 dev-only packages of the ESLint /
  `typescript-eslint` chain, with no viable fix: npm's only suggestion is a
  semver-major downgrade of `eslint` to v4, which cannot read this project's
  flat config. The baseline has not been widened without a decision; `npm run
  lint` passes. See [AGENTS.md](AGENTS.md).

---

## Documentation

- [QUICK_START.md](QUICK_START.md) — clone to first finished clip, prerequisites and troubleshooting
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — setup and commands
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces are wired
- [docs/TESTING.md](docs/TESTING.md) — what each suite covers
- [docs/ROADMAP.md](docs/ROADMAP.md) — phases and acceptance criteria
- [docs/DECISIONS.md](docs/DECISIONS.md) — numbered decisions
