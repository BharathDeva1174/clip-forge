# Phase 4 Reliability Report — AI Highlight-Detection Pipeline

**Status:** Stage A baseline + Stage B root-cause assessment complete.  
**HEAD:** `7502384d` (Phase 2). Working tree dirty on purpose — Phases 3–10 uncommitted, preserved.  
**Schema:** `SCHEMA_VERSION = 7` (`backend/app/db.py`). No migration added yet.

## 1. Baseline results

| Check | Command | Result |
|-------|---------|--------|
| Backend suite | `pytest -q` | 893 passed, 1 skipped |
| Phase 4 focused | `pytest tests/test_highlights.py tests/test_ai_client.py tests/test_highlights_api.py -q` | 75 passed |
| Frontend tests | `npm test` | 14 files, 174 passed |
| Typecheck / lint / build | `npm run typecheck`, `npm run lint`, `npm run build` | Pass |

No changes were made before recording this baseline. All pre-existing uncommitted work is intact.

## 2. Architecture map (as implemented)

```
Job (queued)
  ↓  worker claims it
Persisted transcript (transcripts + transcript_segments)
  ↓  POST /api/v1/jobs/{id}/highlights queues an analysis
highlight_analyses row (status=pending, config snapshot)
  ↓  worker claims the analysis (claim_next_analysis, BEGIN IMMEDIATE + lease)
HighlightRunner.run(conn, analysis, transcript, segments, ...)
  ├─ resolve_integration: integration must still match the queued snapshot
  ├─ resolve_secret: read API key from OS credential store
  ├─ plan_windows(segments): split into overlapping windows
  │      (max_segments=120, overlap=4, max_characters=12000)
  ├─ for each window: send one LlmCall via AiClient.complete
  │      └─ protocol adapter (OpenAI Chat / Responses / Anthropic)
  │      └─ parse_model_json → validate_candidates (grounded in persisted segments)
  ├─ _merge(collected): dedupe overlapping candidates across windows
  ├─ save_candidates(analysis.id, merged): one transaction, replaces prior set
  └─ update_analysis(status=completed): conditional on expected_worker_id
```

Each transition, its failure modes, and retry safety:

| Transition | Function | Sync/Async | Retry-safe? | Failure modes |
|------------|----------|------------|-------------|---------------|
| Analysis creation | `start_analysis` router | sync request | n/a (409/400) | no transcript, no integration, disabled, unsupported protocol |
| Integration resolution | `_resolve_integration` | sync | yes | integration deleted/disabled/changed → `INTEGRATION_CHANGED`, no request made |
| Secret resolution | `resolve_secret` | sync | yes | credential store unavailable → `SECRET_STORE_UNAVAILABLE` |
| Windowing | `plan_windows` | sync | yes | empty transcript → `HIGHLIGHT_TRANSCRIPT_EMPTY` |
| Provider request | `AiClient.complete` → adapter | sync HTTP | bounded (timeout/unavailable) | `ai_timeout`, `ai_unavailable`, `ai_rate_limited`, `ai_auth_failed`, `ai_invalid_request`, `ai_protocol_error`, `ai_response_too_large` |
| Answer parsing | `parse_model_json` | sync | no | malformed → `AI_INVALID_OUTPUT` |
| Candidate validation | `validate_candidates` | sync | no | invalid references → candidate dropped |
| Merge + dedupe | `_merge` / `_overlaps_an_accepted` | sync | yes | overlapping → later candidate dropped |
| Persistence | `save_candidates` + `update_analysis` | sync transaction | conditional (expected_worker_id) | stale worker → write refused, `None` returned |

Lease model: `claim_next_analysis` sets `status='running'`, `worker_id`, `lease_expires_at = now + lease_seconds`. `HighlightRunner.run()` calls `on_tick()` between windows, which renews the lease. `recover_expired_analysis_leases` requeues (`pending`) or fails permanently (`max_attempts_exceeded`).

## 3. Stage B — root-cause assessment

The incident: a 23-minute / 279-segment transcript, analysis failed four times:
1. `ai_timeout`
2. `ai_timeout`
3. `lease_expired`
4. `ai_unavailable`

Provider: a user-configured local OpenAI-compatible endpoint running a free reasoning-class model (endpoint and model identifiers are deliberately not recorded here — this repository stores no provider connection). Connection test succeeded.

Contributing factors (not a single exclusive root cause):

### 3.1 Oversized single-window prompt
`DEFAULT_MAX_TRANSCRIPT_CHARACTERS = 12000` and `DEFAULT_MAX_SEGMENTS_PER_WINDOW = 120`. A 279-segment transcript almost certainly spans **3+ windows**. Each window is sent once, with no automatic retry of an individual window. The model's reasoning (a reasoning-class model spends tokens on invisible output) can consume invisible tokens before writing the visible answer, so a 12 k-character window can easily exceed a reasonable per-request deadline.

### 3.2 Lease expires *during* a single provider call
`on_tick()` (lease renewal) is called **between windows only**, not during a `self.client.complete()` call. If one provider call takes longer than `CLIPFORGE_WORKER_LEASE_SECONDS` (default 300s), the lease expires mid-request. Attempt 3's `lease_expired` is exactly this: a slow provider call outlived the lease, and `run_highlights_once` caught the generic exception and recorded `INTERNAL_ERROR`, while recovery marked the analysis `lease_expired`.

Increasing `CLIPFORGE_WORKER_LEASE_SECONDS` to 900 does **not** fix this if the provider call itself exceeds 900s, and a very large lease delays recovery after a genuine worker crash.

### 3.3 No per-window retry
`AiClient._request` retries `AI_TIMEOUT`/`AI_UNAVAILABLE` with bounded backoff. But `HighlightRunner.run()` fails fast on *any* `AiClientError` from `_analyse_window` — it does not retry an individual window. So one transient provider hiccup on window 2 of 5 fails the entire analysis, even though windows 1 and 3+ would succeed.

### 3.4 Connection test ≠ analysis capability test
`test_connection` sends a one-word probe with `PROBE_MAX_OUTPUT_TOKENS = 1024`. It never sends a transcript-sized request, never exercises structured output, and never measures generation time against the analysis timeout. A local endpoint can answer "ready" instantly but choke on a 12 k-character reasoning request — exactly the incident.

### 3.5 No chunk-level persistence
`save_candidates` is called **once**, after all windows succeed. If the worker crashes or loses its lease after window 2 of 5, all completed windows' candidates are discarded and the analysis restarts from window 1. There is no per-chunk progress record.

### 3.6 No bounded concurrency
Windows are analyzed sequentially in a `for` loop. No concurrent provider requests. For a 3–5 window transcript this is a throughput limitation, not a correctness bug, but it lengthens the total analysis wall-clock time and therefore the lease exposure.

## 4. Required behavior (from the brief)

- A long-running provider call must **not** silently prevent lease renewal.
- If lease renewal fails: stop treating the job as owned, prevent further result publication, avoid destructive cleanup of another worker's work, record a recoverable failure / allow recovery to reclaim it.
- Preserve stale-worker protection (ownership-checked writes).
- Controlled, segment-safe transcript chunking with original indices preserved.
- Bounded concurrency; deterministic aggregation and dedupe; transactional final publication.
- Error classification with bounded retry + backoff; permanent failures fail promptly.
- Chunk-level persistence: a completed chunk is not re-analyzed after an unrelated chunk fails.
- Distinguish connection testing from analysis capability testing.
- Deterministic mock-provider tests; no real provider dependency.

## 5. Implementation sequence (per brief)

| Stage | Goal | Status |
|-------|------|--------|
| A | Baseline and audit | ✅ Done |
| B | Root-cause analysis | ✅ Done (above) |
| C | Lease and ownership hardening | ✅ Done |
| D | Chunking architecture | Planned |
| E | Retry and recovery | Planned |
| F | Candidate aggregation | Planned |
| G | Diagnostics | Planned |
| H | Regression + long-transcript test | Planned |
| I | Documentation + final audit | Planned |

## 6. What will NOT change

- No provider-specific hardcoding; the preset/custom integration architecture is preserved.
- No API contract changes without documented necessity.
- No Phase 5/6 modifications.
- No real provider or paid API calls in tests.
- No `git reset`/`clean`/stash/commit; existing uncommitted work preserved.
- `SCHEMA_VERSION` only bumped if a migration is genuinely needed (chunk-level persistence may require one).

## Stage C completed — what changed

**Root cause fixed:** `highlight_service.py`'s `HighlightRunner.run()` only renewed the analysis lease *between* transcript windows (`on_tick()`), never *during* a blocking provider call. A slow local model call (a reasoning model on a 23-minute transcript) could outlive the lease → `lease_expired` mid-request → analysis aborted.

**Changes:**

- `backend/app/db.py`: added `renew_analysis_lease(conn, analysis_id, *, lease_seconds, worker_id)` — an ownership-checked lease renewal for analyses (mirrors `renew_lease` for jobs).
- `backend/app/config.py`: added `worker_lease_renewal_interval_seconds` (default 30s).
- `backend/app/highlight_service.py`:
  - Added `_LeaseRenewer`: a background thread that renews the analysis lease on a separate DB connection *during* each provider call, using `renew_analysis_lease` (ownership-checked — a worker that lost the analysis cannot keep its lease alive). It renews immediately on start, then every `interval_seconds`. If any renewal fails, it stops and sets a flag.
  - `HighlightRunner.run()` now starts the renewer before processing windows, checks `renewer.failed()` after every window, and aborts with `LEASE_EXPIRED` (no publication) if renewal ever failed. The renewer is always stopped in `finally`.
  - New parameters `db_path`, `lease_seconds`, `renewal_interval_seconds` on `run()`. When `db_path` is `None` (existing tests/callers), renewal is a no-op — backward compatible.
- `backend/app/worker.py`: `run_highlights_once` passes `db_path`, `lease_seconds`, `renewal_interval_seconds` from settings into `runner.run()`.

**Tests added (`tests/test_highlights.py`):**

- `test_lease_is_renewed_during_a_slow_provider_call` — a 1.5s provider call with a 5s lease renewed every 0.2s completes (lease never lapses mid-call).
- `test_renewal_fails_stops_publication` — when the renewer cannot open the DB, the analysis fails with `LEASE_EXPIRED` and publishes nothing.

**Verification:** full backend suite 893 passed + 2 new, 1 skipped. All Phase 4 focused tests pass.

---

## Remaining root causes (from Stage B)

| Incident failure | Root cause | Stage that fixes it |
|------------------|------------|---------------------|
| `ai_timeout` ×2 | A single provider call timed out; no per-window retry, no smaller-request fallback | D+E |
| `lease_expired` | Lease not renewed *during* the blocking call | C ✅ |
| `ai_unavailable` | Provider unavailable; whole analysis failed fast, no retry of remaining windows | E |

The two `ai_timeout` failures show the analysis still needs per-window retry with backoff (so one slow window doesn't kill the whole analysis) and smaller-request recovery (so a context-limit rejection retries with a smaller chunk). Stage D+E address those.

---

Next step: **Stage D — chunking architecture** (configurable segments/tokens per chunk, overlap, bounded concurrency, segment-safe splitting). The existing `plan_windows` already does segment-safe splitting with overlap; the service needs to make it configurable and add bounded concurrency plus per-window retry. Shall I proceed?