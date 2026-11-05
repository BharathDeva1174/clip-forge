# ClipForge — Phase 9: Master Reliability, Security, Integration Testing, and Production-Readiness Audit

**Status:** in progress  
**Date:** 2026-10-27  
**HEAD:** `7502384 docs: record the fresh-clone verification in the Phase 2 report`  
**Branch:** `main`  
**Machine:** Windows 11 Home, Python 3.11.9, Node v26.4.0 / npm 11.14.1, FFmpeg/FFprobe 9.0.2

---

## 1. Baseline (started here)

### 1.1 Repository state at the start

HEAD is `7502384`. The working tree is dirty on purpose: Phases 3 through 8 are uncommitted changes (well over a hundred changed files), all of them preserved. Nothing was reset, cleaned, stashed, staged, or committed during this phase.

The phase reports already record that Phases 1–8 are complete and verified:

| Phase | Report | Verdict | Backend | Frontend |
|---|---|---|---|---|
| 1–2 | PHASE_1/2 reports | complete | — | — |
| 3 | PHASE_3_REPORT.md | complete | 346 passed, 1 skipped | 38 passed |
| 3.1 | PHASE_3_1_REPORT.md | complete | 458 passed, 1 skipped | 41 passed |
| 4 | PHASE_4_REPORT.md | complete | 599 passed | 68 passed |
| 5 | PHASE_5_REPORT.md | ready with documented limitations | 668 passed | 77 passed |
| 6 | PHASE_6_REPORT.md | ready with documented limitations | 820 passed, 1 skipped | 92 passed |
| 7 | PHASE_7_REPORT.md | ready with documented limitations | 843 passed, 1 skipped | 151 passed |
| 8 | PHASE_8_REPORT.md | ready with documented limitations | 889 passed, 1 skipped | 174 passed |

> Note: ROADMAP.md still lists Phases 6–8 as "Not started" — a pre-existing documentation inconsistency acknowledged in PHASE_8_REPORT.md §2. It predates Phase 9 and is left untouched.

### 1.2 Baseline test results (before any Phase 9 change)

**Backend** — `cd backend && ..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN`

```
889 passed, 1 skipped in 114.64s   exit 0
```

The single skip is the privileged-symlink test, which cannot run without a privilege this Windows account does not have.

**Frontend** — `cd frontend && npm test`

```
Test Files  14 passed (14)
Tests  174 passed (174)   exit 0
```

**Frontend typecheck** — `npm run typecheck` (`next typegen && tsc --noEmit`): exit 0.

**Frontend lint** — `npm run lint` (`eslint`): no findings, exit 0.

**Frontend production build** — `npm run build`: exit 0.

**Frontend audit** — `npm run audit:check`: exit 0, reports the accepted dev-only `braces` advisory.

**Dependency discrepancy noted:** `npm audit` (run directly) reports 0 vulnerabilities, while the phase reports document 5 `braces` advisories (GHSA-vfj7-8cjw-p6xm) in the dev-only ESLint chain. `npm run audit:check` reports 0 high/critical/total. This may indicate an environment change since the reports were written (or that the advisory list changed). It is recorded as an observation, not a defect.

### 1.3 End-to-end scripts (run individually, serially)

The four e2e scripts plus `e2e_workflow.py` start real API and worker processes and take minutes each. Per the documented guidance, the backend suite and the e2e scripts are **not** run at the same time, because `test_a_job_left_behind_by_a_dead_worker_is_recovered` is timing-sensitive under CPU contention.

Each is recorded below with its exact result.

---

### 1.4 Baseline captured

- Backend: 889 passed, 1 skipped
- Frontend: 174 passed
- Typecheck, lint, build, audit: all exit 0
- e2e results: pending (run below)

---

### 1.5 E2e results (baseline — run individually, serially)

| Script | Result | Exit |
|---|---|---|
| `tests/e2e_workflow.py` | every step passed | 0 |
| `tests/e2e_phase4.py` | every step passed | 0 |
| `tests/e2e_phase5.py` | every step passed | 0 |
| `tests/e2e_phase6.py` | every step passed | 0 |

All four ran against real API + real worker + real SQLite + real FFmpeg/FFprobe with throwaway storage directories, serially, per the documented guidance.

---

## 2. Data integrity, state transitions, and stale-worker protection — completed

**Finding:** The existing data layer already implements strong protection, verified by reading the implementation and the existing test suite:

- **Atomic claims**: every claim (`claim_next_job`, `claim_next_analysis`, `claim_next_clip`, `claim_next_format`) uses `BEGIN IMMEDIATE`, reads the candidate row under the write lock, and re-checks eligibility in the same transaction (`WHERE status = 'queued'` / `'pending'`). Two workers cannot both own the same attempt. ✓
- **No terminal claim**: the claim only selects rows in queued/pending status; a terminal job/clip/format/analysis can never be claimed. ✓
- **Ownership at write time**: every state-changing update accepts `expected_worker_id` and checks it *inside* the same transaction as the write. A stale worker's update returns `None` and writes nothing. ✓
- **Lease recovery**: `recover_expired_leases` and the per-entity variants requeue expired leases (or fail permanently at `max_attempts`), and never touch terminal rows. ✓
- **Transcript persistence ordered before completion**: the worker commits the transcript in its own transaction and only then marks the job `completed`. ✓

**Existing coverage** (confirmed in `test_job_recovery.py`, `test_clips.py`, `test_format_worker.py`): stale worker cannot replace a transcript, cannot fail a job somebody else owns, cannot complete a clip/format; recovery requeues or fails correctly; terminal states stay terminal; legacy jobs preserved; a failing migration leaves the database intact and is recoverable.

No change needed in this area.

---

## 3. Crash recovery and filesystem/database consistency — completed

**Finding:** The existing architecture already keeps the filesystem/database boundary consistent:

- `BEGIN IMMEDIATE` transactions for all multi-step writes; a failed segment insert rolls back the whole transcript (`test_a_failure_while_storing_the_segments_rolls_everything_back`).
- `snapshot()` reads job + transcript in one read transaction so polls never see half-finished state.
- Temporary files live inside the storage root, named per-attempt (`clip-<id>-<attempt>`, `format-<id>-<attempt>`), so two attempts never share a path.
- Atomic `os.replace` finalisation: verify the temporary file, then rename into `clips/` / `formats/`, then commit — a half-written file is never visible.
- On ownership mismatch at commit, the stale attempt's file is removed rather than orphaned.
- Narrow reconciliation: `reconcile_clips` / `reconcile_formats` remove only files matching the naming pattern, only older than an hour, only inside `temp/`.

**Crash-window findings** (documented, not defects): there is a non-transactional gap between `os.replace` and the database commit (a file can exist without its row). This self-heals on the next render of the same entity because the name is attempt-independent for the final path. Documented in Phase 5/6 reports; reproduced safely at the data layer.

No change needed in this area.

---

## 4. Media correctness and revision/export integrity — completed

**Finding:** Verified against real FFmpeg/FFprobe outputs and the existing test matrix:

- `clip_render.py` and `format_render.py` use argument arrays (`shell=False`), timeouts, terminate-then-kill-reap, stderr-to-temp-file (no pipe deadlock), bounded diagnostics, and independent FFprobe verification (not merely "FFmpeg exited zero").
- `validate_boundaries` rejects NaN/infinity/booleans/negative/reversed/out-of-range ranges before any work.
- Revisions create new artifacts (`parent_clip_id`); originals are never touched.
- Export ZIP is built per-request in memory, not stored; cross-job and unfinished clips refused; duplicate clip IDs deduped.

**Defect found and fixed — unbounded export memory (§15):** `export_service.py` builds the ZIP in `io.BytesIO` with no item bound, so one export request could consume unbounded RAM and disk. Fixed:

- Added `CLIPFORGE_CLIP_EXPORT_MAX_ITEMS` (default 50, range 1–1000) to `backend/app/config.py`.
- Enforced in `export_clips`: more than the limit → 413 `clip_export_too_large`.
- Added error code `CLIP_EXPORT_TOO_LARGE` in `backend/app/errors.py`.
- Added tests `test_an_export_with_too_many_clips_is_refused` and `test_an_export_at_the_limit_is_accepted` in `backend/tests/test_editing.py`.

Backend suite after the fix: **891 passed, 1 skipped** (baseline 889 → +2 new tests).

---

## 5. Cancellation and subprocess lifecycle — completed

**Finding:** Cancellation is implemented as documented, and its limits are honest:

- `POST /api/v1/jobs/{id}/cancel` sets `cancelled` in one transaction, releases the lease, refuses completed/terminal jobs (409 `job_not_cancellable`), unknown IDs (404), and is idempotent.
- Cancellation is **not** a signal to an active subprocess: the `_lease_tick` callback checks the cancelled flag during long steps (download, transcription progress), raises `_JobCancelled`, removes temporary files, and does *not* requeue. This matches the Phase 8 documentation ("Cancellation only prevents further state transitions or result publication").
- A cancelled job is never claimed (`claim_next_job` only selects `queued`).

**Honest limitation (documented):** Cancellation does not terminate an in-flight FFmpeg/yt-dlp process mid-step. Phase 8 records this as a gap; Phase 9 does not pretend it is full process cancellation. No change needed — the documented behavior is what is tested.

> **Amended 2026-10-31 (decision D55):** this limitation is closed. Cancellation now stops the in-flight yt-dlp download, clip render and format render within one lease tick and fails the cancelled job's queued children with `job_cancelled`; clip and format leases are renewed during renders. See [docs/DECISIONS.md](docs/DECISIONS.md).

---

## 6. Security and resource limits — completed

**Finding:** Reviewed the actual surfaces:

- URL validation (`app/youtube.py`): exact hostname match, no credentials, no fragments, canonicalisation, no network access in validation. ✓
- AI client transport (`app/ai_client.py`): HTTPS required for remote, destination checked before every request (loopback/private/link-local/multicast/refused), redirects refused, response capped at 1 MB, bounded retries with backoff, `Retry-After` honoured, key never in URL/log/error. ✓
- Credentials (`app/credentials.py`): keyring only, no plaintext fallback, reference only in DB, `SECRET_STORE_UNAVAILABLE` when no store. ✓
- Path containment (`app/storage.py`): rejects absolute, `..`, drive specs, symlinks-outside; generated names server-owned; client filename never a path. ✓
- Subprocesses: argument arrays, `shell=False`, timeouts, terminate-then-kill-reap, bounded stderr. ✓
- CORS: explicit origins, wildcard rejected at load time. ✓
- Loopback binding: refused unless `CLIPFORGE_ALLOW_LAN_BINDING=true`. ✓
- Error envelope: stable codes, no paths/secrets leaked. ✓

**Dependency discrepancy (observation):** `npm audit` run directly reports 0 vulnerabilities; the phase reports document 5 `braces` advisories in the dev-only ESLint chain. `npm run audit:check` passes (0 high/critical/total). This is an environment discrepancy, not a new issue; recorded rather than remediated (no compatible upstream fix exists; downgrading breaks Next.js 16 lint — verified in Phase 3.1).

---

## 7. API/frontend resilience — completed

**Finding:** The frontend normalises untrusted responses at the boundary (`frontend/src/lib/api.ts`):

- Unknown/missing status → `unknown` sentinel (never rewritten to `queued`), polling stops.
- Progress clamped 0–100; non-numeric counts → 0; malformed error envelope → null; job without id dropped; transcript without segments → empty; non-list → empty list.
- `mediaUrl()` resolves relative media paths against the API address (fixes the cross-port 404 found by the browser in Phase 7).
- One polling rule (`use-polling.ts`): one interval, only while the resource can change, cleared on unmount; transient failures keep last-known state.
- `items<T>()` guards every list response so a surprising payload cannot crash a panel.

No change needed in this area.

---

## 8. Repeatable browser automation — completed (documented gap)

**Gap:** No browser-automation tooling exists (no Playwright/Cypress/Selenium installed), and the built-in browser preview (`preview_start` / `preview_*`) cannot attach to a URL on this install ("not enabled"). The frontend runs on `localhost:3000` and was verified over HTTP (routes return 200, CORS, media containment, readiness report). No repeatable automated browser workflow exists.

**Evidence gathered instead:**
- `/` → 200, `/status` → 200, `/integrations` → 200, `/jobs` → 404 (unrouted dashboard shell), `/health` → `{"status":"ok","service":"clipforge-api"}`.
- Readiness report: all ten capabilities `true`, worker `running`, `last_seen` current, `transcription_model_cached: true`.
- CORS: `http://localhost:3000` preflight allowed (200, allow-origin echoed); `http://evil.example.com` refused (400).
- Media containment: nonexistent clip → 404; `../outside` id → 404.

This gap is carried forward as a documented limitation (Phase 7 made the same honest call: adding a framework was deemed unnecessary without CI). No browser framework was added without an explicit opt-in.

---

## 9. Environment reproducibility and documentation — completed

**Finding:** The documented setup works from the existing checkout without destroying it:

- Python 3.11.9 venv at repo root; `pip install -r requirements-dev.txt` installs the backend (includes `faster-whisper`, `ctranslate2`, `keyring`, `httpx2`).
- Node v26.4.0 / npm 11.14.1; `npm install` uses the committed `package-lock.json`.
- FFmpeg/FFprobe 9.0.2 on PATH; the worker and API bind loopback by default.
- `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` + a cached model runs fully offline; the inference child cannot open a socket (`_forbid_the_network`).
- SQLite migration is versioned (`SCHEMA_VERSION = 7`), transactional, and idempotent; upgrades preserve existing rows.

**Command results (final, after the Phase 9 change):**

| Check | Result | Exit |
|---|---|---|
| Backend `pytest` | **891 passed, 1 skipped** | 0 |
| Frontend `npm test` | **174 passed** | 0 |
| `npm run typecheck` | types generated, no errors | 0 |
| `npm run lint` | no findings | 0 |
| `npm run build` | production build | 0 |
| `npm run audit:check` | accepted advisory only | 0 |
| `e2e_workflow.py` | every step passed | 0 |
| `e2e_phase4.py` | every step passed | 0 |
| `e2e_phase5.py` | every step passed | 0 |
| `e2e_phase6.py` | every step passed | 0 |

No dependency versions changed; no commit made; nothing staged.

---

## 10. Final full regression run — completed

The full backend suite (891 passed, 1 skipped), frontend suite (174 passed), typecheck, lint, production build, audit check, and all four end-to-end scripts pass after the Phase 9 change. The backend suite and e2e scripts were run serially, per the documented guidance (the timing-sensitive dead-worker-recovery test fails under CPU contention when run concurrently).

---

## Changed files (Phase 9)

| File | Reason |
|---|---|
| `backend/app/config.py` | Added `clip_export_max_items` (default 50, range 1–1000) so one export cannot consume unbounded memory. |
| `backend/app/errors.py` | Added `CLIP_EXPORT_TOO_LARGE = "clip_export_too_large"`. |
| `backend/app/routers/clips.py` | Enforce `clip_export_max_items` in `export_clips`; 413 when exceeded. |
| `backend/tests/test_editing.py` | Added `test_an_export_with_too_many_clips_is_refused` and `test_an_export_at_the_limit_is_accepted`. |
| `.claude/launch.json` | Created (preview server config for browser verification). |

**Nothing else was changed.** All Phases 1–8 work is preserved; nothing was reset, cleaned, stashed, staged, or committed.

---

## Final verdict

### **READY FOR PHASE 10 WITH DOCUMENTED LIMITATIONS**

**Why this verdict.** ClipForge is a dependable local MVP:

- Critical state transitions and worker ownership invariants are tested (atomic claims, stale-worker rejection, lease recovery, terminal-states-stay-terminal).
- Crash recovery does not silently corrupt completed artifacts (verify-then-rename, ownership-checked commit, narrow reconciliation).
- Media output is validated beyond file existence (real FFmpeg/FFprobe verification; boundary proven with frame hashes; captions proven burned-in by frame comparison).
- Revision and export semantics are tested (new artifacts, originals untouched, fingerprint distinctness, cross-job refusal, bounded export).
- Cancellation guarantees are accurately implemented and documented (no pretense of full subprocess cancellation).
- API/frontend failures are handled without lying about state (boundary normalisation, `unknown` sentinel, polling cleanup).
- Security and resource limits reviewed; one real defect found and fixed (unbounded export memory).
- A repeatable browser workflow is not possible on this install (no browser framework, preview attach disabled); the gap is documented and honest.
- Existing regression suites pass except the one verified pre-existing skip (privileged symlink, environment-dependent).
- Final backend/frontend checks and production build run after the last change.

**Documented limitations:**
- No repeatable browser automation (no Playwright/Cypress/Selenium; preview attach disabled). This is the main reason the verdict is "with documented limitations" rather than plain "READY".
- ~~Cancellation does not terminate an in-flight FFmpeg/yt-dlp mid-step (documented in Phase 8; Phase 9 does not pretend otherwise).~~ Closed 2026-10-31 by decision D55 — cancellation stops the child within one lease tick.
- `npm audit` discrepancy (0 vs. 5 `braces` advisories) recorded as an environment observation, not remediated.
- The `test_a_job_left_behind_by_a_dead_worker_is_recovered` test is timing-sensitive under CPU contention; suites run serially.
- No live paid AI-provider calls are required; adapter tests use local deterministic mocks.

**Git integrity:** HEAD is unchanged (`7502384`), nothing staged, nothing committed, no existing work discarded.
