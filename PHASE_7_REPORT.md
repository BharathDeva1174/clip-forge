# ClipForge — Phase 7 report: the frontend dashboard and the workflow

**Status:** complete
**Final verdict:** **READY WITH DOCUMENTED LIMITATIONS**
**Date:** 21 October 2026
**HEAD at the time of writing:** `7502384d4b0ef1aeff3d568bd2db7ed790864378` — unchanged, nothing committed, nothing staged

---

## 1. Executive summary

ClipForge now works as **one product** rather than one long page.

Before this phase there was a single page: a hero, the upload forms, and a
list of every job with the transcript and the moments expanded inline. The
capabilities were all there — Phases 1 to 6 delivered them — but the user
had to scroll a long page to find them, and a finished clip that had
already been rendered was invisible.

Phase 7 turned that into a routed application:

| Route | What it does |
| --- | --- |
| `/` | The dashboard: add a video, and the library of every video that has been submitted |
| `/jobs/[jobId]` | The workflow of one video: the source, the transcript, the moments, the clips, the short-form versions |
| `/status` | Whether the backend is ready, and what this build can do |
| `/integrations` | The AI providers (unchanged) |

The header, the navigation and the footer live in the root layout, so
every route is the same shell. The library shows what the backend actually
knows — the status, the stage, whether there is a transcript, and how many
moments, clips and short-form versions came out of it — and every entry
leads to the workflow, which says what can be done next from the real
state.

**No backend pipeline was moved into the browser.** The dashboard reads the
same API it always did. The one backend change is additive: the job
response now carries the artifact counts, gathered in a fixed number of
batched queries rather than a request per row.

---

## 2. The Phase 7 scope, taken from the roadmap

`docs/ROADMAP.md` is authoritative, and it said:

> ## Phase 7 — Frontend dashboard
>
> - Replace the single page with a routed dashboard.
> - Job list with live status, progress and a job detail view.
> - Clip grid with thumbnails and playback.
> - Replace the single health check with a real status view.

All four were implemented. The phase brief asks for more detail than the
roadmap gives, and it is compatible with it rather than in conflict with
it, so the roadmap was followed and the brief's extra detail used as the
acceptance criteria.

**One deviation, and it is a deliberate one.** The roadmap says *"replace*
the single health check with a real status view". The compact health
indicator was **kept** in the header, and a real status view was **added**
at `/status`; the indicator now links to it.

The reason: the indicator answers a question the status view does not —
*is the backend there right now?* — and it is the only part of the
interface that is visible on every route. Removing it would have made the
app quieter without making it better. The status view answers the bigger
question: *is the configuration good, is the speech model cached, and what
can this build actually do?*

The roadmap's *"clip grid with thumbnails and playback"* was **built**, as
section 4 of the workflow: a grid of every clip of the job, with its
measurements, its preview, its download, and the short-form versions that
came out of it.

The *thumbnails* are the browser's own: the grid plays each file through
the API's media route with `preload="metadata"`, so the browser shows the
first frame. A server-rendered poster image would need a new media
operation, a new file on disk and a lifetime to manage, which is more than
a dashboard phase should take on; it is listed as deferred work in §13.

The clips are shown in two places, and that is deliberate: the moment's own
card holds the **control** that makes a clip, and the grid holds **what
has been made**. Neither has to be kept in step with the other.

---

## 3. The repository baseline and the Git state

### 3.1 The Git state before anything was changed

| | |
| --- | --- |
| HEAD | `7502384d4b0ef1aeff3d568bd2db7ed790864378` |
| Modified | 36 |
| Untracked | 80 |
| Staged | 0 |
| Commits created | 0 |

The working tree carried the uncommitted Phase 3, 3.1, 4, 5 and 6 work,
which was already there before this phase. **None of it was reset,
cleaned, stashed or overwritten.**

### 3.2 The baseline test results, measured before any change

| Check | Baseline | Exit |
| --- | --- | --- |
| Backend | **820 passed, 1 skipped** | 0 |
| Frontend | **92 tests**, 9 files | 0 |
| Type check | — | 0 |
| Lint | — | 0 |
| Production build | — | 0 |
| Audit check | 5 `braces` advisories (dev-only ESLint chain) | 0 |

The backend baseline was re-measured at the start of this phase as **820
passed, 1 skipped** — the count the Phase 6 report ended on — and that is
the number the comparison below uses.

### 3.3 The Git state at the end

| | |
| --- | --- |
| HEAD | `7502384d4b0ef1aeff3d568bd2db7ed790864378` — **unchanged** |
| Modified | 39 |
| Untracked | 90 |
| Staged | **0** |
| Commits created | **0** |

---

## 4. The frontend architecture that was inspected

Before writing anything, the existing frontend was read end to end.

| Piece | What it was |
| --- | --- |
| `src/app/page.tsx` | One page: hero, `VideoSourcePanel`, `JobList`, `PipelinePreview`, a "Build status" section, and a footer. The header was repeated on every page. |
| `src/app/integrations/page.tsx` | The same header and footer, repeated. |
| `src/app/layout.tsx` | The HTML shell and the fonts. **No header, no navigation.** |
| `src/components/AppHeader.tsx` | The header, the navigation (`Jobs`, `Integrations`) and the health indicator. |
| `src/components/JobList.tsx` | The list of jobs — and, expanded inline, the transcript and the highlights. |
| `src/components/TranscriptPanel.tsx` | The transcript, with its own polling. |
| `src/components/HighlightsPanel.tsx` | The analysis and its candidates, each candidate carrying a `ClipPanel`. |
| `src/components/ClipPanel.tsx` | The render control for one candidate, carrying the `FormatPanel`. |
| `src/components/FormatPanel.tsx` | The short-form settings, the artifacts, the preview and the download. |
| `src/components/VideoSourcePanel.tsx` | The upload and the YouTube forms. **No test file.** |
| `src/lib/api.ts` | The typed client. `listJobs` returned `payload.items` unchecked. |
| `src/lib/health.ts` | The health check, kept apart from the component so it can be tested without a DOM. |
| `src/lib/jobs-event.ts` | The window event the upload form uses to ask the list to refresh. |
| `src/lib/config.ts` | The public configuration. |

The backend contract that the dashboard consumes:

| Endpoint | What it gives |
| --- | --- |
| `GET /api/v1/jobs` | `{items, total, limit, offset}` of `JobResponse` |
| `GET /api/v1/jobs/{id}` | One `JobResponse` |
| `GET /api/v1/jobs/{id}/transcript` | The transcript and its segments, paginated |
| `GET /api/v1/jobs/{id}/highlights` | The analysis and its candidates, or `status: "none"` |
| `GET /api/v1/jobs/{id}/clips` | The clips of the job |
| `GET /api/v1/clips/{id}/formats` | The formatted artifacts of a clip |
| `GET /ready` | The configuration, the model cache and the capabilities |

---

## 5. The files added and modified

### 5.1 Added

| File | What it is |
| --- | --- |
| `backend/app/worker_heartbeat.py` | The note the worker leaves behind, so the status view can say whether it is running |
| `backend/tests/test_dashboard.py` | 10 tests for the artifact counts |
| `backend/tests/test_worker_heartbeat.py` | 12 tests for the heartbeat and for what the readiness report says about it |
| `frontend/src/app/jobs/[jobId]/page.tsx` | The workflow route |
| `frontend/src/app/status/page.tsx` | The status route |
| `frontend/src/components/JobWorkflow.tsx` | The workflow of one video |
| `frontend/src/components/JobWorkflow.test.tsx` | 12 tests |
| `frontend/src/components/ClipGrid.tsx` | The grid of the clips of a job, with their short-form versions |
| `frontend/src/components/ClipGrid.test.tsx` | 11 tests |
| `frontend/src/components/MediaPreview.tsx` | A preview that says so when the file cannot be played |
| `frontend/src/components/ServiceStatusPanel.tsx` | The status view |
| `frontend/src/components/ServiceStatusPanel.test.tsx` | 9 tests |
| `frontend/src/components/VideoSourcePanel.test.tsx` | 8 tests — the upload panel had **none** |
| `frontend/src/lib/use-polling.ts` | The shared polling rule |

### 5.2 Modified

| File | What changed, and why |
| --- | --- |
| `frontend/src/app/globals.css` | One global focus ring, in the app's accent, for every focusable control. |
| `frontend/src/app/layout.tsx` | The header, the navigation and the footer moved here. They were repeated in every page; the shell belongs in the shell. |
| `frontend/src/app/page.tsx` | The dashboard. The "Build status" section went (it listed Phases 4–6 as *on the roadmap*), and the hero was tightened. |
| `frontend/src/app/integrations/page.tsx` | The repeated header and footer removed. |
| `frontend/src/components/AppHeader.tsx` | Three navigation entries, the active one matched on `/jobs` as well as `/`, the health indicator linked to the status view, and the header made to fit a narrow screen. |
| `frontend/src/components/HealthIndicator.tsx` | The words are for wider screens; the dot carries the state on a narrow one, and the words stay for a screen reader either way. |
| `frontend/src/components/JobList.tsx` | Now the **library**: it shows the counts, it links to the workflow, and the transcript and the moments moved out to the workflow. |
| `frontend/src/components/ClipPanel.tsx` | **A clip that has already been rendered is now found** when the panel opens. It used to offer to render it again. |
| `frontend/src/components/VideoSourcePanel.tsx` | The returned job id is kept and offered as a link to the new workflow. |
| `frontend/src/components/PipelinePreview.tsx` | Steps 3 to 5 said *next up*; they are done. |
| `frontend/src/lib/api.ts` | The new job fields, the normalisation, `getReadiness()`, the `unknown` status sentinel, and **the media routes resolved against the API's own address** (see §7.4). |
| `backend/app/jobs.py` | `JobArtifacts` — the record of what a job has produced. |
| `backend/app/db.py` | `job_artifacts()` — the batched count query set. |
| `backend/app/worker_heartbeat.py` | The note the worker leaves behind, so the status view can say whether it is running. |
| `backend/app/worker.py` | The worker writes the heartbeat as it works. |
| `backend/app/schemas.py` | The four new fields on `JobResponse`. |
| `backend/app/routers/jobs.py` | The list and the detail endpoint fill them in. |
| `backend/app/routers/health.py` | The capability flags now tell the truth (see §7.2), and the readiness report says whether the worker is running (see §7.3). |
| `backend/app/formatting.py` | The font name pattern now allows a non-Latin script, so **every** detected font can be selected. |
| `backend/tests/test_health.py` | The capability test now asserts the truth (see §7.2). |
| `backend/tests/test_formatting.py` | A test for the font name in another script. |
| `backend/tests/test_worker_heartbeat.py` | 12 tests for the worker's note and for what the readiness report says about it |
| `frontend/src/components/*.test.tsx` | The Job, JobList, ClipPanel and api fixtures grew the new fields; the library test was updated for the new copy and the moved behaviour. |
| `README.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/ROADMAP.md`, `docs/TESTING.md` | The documentation. |

**Nothing was deleted.** The one test that was *moved* — "opens the
transcript of a job on request" — was moved to the component that now owns
that behaviour (`JobWorkflow.test.tsx`), and the library gained three tests
in its place.

---

## 6. The routes and the workflow

### 6.1 The shell

The header, the navigation and the footer are in the root layout now. The
navigation is three entries — **Videos**, **Status**, **Integrations** —
and the current one is marked with `aria-current="page"`.

### 6.2 The library

Each row shows what the backend actually reported:

| Shown | Where it comes from |
| --- | --- |
| The source name | `source_reference` (or "Uploaded video" / "YouTube video") |
| The id, the type and the submission time | `id`, `source_type`, `created_at` |
| The status badge | `status`, and *Transcribed* only when `has_transcript` is true |
| The progress bar | `progress`, only while the job is running |
| The stage | `stage` |
| **Transcript saved · N moments · N clips · N short-form versions** | `has_transcript`, `analysis_status`, `candidate_count`, `clip_count`, `format_count` |
| The error | `error.message` and `error.code` |
| **Open workflow** | A link to `/jobs/<id>` |

Nothing is invented. There is no "recent activity", no invented duration
and no invented percentage.

### 6.3 The workflow

The three stages, in the order they happen:

```
1 · Source      the name, the status, the facts, and the error
2 · Transcript  the segments, or why there are none
3 · Moments     the candidates, with the control that makes a clip
4 · Clips and short-form versions
               the grid: every clip, its measurements, its preview,
               its download, and the short-form versions made from it
```

The workflow ends with **what can be done next**, driven by the real
state:

| The state | What it says |
| --- | --- |
| Failed | The reason is above; nothing else can be done with it until it is submitted again. |
| Still running | The worker is still working on it. This page keeps itself up to date. |
| No transcript | There is no transcript, so there are no moments to find. |
| No analysis yet | The transcript is ready. Find the moments worth keeping next. |
| The analysis failed | The highlight analysis failed. You can try it again below. |
| No moments | The model found nothing worth clipping. That is a real answer, not a failure. |
| Moments but no clip | Accept a moment below, then render it into a clip. |
| A clip but no short-form version | The clip is rendered. Make a short-form version of it below. |
| Everything done | Everything is done. Preview and download the outputs below. |

The actions themselves come from the existing panels, so they are offered
exactly when the backend will accept them. **Formatting is not offered
before there is a completed clip** — that is enforced by the backend, and
the workflow does not pretend otherwise.

### 6.4 The status view

`/status` shows the backend's readiness report: the service and its
version, whether the configuration loaded, whether the speech model is
already on this machine, and **the ten capabilities this build has**,
each with a note saying what it does.

---

## 7. The API contract, and the backend changes

### 7.1 The one backend change

The job response gained four fields:

| Field | What it is |
| --- | --- |
| `analysis_status` | The status of the most recent highlight analysis, or `null` when there is none |
| `candidate_count` | How many moments that analysis produced |
| `clip_count` | How many clips have been made from the job |
| `format_count` | How many formatted artifacts have been made from the job |

They are gathered by `job_artifacts()` in `app/db.py`, **for the whole page
in a fixed number of queries**: one for the latest analysis of each job,
one for the candidates, one for the clips and one for the formats.

This matters. The obvious alternative — the dashboard asking
`/highlights` and `/clips` for every row — is two requests per job, so a
library of fifty jobs is a hundred requests every time the page loads.
The codebase already had the pattern: `job_ids_with_transcripts` gathers
the transcript flag for a whole page in one query, for exactly the same
reason.

The "most recent analysis" is chosen by the same rule as
`latest_analysis_for_job` (`created_at` descending, then `id`
descending), so **the count never disagrees with the analysis the detail
endpoint reports**. `test_the_candidate_count_agrees_with_the_analysis_endpoint`
asserts exactly that.

No route was added. No table was added. No migration was needed: the
counts are read from tables that already exist.

### 7.2 The capability flags

`/ready` reported `clip_rendering`, `vertical_formatting` and
`subtitle_generation` as **false** — flags left over from Phase 1, when
those features did not exist. Phases 4 to 6 built them, and nobody
updated the flags.

A status view that says *"Clip rendering: unavailable"* on a build that
renders clips would be actively misleading, so the flags now tell the
truth: all ten are `true`.

The Phase 1 test `test_ready_does_not_claim_unimplemented_capabilities`
asserted the opposite. It was **renamed** to
`test_ready_reports_exactly_the_capabilities_that_exist` and now asserts
that *every* flag matches the feature it names — in both directions — plus
that there is no capability outside the known set. The test's intent is
preserved and strengthened: it still proves the endpoint does not advertise
a capability that does not exist.

This is the one place where an existing test's expectations changed, and
it is called out here rather than buried.

### 7.3 The status view covers the worker, not just the API

The roadmap asks for *"a real status view that covers both the API and
the worker"*, and a status view that only reported the API would have been
half the answer. The API and the worker are separate processes, so the
API cannot see the worker: a job can sit in the queue for ever with
nothing to explain why.

So the worker leaves a small note behind. `app/worker_heartbeat.py`
writes `worker-heartbeat.json` into the storage root every few seconds,
and the API reads it to say how long ago the worker was last seen. The
readiness report gains a `worker` field:

```json
{"running": true, "worker_id": "ASUS-5460-f913c3",
 "last_seen": "2026-10-21T09:56:10.079162Z", "seconds_since_seen": 5.6}
```

and the status view says **"Background worker: Running — seen 3s ago"**,
or **"Not running — nothing will be processed until it is started"**.

It is a **file** rather than a row on purpose. The heartbeat is about the
*process*, not about a job; it changes constantly; and it must not need a
migration or a transaction to read. A missing note and a stale note mean
the same thing to a user — the worker is not running — and that is the
answer the user needs. Writing it is best-effort: a worker that cannot
write the note keeps working.

The liveness contract at `/health` is **unchanged** — it is a stable
contract and says nothing about the worker.

### 7.4 The media routes are resolved against the API's address

The API answers with a `media_url` that is **relative to itself**, for
example `/api/v1/clips/<id>/media`. That is right for a client on the
same origin, and wrong for the frontend, which runs on a different port:
the browser asked the *frontend* for the file, got a 404, and showed an
empty black rectangle where the preview should have been. The same was
true of every download link.

The fix is in the client, in one place: `mediaUrl()` resolves a relative
media path against the API's address, and the clip and format responses are
normalised through it. Every preview and every download now points at the
API's own route.

**This was found in the browser, not in the tests.** The component tests
had asserted the *relative* URL — which is the bug — and passed. The
browser showed a preview that would not load; the media route was checked
directly with `Invoke-WebRequest` and answered 200 with the right bytes,
which proved the server was fine and the client was wrong. The five test
expectations that encoded the bug were then corrected, and they now assert
the absolute URL.

### 7.5 What was **not** changed

- No endpoint was removed, renamed or given a new response shape. The four
  new fields are additive.
- No migration. `SCHEMA_VERSION` is still 6.
- No new dependency. `backend/requirements.txt` is unchanged by this phase
  (its last modification is from the Phase 3/4 work that was already
  uncommitted).
- No video processing, transcription or FFmpeg moved into the browser.
- No authentication, no telemetry, no cloud, no external media service.

---

## 8. The state handling and the polling

### 8.1 One rule, in one place

`frontend/src/lib/use-polling.ts` holds the rule every view follows:

- there is **one** interval, never one per render;
- it runs only while the resource can still change;
- it is cleared when the component goes away;
- the callback is always the current one, so an interval never calls a
  stale closure.

The library and the workflow use it. The panels that already existed
(`TranscriptPanel`, `HighlightsPanel`, `ClipPanel`, `FormatPanel`) already
implemented the same rules with their own effects, and they were **left
alone** rather than rewritten — a wholesale rewrite of five working
components would have been a bigger risk than the duplication it removed.

### 8.2 The polling stops

| When | The behaviour |
| --- | --- |
| The job reaches a terminal state | The library and the workflow stop asking |
| The status is one this build does not know | Polling stops — the interface cannot describe it, so asking for ever would be waste |
| The component unmounts | The interval is cleared |
| The view is left | The component unmounts, so the interval is cleared |

Two tests prove the cleanup: the workflow's *polls while the job is
running and stops when it finishes*, and *stops polling when the component
is gone* — the latter unmounts the component, advances the clock by nine
seconds and asserts that **no further request was made**.

### 8.3 A transient failure is not a lost state

The library keeps the last list it had when a refresh fails, and shows the
error **above** it rather than replacing it with an empty list. The
workflow keeps the job it had and shows the error. A failure is never
dressed up as an empty library, and a success is never claimed when the
API is unavailable.

### 8.4 The normalisation at the boundary

The API client normalises every answer it receives:

| Answer | What it becomes |
| --- | --- |
| A status that is missing or unknown | `unknown` — never `queued` |
| A progress figure outside 0–100 | Clamped |
| A count that is not a number | `0` |
| An error envelope that is not one | `null` |
| A job without an id | Dropped — it cannot be addressed |
| A transcript without its segments | A transcript with no segments |
| A list that is not a list | An empty list |

This is not decoration. **The `TranscriptPanel` used to crash** when the
API answered with something that was not a transcript: it read
`transcript.segments.length` without checking. The crash was found by the
new tests, and the fix went into the client, where it protects every
caller — the same bug class the Phase 6 report recorded for the clip
list.

---

## 9. The accessibility and the responsive behaviour

### 9.1 The labels

Every control has a real accessible name, proved with `getByLabelText`,
which computes the name the way a browser does:

| Control | Its name |
| --- | --- |
| The file input | "Local video file" |
| The URL input | "YouTube link" |
| The authorisation box | "I own this video or have permission to download and reuse it." |
| The resolution | "Resolution" |
| The layout | "Layout" |
| The caption switch | "Burn the captions in" |
| The font, size, colours, outline and position | Their own names, with the help text as a sibling so it does not become part of the name |

The browser panel's snapshot reported two checkboxes as
`missingAccessibleName`. That is a **limitation of the tool's heuristic**
— it reports a select's *options* as its label and looks for an `aria-label`
attribute — and it is contradicted by the tests, which compute the real
name. It is reported here rather than glossed over.

### 9.2 The status is not only colour

Every status is carried by a **word** as well as a colour: the badge says
*Transcribed*, *Queued*, *Failed*; the capabilities say *Available* or
*Unavailable*; the format artifacts say *Rendering…*. The coloured dot in
the header is `aria-hidden`, and the words next to it are for screen
readers at every width.

### 9.3 The keyboard and the focus

The controls are real buttons, links, inputs and selects, in the order a
keyboard would expect.

**The focus ring is the app's own.** There was exactly one control in the
whole interface with an explicit focus style, and the browser's default —
a blue outline, inherited from `outline: auto` — clashed with the ember
palette and was easy to miss on the dark surfaces. A single rule in
`globals.css` now gives **every** focusable control the same ember ring:

```css
:focus-visible {
  outline: 2px solid var(--color-ember-400);
  outline-offset: 2px;
}
```

`:focus-visible` rather than `:focus`, so the ring appears for a keyboard
and not for a mouse click. It was verified in the browser — the focused
input is visibly ringed — and the rule is in the production CSS.

### 9.4 The narrow screen

The header was overflowing horizontally at 390 px — the health indicator
was pushed off the screen. It was caught in the browser, not in the tests,
and it is fixed:

- the header **wraps** on a narrow screen;
- the tagline is hidden below `sm`;
- the health indicator shows only its dot below `sm`, and the words remain
  for a screen reader.

The screenshots before and after are in
`.openchamber/screenshots/phase7-mobile-workflow-*.jpg`.

The rest of the layout is already narrow-screen friendly: the facts grid
is two columns, the transcript and the moments stack, and the video
previews are capped with `max-w-sm` so they cannot overflow.

---

## 10. The tests

### 10.1 The new backend tests

`backend/tests/test_dashboard.py` — 10 tests:

| Test | What it proves |
| --- | --- |
| `test_a_fresh_job_has_produced_nothing` | The counts are zero, and there is no analysis |
| `test_the_counts_are_collected_for_a_whole_page` | The counts are per job, and one job's do not leak into another's |
| `test_an_empty_page_costs_no_queries` | An empty page is an empty dictionary |
| `test_the_count_follows_the_most_recent_analysis` | A second analysis replaces the first in the count |
| `test_the_formatted_artifacts_are_counted` | The formats are counted with the clips |
| `test_the_job_list_reports_the_counts` | The list endpoint carries them |
| `test_one_job_reports_the_same_counts_as_the_list` | **The two endpoints do not disagree** |
| `test_the_candidate_count_agrees_with_the_analysis_endpoint` | **The library and the detail view do not disagree** |
| `test_a_job_without_an_analysis_says_so` | `null`, not a fabricated status |
| `test_the_list_still_works_without_any_jobs` | An empty library is still an empty library |

### 10.2 The new frontend tests

| File | Tests | What it covers |
| --- | --- | --- |
| `JobWorkflow.test.tsx` | 12 | The source and its facts, the transcript, what can be done next, **that formatting is not offered before there is a clip**, a job with no transcript, a failed job with its code, a job that does not exist, a network failure that is not mistaken for an empty job, a malformed job, the polling, and the polling stopping on unmount |
| `ServiceStatusPanel.test.tsx` | 7 | Ready, the capabilities, an unavailable capability, the model not being cached, a failure, a retry, a malformed answer |
| `VideoSourcePanel.test.tsx` | 8 | What is accepted, **the accessible names**, the authorisation, the queued outcome with the returned id, **the wording that says it is queued and not finished**, an actionable error, a network failure, **the duplicate-submission guard** |
| `JobList.test.tsx` | +3 | The link to the workflow, what the job has produced, that an analysis that has not happened is not claimed, and **the submission time** |
| `ClipPanel.test.tsx` | +3 | **A clip that has already been rendered is found**, that it is not offered again, and that another candidate's clip is ignored |
| `ClipGrid.test.tsx` | 11 | The grid: nothing yet, the measurements, the preview and the download, the short-form versions, a version rendered without captions, a failure to load, a transient failure that keeps the clips, the polling, and the polling stopping on unmount. Plus the preview that says so when the file cannot be played |
| `api.test.ts` | +13 | The normalisation of the list, one job, the transcript and the readiness report |

### 10.3 The whole suite

| Check | Baseline | Final | Exit |
| --- | --- | --- | --- |
| **Backend** | 820 passed, 1 skipped | **843 passed, 1 skipped** | 0 |
| **Frontend** | 92 tests | **151 tests** | 0 |
| **Type check** | 0 | **0** | 0 |
| **Lint** | 0 | **0** | 0 |
| **Production build** | 0 | **0** | 0 |
| **Audit check** | 5 advisories | **5 advisories** (the same dev-only `braces` ones) | 0 |
| `e2e_workflow.py` | — | **every step passed** | 0 |
| `e2e_phase4.py` | — | **every step passed** | 0 |
| `e2e_phase5.py` | — | **every step passed** | 0 |
| `e2e_phase6.py` | — | **every step passed** | 0 |

**+23 backend tests, +59 frontend tests.** The backend's one skip is the
privileged-symlink test, which cannot run without the privilege.

The per-file frontend counts, so nothing is hidden behind a total:

```
ClipGrid.test.tsx             11      JobList.test.tsx              14
ClipPanel.test.tsx            12      JobWorkflow.test.tsx          12
FormatPanel.test.tsx          15      ServiceStatusPanel.test.tsx    9
HealthIndicator.test.tsx       3      TranscriptPanel.test.tsx      12
HighlightsPanel.test.tsx      11      VideoSourcePanel.test.tsx      8
IntegrationsPanel.test.tsx    16      api.test.ts                   23
                                       health.test.ts                 5
```

### 10.4 The production build

```
Route (app)
┌ ○ /
├ ○ /_not-found
├ ○ /integrations
├   /jobs/[jobId]
│ └ ◐ /jobs/[jobId]
└ ○ /status
```

Three routes are static, and the workflow is a partial prerender — it
cannot be prerendered, because the job id is only known at request time.
See §11.2.

---

## 11. The browser verification

**This was performed.** The OpenChamber browser panel drove a **real
Chromium** against the **running application**: the real API process, the
real worker process, the real SQLite database and the real FFmpeg.

The data was **not** mock data: the Phase 6 end-to-end script was run
against a known storage directory, which produced a real video, a real
transcription by the local Whisper model, a real highlight analysis, a
real rendered clip and two real formatted artifacts (one of them
deliberately broken, so the failure path could be seen too).

### 11.1 What was checked, and what was seen

| # | Step | What the browser showed |
| --- | --- | --- |
| 1 | Open the application | The dashboard, the header with the navigation, the health indicator reading **Backend online** |
| 2 | Load the dashboard | "Your videos" with the real job: **speech.mp4**, *Transcribed*, the submission time, and the line **"Transcript saved · 1 moment · 1 clip · 3 short-form versions"** — the counts from the API, not from the interface |
| 3 | Open the workflow | The source with its real facts (5 s, 640 × 360, h264, the submission time), the transcript with its two real segments, and the moment with its time range and score |
| 4 | The clip | **"Clip ready · 0:04 · 640×360 · measured 4.50s"** with a Download link — this only appeared after the discovery fix in §11.3 |
| 5 | The short-form versions | The real font list — every font read out of the font files on this machine — the two finished artifacts with their sizes and the **"2 captions burned in"** note, and the deliberately broken one with **"format_source_missing"** and a *Render again* button |
| 6 | The clip grid | **Section 4**: the clip with its measurements, a player that **actually plays the file** (0:00 / 0:04), *Download the clip*, and both short-form versions with their own downloads |
| 7 | Preview and download | The `<video>` element played the formatted file through the API's media route. Fetching both media routes returned **63854 bytes** (640 × 360 h264) and **163479 bytes** (720 × 1280 h264), confirmed with FFprobe |
| 7 | The status view | **Ready**, the configuration loaded, the speech model already on this machine, **the background worker running — seen 3s ago**, and all ten capabilities **Available** |
| 8 | The Integrations page | The provider, its actions, and **no key material anywhere** |
| 9 | Navigate away and back | The **back** button returned to the dashboard and **forward** returned to the workflow |
| 10 | Refresh a supported route | The workflow URL was opened as a **fresh server request** and rendered correctly |
| 11 | An invalid id | **"No such video — Nothing is stored under that id. It may have been removed, or the link may be wrong."** with a *Back to your videos* link |
| 12 | A narrow screen | **Caught a defect**: the header overflowed horizontally at 390 px and the health indicator was pushed off the screen. Fixed, and re-checked — the header wraps and the horizontal scrollbar is gone |

The screenshots are in `.openchamber/screenshots/`.

### 11.2 The one thing the build would not do

The production build failed at first:

> Route "/jobs/[jobId]": Next.js encountered URL data `usePathname()` in a
> Client Component outside of `<Suspense>`.

This is Next 16 with Cache Components on. A dynamic route whose parameter
is not known at build time cannot be prerendered, so the pathname cannot
be resolved, and the `usePathname()` in the shared header suspends.

The Next 16 documentation gives two answers: a Suspense boundary, or a
blocking route. The workflow genuinely depends on a request-time id, so it
is declared a blocking route:

```tsx
export const instant = false;
```

The header keeps its `usePathname()`, the layout keeps its static shell,
and the build is clean.

### 11.3 The defects the browser found

Three, all of them real, all of them fixed.

**A clip that had already been rendered was invisible.** `ClipPanel` only
ever showed a clip after it had *asked* for one, so opening the workflow of
a finished video offered to render the clip all over again, and the
finished clip and its short-form versions could not be seen at all.

The fix is small and in the existing idiom: on mount, the panel asks the
API for the job's clips and looks for one made from this candidate. A
temporary failure falls back to offering the render, so it never hides a
clip permanently.

**The previews and the downloads were asking the wrong server.** The API
answers with a `media_url` that is relative to *itself*, and the browser
resolved that against the *frontend's* address, so every preview and every
download was a 404 — an empty black rectangle where the video should have
been. The media route itself was fine: it answered 200 with the right
bytes when it was asked directly. The fix is in the client, in one place
(§7.4), and the five test expectations that had encoded the bug now
assert the correct URL.

**The header overflowed horizontally at 390 px** — the health indicator
was pushed off the screen. The header now wraps, the tagline is hidden
below `sm`, and the health indicator shows only its dot there, with the
words kept for a screen reader.

The first two are exactly the kind of gap the phase was for, and **the
browser is what found them**: the component tests had asserted the buggy
relative URL and passed, and there was no test that opened a panel for a
candidate that already had a clip.

---

## 12. The regression checks for Phases 1 to 6

| Phase | What was checked | Result |
| --- | --- | --- |
| 1 - the shell | The API, the health and readiness contracts, the error envelope, CORS | 843 passed, 1 skipped |
| 2 — ingestion | The upload and the YouTube submission, the URL validation | `e2e_workflow.py` and `e2e_phase4.py` pass |
| 3 — transcription | The local transcription, the transcript endpoint, the timeout and the recovery | `e2e_workflow.py` passes |
| 3.1 - hardening | The model, the cache, the child process | 843 passed, 1 skipped |
| 4 — the AI integrations | The provider path, the credential store, the analysis | `e2e_phase4.py` passes; the integrations page verified in the browser |
| 5 — the clips | The clip rendering, the retry, the preview and the download | `e2e_phase5.py` passes; the clip and its download verified in the browser |
| 6 — the formatting | The portrait rendering, the caption burn-in | `e2e_phase6.py` passes; the artifacts, the caption count and the download verified in the browser |

**The existing migrations and the stored data are untouched.** No migration
was added, and `SCHEMA_VERSION` is still 6.

**The local-first architecture is intact.** The browser only ever talks to
the local API. No telemetry, no analytics, no cloud storage, no external
media service, no authentication infrastructure was added.

**The dependency advisory is unchanged.** `npm run audit:check` exits 0 with
the same five `braces` advisories in the dev-only ESLint chain. No
dependency was upgraded, downgraded or replaced, and Next.js was not
touched.

---

## 13. The known limitations and the deferred work

### 13.1 The honest ones

1. **No browser automation.** The browser verification was done by hand
   with the OpenChamber browser panel. It is real — a real Chromium
   against the running application — but it is not repeatable in CI. The
   automated browser testing is still a Phase 9 item, and the phase brief
   says a framework must not be introduced unnecessarily.

   **The responsive behaviour was checked in the browser, not in a test.**
   The test setup is jsdom, which has no layout engine: a media query
   cannot be evaluated and a "the element is 390 px wide" assertion would
   be meaningless. The narrow-screen layout was therefore verified by
   looking at it — a screenshot at 390 px — and the horizontal overflow it
   revealed was fixed. A test that asserted nothing real was not written
   to make the count look better.

2. **A font name may not contain a comma or a quote.** The caption style
   is a comma-separated list of libass options, so those two characters
   cannot be in a font name. Everything else can, including a non-Latin
   script — the font list offers all **195** fonts this machine has. (The
   name pattern was widened during this phase, because one of them —
   `微軟正黑體` — was being refused for no good reason.)

3. **The library has no search, filter or sort.** With a local app and a
   handful of videos, a search box would be a control with nothing to do.
   The brief asked for those "only where useful", and they are not yet.

4. **The grid has no server-rendered thumbnails.** The browser shows the
   first frame of each file, because the grid plays it through the media
   route with `preload="metadata"`. A poster image would need a new media
   operation, a new file on disk and a lifetime to manage, which is more
   than a dashboard phase should take on.

5. **The library shows a fixed page of 50.** The API is paginated
   (`limit`, `offset`), and the frontend asks for 50. There is no pager,
   so a library of more than 50 videos shows the first 50. The API can
   already do more; the interface does not yet offer it.

6. **The workflow loads everything it shows.** The transcript is one
   request for up to 500 segments, and the moments are one request for up
   to 100 candidates. A very long video with more than 500 segments shows
   the first 500. The API paginates; the interface does not yet page.

7. **There is no way to cancel anything.** A queued or running job, clip
   or format cannot be cancelled. This gap was already there and is
   listed in the architecture as not existing; Phase 8 is where the
   cancellation API is planned.

8. **The counting is per request, not cached.** Every library load counts
   the artifacts again. It is four queries for the whole page, which is
   nothing on a local SQLite file, but it is not free.

9. **The capability flags are static, even though the worker's status is
   not.** The flags describe the *build* — this build can render clips —
   and they do not check that FFmpeg is installed on this machine. The
   worker's own status *is* live: it comes from a heartbeat the worker
   writes, and it can be up to half a minute out of date.

10. **The heartbeat is a file in the storage root.** It is one more file
   there, and it is only as good as the clock of the machine that wrote
   it. It is deliberately not a database row: it is about the process, and
   it must not need a migration to read.

### 13.2 The environment-dependent skips

- The privileged-symlink test is skipped — **1 skip** in the backend
  suite. It cannot run without the privilege, and it has nothing to do with
  this phase.
- The `test_a_download_that_exceeds_the_limit_is_rejected` test in
  `backend/tests/test_acquisition.py` remains network-dependent. It
  **passed** in this phase's runs, but it can fail without a network.

### 13.3 The carried-forward items

- The **Node LTS parity check** is still not runnable: no LTS is installed
  and the repository has no CI. Carried from Phase 3.1; not a Phase 7
  regression.
- The **`braces` advisory** in the dev-only ESLint chain is unchanged and
  acknowledged.

---

## 14. The security and the privacy

### 14.1 What the frontend does and does not receive

The dashboard reads the same API it always did. It receives no secrets, no
credential references and no filesystem paths, and it displays none:

- the API responses contain no path, no worker id and no lease information
  — that was already true of `JobResponse`, and the four new fields are
  counts;
- the media routes resolve the stored path **inside the storage root**, so
  a formatted clip cannot be used to reach a file elsewhere on the machine;
- the **integrations page** was verified in the browser to show the
  provider's configuration and its actions, and **no key material**.

### 14.2 The API client

- There is **one** client, `frontend/src/lib/api.ts`, and every component
  goes through it. No component calls `fetch()` directly. The one place
  that does — the upload, which needs progress events — is inside the
  client too.
- The base URL comes from `NEXT_PUBLIC_API_BASE_URL`, which is an address
  and nothing else. **No secret is in any `NEXT_PUBLIC_*` variable.**
- The error envelope is preserved: `ApiError` carries the stable code and
  the message, and a non-JSON answer becomes a clean generic error.
- Nothing is written to `localStorage` or `sessionStorage`. There is no
  second frontend database: the backend is the only source of truth, and
  reloading the page reads it again.
- The transcript text is never logged.

### 14.3 What the dashboard did **not** add

- No authentication, no authorisation layer, no session handling.
- No telemetry, no analytics, no error reporting to a third party.
- No cloud storage, no external media service, no network access beyond
  the local API.
- No change to the credential store and no change to the provider
  integrations.
- No new dependency.

The application is still an **unauthenticated local application**. It is
not production-ready for a public deployment, and it is not claimed to be.

---

## 15. The definition of done

| # | Item | Done |
| --- | --- | --- |
| 1 | The authoritative roadmap inspected, and the correct scope implemented | ✅ §2, with one documented deviation |
| 2 | A coherent path through the existing capabilities | ✅ Four routes, one shell |
| 3 | Existing backend data drives the interface, not demo data | ✅ The counts come from the API; verified in the browser against real data |
| 4 | Loading, empty, pending, failed and completed states are clear and accurate | ✅ Every one has a test and was seen in the browser |
| 5 | Navigation and refresh work for the supported routes | ✅ Back, forward and a fresh server request, verified in the browser |
| 6 | Polling and requests clean up correctly | ✅ One shared rule; two tests prove the cleanup |
| 7 | Malformed API responses do not crash the dashboard | ✅ Normalised at the boundary; a real crash was found and fixed |
| 8 | Preview and download stay connected to the right resources | ✅ Verified: 63854 and 163479 bytes, both probed |
| 9 | The new behaviour is tested, and the complete existing suites pass | ✅ +23 backend, +59 frontend; 843 and 151, all green |
| 10 | Type check, lint and production build pass | ✅ 0, 0, 0 |
| 11 | Browser verification performed and reported truthfully | ✅ A real browser against the running app; three defects found and fixed |
| 11a | The clip grid the roadmap asked for | ✅ Section 4 of the workflow: every clip, its measurements, its preview and its download, with the short-form versions |
| 12 | The documentation and `PHASE_7_REPORT.md` are updated | ✅ This file, plus the README, the architecture, the decisions, the roadmap and the testing guide |
| 13 | No secrets or local filesystem paths leak into the frontend | ✅ §14 |
| 14 | Phases 1 to 6 remain intact | ✅ All four end-to-end scripts pass |
| 15 | No commit is created | ✅ HEAD is unchanged, nothing is staged |

---

## 16. The final Git status

```
HEAD    : 7502384d4b0ef1aeff3d568bd2db7ed790864378   (unchanged)
modified: 39
untracked: 95
staged   : 0
commits : 0
```

The fifteen new files of this phase are among the untracked ones:

```
backend/app/worker_heartbeat.py
backend/tests/test_dashboard.py
backend/tests/test_worker_heartbeat.py
frontend/src/app/jobs/[jobId]/page.tsx
frontend/src/app/status/page.tsx
frontend/src/components/ClipGrid.test.tsx
frontend/src/components/ClipGrid.tsx
frontend/src/components/JobWorkflow.test.tsx
frontend/src/components/JobWorkflow.tsx
frontend/src/components/MediaPreview.tsx
frontend/src/components/ServiceStatusPanel.test.tsx
frontend/src/components/ServiceStatusPanel.tsx
frontend/src/components/VideoSourcePanel.test.tsx
frontend/src/lib/use-polling.ts
PHASE_7_REPORT.md
```

**Nothing was committed, nothing was staged, and no existing work was
discarded.**

---

## 17. The final verdict

### **READY WITH DOCUMENTED LIMITATIONS**

### Why

**The product works as one thing.** The dashboard, the library, the
workflow, the status view and the integrations page share one shell, one
navigation and one set of states. The library says what a video has become
using the backend's own numbers; the workflow says what can be done next
from the real state and offers nothing before it is possible. The
capability flags, which had been lying since Phase 4, now tell the truth.

**The evidence is real.** The browser check was not a screenshot of a mock:
it was a real Chromium against the real API, the real worker and a real
SQLite database holding a real transcription, a real analysis, a real clip
and two real formatted artifacts. The download routes returned bytes that
FFprobe could read. Back, forward, a fresh server request and an invalid
id were all exercised.

**Three real defects were found and fixed, not worked around.** The
`TranscriptPanel` crash on a malformed answer was found by the new tests
and fixed in the client, where it protects every caller. The invisible
already-rendered clip and the previews that were asking the frontend
instead of the API were found by the browser and fixed in the panel and in
the client. The horizontal overflow at 390 px was found by the browser and
fixed in the header.

**One test failure was investigated rather than dismissed.** During a
concurrent run of the whole suite and the end-to-end scripts,
`test_a_job_left_behind_by_a_dead_worker_is_recovered` failed: the worker
process could not start fast enough for the test's timeout. It was re-run
on its own (**3 passed in 1.66 s**) and the whole suite was then re-run
**alone** (843 passed). It was CPU contention, not a regression — but it is
recorded here rather than quietly re-run until it was green.

**Everything still passes.** The backend is at 843 passed and 1 skipped,
the frontend at 151, the type check, the lint, the production build and
the audit check are all green, and all four end-to-end scripts pass.

**Why not plain `READY`:** because there is still no repeatable browser
automation — the check was done by hand, and the phase brief says a
framework must not be introduced unnecessarily. The library also has no
paging, no search and no filter, and the transcript and the moments are
loaded in one page each. These are narrow gaps, and they are named rather
than hidden.

**Why not `NOT READY`:** every requirement in the brief that is applicable
is met and demonstrated with executed evidence. The scope boundaries were
respected: no video-processing engine, no AI-provider protocol, no editing
engine, no publishing, no authentication, no cloud, and no dependency was
added.

### One last note

This is an unauthenticated local application. It is **not** production
readiness for a public deployment, and it is not claimed to be. Phase 7 is
about a coherent local product, and that is what has been delivered and
verified.
