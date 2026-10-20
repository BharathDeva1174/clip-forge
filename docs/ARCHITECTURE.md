# Architecture

This document describes how ClipForge is put together. It separates what
**exists today** (Phase 1) from what is **planned** for the remaining phases,
so nothing in the roadmap can be mistaken for working code.

---

## 1. The shape of the system

```
┌──────────────────────────────┐
│  Browser                      │
│  Next.js 16 (App Router)      │
│  http://localhost:3000        │
└───────────────┬──────────────┘
                │  fetch /health, /ready, /api/v1  (CORS, JSON)
                ▼
┌──────────────────────────────┐
│  FastAPI (uvicorn)            │
│  http://localhost:8000        │
│                              │
│  app/main.py   entry point   │
│  app/config.py settings      │
│  app/routers/  HTTP routes   │
└───────────────┬──────────────┘
                │  (planned, Phase 2+)
                ▼
┌──────────────────────────────┐
│  Local storage                │
│  SQLite  + media on disk      │
└──────────────────────────────┘
```

Two processes, two URLs, one machine. The frontend is the only thing the user
touches; the backend owns every privileged operation.

---

## 2. Frontend / backend boundary

**The rule: the browser never talks to a provider, a database or FFmpeg
directly. It only talks to the local API.**

| Concern | Owner | Why |
| --- | --- | --- |
| Talking to the LLM provider | Backend | The API key must not reach the browser |
| Reading/writing SQLite | Backend | Only one writer; keeps the schema in one place |
| Reading/writing media files | Backend | Filesystem paths and FFmpeg stay off the client |
| Rendering, layout, status display | Frontend | React owns the UI |

The frontend is a thin client over the API. When a phase adds a capability, it
adds a backend endpoint first, then a frontend view of it. Nothing in the
frontend reaches around the backend.

### What the frontend talks to today

| Call | Purpose |
| --- | --- |
| `GET {base}/health` | The header status indicator |
| `GET {base}/ready` | (planned) which capabilities exist |

The base URL comes from `NEXT_PUBLIC_API_BASE_URL`, with a documented fallback
to `http://localhost:8000`.

---

## 3. Request flow

### 3.1 A liveness check (exists)

1. The page mounts `<HealthIndicator>`.
2. The component calls `fetchBackendHealth(baseUrl)` once — no polling.
3. `src/lib/health.ts` requests `{base}/health` with a 5 s timeout.
4. Three outcomes:
   - **HTTP 200 with the expected payload** → state `online`, the header shows
     a green *Backend online* pill.
   - **HTTP error or an unexpected payload** → state `offline` with the reason.
   - **Network failure or timeout** → state `offline`, with a *Retry* button.
5. The state is rendered into an ARIA `role="status"` live region so screen
   readers announce the change.

The check never throws and never blocks the page: the frontend renders fully
whether or not the backend is running.

### 3.2 A long-running job (planned, Phase 2+)

```
POST /api/v1/jobs ──►  create job row (status=queued)  ──► 202 + job_id
                              │
                              ▼
                      background worker picks it up
                              │
      ingest ─► transcribe ─► AI highlights ─► render ─► captions
                              │
                              ▼
                   job row updated after each step
                              │
                              ▼
GET /api/v1/jobs/{id} ──►  status, progress, resulting clip paths
```

The HTTP request returns immediately with a job identifier. The browser polls
that job (or subscribes to updates added later) while the worker does the
heavy lifting.

---

## 4. Configuration and secret handling

Both applications read configuration from environment variables only. There is
no config file, no config class hierarchy and no secrets in source control.

### Backend

- All variables use the `CLIPFORGE_` prefix and are typed and validated by
  `pydantic-settings` in `backend/app/config.py`.
- `backend/.env` is read if present; it is git-ignored.
- The service name in `/health` is a **constant**, not a setting, because the
  frontend asserts on it.
- Secrets are `SecretStr`. They are masked in `repr()`, in `model_dump()` and
  in logs.
- The settings validator rejects a wildcard CORS origin, because wildcard
  origins are invalid together with credentials.

### Frontend

- Only `NEXT_PUBLIC_API_BASE_URL` is used. Next.js inlines `NEXT_PUBLIC_*`
  values into the browser bundle at build time, so **a secret must never live
  in one**.
- The value is read at build time: after changing `frontend/.env.local`, restart
  `npm run dev` or rebuild.

### How configuration will evolve

| Phase | New configuration |
| --- | --- |
| 2 | `CLIPFORGE_STORAGE_DIR` becomes the real media + SQLite root |
| 3 | Transcription engine selection and model size |
| 4 | `CLIPFORGE_LLM_*` credentials, base URL and model |
| 5 | FFmpeg binary path and per-clip encoding presets |
| 6 | Caption style, font and safe-area presets |
| 9 | Worker concurrency, timeouts and retry policy |

---

## 5. Planned transcription and video-processing pipeline

Planned, not implemented. The design intent is:

1. **Ingest** — copy or stream the source video into `CLIPFORGE_STORAGE_DIR`,
   recording its duration and resolution. YouTube URLs are fetched only for
   videos the user is authorised to access, subject to YouTube's terms.
2. **Audio extraction** — FFmpeg pulls out a 16 kHz mono WAV for the
   transcriber.
3. **Transcription** — a speech-to-text engine produces a timed transcript
   (word or segment level), stored as structured data.
4. **Highlight detection** — the transcript (and optionally the title and
   description) is sent to an external LLM, which returns candidate moments
   with a start time, an end time, a title and a reason.
5. **Clip rendering** — FFmpeg cuts each accepted moment into its own file.
6. **Vertical formatting** — the clip is scaled and padded to 1080×1920
   (9:16) with the subject kept in frame.
7. **Captions** — the transcript is turned into an SRT/ASS subtitle file and
   burned into the video, with timing matched to the clipped range.
8. **Export** — the finished clips are listed in the dashboard and downloadable.

Each step writes its result to disk and updates the job row, so a failed run
can be resumed or inspected rather than restarted from zero.

---

## 6. Planned SQLite and filesystem storage

### SQLite

Job metadata belongs in SQLite: a single local file, no server, no migration
tooling, easy to inspect and delete.

Planned tables (Phase 2+, exact shape settled when the phase starts):

| Table | Holds |
| --- | --- |
| `jobs` | id, source path or URL, status, progress, error, timestamps |
| `clips` | id, job id, start/end time, file path, title, dimensions |
| `transcripts` | job id, segments with start/end times and text |

SQLite is accessed only by the backend. The API never exposes a raw file path
to the browser; it exposes a download endpoint instead.

### Filesystem

```
data/                      <- CLIPFORGE_STORAGE_DIR
├── sources/               uploaded or downloaded source videos
├── audio/                 extracted audio for transcription
├── clips/                 rendered clips
├── subtitles/             generated .srt / .ass files
└── clipforge.db           SQLite database
```

The whole directory is git-ignored. Everything the user creates stays on their
machine and can be deleted by deleting the folder.

---

## 7. Background processing

Long-running media work **must not** run inside an HTTP request handler.

Reasons:

- Transcribing a one-hour video and rendering clips takes minutes. Holding an
  HTTP connection open for that time hits proxy, browser and server timeouts,
  and the browser cannot show progress.
- FFmpeg and speech-to-text engines are CPU-heavy. Running them in the request
  path would block the event loop and make the API unresponsive to health
  checks — the user would see "backend offline" precisely when it is busy.
- A crash mid-render would lose the work and leave the client with no status.

So the API accepts a job, records it, and hands it to a worker. The worker
updates the job row as it goes, and the frontend reads that row.

Planned worker shape (Phase 5+): a background task started with the FastAPI
lifespan, draining a queue of pending jobs. If the project later needs
persistence across restarts or multiple workers, the queue can move to a table
in the same SQLite file without changing the API contract.

---

## 8. Why media processing must not block HTTP requests

Restating the above as a rule, because it is the single most important
constraint on the backend design:

- HTTP handlers do I/O on the database and return. They never wait for a
  render.
- `/health` must answer in milliseconds even while five renders are in
  progress. That is why FFmpeg and the transcriber are never called from a
  route handler.
- The health contract is deliberately tiny and constant
  (`{"status":"ok","service":"clipforge-api"}`) so it can be polled cheaply and
  compared exactly.
- `/ready` reports which capabilities are actually implemented, so a client
  can tell "not implemented yet" from "not available right now". Every
  capability flag is `false` in Phase 1.

---

## 9. Current implementation map (Phase 1)

### Exists

| Piece | Where |
| --- | --- |
| Application factory, CORS, request logging, error envelope | `backend/app/main.py` |
| Typed, validated settings | `backend/app/config.py` |
| Logging setup | `backend/app/logging_config.py` |
| `/health`, `/ready` | `backend/app/routers/health.py` |
| `/api/v1` | `backend/app/routers/api.py` |
| Dev entry point | `backend/run.py` |
| Backend tests | `backend/tests/` |
| Frontend shell, pipeline preview, disabled video panel | `frontend/src/app`, `frontend/src/components` |
| Health-check logic and indicator | `frontend/src/lib/health.ts`, `frontend/src/components/HealthIndicator.tsx` |
| Frontend tests | `frontend/src/**/*.test.ts(x)` |

### Does not exist yet

Video ingestion, YouTube download, audio extraction, transcription, LLM
integration, FFmpeg rendering, vertical conversion, caption burn-in, SQLite
schema, background worker, job API, clip editor, export, authentication.

---

## 10. Security posture

- The API binds `127.0.0.1` by default and has no authentication. It is a
  local tool, not a service. Adding auth or public deployment infrastructure is
  out of scope for Phase 1 and is deliberately not attempted.
- CORS allows only the configured frontend origins; a wildcard is rejected at
  configuration load time.
- Request logging records method, path, status and duration. Bodies, headers
  and query strings are never logged, because they can carry credentials.
- Unhandled errors return a generic 500 without internal details.
- Secrets are never returned by an endpoint; there is a regression test for it.
- Future YouTube ingestion must respect the copyright of the source material,
  the permissions of the requesting account, and YouTube's terms of service.
