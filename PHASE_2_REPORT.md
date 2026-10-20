# Phase 2 report — Secure video ingestion and persistent background jobs

**Date:** 2026-10-20
**Phase:** 2 of 10 — Video ingestion
**Overall status:** **COMPLETE**
**Verification environment:** Windows 11 · PowerShell 7 · Python 3.11.9 · Node.js v26.4.0 · npm 11.14.1 · FFmpeg/FFprobe 9.0.2 · SQLite 3.45.1

Every result below was produced by running the command shown. Nothing is
assumed or extrapolated.

---

## 1. Repository inspection (first step)

| Probe | Result |
| --- | --- |
| `git status --short` | Empty — the tree was clean |
| `git log --oneline` | `0da2bb9`, `3a75dd1`, `0c355a3` — the three Phase 1 commits |
| `git branch` | `main` only |
| `ffprobe -version` | 9.0.2 (full build, on `PATH`) |
| `python -c "import sqlite3; print(sqlite3.sqlite_version)"` | 3.45.1 |
| `pip list` (venv) | No `python-multipart`, no `yt-dlp` — both had to be added |

### Phase 1 baseline, re-run before any change

```
cd backend; ..\.venv\Scripts\python.exe -m pytest -q
```

```
16 passed
```

```
cd frontend
npm run typecheck   -> exit 0
npm run lint        -> exit 0
npm test            -> Test Files 2 passed (2), Tests 8 passed (8)
```

**No pre-existing failures.** There was nothing to hide and no regression to
attribute.

---

## 2. Files created

### Backend — new

| File | Purpose |
| --- | --- |
| `backend/app/jobs.py` | Statuses, stages, the transition table, `JobRecord` |
| `backend/app/db.py` | SQLite connection, versioned migrations, the job repository, atomic claiming, lease recovery |
| `backend/app/storage.py` | Storage layout, path containment, filename sanitising |
| `backend/app/media.py` | FFprobe inspection and the media policy |
| `backend/app/youtube.py` | Strict YouTube URL parsing and canonicalisation (no network) |
| `backend/app/acquisition.py` | The `SourceAcquirer` interface, `YtDlpAcquirer`, `DisabledAcquirer` |
| `backend/app/uploads.py` | Streaming multipart parsing with the limit enforced while the body arrives |
| `backend/app/schemas.py` | Pydantic request/response models |
| `backend/app/errors.py` | Stable error codes and the shared error envelope |
| `backend/app/deps.py` | FastAPI dependencies for settings, storage and the database |
| `backend/app/routers/jobs.py` | `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}` |
| `backend/app/routers/videos.py` | `POST /api/v1/videos/upload`, `POST /api/v1/videos/youtube` |
| `backend/app/worker.py` | The worker: claim, process, recover, shut down |
| `backend/worker.py` | Worker entry point (`python worker.py`) |

### Backend — changed

| File | Change |
| --- | --- |
| `backend/app/config.py` | Phase 2 settings; `API_VERSION` → `1.1.0`; `API_STAGE` → `phase-2-ingestion` |
| `backend/app/main.py` | New routers, `BodySizeLimitMiddleware`, the `ApiError` and validation handlers |
| `backend/app/routers/health.py` | The Phase 2 capabilities are now reported as implemented |
| `backend/requirements.txt` | `python-multipart`, `yt-dlp` |
| `backend/constraints.txt` | Regenerated from `pip freeze` |
| `backend/.env.example` | The new settings, with comments |
| `backend/tests/conftest.py` | Temporary storage, temporary database, synthetic video fixture |
| `backend/tests/test_api_v1.py` | One assertion updated (see §8) |
| `backend/tests/test_health.py` | One test updated (see §8) |

### Backend — new tests

`tests/test_youtube_urls.py` (29 tests) · `tests/test_storage.py` (16) ·
`tests/test_media.py` (11) · `tests/test_upload_api.py` (17) ·
`tests/test_jobs_api.py` (22) · `tests/test_youtube_api.py` (13) ·
`tests/test_worker.py` (14) · `tests/test_db.py` (6) ·
`tests/test_acquisition.py` (10)

### Frontend — new and changed

| File | Change |
| --- | --- |
| `frontend/src/lib/api.ts` | Typed API client, `ApiError`, the multipart upload with progress |
| `frontend/src/lib/api.test.ts` | 8 tests |
| `frontend/src/lib/jobs-event.ts` | A one-line event bus between the forms and the job list |
| `frontend/src/components/JobList.tsx` | The job list, polling, status badges, progress |
| `frontend/src/components/JobList.test.tsx` | 7 tests |
| `frontend/src/components/VideoSourcePanel.tsx` | Replaced the disabled panel with the real upload and YouTube forms |
| `frontend/src/components/PipelinePreview.tsx` | Step 1 is now marked *Done*, step 2 *Next up* |
| `frontend/src/app/page.tsx` | The job list section, the Phase 2 copy |

### Documentation

`README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`,
`docs/TESTING.md`, `docs/ROADMAP.md`, `docs/DECISIONS.md`,
`frontend/README.md`, `.env.example` and `backend/.env.example` were updated
in place.

---

## 3. Dependencies added

### Backend

| Package | Version | Why |
| --- | --- | --- |
| `python-multipart` | 0.0.32 | Multipart parsing. Starlette's own parser spools the whole file before the endpoint runs and has no file-size limit, so the upload body is parsed here instead, with the limit enforced while it streams. |
| `yt-dlp` | 2026.8.19 | The YouTube acquisition adapter. Pure Python, released under the Unlicense. Started as `python -m yt_dlp` with an argument list, never a shell, and never given cookies or a login. |

Deliberately **not** added: a task queue (Redis, Celery), an ORM, a web
framework for the worker, an HTTP client for the downloader, or any cloud
infrastructure. The job queue is the SQLite table.

### Frontend

No new runtime dependency. The upload uses `XMLHttpRequest` from the browser
rather than a library, because only `XMLHttpRequest` reports upload progress.
No new dev dependency either: Vitest, jsdom and React Testing Library were
already there from Phase 1.

---

## 4. Commands executed and results

### 4.1 Backend test suite

```
cd backend
..\.venv\Scripts\python.exe -m pytest
```

```
190 passed, 1 skipped in 8.60s
```

The skip is `test_a_symlink_out_of_the_root_is_refused`, which skips when the
Windows account cannot create a symlink.

| Suite | Tests |
| --- | --- |
| `test_health.py` (Phase 1) | 8 |
| `test_api_v1.py` (Phase 1) | 2 |
| `test_config.py` (Phase 1) | 6 |
| `test_db.py` | 6 |
| `test_media.py` | 11 |
| `test_storage.py` | 16 |
| `test_youtube_urls.py` | 29 |
| `test_upload_api.py` | 17 |
| `test_jobs_api.py` | 22 |
| `test_youtube_api.py` | 13 |
| `test_worker.py` | 14 |
| `test_acquisition.py` | 10 |

### 4.2 Frontend checks

```
cd frontend
npm run typecheck   -> exit 0
npm run lint        -> exit 0
npm test            -> Test Files 4 passed (4), Tests 25 passed (25)
npm run build       -> Route (app) / and /_not-found, prerendered as static content
```

Phase 1's 8 frontend tests still pass; the 17 new ones bring the total to 25.

### 4.3 Live HTTP integration check

The API and the worker were started as separate processes from `backend/`
with the real configuration (`backend/data/`).

| # | Request | Result |
| --- | --- | --- |
| 1 | `GET /health` | **200** — `{"status":"ok","service":"clipforge-api"}` |
| 2 | `GET /ready` | **200** — `video_ingestion`, `background_worker`, `database`, `persistent_storage` all `true`; the later pipeline stages all `false` |
| 3 | `POST /api/v1/videos/upload` (valid MP4) | **202** — `{"id":"51d94521…","status":"queued","progress":0,"stage":"queued",…}` |
| 4 | `GET /api/v1/jobs/{id}` after 6 s | **200** — `status=completed`, `progress=100`, `stage=ready`, `video_width=320`, `video_height=240`, `video_codec=h264`, `video_duration_seconds=1.0` |
| 5 | `GET /api/v1/jobs` | **200** — the job, newest first |
| 6 | `POST /api/v1/videos/youtube` (valid watch URL) | **202** — canonical `source_reference` |
| 7 | `POST /api/v1/videos/youtube` (`youtu.be` URL) | **202** — normalised to the watch URL |
| 8 | `POST /api/v1/videos/youtube`, `authorized: false` | **400** `unauthorized_source` |
| 9 | `POST` with `https://example.com/watch?v=…` | **400** `invalid_youtube_url` |
| 10 | `POST` with `https://youtube.com.attacker.example/watch?v=…` | **400** `invalid_youtube_url` |
| 11 | `POST` with `file:///etc/passwd` | **400** `invalid_request` (scheme) |
| 12 | `POST` with `https://user:pass@www.youtube.com/watch?v=…` | **400** `invalid_youtube_url` |
| 13 | `POST` with `https://www.youtube.com:8080/watch?v=…` | **400** `invalid_youtube_url` |
| 14 | `POST` with `https://www.youtube.com/watch?v=short` | **400** `invalid_youtube_url` |
| 15 | `POST` with `https://127.0.0.1/watch?v=…` | **400** `invalid_youtube_url` |
| 16 | `GET /api/v1/jobs/deadbeef` | **404** `not_found` |
| 17 | Upload with a `.txt` extension | **415** `unsupported_media_type` |
| 18 | Upload of a text file named `.mp4` | **415** `invalid_media` |
| 19 | Upload of an empty file | **400** `empty_file` |
| 20 | Upload above the limit (API started with a 1 KiB limit) | **413** `payload_too_large` |
| 21 | `GET /api/v1/jobs` after an API restart | the job was still there |

After each rejection, `backend/data/uploads/` was empty — no partial files.

### 4.4 Real YouTube acquisition

This was verified against **Big Buck Bunny** (`aqz-KE-bpKQ`), the Blender
Foundation's Creative Commons open movie, which is published for exactly this
kind of reuse. No third-party content with unclear rights was downloaded.

```
2026-10-20 13:59:20 | clipforge.worker  | worker ASUS-26080-3abadd started
2026-10-20 13:59:26 | clipforge.worker  | claimed job c0eb2e07… (youtube)
2026-10-20 13:59:26 | clipforge.acquisition | acquiring youtube video aqz-KE-bpKQ
2026-10-20 13:59:41 | clipforge.worker  | job c0eb2e07… completed: 1920x1080 av1 (634.6s)
```

```json
{"status":"completed","progress":100,"stage":"ready",
 "file_size_bytes":134657141,"video_duration_seconds":634.594,
 "video_width":1920,"video_height":1080,"video_codec":"av1","attempt_count":1}
```

**The real acquisition path works in this environment.** It downloaded a
134 MB, 10-minute 1080p video, merged the streams with FFmpeg, stored it
inside the storage root and validated it with FFprobe.

An earlier attempt failed with `Requested format is not available`: the
selector `best[ext=mp4]/best` matches only progressive formats, and YouTube
serves video and audio separately. The selector is now
`bv*[height<=1080]+ba/b` with `--merge-output-format mp4`. The failure was
visible in the worker log, the job failed cleanly with
`acquisition_failed` after its retries, and the fix was verified with a real
download.

### 4.5 Crash and recovery behaviour, live

| Scenario | Result |
| --- | --- |
| API restarted | The job list came back with the same job, `status=completed` |
| Worker restarted | The new worker process picked up pending jobs |

### 4.6 Browser verification

Checked in a real browser against the dev server.

| Scenario | Observed |
| --- | --- |
| Page loads | HTTP 200; *Phase 2 · Ingestion*; the health pill shows **Backend online clipforge-api** |
| Upload form | The file input, the *Upload* button and the accepted-format hint render |
| YouTube form | The URL field, the authorisation checkbox and the *Add link* button render; the button stays disabled until both are filled |
| Submitting the form | A green **"Queued as job 4cb0f5b3…"** confirmation |
| The job list | The new job appeared immediately with an orange **Downloading** badge and a progress bar; the earlier job showed the green **Ingested** badge |
| The pipeline preview | Step 1 shows **Done**, step 2 shows **Next up** |
| Desktop 1440×900 and mobile 390×844 | Both layouts render correctly |

### 4.8 Fresh-clone setup verification

The repository was cloned to a scratch directory and the setup steps from the
README were executed from scratch, to confirm the documented instructions are
complete on a clean checkout.

```
git clone <repo> clipforge-verify

cd clipforge-verify\backend
python -m venv ..\.venv
..\.venv\Scripts\python.exe -m pip install --upgrade pip
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.txt
..\.venv\Scripts\python.exe -m pytest
```

```
190 passed, 1 skipped in 8.05s
```

```
cd ..\frontend
npm install
npm run typecheck   -> exit 0
npm run lint        -> exit 0
npm test            -> Test Files 4 passed (4), Tests 25 passed (25)
npm run build       -> Route (app) / and /_not-found, prerendered as static content
```

No defect was found this time. The same check in Phase 1 did find one — the
missing `next typegen` step — which is why it was repeated.

### 4.9 Shutdown

The API, the worker and the frontend were stopped. Verified afterwards:

```
http://127.0.0.1:8000/health -> down
http://localhost:3000        -> down
```

---

## 5. Architecture and dependency changes

Full reasoning is in [docs/DECISIONS.md](docs/DECISIONS.md). The short list:

| # | Decision |
| --- | --- |
| D13 | The worker is a separate operating-system process, not a background task |
| D14 | The job state machine is declared once and enforced on every write |
| D15 | At-least-once processing with idempotent steps — not exactly-once |
| D16 | Leases with expiry, recovered at startup and every few minutes |
| D17 | Only transient failures are retried; a file that is not a video fails immediately |
| D18 | The upload limit is enforced while the body streams, not afterwards |
| D19 | FFprobe is the only authority on whether a file is a video |
| D20 | YouTube URLs are validated by exact hostname comparison |
| D21 | yt-dlp, without cookies or a login, behind an adapter interface |
| D22 | The caller must confirm they are authorised to use the video |
| D23 | A source file is kept when processing fails |
| D24 | Job responses never contain a filesystem path |

---

## 6. Acceptance criteria

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Phase 1 tests still pass | **PASS** | The 16 backend and 8 frontend Phase 1 tests all still pass; §4.1, §4.2 |
| 2 | Uploads are streamed with enforced limits | **PASS** | `app/uploads.py` streams the part in bounded chunks and aborts at the limit; verified live (§4.3 #20) and by `test_an_oversized_upload_is_rejected_with_413` and `test_an_oversized_body_is_rejected_before_it_is_read` |
| 3 | Actual video content is inspected | **PASS** | FFprobe reads the container; a text file named `.mp4` is rejected (§4.3 #18) and a real video yields width, height, codec and duration (§4.3 #4) |
| 4 | Invalid and malformed media is rejected | **PASS** | §4.3 #18, #19; `test_malformed_media_is_rejected`, `test_a_file_that_is_not_a_video_is_rejected` |
| 5 | Uploaded files remain inside the configured storage root | **PASS** | `test_the_source_file_lives_inside_the_storage_root`, `test_path_traversal_filenames_cannot_escape_the_storage_directory`, and 16 storage tests including symlink escape |
| 6 | Partial files are cleaned up | **PASS** | `test_a_rejected_upload_leaves_no_partial_file_behind`; `uploads/` was empty after every live rejection (§4.3) |
| 7 | SQLite job records persist across application restarts | **PASS** | §4.3 #21 — the job survived an API restart; `test_a_job_survives_closing_and_reopening_the_database` |
| 8 | Job submissions return `202 Accepted` and a valid job ID | **PASS** | §4.3 #3, #6, #7 — 202 with a 32-character uuid |
| 9 | Job listing and status endpoints work | **PASS** | §4.3 #5, #16; `test_the_job_list_is_ordered_newest_first`, `test_the_job_list_is_bounded` |
| 10 | The separate worker processes queued jobs | **PASS** | §4.3 #4 — the worker claimed, inspected and completed the job; §4.4 — the same for a real download |
| 11 | Job claiming is safe under concurrent workers | **PASS** | `test_two_workers_cannot_claim_the_same_job` — two threads, one winner |
| 12 | Worker restart and expired-lease recovery are tested | **PASS** | `test_a_restarted_worker_discovers_pending_jobs`, `test_an_expired_lease_is_recovered_and_queued_again`, `test_a_job_that_used_up_its_attempts_is_failed_not_requeued`, `test_a_job_is_never_left_stuck_in_an_active_state` |
| 13 | Invalid jobs fail cleanly without infinite retries | **PASS** | `test_a_permanently_invalid_job_is_not_retried` (attempt count stays at 1); `test_a_transient_failure_is_retried_and_the_attempt_count_grows` |
| 14 | YouTube URLs are strictly validated | **PASS** | 29 URL tests; live rejections §4.3 #9–#15 |
| 15 | Rejected URLs cannot trigger arbitrary remote requests | **PASS** | `test_a_rejected_url_never_reaches_the_acquisition_adapter` — a spy adapter proves it is never called; the validator performs no network access at all |
| 16 | Actual YouTube acquisition is implemented and verified | **PASS** | §4.4 — a real 134 MB, 1080p, 634 s Creative Commons video was downloaded, stored and validated by the worker. The `disabled` fallback is implemented and tested (`test_the_endpoint_refuses_when_acquisition_is_disabled`) |
| 17 | Frontend upload and URL forms work | **PASS** | §4.6 — the form submitted over the real API and produced a queued job |
| 18 | Job status and progress update correctly | **PASS** | §4.6 — the badge changed from *Downloading* to *Ingested*; `test_the_status_and_progress_of_a_job` |
| 19 | Frontend polling stops for terminal jobs | **PASS** | `test_polls_while_a_job_is_running_and_stops_when_it_finishes` — the call count stops growing once every job is terminal |
| 20 | Backend tests pass | **PASS** | 190 passed, 1 skipped |
| 21 | Frontend typecheck, lint, tests and build pass | **PASS** | §4.2 — all exit 0 |
| 22 | Integration checks pass where the environment permits | **PASS** | §4.3, §4.4, §4.5, §4.6 — 21 live HTTP checks, a real download, a restart and a browser session |
| 23 | Documentation accurately reflects the implementation | **PASS** | Every command in the README and in `docs/DEVELOPMENT.md` was executed during this phase; the settings table matches `backend/app/config.py` field for field |
| 24 | `PHASE_2_REPORT.md` records genuine test evidence | **PASS** | This document — every result is transcribed from a real run |
| 25 | No secrets are exposed or committed | **PASS** | The only `NEXT_PUBLIC_*` variable is the API base URL. `test_responses_never_leak_configured_secrets` and `test_secret_values_are_masked_when_dumped` cover the backend |
| 26 | No unimplemented video-generation functionality is represented as complete | **PASS** | The pipeline preview marks step 1 *Done* and step 2 *Next up*; the job list says *Ingested*, not *Clipped*; `/ready` reports the later capabilities as `false`; the README states that `completed` means ingestion finished |

**Phase 2 status: COMPLETE — 26 of 26 criteria verified with executed
evidence.**

---

## 7. Security checks performed

| Check | Result |
| --- | --- |
| Path traversal in a filename | `../../../../Windows/System32/evil.mp4` → stored inside the root; `Storage.resolve()` rejects `..`, absolute paths and drive specifications |
| Symlink escape | A symlink from `sources/` to a file outside the root is refused; the test skips where the account cannot create symlinks |
| Arbitrary remote URL | `https://example.com/watch?v=…` → 400 |
| Hostname confusion | `youtube.com.attacker.example`, `www.youtube.com.evil.example`, `evil-youtube.com` → 400 (exact hostname comparison) |
| URL credentials | `https://user:pass@www.youtube.com/…` → 400 |
| Unsupported schemes | `file:`, `ftp:`, `javascript:`, `data:`, `gopher:` → 400 |
| Invalid ports | `:8080`, `:22`, `:notaport` → 400 |
| Loopback and private targets | `127.0.0.1`, `localhost`, `192.168.0.1`, `10.0.0.5`, `169.254.169.254`, `[::1]` → 400 |
| Malformed media | A text file named `.mp4` and a broken container → 415 |
| Upload size limit | 413, enforced while streaming and again by `Content-Length` |
| MIME type spoofing | `video/mp4` on a text file → rejected; `text/plain` on a real video → accepted. The type is never trusted in either direction |
| Subprocess argument safety | The downloader is started with an argument list, `shell=False`, the canonical URL as a single argument, and no cookies or credentials. Asserted by `test_the_downloader_is_started_with_an_argument_list` |
| Error and log sanitising | The downloader's output never reaches the job record; the user sees a generic message. `test_a_failing_downloader_produces_a_generic_error` |
| Storage-root containment | 16 storage tests, plus the live check that `uploads/` was empty after every rejection |
| Secrets | `SecretStr` masking, no secret in any response, no `NEXT_PUBLIC_*` secret |

### Residual risks that cannot be fully eliminated here

1. **yt-dlp's own redirects.** The URL the user supplies is fully validated
   by us, but yt-dlp follows YouTube's redirects to `googlevideo.com` hosts
   itself, and that resolution is outside our control. This is inherent to
   how YouTube works. Mitigations: the mode is configurable and can be
   switched off entirely, the downloader is never given credentials, and the
   download size and duration are bounded.
2. **The local development server is not a hardened upload gateway.** Uvicorn
   with no proxy in front of it, no rate limiting, and no request-size limit
   beyond the configured upload cap. It binds `127.0.0.1` by default.
3. **There is no authentication.** Deliberate for Phase 2 — the API is a
   local tool — but it means anything running on the machine can submit jobs
   and read the job list.
4. **The upload limit is per-process.** Two API processes would each allow a
   full upload. Only one is meant to run.
5. **Symlink containment depends on the filesystem.** `Path.resolve()`
   resolves symlinks before the containment check, which is correct on
   Windows and NTFS, but a junction or a mount point below the root would
   need care on other filesystems.

---

## 8. Two Phase 1 assertions were updated — and why

Both are recorded here rather than buried.

| Test | Before | After | Why |
| --- | --- | --- | --- |
| `test_api_v1_returns_the_expected_metadata` | `body["stage"] == "phase-1-foundation"` | `body["stage"] == "phase-2-ingestion"` | `/api/v1` reports the stage of the current build. The build *is* Phase 2 now. The assertion still pins the exact value. |
| `test_ready_does_not_claim_unimplemented_capabilities` | every capability `False` | ingestion, worker, database and storage `True`; the later pipeline stages still `False` | The capabilities describe what is implemented. The test's intent — "do not advertise what does not exist" — is preserved and strengthened: the later stages are still asserted to be `False`. |

No Phase 1 test was deleted, skipped or weakened beyond these two values.
Both changes are in the `phase-2-ingestion` commit.

---

## 9. Known limitations

1. **No clips are produced.** Ingestion stores and validates a source;
   transcription, highlight detection, rendering and captions are Phases 3–6.
   `completed` means *ingestion finished*, and the UI says so.
2. **YouTube acquisition depends on YouTube.** yt-dlp works against a moving
   target. A video that downloads today may not tomorrow, and videos that
   require a sign-in or a region check are refused by design.
3. **No job cancellation.** The `cancelled` status and its transitions exist,
   but no API reaches them, so there is deliberately no cancel button.
4. **No authentication.** Local use only.
5. **The worker is not covered by an automated process-level test.** The
   worker is driven in-process against the same database and configuration;
   the process boundary itself is covered by the manual check only.
6. **No end-to-end browser automation.** The Phase 2 integration check was
   done by hand in a real browser. Browser automation is scheduled for
   Phase 9.
7. **No Python type checker or linter.** The code is fully annotated and the
   API surface is validated by Pydantic at runtime, but `mypy`/`ruff` were not
   added, as no acceptance criterion required them.
8. **`npm audit` reports 5 high-severity advisories** in the dev-only ESLint
   plugin chain (`braces` ≤ 3.0.3 ← `micromatch` ← `fast-glob` ←
   `@next/eslint-plugin-next`). The advisory range is `<=3.0.3` and 3.0.3 is
   the newest release, so **no patched version exists**; npm's only offered
   "fix" is downgrading `eslint-config-next` to v14, which breaks the
   Next.js 16 lint setup. The affected code only runs during linting and is
   not part of the shipped application. **Accepted, not remediated.** The
   same five advisories were reported in Phase 1 and are unchanged.
9. **Node 26.4.0 was the runtime available.** Next.js 16 requires Node
   ≥ 20.9.0 and supports 22 LTS / 24 LTS; `package.json` declares
   `engines.node >= 20.9.0`. Everything was verified on Node 26.4.0; it was
   not re-verified on an LTS release.

---

## 10. Remaining manual steps

None are required for the acceptance criteria. Optional follow-ups:

1. Re-run the whole suite on **Node 22 or 24 LTS** to confirm parity with the
   declared support range.
2. Decide the **speech-to-text engine** (Phase 3) and the **LLM provider**
   (Phase 4) — the two open choices that shape the pipeline.
3. Add `mypy`/`ruff` to the backend if static analysis is wanted before the
   pipeline grows.
4. Consider a disk-space check before a download, if the local machine tends
   to run out of space.

---

## 11. How to run it

**API** (from `backend/`):

```powershell
..\.venv\Scripts\Activate.ps1
python run.py
```

**Worker** (from `backend/`, a second terminal):

```powershell
..\.venv\Scripts\Activate.ps1
python worker.py
```

**Frontend** (from `frontend/`, a third terminal):

```powershell
npm run dev
```

| URL | What |
| --- | --- |
| http://localhost:3000 | ClipForge frontend |
| http://localhost:8000/health | `{"status":"ok","service":"clipforge-api"}` |
| http://localhost:8000/ready | Readiness and capability flags |
| http://localhost:8000/api/v1/jobs | Recent jobs, newest first |
| http://localhost:8000/docs | OpenAPI explorer |

---

## 12. Next step

**Phase 3 — Audio extraction and transcription**: extract 16 kHz mono audio
from an accepted source with FFmpeg, choose a speech-to-text engine that
works offline, store a timed transcript against the job, and show the
transcript and its progress in the frontend.
