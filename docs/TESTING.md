# Testing

Two independent suites, one per application. Neither needs an API key, an
internet connection, a GPU or a video file.

| Suite | Runner | Location | Needs the other app running? |
| --- | --- | --- | --- |
| Backend | pytest + httpx (`TestClient`) | `backend/tests/` | No |
| Frontend unit + component | Vitest + React Testing Library | `frontend/src/**/*.test.ts(x)` | No |

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
| `not_a_video` | A file that claims to be an MP4 but is not a media file |

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

### What each test file covers

**`tests/test_health.py`** — the Phase 1 probes, kept intact:

| Test | Verifies |
| --- | --- |
| `test_health_returns_200` | `GET /health` answers 200 |
| `test_health_returns_the_exact_contract` | The body is exactly `{"status": "ok", "service": "clipforge-api"}` |
| `test_ready_reports_a_loaded_configuration` | `GET /ready` answers 200 with `status=ready` and `config=loaded` |
| `test_ready_does_not_claim_unimplemented_capabilities` | The Phase 2 capabilities are true and the later ones are still false |
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
between tests. It runs in well under ten seconds.

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
stopping once every job is terminal**, and the manual refresh.

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

These are not automated because they need two live processes. They were run
manually in Phase 2; see [PHASE_2_REPORT.md](../PHASE_2_REPORT.md) for the
recorded results.

Start the API and the worker, then:

```powershell
# Backend liveness
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing | Select-Object -ExpandProperty Content
# -> {"status":"ok","service":"clipforge-api"}

# Backend readiness
Invoke-WebRequest http://127.0.0.1:8000/ready -UseBasicParsing | Select-Object -ExpandProperty Content

# Queue a video
Invoke-WebRequest http://127.0.0.1:8000/api/v1/videos/upload -Method Post `
  -Form @{ file = Get-Item .\sample.mp4 } -UseBasicParsing | Select-Object -ExpandProperty Content
# -> 202 with a job id

# Watch the worker pick it up
Start-Sleep -Seconds 5
Invoke-WebRequest http://127.0.0.1:8000/api/v1/jobs -UseBasicParsing | Select-Object -ExpandProperty Content

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
  the badge changes to **Ingested** when the worker is done;
- stopping the API and reloading shows **Backend unreachable** with a
  *Retry* button.

---

## 5. Network-dependent tests

**There are none in the normal suite.** Every YouTube test uses a mocked
acquisition adapter or a recording double, so `python -m pytest` never
touches the network.

Verifying the real download path is a manual, opt-in step: it needs a
network connection and a video you are allowed to download. Use a
Creative Commons or public-domain video — never third-party content you have
no rights to. The Phase 2 report records one such manual check.

---

## 6. What is not tested yet

- No end-to-end browser automation. The Phase 2 integration check was done by
  hand in a real browser; adding a browser-testing framework is planned for
  Phase 9.
- No tests for transcription, LLM calls, rendering or captions — none of that
  exists yet.
- No load or performance tests.
- No test that runs the API and the worker as separate OS processes. The
  worker is driven in-process against the same database and configuration, so
  the process boundary itself is covered by the manual check only.
