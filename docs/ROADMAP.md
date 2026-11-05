# Roadmap

ClipForge is planned as ten phases. All ten phases are implemented and verified.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Project foundation | **Complete** |
| 2 | Video ingestion | **Complete** |
| 3 | Audio extraction and transcription | **Complete** |
| 3.1 | Transcription hardening | **Complete** |
| 4 | AI integrations and highlight detection | **Complete** |
| 5 | Video clip generation | **Complete** |
| 6 | Vertical formatting and captions | **Complete** |
| 7 | Frontend dashboard | **Complete** |
| 8 | Editing and exports | **Complete** |
| 9 | Reliability and testing | **Complete** |
| 10 | Local launch and polish | **Complete** |

Each phase is only marked complete once its acceptance criteria have been
verified with real evidence. That evidence is written up per phase in reports
kept outside this repository; the criteria below are what they were checked
against.

Work continued after Phase 10: the highlight-detection reliability incident was
investigated and fixed, and cancellation was hardened to stop in-flight
children and reach queued work (decision D55). Both are recorded in
[docs/DECISIONS.md](DECISIONS.md) and are the current behaviour.

---

## Phase 1 — Project foundation ✅ *complete*

Repository, backend API skeleton, frontend shell, configuration, tests and
documentation.

Delivered:

- FastAPI application with `/health`, `/ready` and `/api/v1`.
- Typed, environment-driven configuration with secret handling.
- Next.js shell with a live backend status indicator.
- Backend test suite, frontend unit and component tests, lint and type
  checks.
- Documentation: README, architecture, development, testing, roadmap,
  decisions.

---

## Phase 2 — Video ingestion ✅ *complete*

Secure ingestion, persistent jobs and a separate worker.

Delivered:

- `POST /api/v1/videos/upload` — a streamed multipart upload with the size
  limit enforced while the body arrives, validated by FFprobe.
- `POST /api/v1/videos/youtube` — a strictly validated YouTube URL, with the
  caller confirming they are authorised to use the video.
- `GET /api/v1/jobs` and `GET /api/v1/jobs/{id}` — a bounded, ordered job
  list and one job's status.
- SQLite job records with a versioned schema, atomic claiming, leases and
  crash recovery.
- A separate worker process that polls, claims, acquires and inspects.
- The frontend upload and YouTube forms, and a job list that polls while a
  job is running and stops when it is finished.
- A documented storage layout with path containment, and documented retention
  behaviour.

**`completed` means ingestion and media inspection finished.** It does not mean
clips exist. Phase 3 extends this: a `completed` job also has its transcript
stored.

---

## Phase 3 — Audio extraction and transcription ✅ *complete*

Offline audio extraction and speech-to-text.

Delivered:

- `app/audio.py` — FFmpeg audio extraction to 16 kHz mono PCM WAV, with a
  bounded, shell-free subprocess, a timeout, verified output and no shell
  metacharacter risk.
- `app/transcription.py` — a local `faster-whisper` engine, loaded lazily and
  reused, with an offline mode that never reaches the network.
- `app/transcripts.py` — the rules about what a valid segment is: finite,
  ordered, inside the audio, with meaningful text preserved.
- Two new tables, `transcripts` and `transcript_segments`, with a migration
  that leaves existing jobs untouched.
- Two new worker stages, `extracting_audio` and `transcribing`, and a
  `completed` job that always has its transcript stored first.
- `GET /api/v1/jobs/{id}/transcript` — the stored transcript, its ordered
  segments and the state the job is in.
- The frontend shows the extraction and transcription stages, an explicitly
  approximate transcription progress, and the timestamped transcript itself.

**`completed` means the video was stored, its audio was extracted and its
transcript was saved.** It does not mean clips exist.

---

## Phase 3.1 — Transcription hardening ✅ *complete*

Reliability and verification of the Phase 3 pipeline, before Phase 4 builds
on it.

Delivered:

- The speech model runs in a **child process** of the worker, so
  `CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS` is a hard timeout: a stalled
  inference is stopped, the child is reaped, the job fails with
  `transcription_timeout`, and the worker carries on.
- The normalisation rules are complete and tested one by one, and the
  duration they normalise against is the extracted file's own, measured by
  FFprobe.
- The model name is validated as configuration, and the offline behaviour is
  explicit: a missing model fails clearly instead of triggering a download.
- The worker cannot overwrite the result of a newer attempt, and a terminal
  job stays terminal.
- The transcript API and panel distinguish pending, processing, ready, empty
  and failed, including a job that was finished before transcripts existed.
- The one dependency advisory is investigated, recorded and guarded by
  `npm run audit:check`. (The guard has since caught a different advisory —
  see [Open items](#open-items).)
- A real-model test matrix and an end-to-end workflow check, both run
  locally and offline.

---

## Phase 4 — AI integrations and highlight detection ✅ *complete*

A provider-agnostic integration system, and highlight detection that runs
on the persisted transcript.

Delivered:

- The **Integrations** page: add a preset or a custom endpoint, keep the
  key in the operating system's credential store, fetch the model's list,
  test the connection, select the default, disable and delete.
- A provider abstraction with one method, and one adapter per API protocol:
  OpenAI-compatible Chat Completions, the OpenAI Responses API and the
  Anthropic Messages API.
- A transport that checks the destination, refuses redirects, bounds the
  request and the response, and never leaks the key or the provider's own
  error body.
- The candidate rules: the model selects transcript segments, the
  boundaries come from the persisted segments, and an invalid candidate is
  dropped rather than repaired.
- A versioned prompt that treats the transcript as quoted material, and a
  windowing that splits a long transcript instead of truncating it.
- The analysis runs in the worker, never in a request handler, with the
  same lease, recovery and stale-worker protection the jobs use.
- Migration v4: `integrations`, `highlight_analyses` and
  `highlight_candidates`.

**The transcript is sent to the provider the user chose, and only when the
user asks for it.** The video and the audio never leave the machine.

---

## Phase 5 — Video clip generation ✅ *complete*

Cutting the chosen moments into real, playable clips.

Delivered:

- A clip entity with its own lifecycle — `queued → preparing → rendering →
  verifying → completed` — a lease, and the same stale-worker ownership
  protection the jobs use.
- The boundaries come from the **persisted candidate**, and the clip is
  **re-encoded** so the boundary is exact; the seek semantics were verified
  against a source with a non-zero start time.
- The output is **verified independently** with FFprobe before the clip is
  marked complete, and the file is moved into place only after it has been
  verified.
- One clip per candidate, so a repeated request does not render twice, and
  an explicit retry for a failed clip.
- The API: request, list, read, retry and play or download a clip.
- The frontend: **Render clip** on a candidate, the render stages, the
  finished clip with a preview and a download.

**A clip is a moment, cut out of the source video.** The vertical
conversion and the captions are Phase 6.

---

## Phase 6 — Vertical formatting and captions ✅ *complete*

Turning a clip into a portrait, captioned file that can be uploaded as it
is.

Delivered:

- A **formatted clip** is its own artifact, with its own file, its own
  lifecycle and its own lease. The landscape clip it came from is never
  changed.
- **Two layouts**, both of which preserve the aspect ratio: *centre crop*
  fills the 9:16 canvas, *fit and pad* shows the whole frame.
- **Burned-in captions**, generated from the persisted transcript on the
  clip's own timeline, with the font, size, colours, outline and position
  under the user's control. The fonts offered are the ones installed on
  this machine.
- The captions are an **SRT** file, which has no markup, so the user's
  speech cannot inject anything into it; the styling comes from libass.
- The output is **verified independently** with FFprobe, and the caption
  burn-in is proved at the media level by comparing the captioned and the
  uncaptioned renders frame by frame.
- One clip with one configuration is one artifact: a repeated request does
  not render twice, and changing a setting produces a second artifact
  rather than replacing a finished one.

**Formatting is deterministic media processing.** It needs no AI request at
all, and it never calls the transcription model again.

---

## Phase 7 — Frontend dashboard ✅ *complete*

Turning the working pieces into one product: a routed dashboard, a library of
every video, a workflow view for one video, and a real status view.

Delivered:

- A **routed dashboard**: `/` (the library), `/jobs/<id>` (the workflow),
  `/status` (the service status) and `/integrations` (unchanged), with the
  header, the navigation and the footer in the root layout so every route
  is the same shell.
- The **library** shows what the backend actually knows: the source, the
  type, the status, the stage, whether there is a transcript, and how many
  moments, clips and short-form versions there are. Every entry leads to
  the workflow.
- The **workflow view** brings the stages together in the order they
  happen — the source, the transcript, the moments, and a **grid of the
  clips and the short-form versions** — and says what can be done next
  from the real state.
- A **real status view** replacing the single health check: the
  configuration, the speech model cache, **whether the background worker
  is actually running**, and the capabilities this build has.
- The **capability flags** now tell the truth: the highlight detection, the
  clip rendering and the vertical formatting are reported as available,
  because they are.
- A **clip that has already been rendered is found** when the workflow is
  opened, instead of offering to render it all over again.
- The API answers are **normalised at the boundary**, so a surprising
  response cannot crash the dashboard.
- One shared **polling rule**: one interval, only while the resource can
  change, cleared when the view is left.

**No backend pipeline was moved into the browser.** The dashboard reads the
same API it always did.

---

## Phase 8 — Editing and exports ✅ *complete*

Editing clips and exporting them.

Delivered:

- **Clip editing.** Trimming a clip produces a **revision**: a new clip with its own identity, its own file and its own lifecycle (`clips/parent_clip_id`). The clip that was trimmed keeps its file and boundaries, and the AI's candidate is never rewritten.
- **Clip relabelling.** The user's title is saved on the clip; the file is not touched.
- **Caption text edits.** Per-segment caption overrides are stored and are part of the short-form format configuration fingerprint, so a changed text produces a distinct artifact.
- **Batch export (ZIP).** `POST /api/v1/jobs/{id}/exports` builds a ZIP of chosen finished clips and streams it; it is not stored.
- **Job cancellation.** `POST /api/v1/jobs/{id}/cancel` stops a job that has not finished; finished jobs cannot be cancelled.

---

## Phase 9 — Reliability and testing ✅ *complete*

Master reliability, security, integration testing, and production-readiness audit.

Delivered:

- Data integrity, state transitions and stale-worker protection verified across all four lifecycle entities (jobs, analyses, clips, formats).
- Crash recovery and filesystem/database consistency: transactional writes, verify-then-rename finalisation, ownership-checked commits, narrow temp-file reconciliation.
- Media correctness: real FFmpeg/FFprobe verification, boundary validation, independent frame-level proof of caption burn-in.
- Revision and export integrity: bounded ZIP export (item count `CLIPFORGE_CLIP_EXPORT_MAX_ITEMS` default 50, byte budget `CLIPFORGE_CLIP_EXPORT_MAX_BYTES` default 1 GiB), cross-job refusal, idempotency.
- Cancellation implemented as documented: prevents state transitions and result publication; cancelled results are never published. (Amended 2026-10-31, decision D55: cancellation now also stops in-flight yt-dlp/FFmpeg children within one tick and fails the job's queued children; the original mid-step limitation is closed.)
- Security and resource limits reviewed across URLs, credentials, path containment, subprocesses, CORS, loopback binding and error envelopes.
- API/frontend resilience: boundary normalisation, `unknown` status sentinel, polling cleanup, media URL resolution against the API address.
- A real defect found and fixed: unbounded export memory.
- End-to-end browser automation is **not** available — no Playwright/Cypress/Selenium and the built-in preview cannot attach. Documented as a limitation, carried forward.

**A repeatable browser workflow is not possible on this install; the gap is documented rather than glossed over.**

---

## Phase 10 — Local launch and polish ✅ *complete*

Local launch and polish.

Delivered:

- A single-command local launch script, `scripts/start-local.ps1`, verified end to end (API → worker → frontend, readiness-gated, not fixed-delay). It logs each process to a file (never a pipe — per the project's pipe-deadlock guidance).
- A full local-launch and shutdown verification on real ports.
- Confirmation that the ZIP export resource bound, cancellation semantics, migration/crash-recovery behaviour, and offline transcription all behave as documented.
- `.env` / `.env.local` were created from the `*.example` templates for local verification (both are git-ignored and never committed).
- No source file was changed; the only new file is `scripts/start-local.ps1`.

**Final verdict: READY WITH DOCUMENTED LIMITATIONS.** The limitation carried
forward from Phase 10 is the lack of repeatable browser automation (no
Playwright/Cypress/Selenium available; the built-in browser preview cannot
attach on this install). Everything else in the Phase 10 definition of done is
verified or explicitly bounded. One further item has appeared since, recorded
below.

---

## Open items

Nothing below is a phase; each is a known, current gap against the tree.

| Item | State | What it needs |
| --- | --- | --- |
| Repeatable browser automation | Still unavailable on this install — the documented Phase 9/10 limitation | A machine where Playwright (or similar) can attach; UI is verified over HTTP and by component tests in the meantime |
| `npm run audit:check` fails (31 October 2026) | GHSA-g3hv-75wg-m249, a moderate denial of service in `minimatch` (`<11.0.1`), reaches the 16 dev-only packages of the ESLint / `typescript-eslint` chain. The old `braces` advisory the baseline accepts is no longer reported, so the check now refuses to pass. npm's only offered fix is a semver-major downgrade of `eslint` to v4, which cannot read this project's flat config | A decision, not a code change: either widen `ACCEPTED_ADVISORIES` in `frontend/scripts/check-audit.mjs` and record the reasoning in `docs/DECISIONS.md`, or move the lint toolchain. `npm run lint` passes either way |
| Live paid-provider verification | Every protocol adapter and analysis test runs against a local deterministic mock; the shape of a real vendor's answer is not asserted anywhere | One manual run against a real endpoint, recorded like the other evidence |

**Closed since the Phase 10 report:** `API_STAGE` no longer reads
`phase-3-transcription` — `backend/app/config.py:31` reports
`phase-10-local-launch` alongside `API_VERSION` `1.2.0`, so the startup log and
the `/api/v1` metadata describe the build that exists. The repository is now
published at https://github.com/BharathDeva1174/clip-forge, which is why
[QUICK_START.md](../QUICK_START.md) exists and why no environment file,
database, media or provider configuration is tracked.
