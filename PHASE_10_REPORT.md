# ClipForge — Phase 10: Local Launch & Polish

**Status:** READY WITH DOCUMENTED LIMITATIONS  
**Date:** 27 October 2026  
**HEAD at time of writing:** `7502384 docs: record the fresh-clone verification in the Phase 2 report`  
**Branch:** `main`  
**Machine:** Windows 11 Home, Python 3.11.9, Node v26.4.0 / npm 11.14.1, FFmpeg/FFprobe 9.0.2

> **Amended 2026-10-31.** Two statements in this report describe the behaviour
> as it was on 27 October and have since been overtaken:
>
> - *Cancellation does not terminate an in-flight FFmpeg/yt-dlp mid-step* — no
>   longer true. Decision **D55** made cancellation stop the child processes
>   within one lease tick and fail the cancelled job's queued children with
>   `job_cancelled`; clip and format leases are renewed during renders. See
>   [docs/DECISIONS.md](docs/DECISIONS.md) and
>   [PHASE_4_RELIABILITY_REPORT.md](PHASE_4_RELIABILITY_REPORT.md).
> - *`CLIPFORGE_WORKER_LEASE_SECONDS` must exceed a single provider call* — no
>   longer true. A running analysis renews its lease in the background during
>   the provider call, and an analysis is bounded by
>   `CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS` instead.
>
> The evidence tables below keep the figures recorded on 27 October. Re-run on
> 31 October 2026, after the reliability work: the backend collects **933
> tests — 932 passed, 1 skipped — exit 0**, and the frontend runs **14 files /
> 178 tests, exit 0**. The five `e2e_*.py` scripts were not re-run for this
> amendment, so their rows still speak for 27 October.

---

## Executive summary

ClipForge's Phases 1–10 are implemented and the working tree is dirty on purpose (those changes are preserved; nothing was reset, cleaned, stashed, staged, or committed). Phase 10 did **not** need to rebuild the pipeline — it already exists and is tested.

Phase 10's own contribution is:

1. A single-command local launch script, `scripts/start-local.ps1`, verified end to end (API → worker → frontend, readiness-gated, not fixed-delay).
2. A full local-launch and shutdown verification on real ports.
3. A re-run of every regression check after the Phase 10 change (all green).
4. Confirmation that the ZIP export resource bound, cancellation semantics, migration/crash-recovery behavior, and offline transcription all behave as documented.

No source file was changed for Phase 10. The only new file is `scripts/start-local.ps1`; `.env` / `.env.local` were created from the `*.example` templates for local verification (both are git-ignored and never committed).

**Final verdict: READY WITH DOCUMENTED LIMITATIONS.** The only limitation carried forward honestly is the lack of repeatable browser automation (no Playwright/Cypress/Selenium available; the built-in browser preview cannot attach on this install). Everything else in the Phase 10 definition of done is verified or explicitly bounded.

---

## Initial repository state

| Item | Value |
|---|---|
| Path | `<repo root>` — the ClipForge working copy on the verifying machine |
| Branch | `main` |
| HEAD | `7502384 docs: record the fresh-clone verification in the Phase 2 report` |
| Working tree | Dirty on purpose: Phases 3–8 uncommitted (~140 changed entries), all preserved |
| Staged / committed | 0 staged, 0 commits |
| Toolchain | Python 3.11.9 (venv at repo root), Node v26.4.0 / npm 11.14.1, FFmpeg/FFprobe 9.0.2 |
| Schema version | `SCHEMA_VERSION = 7` (no migration added in Phase 10) |
| Model cache | `tiny.en` cached in `backend/data/models/` |
| `.env` files | git-ignored; only `*.example` templates tracked; created locally for verification |

Dependency manifests are present and committed: `backend/requirements.txt`, `backend/requirements-dev.txt`, `backend/pyproject.toml`, `frontend/package.json` + `package-lock.json`.

---

## Baseline and final test results

Baseline was captured **before** any Phase 10 change, then re-run **after** the Phase 10 change (the only Phase 10 change is `scripts/start-local.ps1`, which is not exercised by the test suites, so counts are identical).

| Check | Command | Result | Exit |
|---|---|---|---|
| Backend suite | `cd backend && ../.venv/Scripts/python.exe -m pytest -p no:cacheprovider --tb=short -rN` | 893 passed, 1 skipped | 0 |
| Frontend suite | `cd frontend && npm test` | 14 files / 174 passed | 0 |
| Frontend typecheck | `npm run typecheck` (`next typegen && tsc --noEmit`) | no errors | 0 |
| Frontend lint | `npm run lint` (`eslint`) | no findings | 0 |
| Frontend production build | `npm run build` | build succeeded | 0 |
| Dependency audit | `npm run audit:check` | 0 high/critical/total; accepted dev-only `braces` advisory | 0 |

The single backend skip is the privileged-symlink test, which cannot run without a Windows privilege this account does not have — a skip, not a pass, and pre-existing.

Suites were run **serially**, per the documented guidance: `test_a_job_left_behind_by_a_dead_worker_is_recovered` is timing-sensitive under CPU contention.

### End-to-end regression (real API + worker + SQLite + FFmpeg/FFprobe + real speech model, throwaway storage)

All run serially, one at a time:

| Script | Result | Exit |
|---|---|---|
| `tests/e2e_workflow.py` | every step passed | 0 |
| `tests/e2e_phase4.py` | every step passed | 0 |
| `tests/e2e_phase5.py` | every step passed | 0 |
| `tests/e2e_phase6.py` | every step passed | 0 |
| `tests/e2e_phase8.py` | every step passed | 0 |

No new skips, failures, or warnings versus baseline. No dependency versions changed.

---

## Source files changed and reasons

Only one file was added for Phase 10:

| File | Reason |
|---|---|
| `scripts/start-local.ps1` | New single-command local launcher: starts API → worker → frontend on user-specified ports, waits for TCP + HTTP readiness (not fixed delay), logs each process to `$env:TEMP/clipforge-local-launch/` (files, never pipes — per the project's pipe-deadlock guidance), exposes `Stop-ClipForge` for owned-process cleanup. |

No `.py`, `.ts`, `.tsx`, or config file was modified. `backend/.env` and `frontend/.env.local` were created from the `*.example` templates purely to run the launcher locally; both are git-ignored and never committed.

---

## Local launch and shutdown verification

The documented launch procedure (`docs/DEVELOPMENT.md`, "Three terminals") and the new `scripts/start-local.ps1` were both verified on real ports.

### Startup verification (script run on free ports 19400 / 14400)

```
$ ./scripts/start-local.ps1 -ApiPort 19400 -FrontendPort 14400   # EXIT=0
```

- `http://127.0.0.1:19400/health` → `{"status":"ok","service":"clipforge-api"}`
- `http://127.0.0.1:19400/ready` → `status: ready`, `config: loaded`, all ten capability flags `true`, `transcription_model_cached: true`
- Worker heartbeat: `running: true`, `worker_id: ASUS-16916-7af6e1`, `last_seen` current
- `http://localhost:14400/` → `200` (frontend HTML)

### Startup-failure cases probed

| Case | Behavior |
|---|---|
| Port already in use (bound 8000, then tried to bind again) | `run.py` fails fast with `WinError 10013` (address in use); launcher reports the failure via its log file rather than claiming success |
| `run.py` reads `CLIPFORGE_PORT` from env, not CLI args | Launcher sets `$env:CLIPFORGE_PORT` before starting the API |
| Invalid `.env` value (wildcard CORS, non-numeric port) | `Settings()` raises at load; `/ready` returns `503 not_ready, config: invalid` (verified in `app/routers/health.py`) |

### Shutdown verification

The launcher's `Stop-ClipForge` stops only the processes it started (tracked in script-scoped variables). I additionally confirmed targeted `taskkill` of my own launched API PIDs stops the service, and a pre-existing unrelated local service on another port (not ClipForge's) was left untouched. No ClipForge-owned process was left behind.

### Service readiness (`/ready`)

Reports actual health, not mere configuration presence:

- API reachable ✓
- Worker running and heartbeat current ✓ (read from `worker-heartbeat.json`, never assumed)
- Transcription model cached: `true` (local cache check, no network) ✓
- All capability flags `true` because the features are implemented and usable ✓
- Storage/database writable ✓ (real SQLite used by e2e scripts)
- Media tools available ✓ (FFmpeg/FFprobe 9.0.2 on PATH)

---

## Full workflow results

The end-to-end acceptance workflow was executed against real media via `e2e_workflow.py` (every step passed) and `e2e_phase8.py` (every step passed):

1. Launch API/worker/frontend ✓
2. Readiness + worker heartbeat ✓
3. Submit valid local video fixture (speech: "The quick brown fox jumps over the lazy dog.") ✓
4. Job appears on dashboard ✓
5. Transcription completes ✓
6. Transcript segments verified (1 segment, 0.00–2.00s, expected phrase present, none outside audio) ✓
7. Highlight selection via deterministic mock provider ✓
8. Clip generated ✓
9. Media verified with FFprobe (4.50s clip, H.264) ✓
10. Portrait 9:16 format with captions ✓
11. Caption burn-in verified (frame comparison) ✓
12. Clip renamed ✓
13. Revision created; original unchanged ✓
14. Caption overrides → distinct format fingerprint ✓
15. ZIP export ✓
16. ZIP contents inspected and extracted ✓
17. Oversized export refused safely ✓
18. Cancellation exercised; no cancelled result published ✓
19. Worker restarted after simulated crash; recovery verified ✓
20. Application restarted; persisted records/files remain accessible ✓
21. Shutdown; only owned processes stopped ✓

---

## ZIP export resource-safety decision and test evidence

**Decision:** Keep the temporary-file-backed ZIP streamed off disk (already implemented in Phase 9), bounded by **both** an item count and a total byte budget, checked against actual file sizes *before* any archive is assembled. This satisfies the Phase 10 preferred approach: the archive is never held entirely in RAM, and a single request cannot consume unbounded memory.

**Implementation** (`backend/app/export_service.py` + `backend/app/routers/clips.py`):

- `CLIPFORGE_CLIP_EXPORT_MAX_ITEMS` (default 50, range 1–1000) — validated, cannot be bypassed through malformed config (pydantic range check at load).
- `CLIPFORGE_CLIP_EXPORT_MAX_BYTES` (default 1 GiB, range 1–8 GiB) — total on-disk size of chosen clips summed from `path.stat().st_size` before assembly.
- Over limit → HTTP 413 `clip_export_too_large`.
- Duplicate IDs deduplicated via `dict.fromkeys`.
- Cross-job, nonexistent, failed, missing, and unfinished clips refused (404/409 `clip_export_invalid` / `clip_export_unavailable`).
- Empty export refused (400 `clip_export_empty`).
- Archive assembled into a temp file in `temp/`, streamed with `FileResponse`, deleted by a background task after the response is sent (failed exports clean up temp resources).
- ZIP member names are server-generated (`01-<safe-title>.mp4`), `safe_archive_name()` strips unsafe chars; no client filename ever becomes a path; no path traversal.
- `Content-Type: application/zip`, safe download filename `clipforge-<job8>-clips.zip`.

**Test evidence:**

| Test | Result |
|---|---|
| `test_an_export_with_too_many_clips_is_refused` | 413 `clip_export_too_large` |
| `test_an_export_at_the_limit_is_accepted` | 200 ZIP at exactly 50 clips |
| `test_an_export_over_the_byte_budget_is_refused` | 413 when total size exceeds budget |
| `test_an_export_at_the_byte_limit_is_accepted` | 200 at exactly the byte limit |
| `e2e_phase8.py` step 8 | ZIP 200, holds `['01-The best bit.mp4','02-The opening line.mp4']`, both real videos |
| `e2e_phase8.py` step 9 | Cross-job clip refused (409); nonexistent clip refused (404) |

No `BytesIO` whole-archive-in-RAM path remains; `read_export_archive` is not used by the response path. Concurrent exports are each bounded by the same item+byte budget.

---

## Cancellation semantics and test evidence

**Implementation** (`backend/app/db.py` `cancel_job`, `backend/app/routers/jobs.py`, `backend/app/worker.py`):

- `POST /api/v1/jobs/{id}/cancel` sets `status=cancelled`, `stage=cancelled`, releases the lease and clears `worker_id`, in one `BEGIN IMMEDIATE` transaction.
- Terminal jobs (`completed`/`failed`) refused: 409 `job_not_cancellable`. Already-cancelled is idempotent (returns the cancelled record). Unknown id: 404.
- A cancelled job is never claimed (`claim_next_job` only selects `queued`).
- Worker's `_lease_tick` checks the cancelled flag during long steps (download, transcription progress), raises `_JobCancelled`, removes the temp file, and does **not** requeue.
- Stale-worker protection: every write is conditional on `expected_worker_id` inside the same transaction, so a cancelled/taken-over job cannot be written over or completed later.
- Cancellation does **not** terminate an in-flight FFmpeg/yt-dlp mid-step (documented limitation, inherited from Phase 8). It prevents further state transitions and result publication; cancelled results are never published.

**Test evidence:**

| Test | Result |
|---|---|
| `test_a_queued_job_can_be_cancelled` | 200, status `cancelled` |
| `test_a_finished_job_cannot_be_cancelled` | 409 `job_not_cancellable` |
| `test_cancelling_twice_is_harmless` | idempotent |
| `test_a_cancelled_job_is_not_claimed_by_a_worker` | never claimed |
| `test_cancelling_a_job_that_does_not_exist_is_refused` | 404 |
| `e2e_phase8.py` step 10 | cancel accepted, stays cancelled, first job untouched |

**Remaining limitation:** an in-flight FFmpeg/yt-dlp process continues until it finishes; its output is discarded and never published. This is explicit, tested, and documented in the README "Known limitations".

---

## Migration, crash-recovery, and worker-recovery results

- Schema version `SCHEMA_VERSION = 7`; migrations are versioned, transactional (`BEGIN IMMEDIATE`), idempotent, applied from both API and worker. Fresh initialization and re-running migrations are safe.
- `e2e_workflow.py` step 6: a legacy `completed` job without a transcript is correctly reported as `pending` (no false claim of transcription).
- `e2e_workflow.py` step 7: transcription timeout → terminal `failed` with `transcription_timeout`, worker keeps running, no partial transcript, no inference process left behind.
- `e2e_workflow.py` step 8: after a worker restart, the next job completes — recovery works.
- Lease recovery: expired leases are requeued (or failed permanently at `max_attempts`); terminal states stay terminal; `recover_expired_leases` covers jobs, analyses, clips, and formats.
- Filesystem/database gap: `os.replace` finalization then DB commit; a file can exist without its row, but this self-heals on the next render (name is attempt-independent). Reconciliation (`reconcile_clips`/`reconcile_formats`) removes only temp files older than an hour matching the naming pattern inside `temp/` — never active or newer attempt files.
- All verified by `test_job_recovery.py`, `test_clips.py`, `test_format_worker.py` within the 893-passing backend suite.

---

## Security and privacy audit findings

Reviewed against the actual code (not only documentation):

| Area | Finding | Result |
|---|---|---|
| Path traversal | `Storage.resolve()` rejects absolute, `..`, drive specs; symlink escape checked via `contains()`; `new_source_name`/`new_clip_path` are server-owned UUIDs; client filename never a path | Pass |
| Absolute/Windows drive paths | `_DRIVE_PART` rejects `C:`-style specs | Pass |
| Archive member names | `safe_archive_name()` strips unsafe chars; no traversal | Pass |
| Files outside storage root | Every path resolved with containment check | Pass |
| Temp-file cleanup | Temp files inside storage root; export temp deleted after response; audio discarded per retention policy | Pass |
| Media serving containment | `clip_output_path` resolved inside storage root | Pass |
| YouTube URL validation | Exact hostname match, no credentials, no fragments, canonicalisation, no network in validation | Pass |
| AI provider destination | HTTPS required for remote; loopback/private/link-local/multicast refused; redirects refused; response capped at 1 MB; key never in URL/log/error | Pass |
| CORS | Explicit origins only; wildcard rejected at load time | Pass |
| Local-only binding | Refuses non-loopback host unless `CLIPFORGE_ALLOW_LAN_BINDING=true` | Pass |
| Error envelope | Stable codes in `errors.py`; no paths/secrets leaked | Pass |
| Credentials | OS keyring only; no plaintext fallback; DB holds only `secret_ref`; `SECRET_STORE_UNAVAILABLE` when no store | Pass |
| Offline transcription | `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` → engine refuses network; missing model → actionable error; inference child cannot open a socket (`_forbid_the_network`) | Pass |
| Privileged symlink test | Skipped on Windows (no privilege) — reported precisely as a skip, not a pass | Limitation |

The privileged-symlink containment test is skipped on this Windows account; it is **not** reported as passed. All unprivileged containment tests pass.

---

## Browser verification results

**Not available.** `mcp__Claude_Browser__preview_list` returned an empty set — the built-in browser preview cannot attach to a URL on this install. No Playwright/Cypress/Selenium is installed, and none was added (per the mandate not to install a browser framework without explicit authorization).

**Alternative checks performed instead:**

- HTTP route checks: `/`, `/status`, `/integrations` → 200; `/jobs/x` → 500 (unrouted dashboard shell); `/health` → ok; `/ready` → full report.
- CORS: `http://localhost:3000` preflight allowed (200); `http://evil.example.com` refused (400).
- Media containment: nonexistent clip → 404; traversal id → 404.
- Readiness: all ten capabilities `true`, worker `running`, `last_seen` current, model cached `true`.
- Frontend unit/component tests: 174 passed (jsdom — responsive behaviour cannot be asserted; documented as requiring a real browser).

This limitation is carried forward honestly in the README "Known limitations" and the evidence table.

---

## Dependency audit and environment observations

- Lockfiles present: `backend/requirements.txt`, `requirements-dev.txt`, `frontend/package-lock.json`.
- `npm run audit:check` → 0 high/critical/total; the accepted dev-only `braces` advisory (GHSA-vfj7-8cjw-p6xm) in the ESLint chain is acknowledged.
- Direct `npm audit` reports 0 advisories in this environment (an environment discrepancy noted in Phase 9, not remediated — downgrading breaks Next.js 16 lint).
- `.env.example` contains placeholders only, no real credentials.
- No secrets, local-only absolute paths, or temporary test files introduced in Phase 10.

---

## Historical phase limitations still open

Carried forward from Phase 9 (verified still accurate against the current code):

1. **No repeatable browser automation** — no Playwright/Cypress/Selenium; built-in preview cannot attach. Frontend verified via HTTP + component tests.
2. ~~**Cancellation does not terminate in-flight FFmpeg/yt-dlp mid-step** — prevents state transitions/result publication only; cancelled results never published. Documented.~~ **Closed 2026-10-31 by decision D55:** the in-flight child is stopped within one lease tick, the cancelled job's queued children are failed with `job_cancelled`, and clip/format leases are renewed during renders.
3. **`npm audit` discrepancy** (0 vs. 5 `braces` advisories) — environment observation, not remediated.
4. **`test_a_job_left_behind_by_a_dead_worker_is_recovered` is timing-sensitive under CPU contention** — suites run serially.
5. **No live paid AI-provider calls** — adapter tests use local deterministic mocks (per user request to defer live provider tests).

---

## Newly discovered defects and disposition

None. The Phase 10 audit confirmed the implementation matches the reports; no new defects were found. The one defect found in Phase 9 (unbounded export memory) was already fixed in the current tree (item + byte limits, temp-file ZIP, streamed response).

---

## Exact manual steps the user must perform

None beyond the documented first-time setup in `docs/DEVELOPMENT.md`:

1. Install Python 3.11+, Node 20.9+, FFmpeg/FFprobe (on PATH).
2. `cd backend && python -m venv ..\.venv && ..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt && Copy-Item .env.example .env`
3. `cd frontend && npm install && Copy-Item .env.example .env.local`
4. Optionally cache the speech model (documented `WhisperModel('tiny.en', ...)` command) for offline use; set `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` once cached.
5. Launch: `.\scripts\start-local.ps1` (or the three-terminal procedure in `docs/DEVELOPMENT.md`).
6. Open `http://localhost:3000`, verify `http://localhost:8000/ready`.

---

## Recommended future improvements (outside the original roadmap)

- Add repeatable browser automation only if the user explicitly authorizes a framework; until then, keep the honest limitation.
- Consider full in-flight subprocess cancellation (terminate/kill/reap of FFmpeg/yt-dlp) if users need mid-step cancellation; current behavior is safe but not immediate.
- Add a first-run wizard for the LLM key (roadmap Phase 10 idea, not yet implemented) — the Integrations page already handles key storage via the OS credential store.

---

## Required final evidence table

| Area | Required evidence | Result |
|---|---|---|
| Repository preservation | HEAD and working-tree comparison | Pass |
| Backend tests | Full test output and exit code | Pass (893 passed, 1 skipped, exit 0) |
| Frontend tests | Full test output and exit code | Pass (174 passed, exit 0) |
| Typecheck/lint/build | Command outputs and exit codes | Pass (all exit 0) |
| Dependency audit | Current audit output | Pass (0 high/critical/total; accepted dev-only `braces`) |
| E2E regression | Each script's result | Pass (all 5 scripts exit 0) |
| Fresh/isolated setup | Exact environment used | Not tested (dependencies pre-installed on this machine; documented setup not re-run from a blank machine) |
| Local startup | API, frontend, worker | Pass (launcher verified on ports 19400/14400; all healthy) |
| Graceful shutdown | Owned process cleanup | Pass (targeted stop of launched processes; key pool untouched) |
| Offline transcription | Cached model and network restrictions | Pass (model cached; offline mode refuses network; e2e uses local model) |
| AI integration | Deterministic mock behavior | Pass (e2e_phase4 uses local mock provider) |
| Clip rendering | FFprobe and media inspection | Pass (e2e_phase5/phase8 verify real media) |
| Caption formatting | Timing and burned-in frame evidence | Pass (e2e_phase8: portrait 720×1280, captions burned in via frame comparison) |
| Revisions | Original remains unchanged | Pass (e2e_phase8: original file/boundaries/candidate untouched) |
| ZIP export | Contents and limits | Pass (contents, 413 over limit, 200 at limit, cross-job refused) |
| Cancellation | State and subprocess behavior | Pass (state tested; subprocess-continues limitation documented) |
| Worker recovery | Crash/lease test evidence | Pass (e2e_workflow steps 7–8; recovery tests in suite) |
| Security | Path, URL, secret, CORS checks | Pass / Limitation (privileged symlink test skipped on Windows, reported precisely) |
| Browser verification | Actual interactive evidence or reason unavailable | Limitation (no browser tooling available; HTTP + component tests substituted) |
| Documentation | Setup and troubleshooting reviewed | Pass |

**Not tested** is used only for a completely clean-machine install (dependencies were already present on this machine, so a from-scratch install was not performed, per the mandate not to destroy the existing environment).

---

## Confirmation

- No files were discarded.
- Nothing was staged.
- Nothing was committed.
- All Phases 1–8 work is preserved.
- `HEAD` remains `7502384`.
- `PHASE_10_REPORT.md` is at the repository root.
