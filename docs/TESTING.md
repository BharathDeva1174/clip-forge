# Testing

Two independent suites, one per application. Neither needs an API key, an
internet connection, a GPU or a video file.

| Suite | Runner | Location | Size on this machine | Needs the other app running? |
| --- | --- | --- | --- | --- |
| Backend | pytest + httpx (`TestClient`) | `backend/tests/` | 933 tests, about 3.5 min | No |
| Frontend unit + component | Vitest + React Testing Library | `frontend/src/**/*.test.ts(x)` | 14 files, 178 tests, about 16 s | No |

The `Not implemented: navigation to another Document` line the frontend run
prints is jsdom noise from a test that clicks a link; it is not a failure.

---

## 1. Backend tests

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python -m pytest
```

Useful variations:

```powershell
python -m pytest -v                          # one line per test
python -m pytest tests/test_worker.py        # one file
python -m pytest -k "cors"                   # by name
python -m pytest --tb=short                  # shorter tracebacks
python -m pytest -m "not real_model"         # everything except the model tests
python -m pytest -m real_model               # only the model tests
```

### Fixtures

| Fixture | What it provides |
| --- | --- |
| `clean_environment` | Removes every `CLIPFORGE_*` variable, so the tests are deterministic |
| `storage_root` | A temporary storage root under `tmp_path` |
| `settings` | Settings pointing at the temporary root, a temporary database and small limits |
| `storage` | The storage layout for that root, with the directories created |
| `app` / `client` | The application and a `TestClient` bound to it |
| `db` | A database connection with the schema applied |
| `sample_video` | A tiny valid MP4 **generated with FFmpeg** (see below) |
| `sample_video_without_audio` | A valid MP4 with **no audio track**, also generated with FFmpeg |
| `sample_speech_audio` | A WAV file with real speech, produced by the Windows speech synthesiser |
| `sample_video_with_speech` | A short video whose audio track contains that speech |
| `not_a_video` | A file that claims to be an MP4 but is not a media file |
| `host_mode` | Chooses how the stub inference process behaves |

**No test ever touches a developer database or the real `backend/data`
directory**, and no test downloads third-party content.

### The synthetic test video

`sample_video` is generated at test time with FFmpeg:

```
ffmpeg -f lavfi -i "testsrc=duration=1:size=320x240:rate=10" \
       -f lavfi -i "sine=frequency=440:duration=1" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest sample.mp4
```

It is 1 second, 320×240, H.264. The tests that need it **skip** when FFmpeg
is not installed, rather than failing.

`sample_speech_audio` says *"The quick brown fox jumps over the lazy dog."*
using the Windows speech synthesiser, and `sample_video_with_speech` muxes
it into a video. Both are generated locally, so the repository stays free of
third-party media, and the phrase gives a real transcription something
specific to be checked against. On a machine without the synthesiser those
fixtures **skip**.

### The speech model

The ordinary test suite **never loads the speech model**. The engine is
faked, so the tests are fast, deterministic and need no network. The
inference *process* is faked too, with `tests/host_stub.py`, which speaks the
same protocol as the real one and can be told to stall, crash or fail.

The real model is used in one place only: `tests/test_real_model.py`, which
is marked `real_model` and **skips** unless the model is already in the local
cache. It never downloads anything. See §5.

### The new test files (Phase 3.1)

| File | Tests | Covers |
| --- | --- | --- |
| `test_transcription_process.py` | 15 | The child process: the timeout, the cleanup, the failures, the reuse of the model, the network guard, the shutdown path |
| `test_transcription_timeout.py` | 3 | The timeout at the process level: a stalled inference, a worker that survives, a graceful shutdown, no orphan |
| `test_transcription_config.py` | 40 | The model name, the model cache, the offline behaviour, the isolation mode |
| `test_transcript_normalisation.py` | 28 | The normalisation rules, one test per case |
| `test_job_recovery.py` | 13 | Ownership, lease recovery, retries, terminal states, the legacy jobs, a failed migration |
| `test_real_model.py` | 10 | The real model against a matrix of locally generated audio |

### The new test files (Phase 4)

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_client.py` | 59 | The three protocol adapters, the model list, the connection test, the URL checks, the error classification, the timeouts, the rate limits, the redirects, the response cap, the preset catalog |
| `test_integrations_api.py` | 31 | Saving, editing, listing, selecting, disabling and deleting a configuration; the key never leaving the credential store |
| `test_highlights.py` | 58 | The candidate rules, the windowing, the prompt, the service, the worker, the recovery, the background lease renewer, the per-window retry budget and backoff, the context-limit split, and the analysis deadline |
| `test_highlights_api.py` | 16 | Queuing an analysis, reading the result, accepting and rejecting a candidate, the error cases |

### The new test files (Phase 5)

| File | Tests | Covers |
| --- | --- | --- |
| `test_clips.py` | 33 | The boundary rules, the lifecycle, the claim, the ownership, the retry, the recovery, the reconciliation, and a real render by the worker |
| `test_clip_render.py` | 19 | **Real FFmpeg.** The command, the probe, the render, the timeout, the verification, and every tolerance |
| `test_clips_api.py` | 17 | Requesting, listing, reading, retrying and playing a clip; the idempotency; the containment of the media route |

The clip fixtures in `conftest.py` are generated with FFmpeg at test time:
a video with sound, a video without sound, a video whose media timeline
starts part-way into the file, a file whose name has spaces, Unicode and a
semicolon in it, and a file that is not a media file at all. Nothing is
downloaded.

### The new test files (Phase 6)

| File | Tests | Covers |
| --- | --- | --- |
| `test_formatting.py` | 88 | The settings and their validation, the fingerprint, the caption timeline, the boundary clipping, the text cleaning, the line wrapping, the SRT document, and the filter-path escaping |
| `test_format_render.py` | 23 | **Real FFmpeg.** The filters, the command, the render, the timeout, the verification, every tolerance, and the **caption burn-in proved frame by frame** |
| `test_format_worker.py` | 14 | The claim, the ownership, the retry, the lease recovery, the reconciliation, the cleanup, and a real render from a real clip |
| `test_formats_api.py` | 29 | Requesting, listing, reading, retrying and playing a formatted clip; the idempotency; the containment of the media route; the font list |

**The burn-in proof.** A subtitle file existing on disk proves nothing, so
`test_format_render.py` renders the same input **twice** — once with the
subtitles filter and once without — and compares the MD5 of every frame.
The captioned frames have to differ, and a caption whose time falls outside
the video has to leave the two renders identical. The end-to-end script
does the same against the real API output.

### The new test files (Phase 7)

| File | Tests | Covers |
| --- | --- | --- |
| `test_dashboard.py` | 10 | The artifact counts, the batched query set, the most-recent-analysis rule, and that the list and the detail endpoint agree |
| `test_worker_heartbeat.py` | 12 | The worker's note: that it is written, that a missing or stale one means the worker is not running, that a malformed one is ignored, and what the readiness report says about it |
| `frontend/src/components/JobWorkflow.test.tsx` | 12 | The workflow: the source, the transcript, the next step, the not-found state, the network failure, a malformed job, the polling and its cleanup |
| `frontend/src/components/ClipGrid.test.tsx` | 11 | The grid: the empty state, the measurements, the preview and the download, the short-form versions, a failure to load, a transient failure, the polling and its cleanup, and a preview that reports a file it cannot play |
| `frontend/src/components/ServiceStatusPanel.test.tsx` | 7 | The status view: ready, the capabilities, the model cache, a failure and a retry, and a malformed answer |
| `frontend/src/components/VideoSourcePanel.test.tsx` | 8 | The upload and the YouTube form: the accessible names, the authorisation, the queued outcome, the errors, and the duplicate-submission guard |
| `frontend/src/lib/api.test.ts` | +9 | The normalisation of the job list, one job, the transcript and the readiness report |

### The new test files (Phase 8)

| File | Tests | Covers |
| --- | --- | --- |
| `test_editing.py` | 50 | Clip revisions (trim → new clip), clip titles, per-segment caption edits, the ZIP export with item and byte limits, cross-job refusal, job cancellation, idempotency |
| `test_job_cancellation.py` | 10 | The cancellation contract of decision D55: the queued children of a cancelled job are failed in the same transaction with `job_cancelled`, a render tick raises `WorkCancelled` so the FFmpeg child is stopped within one tick, a cancelled clip or format is terminal rather than requeued, and a worker *shutdown* during a render stays a plain interruption that leaves the artifact for lease recovery |

**The export limits.** `test_an_export_with_too_many_clips_is_refused` and
`test_an_export_at_the_limit_is_accepted` prove the item bound; the byte
budget is covered by `test_an_export_over_the_byte_budget_is_refused` and
`test_an_export_at_the_byte_limit_is_accepted`.

**What the normalisation tests prove.** A malformed answer is turned into
something the interface can render: a status that is not there becomes
`unknown`, a progress figure outside 0–100 is clamped, an entry without an
id is dropped, and a transcript without its segments is a transcript with
none. None of them crashes a panel.

### What each test file covers

**`tests/test_health.py`** — the Phase 1 probes, kept intact:

| Test | Verifies |
| --- | --- |
| `test_health_returns_200` | `GET /health` answers 200 |
| `test_health_returns_the_exact_contract` | The body is exactly `{"status": "ok", "service": "clipforge-api"}` |
| `test_ready_reports_a_loaded_configuration` | `GET /ready` answers 200 with `status=ready` and `config=loaded` |
| `test_ready_reports_exactly_the_capabilities_that_exist` | Every capability flag matches the feature it names — all ten are `true`, and there is no capability outside the known set |
| `test_unknown_route_returns_404` | An unknown path answers 404 |
| `test_cors_allows_the_configured_frontend_origin` | A preflight from `http://localhost:3000` is allowed |
| `test_cors_does_not_allow_an_unknown_origin` | A preflight from an unknown origin gets no allow-origin header |
| `test_responses_never_leak_configured_secrets` | A configured LLM key never appears in any response body |

**`tests/test_api_v1.py`** — the API metadata endpoint.

**`tests/test_config.py`** — configuration loading and validation: safe
defaults, an invalid port, an out-of-range port, a wildcard CORS origin,
environment variables, and secret masking.

**`tests/test_youtube_urls.py`** — the URL validator, with no network access
at all:

| Group | Covers |
| --- | --- |
| Accepted | Watch, short, embed, shorts and live URLs; extra query parameters; canonicalisation |
| Rejected | External hosts, deceptive hostnames, `file:`/`ftp:`/`javascript:` schemes, credentials in the URL, malformed ids, invalid ports, loopback and private targets, unknown paths, empty input |
| Codes | The stable error code for each rejection class |

**`tests/test_storage.py`** — path containment: relative paths resolve inside
the root; `..`, absolute paths, drive specifications and symlinks pointing
outside are refused; generated names are unpredictable; client filenames are
reduced to a safe display form.

**`tests/test_media.py`** — FFprobe inspection: a valid video is read, a file
that is not a video is rejected, an empty file is rejected, a missing FFprobe
is reported as `media_tool_unavailable`, and the duration, dimension and
codec policies are enforced.

**`tests/test_upload_api.py`** — the upload endpoint:

| Test | Verifies |
| --- | --- |
| `test_a_valid_video_is_accepted` | 202, a queued job and a 32-character id |
| `test_the_job_is_persisted_in_sqlite` | The job row exists with the right status |
| `test_the_source_file_lives_inside_the_storage_root` | The stored path resolves inside the root |
| `test_the_job_response_does_not_leak_a_filesystem_path` | No path or `source_path` in the response |
| `test_a_zero_byte_file_is_rejected` | 400 `empty_file` |
| `test_an_unsupported_extension_is_rejected` | 415 `unsupported_media_type` |
| `test_a_misleading_extension_is_rejected_by_media_inspection` | A text file named `.mp4` is rejected |
| `test_malformed_media_is_rejected` | A broken container is rejected |
| `test_an_oversized_upload_is_rejected_with_413` | 413 `payload_too_large` |
| `test_an_oversized_body_is_rejected_before_it_is_read` | The `Content-Length` middleware rejects it |
| `test_a_rejected_upload_leaves_no_partial_file_behind` | `uploads/` is empty afterwards |
| `test_path_traversal_filenames_cannot_escape_the_storage_directory` | The stored file stays inside the root |
| `test_repeated_uploads_do_not_overwrite_each_other` | Two uploads, two distinct files |
| `test_the_client_supplied_content_type_cannot_bypass_validation` | The MIME type is never trusted in either direction |
| `test_a_missing_ffprobe_produces_an_clear_error` | 415 `media_tool_unavailable` naming ffprobe |
| `test_a_request_without_a_multipart_body_is_rejected` | 415 |
| `test_a_non_multipart_body_is_rejected` | 415 |
| `test_invalid_configuration_is_rejected_without_exposing_secrets` | A validation error never contains the secret |

**`tests/test_youtube_api.py`** — the submission endpoint: a valid URL is
queued, a short URL is canonicalised, authorisation must be confirmed, bad
URLs are rejected, and — using a spy adapter — the acquisition adapter is
**never** called during validation.

**`tests/test_jobs_api.py`** — the job endpoints: persistence across a
reconnect, ordering and bounding of the list, 404 for an unknown id, invalid
pagination values, no path or worker internals in the response, the error
shape, the database `CHECK` constraints, and the allowed and forbidden state
transitions.

**`tests/test_worker.py`** — the worker: a queued upload completes with real
media metadata, invalid media fails with a stable code, a failed job never
stays in an active status, a permanent error is not retried, a transient error
is retried and the attempt count grows, two workers cannot claim the same
job, a restarted worker discovers pending jobs, an expired lease is recovered
or failed, a missing FFprobe fails the job cleanly, and processing a job
twice neither corrupts the source nor duplicates the job record.

**`tests/test_db.py`** — the schema: a fresh database is at the current
version, an older database is upgraded without losing data, migrating twice
is harmless, and the `CHECK` constraints reject an impossible status, source
type or stage.

**`tests/test_acquisition.py`** — the downloader adapter, with the child
process replaced by a recording double: the downloader is started with an
argument list and never a shell, the canonical URL is a single argument, no
cookies or credentials are passed, the size is bounded, a non-zero exit
produces a generic error that does not leak the downloader's output, an empty
or oversized download is rejected, and a timeout terminates the child process.

**`tests/test_audio.py`** — FFmpeg audio extraction, against real FFmpeg and
a real generated video:

| Group | Covers |
| --- | --- |
| Success | The audio is extracted, and it really is mono, 16 kHz, 16-bit PCM WAV with a matching duration |
| Failure | A missing FFmpeg, invalid media, a truncated source, a source with no audio track, a timeout, a source that is too long, and not enough disk space |
| Stub-driven | A non-zero exit, an empty output and an output that is not audio all fail with stable codes |
| Safety | The diagnostic output is bounded and never appears in the error message; a path with spaces and Unicode works; a path with shell metacharacters is not run through a shell; a path outside the storage root is refused; the output can never overwrite a source or another result |

**`tests/test_transcript_model.py`** — the transcript rules: conversion into
the internal schema, deterministic ordering, the timestamp boundaries, the
handling of Whisper's window overshoot, Unicode and punctuation preservation,
empty results, and malformed data.

**`tests/test_transcription.py`** — the engine, with faster-whisper replaced
by a fake: the model is loaded lazily and only once, the configuration is
passed through, a model that cannot be loaded is reported, offline mode never
reaches the network, a cached model is recognised, failures are mapped to
stable codes, the timeout is enforced, the configured concurrency is
respected, and the engine never opens a network connection.

**`tests/test_transcript_storage.py`** — the database: a Phase 2 database
migrates to the Phase 3 schema without losing a job, the migration is
idempotent and leaves no placeholder table, the indexes survive, the foreign
keys are enforced, the segments come back in order and are paginated, a
failure while storing the segments rolls everything back, and a retry
replaces the transcript instead of duplicating it.

**`tests/test_worker_pipeline.py`** — the Phase 3 pipeline: a job goes through
extraction and transcription, the engine receives the extracted audio, the
audio is deleted afterwards (or kept on request), a source without audio fails
with `no_audio_stream`, a failing transcription fails the job with a stable
code, a missing model is queued for another attempt, an interrupted
extraction does not complete the job, a retry does not duplicate the
transcript, two workers cannot process the same job, the API stays responsive
while the transcription runs, and the worker logs contain no absolute path.

**`tests/test_worker_process.py`** — the worker as a **separate operating
system process**: it claims a job and works on it, a job left behind by a
dead worker is taken over, and stopping it leaves the database consistent.

**`tests/test_transcript_api.py`** — the transcript endpoint: the transcript
of a finished job, the timestamped segments, pagination, a job that is still
running, a failed job, an unknown id, a malformed id, invalid pagination, the
stable error code of a failure, the `has_transcript` flag, and that no
response ever contains a path.

**`tests/test_storage_consistency.py`** — the boundary between the filesystem
and the database: a queued job points at a file that exists, and a failure at
the upload, the move or the job-record boundary leaves neither an orphaned
file nor a job row behind.

### Reading failures

| Symptom | Likely cause |
| --- | --- |
| `ModuleNotFoundError: No module named 'app'` | You are not in `backend/`, or `pythonpath` was lost from `backend/pyproject.toml` |
| `ModuleNotFoundError: No module named 'multipart'` | The virtual environment is not active, or dependencies were not installed |
| `ValidationError` on almost every test | An environment variable in your shell is invalid — check `CLIPFORGE_PORT` and `CLIPFORGE_CORS_ORIGINS` |
| `test_health_returns_the_exact_contract` fails | The `/health` contract changed. That is a breaking API change; update the frontend and the docs together |
| `test_cors_*` fails | `CLIPFORGE_CORS_ORIGINS` in your environment does not include `http://localhost:3000` |
| Many tests are **skipped** | FFmpeg is not installed, so the synthetic video cannot be generated |
| `test_a_symlink_out_of_the_root_is_refused` is skipped | Symlinks need a privilege on this Windows account |

The suite is deterministic: no network, no clock dependency, no shared state
between tests. On this machine it collects **933 tests — 932 passed, 1
skipped — in about three and a half minutes**. The one skip is
`test_a_symlink_out_of_the_root_is_refused`, which needs symlink privileges
(see the table above); the wall time is dominated by the tests that spawn
real subprocesses, not by the number of tests.

---

## 2. Frontend tests

```powershell
cd clipforge\frontend
npm test
```

Useful variations:

```powershell
npm run test:watch               # re-run on change
npm test -- health               # filter by file name
npm test -- --reporter=verbose   # detailed output
```

### What each test file covers

**`src/lib/health.test.ts`** — the health-check logic, with a stubbed
`fetch`: an online backend, a network failure, a non-2xx response, an
unexpected payload and trailing-slash handling.

**`src/lib/api.test.ts`** — the API client:

| Test | Verifies |
| --- | --- |
| `sends the url and the authorisation confirmation as JSON` | The request body is `{ url, authorized }` |
| `reports the API error code and message` | The error envelope is surfaced |
| `reports a network failure clearly` | A thrown fetch becomes an `ApiError` |
| `asks for the job list` | The list URL and the parsed result |
| `falls back to a generic error for a non-JSON answer` | An unparsable body still produces a clean error |
| `sends the file as multipart form data` | A `FormData` with the file in the `file` field |
| `reports a rejected upload with its error code` | A 415 keeps its code |
| `reports a network failure` | The upload error path |
| `reports upload progress` | The progress callback receives the fraction |

**`src/components/HealthIndicator.test.tsx`** — the Phase 1 indicator:
loading state, online state, and the unreachable state with a working retry.

**`src/components/JobList.test.tsx`** — the job list: the empty state, the
status and progress of a job, no progress bar for a finished job, the error
of a failed job, an unreachable API, polling while a job runs, **polling
stopping once every job is terminal**, the manual refresh, the two
transcription stages, the approximate transcription progress, and the badge
that does **not** claim a transcript that was never stored.

**`src/components/TranscriptPanel.test.tsx`** — the transcript panel: the
timestamped segments, the segment count and the metadata, a job that is still
running, a job that failed, a transcript that could not be loaded, audio with
no speech in it, polling while the job runs, polling stopping once the job
is finished, and that no path is ever shown.

**`src/components/ClipPanel.test.tsx`** — the clip panel: the default
settings, requesting a clip, the duplicate-submission guard, the lifecycle
stages, the failure and its retry, the preview and the download, and that
polling stops once the clip is finished.

**`src/components/FormatPanel.test.tsx`** — the short-form formatting panel:
the default settings, changing the resolution and the layout, turning the
captions on and off, the caption styling controls, the fonts this machine
has, the request, the **duplicate-submission guard**, the queued stage, the
finished artifact with its preview and download, **the case where a clip was
rendered without captions**, the failure with its stable code, the retry, an
error from the server, **that an answer which is not a list does not take
the panel down**, and **that polling stops when the component is
gone**.

**`src/components/VideoSourcePanel.test.tsx`** — the upload and the YouTube
form: what is accepted, **the accessible names of every control**, the
authorisation box and the button it enables, the queued outcome with the
returned job id, **the wording that says it is queued rather than
finished**, an actionable error, a network failure, and **the
duplicate-submission guard**.

**`src/components/JobWorkflow.test.tsx`** — the workflow of one video: the
source and its facts, the transcript, **what can be done next from the real
state**, **that formatting is not offered before there is a clip**, a job
with no transcript, a failed job with its code, a job that does not exist,
a network failure that is not mistaken for an empty job, a malformed job,
the polling while it runs, the polling stopping when it finishes, and the
polling stopping when the component is gone.

**`src/components/ClipGrid.test.tsx`** — the grid of a job's clips: the
empty state, the measurements of a clip, **the preview and the download
pointing at the API's media route**, the short-form versions, a version
that was rendered without captions, a failure to load the clips, **a
transient failure that keeps the clips that are already shown**, the
polling while a clip renders, the polling stopping when it is done, and
the polling stopping when the component is gone. It also covers the
preview that **says so when the file cannot be played**, rather than
showing an empty black rectangle.

**`src/components/ServiceStatusPanel.test.tsx`** — the status view: the
backend being ready, the capabilities that exist, a capability that is not
available, the speech model not being downloaded yet, a failure, a retry,
and a malformed answer.

**`src/components/HighlightsPanel.test.tsx`** — the moments panel: offering to
start an analysis when there is none, the candidates once it is finished, an
empty answer treated as a real answer, a pending analysis being re-read, a
failure with its stable code, starting an analysis, accepting and rejecting a
candidate, pointing at the transcript interval a candidate covers, **never
presenting a candidate as a rendered clip**, and not leaking a transcript or a
key in an error.

**`src/components/IntegrationsPanel.test.tsx`** — the provider page: the empty
state, the saved configurations, which one is the highlight default, **never
showing an API key**, the presets filling in the base URL, masking the key field
and keeping the saved key on an edit, **not erasing a key when the form is saved
without one**, replacing it when one is typed, a failed save that keeps the form,
testing the connection, a connection failure that leaks nothing, fetching the
model list and choosing a model, saying when a model list cannot be fetched,
selecting a default, and disabling and deleting a configuration.

**`src/components/ClipEditor.test.tsx`** — the clip editor: the title and the
boundaries, **that the boundaries are on the source video**, saving a title
without re-rendering, trimming into a new revision, refusing a boundary that is
not a number before the request, a refused trim with its error, the words the
clip covers, saving changed words, a caption save that failed, no words offered
when the job has no transcript, and the two facts the interface states plainly —
a title is not a re-render, and a trimmed copy is a new clip.

**`src/components/ClipPanel.test.tsx`** — as described above, plus **a clip that
has already been rendered being found**, and **a clip that belongs to another
candidate being ignored**.

The component tests use a stubbed global `fetch` and a fake `XMLHttpRequest`,
so they are deterministic and never touch the network.

### Reading failures

| Symptom | Likely cause |
| --- | --- |
| `Cannot find module '@/...'` | The `@/*` alias is missing from `vitest.config.mts` |
| `document is not defined` | The environment is not `jsdom` — check `test.environment` in `vitest.config.mts` |
| `getByText` cannot find an element | The rendered copy changed — update the test to the new text |
| A test hangs | A promise that never settles; the deferred-promise pattern in the component tests avoids this |

---

## 3. Static checks

Run from `clipforge\frontend\`:

```powershell
npm run typecheck   # next typegen && tsc --noEmit
npm run lint        # ESLint (flat config, eslint-config-next)
npm run build       # production build
```

`npm run typecheck` runs `next typegen` first. Next.js 16 generates the
`LayoutProps`/`PageProps` route types into `.next/types`, and they do not exist
in a fresh clone — without `typegen` the type check fails on a clean checkout
with `Cannot find name 'LayoutProps'`.

`npm run build` also runs TypeScript over the project, so it covers the type
check as well. `next lint` no longer exists in Next.js 16 — ESLint runs
directly.

---

## 4. Integration checks (manual, the API and the worker running)

These are not automated because they need live processes. They were run
manually in Phase 3 and the recorded results live in the per-phase reports,
which are kept outside this repository.

Start the API and the worker, then:

```powershell
# Backend liveness
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing | Select-Object -ExpandProperty Content
# -> {"status":"ok","service":"clipforge-api"}

# Backend readiness — note the capabilities and the model flag
Invoke-WebRequest http://127.0.0.1:8000/ready -UseBasicParsing | Select-Object -ExpandProperty Content

# Queue a video
Invoke-WebRequest http://127.0.0.1:8000/api/v1/videos/upload -Method Post `
  -Form @{ file = Get-Item .\sample.mp4 } -UseBasicParsing | Select-Object -ExpandProperty Content
# -> 202 with a job id

# Watch the worker pick it up
Start-Sleep -Seconds 5
Invoke-WebRequest http://127.0.0.1:8000/api/v1/jobs -UseBasicParsing | Select-Object -ExpandProperty Content

# Read the transcript of the job
Invoke-WebRequest http://127.0.0.1:8000/api/v1/jobs/JOB_ID/transcript -UseBasicParsing |
  Select-Object -ExpandProperty Content
# -> {"status":"ready", "segment_count":2, "segments":[...]}

# Queue a YouTube video
Invoke-WebRequest http://127.0.0.1:8000/api/v1/videos/youtube -Method Post `
  -Body (@{ url = "https://www.youtube.com/watch?v=VIDEO_ID"; authorized = $true } | ConvertTo-Json) `
  -ContentType "application/json" -UseBasicParsing | Select-Object -ExpandProperty Content

# Unknown job must be 404
Invoke-WebRequest http://127.0.0.1:8000/api/v1/jobs/deadbeef -UseBasicParsing -SkipHttpErrorCheck | Select-Object StatusCode
```

Then open http://localhost:3000 in a browser and confirm:

- the header shows **Backend online** while the API is running;
- submitting the YouTube form shows a green *Queued as job …* confirmation;
- the job list shows the new job with a status badge and a progress bar, and
  the badge changes to **Transcribed** when the worker is done;
- pressing **Transcript** on the job shows the timestamped segments;
- a job that was finished before transcripts existed says **Completed**, not
  **Transcribed**;
- stopping the API and reloading shows **Backend unreachable** with a
  *Retry* button.

---

## 5. Network-dependent tests

**There are none in the normal suite.** Every YouTube test uses a mocked
acquisition adapter or a recording double, and the speech engine is faked, so
`python -m pytest` never touches the network.

Verifying the real download path is a manual, opt-in step: it needs a
network connection and a video you are allowed to download. Use a
Creative Commons or public-domain video — never third-party content you have
no rights to. The Phase 2 report records one such manual check.

### The real model

`tests/test_real_model.py` is the one suite that loads the real model. It:

- **skips** unless the model is already in the local cache, so it never
  downloads anything;
- runs with `offline=True`, and the inference process is not allowed to open
  a socket at all, so the suite passes with no network;
- builds its own fixtures on the machine: the speech comes from the Windows
  speech synthesiser and the audio is assembled with FFmpeg. No third-party
  media is used.

```powershell
python -m pytest -m real_model -v
```

The assertions are about *structure*, not about getting every word right: a
small model on synthetic speech is not a word-accuracy benchmark, and **no
word error rate is claimed anywhere in this repository**, because none has
been measured.

The matrix it covers:

| Fixture | What it is |
| --- | --- |
| clear speech | Ordinary English, one sentence |
| speech with pauses | The same, with pauses between the clauses |
| short speech then silence | A short phrase followed by four seconds of silence |
| speech at the edges | Silence before and after the speech |
| speech with a tone | The speech mixed with a steady 220 Hz tone |
| another voice | The same kind of sentence, a different voice and a slower rate |
| silence | Three seconds with no speech at all |
| a corrupted file | A file that claims to be audio and is not |

---

## 6. The end-to-end workflow check

`backend/tests/e2e_workflow.py` is a script rather than a test. It starts the
real API process and the real worker process, uses a throwaway storage
directory, and drives the whole pipeline over HTTP:

```powershell
cd clipforge\backend
..\.venv\Scripts\python.exe tests\e2e_workflow.py
```

It checks that a video can be uploaded over HTTP, that the job is created
durably, that the audio is extracted, that the real model transcribes it
locally, that the transcript and its segments are stored, that the API
returns the right timestamps, that a controlled timeout ends the job with
`transcription_timeout` instead of leaving it `transcribing`, that the worker
survives that timeout and processes the next job, and that no temporary
artifact and no child process is left behind. It exits non-zero if any step
fails.

There are four more, in the same shape:

```powershell
cd clipforge\backend
..\.venv\Scripts\python.exe tests\e2e_phase4.py   # the provider path
..\.venv\Scripts\python.exe tests\e2e_phase5.py   # the clip path
..\.venv\Scripts\python.exe tests\e2e_phase6.py   # the formatting path
..\.venv\Scripts\python.exe tests\e2e_phase8.py   # editing, revisions and export
```

`e2e_phase4.py` starts a **real local HTTP server** that speaks the Chat
Completions dialect, so the whole provider path — the credential store, the
URL checks, the transport, the protocol adapter, the worker's analysis and
the API's responses — is exercised without a real provider and without a
real credential.

`e2e_phase5.py` takes the whole chain: upload, transcribe, analyse, request
a clip, render it with the real FFmpeg, verify it with the real FFprobe,
play it back through the API, and prove that a repeated request does not
render twice and that a missing source fails with a stable code.

`e2e_phase6.py` goes one step further: it requests a formatted version of a
**real** clip, waits for the worker to render it, checks the portrait
dimensions, the duration, the codec and the audio, streams the bytes back
through the API, and then **compares the frames of the served file with the
frames of the uncaptioned clip** to prove the captions are in the picture
rather than merely in a subtitle file. It then asks for a second format with
different settings and checks it produced a different artifact, checks the
original clip is untouched and still playable, and finally breaks a format
deliberately and retries it.

`e2e_phase8.py` covers the editing surface against real artifacts: a clip is
renamed, trimmed into a **revision** whose original is left untouched and still
playable, caption overrides are stored and produce a **distinct format
fingerprint**, a ZIP of the finished clips is built and its contents extracted
and probed, an over-limit export is refused, a cross-job and a nonexistent clip
are refused, and a cancelled job publishes nothing.

**On Windows, the child's stdout has to go to a file.** An anonymous pipe
holds about 4 KB, and a server that fills it blocks while writing its log —
which looks exactly like the server having hung. All five scripts send the
output to a file for that reason. Run them **one at a time**, never alongside
`python -m pytest`: `test_a_job_left_behind_by_a_dead_worker_is_recovered` is
timing-sensitive and fails under CPU contention.

---

## 7. What is not tested yet

- No end-to-end browser automation. The Phase 7 browser check was done by
  hand in a real browser against the running application, and it is still the
  only interactive evidence the UI has. No browser-testing framework is
  installed (Playwright, Cypress and Selenium are all absent), the built-in
  preview cannot attach on this machine, and none has been added without
  explicit authorisation. This gap was scheduled for Phase 9 and was **not**
  closed there; it is carried forward as a documented limitation rather than
  claimed as covered.
- **The responsive layout is checked in the browser, not in a test.** The
  test setup is jsdom, which has no layout engine, so a media query
  cannot be evaluated and an assertion about a width would mean nothing.
  The narrow-screen layout was verified by looking at it, and the
  horizontal overflow it revealed was fixed.
- No load or performance tests.
- The real-model suite runs on one model (`tiny.en`) and on locally
  synthesised speech. It says nothing about how `base.en` or `small.en`
  behave on real recordings, and no accuracy figure is claimed.
- The end-to-end check does not drive the browser. It proves the API, the
  worker, the database and the model; the UI is covered by component tests.
- A worker that is killed with `SIGKILL` leaves its inference child to
  notice the closed pipe on its own. The child does exit, and the automated
  test proves it, but the exit is not instantaneous.
- **The caption burn-in is checked by comparing frames, not by reading the
  text.** The proof that the captions are in the picture is that a captioned
  render differs from an uncaptioned one. Nothing checks *which* words are
  on screen; that would need OCR, and it is not claimed.
- **A font that is not installed is not detected at render time.** libass
  silently falls back to another font. The interface offers the fonts that
  are installed, so this is unlikely, but it is not enforced.
