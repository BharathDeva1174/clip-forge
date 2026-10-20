# Architecture

This document describes how ClipForge is put together. It separates what
**exists today** (Phases 1 and 2) from what is **planned** for the remaining
phases, so nothing in the roadmap can be mistaken for working code.

---

## 1. The shape of the system

```
┌────────────────────────────────────────────┐
│  Browser                                    │
│  Next.js 16 (App Router)                    │
│  http://localhost:3000                       │
└──────────────────────┬─────────────────────┘
                       │  fetch /health, /api/v1/jobs, /api/v1/videos/*
                       │  (explicit-origin CORS, JSON)
                       ▼
┌────────────────────────────────────────────┐
│  FastAPI (uvicorn)  —  process 1            │
│  http://localhost:8000                       │
│                                            │
│  app/main.py     entry point                │
│  app/config.py   settings                   │
│  app/routers/    videos, jobs, health, api  │
│  app/uploads.py  streaming multipart        │
└──────────────────────┬─────────────────────┘
                       │  read + write (short transactions only)
                       ▼
┌────────────────────────────────────────────┐
│  SQLite  —  backend/data/database/          │
│  clipforge.db                                │
│                                            │
│  jobs: id, status, progress, stage, ...     │
└──────────────────────▲─────────────────────┘
                       │  claim / update (short transactions only)
┌──────────────────────┴─────────────────────┐
│  Worker  —  backend/worker.py  —  process 2 │
│                                            │
│  poll → claim → validate → download →       │
│  inspect → update the job                   │
└──────────────────────┬─────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────┐
│  Local storage — backend/data/              │
│  uploads/ sources/ temp/                    │
│                                            │
│  FFprobe  (media validation)                │
│  yt-dlp    (authorised YouTube acquisition) │
└────────────────────────────────────────────┘
```

Three moving parts, one machine: the browser, the API and the worker. The
frontend is the only thing the user touches; the API owns every privileged
operation; the worker owns every slow one.

---

## 2. Frontend / backend boundary

**The rule: the browser never talks to a provider, a database or FFmpeg
directly. It only talks to the local API.**

| Concern | Owner | Why |
| --- | --- | --- |
| Talking to the LLM provider | Backend | The API key must not reach the browser |
| Reading/writing SQLite | Backend | Only one writer; keeps the schema in one place |
| Reading/writing media files | Backend | Filesystem paths and FFmpeg stay off the client |
| Downloading from YouTube | Worker | It is slow and it needs process control |
| Rendering, layout, status display | Frontend | React owns the UI |

### What the frontend talks to today

| Call | Purpose |
| --- | --- |
| `GET {base}/health` | The header status indicator |
| `POST {base}/api/v1/videos/upload` | Queue a local video (multipart) |
| `POST {base}/api/v1/videos/youtube` | Queue an authorised YouTube video (JSON) |
| `GET {base}/api/v1/jobs` | The job list, polled while a job is running |
| `GET {base}/api/v1/jobs/{id}` | One job |

The base URL comes from `NEXT_PUBLIC_API_BASE_URL`, with a documented fallback
to `http://localhost:8000`.

---

## 3. Request flow

### 3.1 A liveness check

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

### 3.2 Uploading a video

```
browser                          API                        disk
   │  POST /api/v1/videos/upload │                            │
   │  multipart/form-data         │                            │
   │─────────────────────────────►│ 1. Content-Length check   │
   │                             │    (> limit → 413, body    │
   │                             │     never read)            │
   │                             │ 2. stream the part to     │
   │                             │    uploads/<uuid>.mp4      │
   │                             │    counting bytes; abort  │
   │                             │    at the limit            │
   │                             │ 3. extension allowlist    │
   │                             │    (→ 415)                 │
   │                             │ 4. FFprobe validation     │
   │                             │    (→ 415)                 │
   │                             │ 5. move to sources/<uuid> │
   │                             │ 6. INSERT job (queued)    │
   │  202 { job }  ◄─────────────│                            │
```

The job row is written **after** the file is complete and validated, so a
queued job always refers to a file that exists. If anything fails, the
partial file is deleted and no job is created.

The upload is parsed by `app/uploads.py`, not by Starlette's own form parser.
Starlette spools the whole file to a temporary file *before* the endpoint runs
and has no limit on file part size, which means an oversized upload would be
written to disk in full before the application could reject it. Driving
`python-multipart`'s parser from the request stream keeps the limit enforced
while the body is arriving and writes the file straight into the application
storage directory.

### 3.3 Submitting a YouTube URL

```
browser                          API
   │  POST /api/v1/videos/youtube │  1. authorised == true?   (→ 400)
   │  { url, authorized }         │  2. strict URL validation │
   │─────────────────────────────►│     (→ 400)               │
   │                             │  3. acquisition mode on?  │
   │                             │     (→ 503)               │
   │                             │  4. INSERT job (queued)   │
   │  202 { job }  ◄─────────────│
```

The URL is validated by `app/youtube.py`, which never touches the network.
Only a **canonical** URL (`https://www.youtube.com/watch?v=<id>`) is stored,
and only that canonical URL is ever handed to the downloader.

### 3.4 The worker processing a job

```
worker                          SQLite                     disk
   │  poll every N seconds       │                          │
   │  BEGIN IMMEDIATE            │                          │
   │  SELECT oldest queued ─────►│                          │
   │  UPDATE status=validating   │                          │
   │  (lease, worker_id, attempt)│                          │
   │  COMMIT                     │                          │
   │                             │                          │
   │  upload  → FFprobe inspect ───────────────────────────►│
   │  youtube → yt-dlp download ────────────────────────────►│
   │             → FFprobe inspect                            │
   │  UPDATE status=completed, progress=100                 │
```

No transaction is held open across file I/O, media probing or a download.

### 3.5 The frontend following along

`JobList` fetches `GET /api/v1/jobs` on mount and then polls every 3 seconds
**only while at least one job is non-terminal**. When every job reaches a
terminal status the interval is cleared. A manual *Refresh* button is always
available, and a form that queues a job asks the list to refresh
immediately.

---

## 4. The job model

### 4.1 Schema

The database is created and upgraded by a versioned migration mechanism in
`app/db.py`. `schema_migrations` records which versions have been applied;
`ensure_schema()` applies anything pending inside a transaction, and the
common path does not even take a write lock.

```sql
CREATE TABLE jobs (
    id                      TEXT PRIMARY KEY,       -- uuid4 hex
    source_type             TEXT NOT NULL,          -- 'upload' | 'youtube'
    source_reference        TEXT NOT NULL,          -- display name or canonical URL
    source_path             TEXT,                   -- relative to the storage root
    status                  TEXT NOT NULL,          -- see below
    progress                INTEGER NOT NULL,       -- 0..100
    stage                   TEXT NOT NULL,
    created_at              TEXT NOT NULL,          -- ISO 8601 UTC
    updated_at              TEXT NOT NULL,
    started_at              TEXT,
    completed_at            TEXT,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    error_code              TEXT,                   -- stable machine-readable code
    error_message           TEXT,                   -- sanitised, user-facing
    worker_id               TEXT,
    lease_expires_at        TEXT,
    original_filename       TEXT,
    file_size_bytes         INTEGER,
    video_duration_seconds  REAL,
    video_width             INTEGER,
    video_height            INTEGER,
    video_codec             TEXT,
    video_format            TEXT
);
```

`CHECK` constraints enforce the status set, the stage set, the source type,
`progress BETWEEN 0 AND 100` and a non-negative attempt count.

### 4.2 Statuses and stages

| Status | Stage | Meaning |
| --- | --- | --- |
| `queued` | `queued` | Waiting for a worker |
| `validating` | `validating` | A worker is checking the source |
| `downloading` | `downloading` | A worker is fetching an authorised YouTube video |
| `inspecting` | `inspecting` | A worker is reading the media metadata |
| `completed` | `ready` | The source is stored and validated |
| `failed` | `failed` | The job could not be completed |
| `cancelled` | `cancelled` | The job was cancelled (no API reaches this yet) |

`completed` means **ingestion and media inspection finished**. It does not
mean clips exist; that is Phase 5.

### 4.3 Transitions

Transitions are declared once, in `app/jobs.py`, and enforced by
`assert_transition()` on every write:

```
queued      → validating | downloading | failed | cancelled
validating  → downloading | inspecting  | failed | cancelled
downloading → inspecting  | failed | cancelled
inspecting  → completed   | failed | cancelled
completed   → (nothing)
failed      → (nothing)
cancelled   → (nothing)
```

A terminal job never moves on its own. Moving one out is an **explicit
recovery operation** (`release_for_retry` in `app/db.py`), which is only used
by the worker for a retryable failure or an expired lease.

### 4.4 Progress

Progress is a stage-derived number, not an invented percentage:

| Stage | Progress |
| --- | --- |
| queued | 0 |
| validating | 10 |
| downloading | 30 |
| inspecting | 70 |
| ready | 100 |

`update_job` stores `max(current, new)`, so progress never goes backwards,
and the database rejects anything outside 0–100.

### 4.5 Timestamps

All timestamps are UTC, serialised as ISO 8601 with an explicit offset
(`2026-10-20T13:45:01.123456+00:00`). The same format is used in SQLite, so
the strings sort correctly and no conversion is needed in the API.

---

## 5. Atomic claiming

Two workers must never own the same job. The claim uses one write
transaction:

```sql
BEGIN IMMEDIATE;                    -- takes the write lock immediately
SELECT id FROM jobs
 WHERE status = 'queued'
 ORDER BY created_at ASC, id ASC
 LIMIT 1;
UPDATE jobs
   SET status = 'validating', stage = 'validating',
       progress = MAX(progress, 10),
       worker_id = ?, lease_expires_at = ?,
       started_at = COALESCE(started_at, ?),
       updated_at = ?, attempt_count = attempt_count + 1,
       error_code = NULL, error_message = NULL
 WHERE id = ? AND status = 'queued';   -- re-checked under the lock
COMMIT;
```

`BEGIN IMMEDIATE` acquires the write lock *before* the candidate row is read,
so a second worker waits and then sees the committed state. The `AND
status = 'queued'` guard makes the update safe even if the lock were
acquired later. `busy_timeout` is set from
`CLIPFORGE_DATABASE_BUSY_TIMEOUT_SECONDS`.

WAL mode is on by default so a reader (the API listing jobs) never blocks the
writer (the worker). WAL requires a local filesystem; set
`CLIPFORGE_DATABASE_WAL_MODE=false` if the database is ever placed on a
network share.

---

## 6. Leases and crash recovery

A claimed job carries a **lease**: `lease_expires_at = now + lease_seconds`.
The worker renews the lease while a download runs.

If a worker dies, the lease expires. The next worker — and the worker itself
at startup and every few minutes — runs `recover_expired_leases()`, which
finds jobs in an active status whose lease has passed and either:

- **requeues** them (`status = queued`, worker and lease cleared, error code
  `lease_expired`), so another worker picks them up; or
- **fails** them with `max_attempts_exceeded` when `attempt_count` has
  reached `CLIPFORGE_WORKER_MAX_ATTEMPTS`.

A crashed worker therefore cannot leave a job stuck in `validating`,
`downloading` or `inspecting`.

### Retry policy

| Error | Retryable |
| --- | --- |
| `acquisition_failed` | yes, up to `worker_max_attempts` |
| `acquisition_timeout` | yes |
| `media_tool_unavailable` | yes |
| `invalid_media` | **no** — a file that is not a video will not become one |
| `unsupported_codec` | **no** |
| `media_too_long` | **no** |
| `media_dimensions_exceeded` | **no** |

A retryable failure that exhausts its attempts keeps the underlying error code
and adds *"(gave up after N attempts)"* to the message.

### Delivery semantics

The guarantee is **at-least-once, not exactly-once**. A worker may die after
doing the work but before recording the result, and the job will be redone.
Each step is written to be idempotent: a source that is already stored is
re-inspected rather than re-downloaded, and a job record is never duplicated.

---

## 7. Storage boundaries

```
backend/data/
├── uploads/    in-flight uploads; deleted on every error path
├── sources/    accepted source videos, named with a server-generated id
├── temp/       scratch space used by the worker, removed after each job
└── database/   clipforge.db
```

- The database stores **relative** paths.
- `Storage.resolve()` rejects absolute paths, `..` segments and Windows drive
  specifications, resolves the path, and then checks containment against the
  storage root — which also rejects symlinks that point outside it.
- The stored filename is `uuid4().hex` plus an extension from the allowlist.
  The client's filename is never used as a path; it is sanitised and kept only
  so the user can recognise their own file.
- A source file is **not** deleted when processing fails. Delete
  `backend/data/sources` by hand to reclaim the space.
- The whole `data` directory is git-ignored.

---

## 8. Media validation

FFprobe is the only thing trusted to decide whether a file is a usable video.
Extensions and client-supplied MIME types are never evidence.

```python
subprocess.run(
    [ffprobe_path, "-v", "error", "-print_format", "json",
     "-show_format", "-show_streams", str(path)],
    capture_output=True, timeout=timeout_seconds, check=False,
)
```

- An argument list, never a shell.
- FFprobe's output is untrusted: every value is type-checked and range-checked
  before it is used. FFprobe reports some values as JSON numbers and others as
  decimal strings (`"1.000000"`); both are accepted, anything else is not.
- The policy — maximum duration, maximum dimension, codec allowlist — comes
  from the configuration, not from hard-coded numbers in several places.
- A missing FFprobe is reported as `media_tool_unavailable`, not as a
  successful validation.

---

## 9. YouTube acquisition

### 9.1 Validation

`app/youtube.py` is a pure function with no network access. It accepts only:

- `http` or `https`;
- no credentials in the URL;
- only the default ports;
- an **exact** hostname match against `CLIPFORGE_YOUTUBE_ALLOWED_HOSTS`, so
  `youtube.com.attacker.example` and `notyoutube.com` are rejected;
- a well-known path shape (`/watch`, `/embed/`, `/shorts/`, `/live/`, or
  `youtu.be/<id>`);
- an 11-character `[A-Za-z0-9_-]` video id.

The result is a canonical `https://www.youtube.com/watch?v=<id>`.

### 9.2 The adapter

Acquisition sits behind a narrow interface (`SourceAcquirer`) so the rest of
the application does not depend on a particular downloader. Two
implementations exist:

- `DisabledAcquirer` — refuses everything and points the user at local
  uploads. It is used when `CLIPFORGE_YOUTUBE_ACQUISITION_MODE=disabled` or
  when yt-dlp is not installed.
- `YtDlpAcquirer` — runs `python -m yt_dlp` with an argument list.

The downloader is given a validated canonical URL and nothing else: no
cookies, no username, no playlist, no secondary resources. Downloads are
bounded by `--max-filesize` and by a timeout, and the child process is
terminated when the worker shuts down.

### 9.3 Residual risk

The initial URL is fully under our control. **The redirects yt-dlp follows
internally are not.** YouTube serves media from `googlevideo.com` hosts that
are chosen by YouTube, and yt-dlp resolves and follows those itself. This is
inherent to how YouTube works, and it is why the acquisition mode is
configurable and off by default in any installation that does not need it.

The trust boundary is documented rather than hidden: the user-supplied part of
the URL can never point anywhere but YouTube, and the rest is YouTube's own
infrastructure.

### 9.4 Authorisation

The API requires `authorized: true` in the submission, which the frontend
presents as an explicit checkbox: *"I own this video or have permission to
download and reuse it."* The downloader runs without credentials, so it can
only reach publicly available material. No mechanism exists to bypass a
login, a DRM, an age restriction, a private-video permission or a rate
limit, and none is planned.

---

## 10. Why media processing must not block HTTP requests

- FFprobe reads container metadata and does not decode frames, so the
  validation in the upload handler is bounded and fast. The *download* is not,
  and it never happens in a request handler.
- Holding an HTTP request open for a multi-minute download would hit proxy,
  browser and server timeouts, and the browser could not show progress.
- FFmpeg and the downloader are CPU- and network-heavy. Running them in the
  request path would block the event loop and make the API unresponsive to
  health checks — the user would see "backend offline" precisely when it is
  busy.
- A crash mid-download would lose the work and leave the client with no
  status.

So the API accepts a job, records it, and hands it to the worker. The worker
updates the job row as it goes, and the frontend reads that row.

---

## 11. Current implementation map

### Exists

| Piece | Where |
| --- | --- |
| Application factory, CORS, body size limit, error envelope | `backend/app/main.py` |
| Typed, validated settings | `backend/app/config.py` |
| Job states, stages, transitions | `backend/app/jobs.py` |
| Schema, migrations, job repository, claiming, recovery | `backend/app/db.py` |
| Storage layout and path containment | `backend/app/storage.py` |
| FFprobe inspection and media policy | `backend/app/media.py` |
| YouTube URL validation | `backend/app/youtube.py` |
| Downloader adapter | `backend/app/acquisition.py` |
| Streaming multipart upload | `backend/app/uploads.py` |
| Upload and YouTube endpoints | `backend/app/routers/videos.py` |
| Job list and detail endpoints | `backend/app/routers/jobs.py` |
| Worker | `backend/app/worker.py`, `backend/worker.py` |
| API client | `frontend/src/lib/api.ts` |
| Upload and YouTube forms | `frontend/src/components/VideoSourcePanel.tsx` |
| Job list with polling | `frontend/src/components/JobList.tsx` |
| Health indicator | `frontend/src/components/HealthIndicator.tsx` |

### Does not exist yet

Transcription, LLM integration, highlight selection, FFmpeg rendering,
vertical conversion, caption burn-in, clip storage, a clip editor, export,
authentication, and any way to cancel a job.

---

## 12. Security posture

- The API binds `127.0.0.1` by default and has no authentication. It is a
  local tool, not a service.
- CORS allows only the configured origins; a wildcard is rejected when the
  settings load.
- The upload limit is enforced while the body streams, and a
  `Content-Length` above the limit is refused before the body is read at all.
- Uploaded files are validated by content, never by extension or MIME type.
- The client filename is never used as a path; every stored path is checked
  for containment.
- Request logging records method, path, status and duration. Bodies, headers
  and query strings are never logged.
- Unhandled errors return a generic 500 without internal details.
- Secrets are never returned by an endpoint; there is a regression test for it.
- FFprobe and yt-dlp are always started with an argument list, never a shell.
- Future YouTube ingestion must respect the copyright of the source material,
  the permissions of the requesting account, and YouTube's terms of service.
