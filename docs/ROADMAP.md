# Roadmap

ClipForge is planned as ten phases. Only Phase 1 has been started; the rest are
described so that the architecture has something to grow into.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Project foundation | **In progress** — acceptance criteria under verification |
| 2 | Video ingestion | Not started |
| 3 | Audio extraction and transcription | Not started |
| 4 | AI highlight detection | Not started |
| 5 | Video clip generation | Not started |
| 6 | Vertical formatting and captions | Not started |
| 7 | Frontend dashboard | Not started |
| 8 | Editing and exports | Not started |
| 9 | Reliability and testing | Not started |
| 10 | Local launch and polish | Not started |

Phase 1 stays *in progress* until every item in its acceptance list has been
verified with real evidence. The evidence is recorded in
[PHASE_1_REPORT.md](../PHASE_1_REPORT.md).

---

## Phase 1 — Project foundation ✅ *in progress*

Repository, backend API skeleton, frontend shell, configuration, tests and
documentation.

Delivers:

- FastAPI application with `/health`, `/ready` and `/api/v1`.
- Typed, environment-driven configuration with secret handling.
- Next.js shell with a live backend status indicator (loading, success,
  failure, retry).
- A disabled video-input section and a pipeline preview, so the UI never
  pretends a feature exists that does not.
- Backend test suite, frontend unit and component tests, lint and type
  checks.
- Documentation: README, architecture, development, testing, roadmap,
  decisions.

Acceptance criteria are listed in [PHASE_1_REPORT.md](../PHASE_1_REPORT.md).

---

## Phase 2 — Video ingestion

Not started.

- Upload local video files to `CLIPFORGE_STORAGE_DIR/sources`.
- Accept a YouTube URL, **only** for videos the user is authorised to access
  and permitted to download, respecting YouTube's terms of service.
- Create a job row in SQLite and return a job identifier.
- Background worker picks the job up; the API never blocks on it.
- Frontend: replace the disabled panel with a real upload and URL form.

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

- Replace the landing shell with a working dashboard.
- Job list with live status and progress.
- Clip grid with thumbnails and playback.
- Replace the current single health check with a real status view.

---

## Phase 8 — Editing and exports

Not started.

- Trim clip start and end points.
- Edit the title and the caption text.
- Choose which clips to export.
- Download individual clips and a ZIP of a batch.

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

- One-command local start (a script that launches both applications).
- First-run wizard for configuration and the LLM key.
- Cleanup and disk-usage handling for generated media.
- Error reporting that a non-technical user can act on.
- Final documentation pass and a production build that runs from a clean
  checkout.
