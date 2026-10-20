# Roadmap

ClipForge is planned as ten phases. Phases 1 and 2 are implemented; the rest
are described so that the architecture has something to grow into.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Project foundation | **Complete** — see [PHASE_1_REPORT.md](../PHASE_1_REPORT.md) |
| 2 | Video ingestion | **Complete** — see [PHASE_2_REPORT.md](../PHASE_2_REPORT.md) |
| 3 | Audio extraction and transcription | Not started |
| 4 | AI highlight detection | Not started |
| 5 | Video clip generation | Not started |
| 6 | Vertical formatting and captions | Not started |
| 7 | Frontend dashboard | Not started |
| 8 | Editing and exports | Not started |
| 9 | Reliability and testing | Not started |
| 10 | Local launch and polish | Not started |

Each phase is only marked complete once its acceptance criteria have been
verified with real evidence. The evidence for Phases 1 and 2 is recorded in
their reports.

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
clips exist.

---

## Phase 3 — Audio extraction and transcription

Not started.

- Extract 16 kHz mono audio with FFmpeg.
- Transcribe to a timed, segmented transcript.
- Store segments in SQLite.
- Choose a speech-to-text engine that works offline on the target machine.
- Frontend: show the transcript and its progress.

---

## Phase 4 — AI highlight detection

Not started.

- Send the transcript to an external LLM through a provider abstraction.
- Return candidate moments with start time, end time, title and a reason.
- Store the candidates; let the user accept or reject them.
- API credentials stay in the backend, in `CLIPFORGE_LLM_*`.
- No provider SDK is committed to until the provider and its endpoint
  compatibility are confirmed.

---

## Phase 5 — Video clip generation

Not started.

- Render each accepted moment into its own file with FFmpeg.
- Record the clip path, duration and dimensions in SQLite.
- Run rendering in the background worker; never inside a request handler.
- Report progress and errors per job.

---

## Phase 6 — Vertical formatting and captions

Not started.

- Convert each clip to 9:16 (1080×1920), keeping the subject in frame.
- Generate SRT/ASS subtitles from the transcript, re-timed to the clip.
- Burn the subtitles into the video.
- Offer per-platform presets (Shorts, Reels, TikTok).

---

## Phase 7 — Frontend dashboard

Not started.

- Replace the single page with a routed dashboard.
- Job list with live status, progress and a job detail view.
- Clip grid with thumbnails and playback.
- Replace the single health check with a real status view.

---

## Phase 8 — Editing and exports

Not started.

- Trim clip start and end points.
- Edit the title and the caption text.
- Choose which clips to export.
- Download individual clips and a ZIP of a batch.
- Add the job cancellation API that the `cancelled` status is waiting for.

---

## Phase 9 — Reliability and testing

Not started.

- End-to-end browser automation over the whole flow.
- Unit tests for transcription, highlighting, rendering and captioning.
- Failure and retry handling for every pipeline step.
- Restart safety: a job interrupted mid-run must be resumable.
- A test suite that runs without an API key or a GPU where possible.

---

## Phase 10 — Local launch and polish

Not started.

- One-command local start (a script that launches the API, the worker and the
  frontend together).
- First-run wizard for configuration and the LLM key.
- Cleanup and disk-usage handling for generated media.
- Error reporting that a non-technical user can act on.
- Final documentation pass and a production build that runs from a clean
  checkout.
