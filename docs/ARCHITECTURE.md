# Architecture

This document describes how ClipForge is put together — what **exists today**.
All ten roadmap phases are implemented, so nothing here is a plan; where a
behaviour is bounded or deliberately not attempted, it is said as plainly.
The per-phase evidence lives in the `PHASE_*_REPORT.md` files at the
repository root and the numbered decisions in
[DECISIONS.md](DECISIONS.md).

---

## 1. The shape of the system

```
┌────────────────────────────────────────────┐
│  Browser                                    │
│  Next.js 16 (App Router)                    │
│  http://localhost:3000                       │
└──────────────────────┬─────────────────────┘
                       │  fetch /health, /api/v1/jobs, /api/v1/jobs/*/transcript
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
│                                            │
│  Never runs FFmpeg. Never loads a model.    │
└──────────────────────┬─────────────────────┘
                       │  read + write (short transactions only)
                       ▼
┌────────────────────────────────────────────┐
│  SQLite  —  backend/data/database/          │
│  clipforge.db                                │
│                                            │
│  jobs                    source video jobs │
│  transcripts             one per job       │
│  transcript_segments     ordered timestamps│
│  integrations            provider configs  │
│  highlight_analyses      per job, with lease│
│  highlight_candidates    moments           │
│  clips                   rendered clips    │
│  clip_formats            9:16 artifacts    │
│  clip_caption_edits      per-segment text  │
└──────────────────────▲─────────────────────┘
                       │  claim / update (short transactions only)
┌──────────────────────┴─────────────────────┐
│  Worker  —  backend/worker.py  —  process 2 │
│                                            │
│  poll → claim → validate → download →       │
│  inspect → extract audio → transcribe →     │
│  store the transcript → update the job      │
│                                             │
│  the same loop also claims highlight        │
│  analyses, clips and formatted clips, each  │
│  with its own lease, tick and cancellation  │
│  check                                      │
└──────────────────────┬─────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────┐
│  Local storage — backend/data/              │
│  uploads/ sources/ temp/ models/            │
│                                            │
│  FFprobe    (media validation)               │
│  FFmpeg     (audio extraction)               │
│  yt-dlp     (authorised YouTube acquisition) │
│  Whisper    (local speech-to-text)           │
└────────────────────────────────────────────┘
```

Three moving parts, one machine: the browser, the API and the worker. The
frontend is the only thing the user touches; the API owns every privileged
operation; the worker owns every slow one.

The API never runs FFmpeg and never loads a speech model. That is the whole
point of the worker: a request is never blocked by the length of a recording,
and the model is loaded once in the worker rather than once per request.

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
| `GET {base}/ready` | Readiness: capabilities, model cache, worker heartbeat |
| `GET {base}/api/v1` | API metadata |
| `POST {base}/api/v1/videos/upload` | Queue a local video (multipart) |
| `POST {base}/api/v1/videos/youtube` | Queue an authorised YouTube video (JSON) |
| `GET {base}/api/v1/jobs` | The job list, polled while a job is running |
| `GET {base}/api/v1/jobs/{id}` | One job |
| `GET {base}/api/v1/jobs/{id}/transcript` | The stored transcript and its segments |
| `POST {base}/api/v1/jobs/{id}/cancel` | Cancel a job and stop its work |
| `POST {base}/api/v1/jobs/{id}/highlights` | Queue a highlight analysis |
| `GET {base}/api/v1/jobs/{id}/highlights` | The analysis and its candidates |
| `PATCH {base}/api/v1/jobs/{id}/highlights/{candidate_id}` | Accept or reject a candidate |
| `POST {base}/api/v1/jobs/{id}/clips` | Render a clip from an accepted candidate |
| `GET {base}/api/v1/jobs/{id}/clips` | The clips of a job |
| `POST {base}/api/v1/jobs/{id}/exports` | Build a ZIP of chosen finished clips |
| `GET {base}/api/v1/clips/{id}` | One clip: state and verified metadata |
| `PATCH {base}/api/v1/clips/{id}` | Rename a clip; the file is not touched |
| `POST {base}/api/v1/clips/{id}/revisions` | Trim a clip into a new revision |
| `PUT {base}/api/v1/clips/{id}/captions` | Store per-segment caption overrides |
| `POST {base}/api/v1/clips/{id}/retry` | Retry a failed clip |
| `GET {base}/api/v1/clips/{id}/media` | Play or download a finished clip |
| `GET {base}/api/v1/clips/{id}/formats` | The formatted artifacts of a clip |
| `POST {base}/api/v1/clips/{id}/formats` | Request a portrait, captioned artifact |
| `GET {base}/api/v1/formats/{id}` | One formatted artifact |
| `POST {base}/api/v1/formats/{id}/retry` | Retry a failed format |
| `GET {base}/api/v1/formats/{id}/media` | Play or download a finished formatted clip |
| `GET {base}/api/v1/fonts` | The fonts installed on this machine |
| `GET {base}/api/v1/integrations` | The saved provider configurations |
| `POST {base}/api/v1/integrations` | Save a provider configuration |
| `GET {base}/api/v1/integrations/presets` | The provider preset catalog |
| `GET {base}/api/v1/integrations/{id}` | One provider configuration |
| `PATCH {base}/api/v1/integrations/{id}` | Edit a provider configuration |
| `DELETE {base}/api/v1/integrations/{id}` | Delete a provider configuration |
| `POST {base}/api/v1/integrations/{id}/key` | Store its key in the credential store |
| `DELETE {base}/api/v1/integrations/{id}/key` | Forget its key |
| `POST {base}/api/v1/integrations/{id}/select` | Make it the default for its role |
| `POST {base}/api/v1/integrations/{id}/test` | Test the connection |
| `GET {base}/api/v1/integrations/{id}/models` | Ask the provider for its model list |

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
   │                             │                          │
   │  UPDATE status=extracting_audio                         │
   │  FFmpeg → 16 kHz mono PCM WAV ─────────────────────────►│
   │                             │                          │
   │  UPDATE status=transcribing                             │
   │  Whisper (local model) → segments                      │
   │                             │                          │
   │  BEGIN IMMEDIATE                                       │
   │  INSERT transcript + segments ─────────────────────────►│
   │  COMMIT                                               │
   │                             │                          │
   │  UPDATE status=completed, progress=100                 │
   │  delete the extracted audio                            │
```

No transaction is held open across file I/O, media probing, a download, an
FFmpeg run or the model inference. The transcript is committed in its own
short transaction, and the job is only marked `completed` **after** that
commit, so a client that sees a completed job can always read its transcript.

### 3.5 The frontend following along

`JobList` fetches `GET /api/v1/jobs` on mount and then polls every 3 seconds
**only while at least one job is non-terminal**. When every job reaches a
terminal status the interval is cleared. A manual *Refresh* button is always
available, and a form that queues a job asks the list to refresh
immediately.

`TranscriptPanel` follows the same rule: it reads
`GET /api/v1/jobs/{id}/transcript` when the job record changes, and polls
every 3 seconds while the job is still running, so the transcript appears as
soon as it has been stored.

The same rule — one interval, only while the resource can change, cleared when
the view is left — covers the rest of the dashboard: `JobWorkflow` and
`HighlightsPanel` at 3 seconds, and `ClipPanel` and `FormatPanel` at 2.5
seconds, because a render is the shortest thing a user waits for. Every one of
them goes through `usePolling()` in `frontend/src/lib/use-polling.ts`; no view
invents its own timer.

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

Phase 3 adds two tables:

```sql
CREATE TABLE transcripts (
    id                  TEXT PRIMARY KEY,       -- uuid4 hex
    job_id              TEXT NOT NULL UNIQUE    -- one transcript per job
                        REFERENCES jobs (id) ON DELETE CASCADE,
    language            TEXT,                   -- detected language
    model_name          TEXT NOT NULL,          -- the model that produced it
    audio_sample_rate   INTEGER NOT NULL,       -- 16000
    audio_channels      INTEGER NOT NULL,       -- 1
    duration_seconds    REAL,                   -- the audio that was transcribed
    segment_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE TABLE transcript_segments (
    id                      TEXT PRIMARY KEY,   -- uuid4 hex
    transcript_id           TEXT NOT NULL
                            REFERENCES transcripts (id) ON DELETE CASCADE,
    segment_index           INTEGER NOT NULL,   -- 0-based, contiguous
    start_seconds           REAL NOT NULL,
    end_seconds             REAL NOT NULL,
    text                    TEXT NOT NULL,
    average_log_probability REAL,               -- engine log-probability
    no_speech_probability   REAL,               -- engine probability
    UNIQUE (transcript_id, segment_index)
);
```

The unique constraint on `job_id` is what makes a retry **replace** a
transcript instead of adding a second one, and the unique constraint on
`(transcript_id, segment_index)` is what makes the order of a transcript
unambiguous.

The `jobs` table itself is rebuilt by the Phase 3 migration, because SQLite
cannot widen a `CHECK` constraint in place. The rebuild copies every row into
a new table, drops the old one and renames the new one, all inside one
transaction: either every job survives or the database is left exactly as it
was.

### 4.2 Statuses and stages

| Status | Stage | Meaning |
| --- | --- | --- |
| `queued` | `queued` | Waiting for a worker |
| `validating` | `validating` | A worker is checking the source |
| `downloading` | `downloading` | A worker is fetching an authorised YouTube video |
| `inspecting` | `inspecting` | A worker is reading the media metadata |
| `extracting_audio` | `extracting_audio` | A worker is pulling the audio track out with FFmpeg |
| `transcribing` | `transcribing` | A worker is transcribing the audio with the local model |
| `completed` | `ready` | The source is stored, validated and transcribed |
| `failed` | `failed` | The job could not be completed |
| `cancelled` | `cancelled` | The job was cancelled, its work stopped and its queued children failed |

`completed` means **the source was stored, its audio was extracted and its
transcript was saved**. It does not mean clips exist; that is Phase 5.

Jobs that were finished before transcripts existed are still `completed`.
They have no transcript, and the API says so through the `has_transcript`
flag rather than letting a client infer it from the status.

### 4.3 Transitions

Transitions are declared once, in `app/jobs.py`, and enforced by
`assert_transition()` on every write:

```
queued            → validating | downloading | failed | cancelled
validating        → downloading | inspecting  | failed | cancelled
downloading       → inspecting  | failed | cancelled
inspecting        → extracting_audio | failed | cancelled
extracting_audio  → transcribing | failed | cancelled
transcribing      → completed   | failed | cancelled
completed         → (nothing)
failed            → (nothing)
cancelled         → (nothing)
```

A terminal job never moves on its own. Moving one out is an **explicit
recovery operation** (`release_for_retry` in `app/db.py`), which is only used
by the worker for a retryable failure or an expired lease.

Note that `inspecting` can no longer go straight to `completed`. Since
Phase 3 a job has to pass through the two transcription stages, so
`completed` always implies that a transcript was committed first. The state
machine enforces the guarantee rather than leaving it to the worker's
discipline.

### 4.4 Progress

Progress is a stage-derived number, not an invented percentage:

| Stage | Progress |
| --- | --- |
| queued | 0 |
| validating | 10 |
| downloading | 30 |
| inspecting | 70 |
| extracting_audio | 80 |
| transcribing | 85 |
| ready | 100 |

`update_job` stores `max(current, new)`, so progress never goes backwards,
and the database rejects anything outside 0–100.

During transcription the worker refines the progress from how far the decoder
has read into the audio, keeping it inside the 85–99 band. That number is an
**estimate**, not a percentage: the library does not report an exact one, so
the API and the UI label it as approximate rather than presenting it as fact.

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

Highlight analyses, clips and formatted clips run under the same lease idea as
jobs, but the lease is kept alive **while** the slow work is in flight, not
only between steps:

- An analysis renews its lease from a background thread, so a renewal is not
  gated on the provider answering. A reasoning model can take minutes per
  window and the lease no longer expires underneath it. A renewal that has
  **never** succeeded means this worker does not own the row and it must not
  publish; a renewal that fails transiently (SQLite under contention) is
  retried, once one has succeeded, until the original lease would have run out.
- Each window is retried within `CLIPFORGE_AI_MAX_REQUEST_ATTEMPTS`, with
  exponential backoff and jitter; a window the provider rejects for its
  context limit is split and retried; and the whole analysis has to finish
  inside `CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS`. That deadline, not the
  lease, is what bounds an analysis now.
- A clip or a format render runs a tick before the render, every quarter of a
  second during it, and before verification. The tick renews the artifact's
  lease — renders had none at all, so a render longer than the lease used to be
  stealable mid-flight — and raises `WorkCancelled` when the parent job has
  been cancelled, which the renderer answers by terminating and reaping the
  FFmpeg child.

A `lease_expired` on an analysis therefore no longer means "the provider was
slow". It means the lease genuinely could not be renewed: the worker died or
was restarted, or another worker took the row over.

---

## 7. Storage boundaries

```
backend/data/
├── uploads/    in-flight uploads; deleted on every error path
├── sources/    accepted source videos, named with a server-generated id
├── temp/       scratch space used by the worker, removed after each job
├── models/     the local Whisper model cache (downloaded once, then offline)
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
- The extracted audio lives in a per-job directory under `temp/` and is
  **deleted** as soon as the transcript is stored. It is a large, temporary
  artefact; the transcript is the durable result. Set
  `CLIPFORGE_AUDIO_TEMP_RETENTION=keep` to keep it for inspection.
- The model cache is the only large download. It is written by the model
  library, not by ClipForge, and can be deleted by hand at any time.
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

### 8.1 Audio extraction

The same principle applies to the audio: **a zero exit code is not trusted**.
`app/audio.py` runs FFmpeg to produce 16 kHz mono PCM WAV, and then probes the
result again. The file is only accepted if it really is mono, really is
16 kHz, really is PCM and really has a duration. Anything else is an error.

- The output file name is generated inside the service, so an extraction can
  never overwrite a source video or another job's artefact.
- FFmpeg's stderr goes to a temporary file rather than a pipe: a pipe has a
  fixed buffer, and a child that fills it while the parent is not reading
  would block forever.
- Only a small, truncated part of FFmpeg's stderr is kept, and it never
  reaches an API response.
- The child process has a timeout and can be terminated, so a worker that is
  asked to shut down stops FFmpeg instead of waiting for it.
- The source is checked to be inside the storage root before it is read.

### 8.2 Transcript validation

The engine's output is untrusted, exactly like FFprobe's. `app/transcripts.py`
owns the rules, and it performs no I/O, so the same rules apply to any engine.

| Rule | Why |
| --- | --- |
| The start and the end are finite numbers | A `NaN` or an infinity is not a time |
| The start is not negative | There is nothing before the audio |
| The end is not before the start | A segment cannot run backwards |
| The end is within the audio | Speech cannot happen after the audio ends |
| The text is a string | The engine must not decide what a segment is |
| Only whitespace is changed | Punctuation, accents and every other character are the user's content and are preserved verbatim |
| The order is deterministic | Two runs over the same audio produce the same transcript |

Two real-world cases are handled explicitly:

- **Whisper's timestamps overshoot.** Whisper's token timestamps are quantised
  to 20 ms and are produced from a 30-second sliding window, so the end of the
  last segment regularly points *past* the end of the audio — by up to a whole
  window. Such a timestamp is pulled back to the end of the audio rather than
  rejected, because it is a known artefact of the model and not a broken
  value. A timestamp further out than that allowance is rejected.
- **A segment can lie entirely outside the audio.** That is what a
  hallucination over trailing silence looks like. There is no honest timestamp
  for it, so it is dropped instead of being stored with an invented one.

A segment whose text is empty after normalisation carries no speech and is
dropped too.

The rules live in one module and are applied before anything is written, so a
transcript that claims to be complete is complete.

### 8.3 Where the audio duration comes from

The duration the timestamps are normalised against is the duration of the
**extracted WAV**, measured by FFprobe when the file was produced
(`app/audio.py` probes the result and refuses it if it is not mono, 16 kHz,
PCM and non-empty). The worker hands that measurement to the engine, and the
engine uses it in preference to anything the model reports.

This matters for two reasons:

- The source video's nominal duration is *not* the audio's duration. A video
  whose audio track is shorter, or which was trimmed, would otherwise
  produce a transcript that claims speech after the audio ended.
- Without a known duration no timestamp can be checked against the audio at
  all, and the "a segment that lies entirely outside the audio is dropped"
  rule above would silently not apply. The duration is therefore always
  known, never optional.

### 8.4 The inference process boundary

The speech model does not run inside the worker. It runs in a **child
process** of the worker, started by `app/transcription_process.py` and
driven over a pipe by a small line-delimited JSON protocol
(`app/transcription_host.py`).

```
worker process                      inference process
────────────────                    ──────────────────
engine.transcribe()  ──configure──►  load the model
                   ──transcribe──►  decode the audio
              ◄──segment/progress──  report what it found
                   ◄──done────────   finished
                   ──kill─────────►  terminate + reap
```

Why a process and not a thread:

- The model decodes audio in C++ and releases the GIL. A Python-level
  interruption cannot reach it, so a stalled inference in a thread cannot be
  stopped at all.
- A child process can be stopped. That is what makes
  `CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS` a *hard* timeout rather than a
  deadline that is checked between decoded segments.

The guarantees:

| Property | How |
| --- | --- |
| The timeout is enforced | The parent owns the deadline; on expiry the child is terminated, then killed, then reaped |
| The worker survives | The child is a separate process; killing it does not touch the worker |
| No orphans | The parent reaps the child; the child also exits when its pipe closes, so a worker that is killed does not leave it running |
| The model is loaded once | The child is kept between jobs and reused |
| Nothing leaks into the arguments | The configuration and the audio path travel over the pipe, never on a command line |
| Bounded IPC | The message queue is bounded, and the child's standard error goes to a temporary file of which only a truncated tail is logged |
| Nothing leaves the machine | The child refuses to open a socket at all |

The last point is enforced in the child: `socket.connect` and
`socket.create_connection` are replaced with a function that raises, so an
accidental network call fails loudly instead of quietly sending data out.

`CLIPFORGE_TRANSCRIPTION_ISOLATION=inline` selects the older behaviour, where
the model runs inside the worker. Its timeout is a *soft* one — checked
between decoded segments — and it is documented as such.

---

## 9. The AI integrations

### 9.1 The shape of it

```
Integrations page                  API                       worker
──────────────────                  ───                       ──────
save a configuration    ──POST──▶  integrations table
store the key           ─────────▶  OS credential store
select a default        ──POST──▶  one enabled default per role

start an analysis       ──POST──▶  highlight_analyses (pending)
                                          │
                                          └──▶  worker claims it
                                                       │
                          transcript segments ◀────────┤
                                                       │
                          prompt + window  ──────────▶  provider
                                                       │
                          validate + merge  ◀──────────┤
                                                       │
                          candidates      ◀───────────┘
read the result         ──GET───▶   highlight_candidates
```

Six modules, each with one job:

| Module | What it owns |
| --- | --- |
| `app/integrations.py` | The domain model: the protocols, the model capabilities, the pricing and connection statuses. No I/O. |
| `app/providers.py` | The preset catalog. Stable defaults only: no model list, no prices. |
| `app/protocols.py` | The protocol adapters. One per API, with its own endpoint, authentication, request shape and answer parsing. |
| `app/ai_client.py` | The transport. The only place that opens a socket. |
| `app/credentials.py` | The operating system's credential store. |
| `app/highlights.py` | The candidate rules. No I/O. |
| `app/highlight_prompt.py` | The versioned prompt and the windowing. |
| `app/highlight_service.py` | The orchestration the worker calls. |

The highlight business logic knows about *segments*, *moments* and
*providers*. It never knows about bearer tokens, `x-api-key` headers,
`choices[0].message.content` or `content[0].text`. That is what makes a
second provider a configuration change rather than a code change.

### 9.2 The protocols

| Protocol | Endpoint | Authentication | Reasoning | Structured output |
| --- | --- | --- | --- | --- |
| `openai_chat_completions` | `{base}/chat/completions` | `Authorization: Bearer` | `reasoning_effort` | `json_object` / `json_schema` |
| `openai_responses` | `{base}/responses` | `Authorization: Bearer` | `reasoning: {effort}` | `json_schema` |
| `anthropic_messages` | `{base}/messages` | `x-api-key` + `anthropic-version` | **not supported** — omitted | none; JSON is requested and validated locally |

Two protocols are *recognised* but not implemented: `google_generate_content`
and `system_one`. A configuration that needs one is reported as unsupported
rather than routed to an endpoint that cannot understand it.

### 9.3 The rules that keep a provider request safe

- **HTTPS for a remote provider.** Plain `http` needs the explicit local
  endpoint option.
- **The destination is checked.** The host is resolved immediately before
  every request and every address it resolves to is examined. Loopback,
  private, link-local (which includes the cloud metadata address),
  unspecified, multicast and reserved addresses are refused, as are the
  `localhost` and `metadata.google.internal` names.
- **Redirects are not followed.** A redirect to another origin would carry
  the authorization header with it.
- **Every request is bounded.** A timeout, a response-size cap, a bounded
  retry budget with backoff, and `Retry-After` honoured.
- **The provider's own error body never reaches the user.** It is inspected
  so the failure can be classified, and then summarised.
- **The key is never in a URL, a log, an error or a response.**

The residual risk: a name that resolves to a permitted address once and a
forbidden one later. Closing that completely needs a pinned-IP resolver,
which the client does not offer.

### 9.4 What the model is allowed to do

The model is given a block of transcript text and a system prompt that says
the transcript is quoted material, not instructions. The answer is a JSON
object, and it is validated locally whatever comes back.

A candidate is valid when its segment references exist and belong to this
transcript, the order is chronological, the start is before the end, the
boundaries are inside the source duration, the duration is within the
product's limits, the title is a non-empty string and the rationale is a
string. A candidate that fails is **dropped**, never repaired with an
invented timestamp.

The boundaries are read from the persisted segments. Times the model proposes
are advisory: they are checked against the segments the candidate cites, and
a contradiction drops the candidate.

### 9.5 The analysis is not a job stage

The `jobs` state machine is untouched. `completed` still means exactly what
it meant in Phase 3: the source was stored, its audio was extracted and its
transcript was saved.

A highlight analysis is a separate entity with its own lease, claimed by the
worker with the same atomic-claim and recovery pattern the jobs use. The
worker does the provider call, so a request handler never waits on a model.

---

## 10. Clips

### 10.1 The shape of it

```
highlight candidate          clip
──────────────────          ────
start/end seconds     ──▶   the same seconds, re-checked
start/end segments    ──▶   the ground truth for the boundaries
                               │
                               ├──▶  FFmpeg re-encodes the range
                               ├──▶  FFprobe verifies the result
                               └──▶  the verified file is moved into place
```

A clip is cut from a **persisted candidate**, which was itself derived from
the persisted transcript segments. The renderer never invents a timestamp
and never takes one from the request.

### 10.2 The lifecycle

`queued → preparing → rendering → verifying → completed`, with `failed`
reachable from every non-terminal state. The rules live in `app/clips.py`
and are enforced in `app/db.py` inside the same transaction as the write.

`completed` is only reachable from `verifying`, and the verification is what
proves the output. A clip that is not failed is never retried, and a
completed clip is never requeued by the recovery.

### 10.3 The timestamp policy

The requested start and end are seconds on the **source media timeline**.

FFmpeg's `-ss` before `-i` seeks relative to the file's own start time,
which is what the media timeline is. This was **verified**, not assumed: a
file with a non-zero start time and the plain file it was made from hold
the same content, and a clip taken from the same media moment of each
produces byte-identical frames. The policy is therefore correct for a
source whose streams do not start at zero.

The clip is **re-encoded**, never stream-copied. A stream copy cuts on
keyframes, so it would silently start earlier and end later than the
candidate asks for.

The measured duration of the output has to be within
`clip_duration_tolerance_seconds` of the requested one. A clip that cannot
be produced within the tolerance **fails**; it is never shifted to a
convenient keyframe.

### 10.4 The encoding defaults

H.264 (`libx264`) in `yuv420p`, AAC audio, MP4 with `+faststart`, at
`clip_encoder_preset` (default `veryfast`) and CRF 23. These are chosen for
**playability everywhere** and for speed on a local machine, not for
quality. The audio stream is mapped optionally, so a source without sound
still renders.

### 10.5 Verification, and atomic finalisation

FFmpeg exiting with zero is not evidence. The temporary file is probed again
and has to have a video stream, positive dimensions, a finite duration, a
duration within the tolerance, the expected codecs, and the expected audio
presence. Only then is it renamed into `clips/`, and only then is the
database row committed — with an ownership check, so a worker whose lease
expired cannot claim a success.

The temporary file is named after the clip **and the attempt**, so two
attempts never share a path and a stale worker cannot remove a newer
attempt's file.

### 10.6 Idempotency and recovery

One clip per candidate, enforced by a partial unique index. A repeated
request finds the clip that already exists instead of rendering the moment
twice. A failed clip is retried through an explicit operation, which is the
only way a terminal clip is moved.

Recovery requeues a clip whose lease expired, fails one that has run out of
attempts, and leaves a completed clip alone. The reconciliation of
temporary files is narrow: only files matching the clip naming pattern, only
older than an hour, and only inside the scratch directory.

---

## 11. Formatted clips

### 11.1 The shape of it

```
Phase 5 clip (landscape)         formatted clip (9:16)
─────────────────────────         ─────────────────────
clip.start_seconds  ──┐
clip.end_seconds    ──┤
                      ├──▶  overlap with the transcript segments
transcript segments ──┘           │
                                  ├──▶  SRT on the clip's own timeline
                                          │
                            scale + crop / pad ──▶  H.264 + AAC + yuv420p
                                          │
                            subtitles filter ────▶  captions burned in
                                          │
                                          ▼
                                    formats/<id>.mp4
```

A formatted clip is a **separate artifact** from the clip it came from. The
landscape original is never touched, and a second set of settings produces a
second artifact rather than replacing the first.

### 11.2 The two timelines

The transcript lives on the **source timeline**; a clip's times are on the
same timeline; and the captions have to be on the **clip's** timeline, which
starts at zero. The conversion is the overlap:

    overlap_start  = max(segment_start, clip_start)
    overlap_end    = min(segment_end,   clip_end)
    relative_start = overlap_start - clip_start
    relative_end   = overlap_end   - clip_start

A segment outside the clip contributes nothing, and a segment that crosses a
boundary contributes only the part that is inside. The result is then
constrained to the clip's **measured** duration -- the length of the rendered
file, not the length that was asked for -- so a caption is never before zero
and never after the end of the video.

### 11.3 The layout

| Mode | The filter | What it does |
| --- | --- | --- |
| `center_crop` | `scale=W:H:force_original_aspect_ratio=increase,crop=W:H` | Fill the canvas, cut the excess |
| `fit_pad` | `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:…:color=…` | Show the whole frame, pad the rest |

`force_original_aspect_ratio` is what keeps the picture from being
stretched, and the explicit canvas size is what makes the output exactly the
configured resolution.

### 11.4 The captions

The captions are written as an **SRT** file. SRT has no markup at all, so
the user's speech cannot inject a styling tag, an extra field or a command
into the file. The *styling* comes from libass's `force_style`, whose value
is built from validated settings.

The file is written into a directory the application controls, and FFmpeg is
run with that directory as its working directory, so the filter refers to the
file by a plain, server-generated name. This is not a convenience: FFmpeg's
subtitle filter **cannot open a path that contains a single quote**, and the
escaping helper refuses such a path rather than producing a filter that
silently points at the wrong file.

### 11.5 The verification

FFmpeg exiting with zero is not evidence. The output is probed again and
has to have the exact configured dimensions, a finite duration within the
tolerance of the clip's measured duration, the expected codec, the expected
pixel format and the expected audio.

Whether the captions were really burned in is proved in the test suite by
rendering the same input twice, once with the subtitles filter and once
without, and comparing the frames: the captioned frames have to differ.

---

## 12. YouTube acquisition

### 12.1 Validation

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

### 12.2 The adapter

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

### 12.3 Residual risk

The initial URL is fully under our control. **The redirects yt-dlp follows
internally are not.** YouTube serves media from `googlevideo.com` hosts that
are chosen by YouTube, and yt-dlp resolves and follows those itself. This is
inherent to how YouTube works, and it is why the acquisition mode is
configurable and off by default in any installation that does not need it.

The trust boundary is documented rather than hidden: the user-supplied part of
the URL can never point anywhere but YouTube, and the rest is YouTube's own
infrastructure.

### 12.4 Authorisation

The API requires `authorized: true` in the submission, which the frontend
presents as an explicit checkbox: *"I own this video or have permission to
download and reuse it."* The downloader runs without credentials, so it can
only reach publicly available material. No mechanism exists to bypass a
login, a DRM, an age restriction, a private-video permission or a rate
limit, and none is planned.

---

## 13. Why media processing must not block HTTP requests

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

## 14. Current implementation map

### Exists

| Piece | Where |
| --- | --- |
| Application factory, CORS, body size limit, error envelope | `backend/app/main.py` |
| Typed, validated settings | `backend/app/config.py` |
| Job states, stages, transitions | `backend/app/jobs.py` |
| Schema, migrations, job repository, claiming, recovery | `backend/app/db.py` |
| Storage layout and path containment | `backend/app/storage.py` |
| FFprobe inspection and media policy | `backend/app/media.py` |
| FFmpeg audio extraction | `backend/app/audio.py` |
| Local speech-to-text engine | `backend/app/transcription.py` |
| The isolated inference process | `backend/app/transcription_process.py`, `backend/app/transcription_host.py` |
| Transcript rules and validation | `backend/app/transcripts.py` |
| The "stop now" signal for long steps | `backend/app/interruption.py` |
| The integration domain model | `backend/app/integrations.py` |
| The provider preset catalog | `backend/app/providers.py` |
| The API protocol adapters | `backend/app/protocols.py` |
| The provider transport | `backend/app/ai_client.py` |
| The operating system's credential store | `backend/app/credentials.py` |
| The highlight candidate rules | `backend/app/highlights.py` |
| The versioned prompt and the windowing | `backend/app/highlight_prompt.py` |
| The highlight analysis service | `backend/app/highlight_service.py` |
| The clip lifecycle and the boundary rules | `backend/app/clips.py` |
| The FFmpeg command, the run and the verification | `backend/app/clip_render.py` |
| The render the worker performs | `backend/app/clip_service.py` |
| The formatting settings, validated and fingerprinted | `backend/app/formatting.py` |
| The caption timeline and the subtitle file | `backend/app/captions.py` |
| The FFmpeg command, the run and the verification | `backend/app/format_render.py` |
| The render the worker performs | `backend/app/format_service.py` |
| The formatted-clip domain model | `backend/app/formats.py` |
| The bounded ZIP export | `backend/app/export_service.py` |
| The fonts this machine has | `backend/app/fonts.py` |
| The interrupt/cancel signal shared by every long step | `backend/app/interruption.py` |
| The stable error codes and the error envelope | `backend/app/errors.py` |
| The request and response models | `backend/app/schemas.py` |
| The dependencies the routers share | `backend/app/deps.py` |
| The single `clipforge` logger and its handler | `backend/app/logging_config.py` |
| YouTube URL validation | `backend/app/youtube.py` |
| Downloader adapter | `backend/app/acquisition.py` |
| Streaming multipart upload | `backend/app/uploads.py` |
| `/health` and `/ready`, including the capability flags and the worker heartbeat read | `backend/app/routers/health.py` |
| `/api/v1` metadata | `backend/app/routers/api.py` |
| Upload and YouTube endpoints | `backend/app/routers/videos.py` |
| Job list, detail and transcript endpoints | `backend/app/routers/jobs.py` |
| Integration endpoints | `backend/app/routers/integrations.py` |
| Highlight analysis endpoints | `backend/app/routers/highlights.py` |
| Clip endpoints | `backend/app/routers/clips.py` |
| Formatted-clip endpoints and the font list | `backend/app/routers/formats.py` |
| Worker | `backend/app/worker.py`, `backend/worker.py` |
| The artifact counts, counted in one batched query set | `backend/app/db.py` (`job_artifacts`) |
| The worker's heartbeat, so the status view can see it | `backend/app/worker_heartbeat.py` |
| API client | `frontend/src/lib/api.ts` |
| The application shell: header, navigation and footer | `frontend/src/app/layout.tsx`, `frontend/src/components/AppHeader.tsx` |
| The global focus ring | `frontend/src/app/globals.css` (`:focus-visible`) |
| The polling rule every view follows | `frontend/src/lib/use-polling.ts` |
| Clip title editing, revisions and caption overrides | `frontend/src/components/ClipEditor.tsx` |
| The export action | `frontend/src/components/ExportButton.tsx` |
| The shared button, badge, empty state and error surface | `frontend/src/components/ui/` |
| Upload and YouTube forms | `frontend/src/components/VideoSourcePanel.tsx` |
| The video library | `frontend/src/components/JobList.tsx` |
| The workflow of one video | `frontend/src/components/JobWorkflow.tsx`, `frontend/src/app/jobs/[jobId]/page.tsx` |
| The grid of a job's clips and their short-form versions | `frontend/src/components/ClipGrid.tsx` |
| A preview that reports a file it cannot play | `frontend/src/components/MediaPreview.tsx` |
| The service status view | `frontend/src/components/ServiceStatusPanel.tsx`, `frontend/src/app/status/page.tsx` |
| Transcript panel | `frontend/src/components/TranscriptPanel.tsx` |
| Highlight panel | `frontend/src/components/HighlightsPanel.tsx` |
| Clip render panel | `frontend/src/components/ClipPanel.tsx` |
| The short-form formatting panel | `frontend/src/components/FormatPanel.tsx` |
| The Integrations page | `frontend/src/app/integrations/page.tsx`, `frontend/src/components/IntegrationsPanel.tsx` |
| Health indicator | `frontend/src/components/HealthIndicator.tsx` |

### The frontend routes

| Route | What it is |
| --- | --- |
| `/` | The dashboard: add a video, and the library of every video that has been submitted |
| `/jobs/[jobId]` | The workflow of one video: the source, the transcript, the moments, and the grid of the clips and the short-form versions |
| `/status` | Whether the backend is ready, and what this build can do |
| `/integrations` | The AI provider configurations |

The header, the navigation and the footer live in the **root layout**, so
every route is the same shell and the navigation is in one place.

The workflow route is a **blocking** route (`export const instant = false`):
the job id is only known at request time, so the page cannot be prerendered.
That is the documented way to say so in Next 16.

### The media routes

The API answers with a `media_url` that is **relative to itself**, for
example `/api/v1/clips/<id>/media`. The frontend usually runs on a
different port, so the client resolves that path against the API's own
address in one place — `mediaUrl()` in `frontend/src/lib/api.ts` — and
every preview and every download points at the API's route.

### Is the worker running?

The API and the worker are separate processes, so the API cannot see the
worker. The worker therefore leaves a **heartbeat** — a small JSON file in
the storage root — every few seconds, and the readiness report says how
long ago it was last seen. It is a file and not a row because it is about
the *process*, it changes constantly, and it must not need a migration or
a transaction to read.

### Does not exist yet

Authentication (the API is a local tool with none), a retention policy for the
media on disk (sources, clips and formats are kept forever until deleted by
hand), per-caption rather than render-wide caption styling (the font, size,
colours, outline, shadow, position and margins are all set once for a render),
and end-to-end browser automation.

Cancellation (`POST /api/v1/jobs/{id}/cancel`) stops the work as well as the
bookkeeping — decision D55. The queued children of the job (its clips,
formatted clips and pending analyses) are failed in the same transaction with
the stable `job_cancelled` code, so the worker never claims them; an in-flight
yt-dlp download, clip render or format render is stopped within one lease
tick, because the tick renews the artifact's lease and raises `WorkCancelled`,
which the renderer's `finally` answers by terminating and reaping the child. A
cancel that lands inside the bounded (≤ 60 s) verification step is noticed at
the next boundary rather than mid-probe. A *worker shutdown* during a render is
still a plain interruption, which leaves the artifact for lease recovery
instead of failing it. Export (`POST /api/v1/jobs/{id}/exports`) is bounded by
an item count and a byte budget.

---

## 15. Security posture

- The API binds `127.0.0.1` by default and has no authentication. It is a
  local tool, not a service. Binding it to any other address is **refused**
  when the settings load, unless `CLIPFORGE_ALLOW_LAN_BINDING=true` is set
  explicitly.
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
- FFprobe, FFmpeg and yt-dlp are always started with an argument list, never a
  shell.
- The speech model runs **entirely on this machine**. No audio, no transcript
  and no request ever leaves it, and there is no transcription API key. With
  `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` the engine does not even reach the
  network for the model files.
- The transcript endpoint returns no filesystem path, no model cache location
  and no raw subprocess output.
- An AI provider's API key lives in the operating system's credential store.
  Only a reference reaches the database, the key is never returned by an
  endpoint, and there is no plaintext fallback when no credential store is
  available.
- A provider request uses HTTPS for a remote endpoint, the destination is
  checked against the prohibited address ranges immediately before the
  request, redirects are not followed, and the response is bounded in size
  and time.
- A clip is rendered from a source path that is resolved inside the storage
  root, with an argument list and no shell, under a timeout, and the output
  is verified and moved into place only after it has been probed. The media
  route resolves the stored path with the same containment check, so a clip
  cannot be used to reach a file elsewhere on the machine.
- A formatted clip is rendered the same way, and its captions come from the
  persisted transcript as an SRT file that has no markup in it, so the
  user's speech cannot inject a styling tag or an extra field. The
  subtitle file is referred to by a server-generated name, and the
  directory that holds it never reaches the filter.
- The transcript is sent to the provider the user chose, and only when the
  user asks for an analysis. The video and the audio are never sent.
- YouTube ingestion respects the copyright of the source material: the caller
  has to confirm they own the video or may reuse it, the downloader runs
  without credentials so only public material is reachable, and nothing
  bypasses a login, a DRM or an age restriction.
