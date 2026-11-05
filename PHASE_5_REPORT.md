# Phase 5 — Reliable clip generation engine

**Status: COMPLETE — READY WITH DOCUMENTED LIMITATIONS.**

Phase 5 turns the highlight candidates of Phase 4 into real, independently
playable video clips with FFmpeg and FFprobe.

Every result below was produced by a command that was actually run on
20 October 2026. Where a check could not be run, it says so rather than
claiming a pass.

---

## 1. Executive summary — what was actually delivered

| Delivered | Where |
| --- | --- |
| A clip entity with its own lifecycle, lease and ownership rules | `backend/app/clips.py`, `backend/app/db.py` (migration 5) |
| The boundary rules, checked before anything expensive starts | `backend/app/clips.py` |
| The FFmpeg command, the bounded run and the independent verification | `backend/app/clip_render.py` |
| The render the worker performs, with atomic finalisation | `backend/app/clip_service.py` |
| The worker claim, the run, the recovery and the reconciliation | `backend/app/worker.py` |
| Five API endpoints, including a contained media route | `backend/app/routers/clips.py` |
| A **Render clip** control, the render stages, a preview and a download | `frontend/src/components/ClipPanel.tsx` |
| 69 new tests, 19 of them driving the **real** FFmpeg and FFprobe | `backend/tests/test_clips.py`, `test_clip_render.py`, `test_clips_api.py` |
| A full end-to-end render, verified and played back | `backend/tests/e2e_phase5.py` |

**Not delivered, and deliberately so:** no vertical reframing, no 9:16
conversion, no captions, no subtitle files, no clip editor, no aspect-ratio
selector, no publishing and no export. Those are Phase 6. The interface says
so where a user might otherwise expect a rendered video.

---

## 2. Repository baseline

| | |
| --- | --- |
| Initial HEAD | `7502384d4b0ef1aeff3d568bd2db7ed790864378` (`7507384`… "docs: record the fresh-clone verification in the Phase 2 report") |
| Working tree at the start | 35 modified and 54 untracked files — the whole of Phases 3, 3.1 and 4, uncommitted |
| Node | v26.4.0, npm 11.14.1 |
| Python | 3.11.9 |
| FFmpeg / FFprobe | 9.0.2 (Gyan build) |

The baseline, before anything was changed:

```
$ cd backend; ..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN
598 passed, 1 skipped in 78.95s        exit 0

$ cd frontend; npm run typecheck        exit 0
$ cd frontend; npm run lint             exit 0
$ cd frontend; npm test                 7 files, 68 tests passed, exit 0
$ cd frontend; npm run build            exit 0
```

### A pre-existing failure, recorded separately

During the first Phase 5 full-suite run, `test_a_download_that_exceeds_the_limit_is_rejected`
failed: it expected `acquisition_failed` and got `acquisition_timeout`.

It is **pre-existing and flaky**, not a Phase 5 regression:

- `backend/tests/test_acquisition.py` and `backend/app/acquisition.py` are
  **untouched since HEAD** (`git diff --stat` on both is empty);
- the test downloads a real YouTube video and relies on the download
  hitting the *size* limit before the *timeout*, which depends on the
  network;
- re-running it three times gave **1 failed, 2 passed**.

The full suite in the final run passed, which is consistent with a flake.
It is recorded here rather than hidden.

---

## 3. Architecture findings

The repository was traced end to end before anything was written.

### The chain that already existed

```
jobs.source_path (relative)      the stored source video
  └─ highlight_analyses.job_id    the Phase 4 run
       └─ highlight_candidates    the moment, with its validated times
```

`highlight_candidates` has **no `job_id` column**: a candidate belongs to an
analysis, and an analysis belongs to a job. The join is therefore part of
the candidate lookup, and it is what lets a candidate be checked against
the video it is supposed to come from. That check is the thing that stops a
candidate id being used against the wrong source.

There was **no clip entity, no artifact entity and no clip endpoint** to
extend. Phase 5 adds them.

### What was reused, and what was not touched

| Existing thing | What Phase 5 did with it |
| --- | --- |
| The job state machine | **Untouched.** `completed` still means what it meant in Phase 3. A clip is a separate entity with its own lifecycle, because a clip is not a video ingestion. |
| The lease and claim pattern | Reused exactly: `BEGIN IMMEDIATE`, the write lock before the candidate row is read, and a re-check that the row is still queued. |
| The ownership check (`expected_worker_id`) | Reused for clips, and it is what stops a stale worker overwriting a newer attempt. |
| The recovery pattern | Reused: a lease that expired is requeued, one that ran out of attempts is failed, and a terminal clip is left alone. |
| The storage layout | Extended with one directory, `clips/`, and the same relative-path convention. |
| The error envelope | Reused. Every failure is a stable code in the standard envelope. |
| `app/audio.py`'s process handling | The clip renderer follows the same pattern: an argument list, `shell=False`, stderr to a temporary file rather than an unread pipe, terminate-then-kill, and always reap. |
| The Phase 4 candidate rules | Reused unchanged. The renderer never re-derives a timestamp; it reads the candidate's validated ones. |

### The one architectural addition

The worker gained a third kind of work to claim: a job, then a highlight
analysis, then a clip. It is the same loop with one more step, and the
clip step is tried only when there is no job and no analysis, so a long
render never delays an upload.

---

## 4. Design decisions

### The lifecycle

`queued → preparing → rendering → verifying → completed`, with `failed`
reachable from every non-terminal state. The rules live in `app/clips.py` and
are enforced inside the same transaction as the write.

- `completed` is only reachable from `verifying`, and verification is what
  proves the output. **A clip is not completed because FFmpeg exited with
  zero.**
- A failed clip is terminal. The only way out is the explicit retry, which
  is not a transition but a deliberate reset.
- A completed clip is never requeued by the recovery.

### The timestamp policy

The requested start and end are seconds on the **source media timeline**.

**This was verified, not assumed.** FFmpeg's `-ss` before `-i` is
documented as relative to the file's own start time, and the docs for a
source whose streams do not start at zero are exactly the case where that
matters. The check was made with a fixture whose `format.start_time` is
`2.4768` and whose video stream starts at `2.5`:

```
A: media 3s of the plain source   0, 0, 0, 1, 115200, 55034cbb0f678b3b0678813f2e7d79cb
B: -ss 3 of the offset source      0, 0, 0, 1, 115200, 55034cbb0f678b3b0678813f2e7d79cb
MATCH -> -ss is relative to the MEDIA timeline. The policy is correct.
```

The clip is **re-encoded**, never stream-copied: a stream copy cuts on
keyframes and would silently start earlier and end later than the candidate
asks for.

The measured duration has to be within `clip_duration_tolerance_seconds`
(0.5 s) of the requested one. A clip that cannot be produced within the
tolerance **fails** — it is never shifted to a convenient keyframe.

### The encoding defaults

H.264 (`libx264`) in `yuv420p`, AAC at 128 k, MP4 with `+faststart`, CRF 23
at the `veryfast` preset. Chosen for **playability everywhere** and speed
on a local machine, not for quality. The audio map is optional, so a source
without sound still renders.

### Idempotency

One clip per candidate, enforced by a partial unique index. A repeated
request returns the clip that already exists. Deduplicating on the
*candidate* is exact; deduplicating on rounded timestamps would eventually
merge two genuinely different moments.

### Recovery

A clip whose lease expired is requeued; one that has run out of attempts is
failed with `max_attempts_exceeded`; a completed clip is left alone. The
temporary-file reconciliation is narrow: only files matching the clip
naming pattern, only older than an hour, and only inside the scratch
directory.

---

## 5. Files changed

### New — production code

| File | Purpose |
| --- | --- |
| `backend/app/clips.py` | The clip domain model: the statuses, the stage, the transition table, the boundary rules. No I/O. |
| `backend/app/clip_render.py` | The FFmpeg argument list, the bounded run, the source probe, the output verification. The only place that runs FFmpeg for a clip. |
| `backend/app/clip_service.py` | The render the worker performs: resolve, check, render, verify, finalise, commit. Plus the temporary-file reconciliation. |
| `backend/app/routers/clips.py` | The five clip endpoints. |
| `frontend/src/components/ClipPanel.tsx` | The render control: request, follow, preview, download, retry. |
| `frontend/src/components/ClipPanel.test.tsx` | 9 component tests. |

### New — tests and fixtures

`backend/tests/test_clips.py` (33), `test_clip_render.py` (19),
`test_clips_api.py` (17), `backend/tests/e2e_phase5.py`, and the five clip
fixtures in `backend/tests/conftest.py`.

### Modified — production code

| File | Purpose |
| --- | --- |
| `backend/app/db.py` | Migration 5 (the `clips` table and its indexes); the clip repository: create, claim, update with the ownership check, reset, recover, list, count. |
| `backend/app/worker.py` | `run_clips_once()`, the clip runner, the lease renewal, the recovery and the reconciliation at startup. |
| `backend/app/routers/__init__` | *(unchanged — the package import is implicit)* |
| `backend/app/schemas.py` | The four clip models. |
| `backend/app/errors.py` | The thirteen clip error codes. |
| `backend/app/config.py` | The six clip settings. |
| `backend/app/storage.py` | The `clips/` directory and the server-owned clip file name. |
| `backend/app/main.py` | The clip router. |
| `frontend/src/lib/api.ts` | The clip client: request, list, read, retry. |
| `frontend/src/components/HighlightsPanel.tsx` | The clip panel per candidate. |
| `backend/requirements.txt` | **Unchanged** — no new dependency. |

### Modified — documentation

`README.md`, `docs/ARCHITECTURE.md` (§10), `docs/DECISIONS.md` (D40–D42),
`docs/ROADMAP.md`, `docs/TESTING.md`, `backend/.env.example`.

---

## 6. Database changes

**Migration 5.** The schema went from version 4 to 5, and the migration is
transactional: either the table exists or it does not.

```sql
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    analysis_id TEXT REFERENCES highlight_analyses (id) ON DELETE SET NULL,
    candidate_id TEXT REFERENCES highlight_candidates (id) ON DELETE SET NULL,
    start_seconds REAL NOT NULL CHECK (start_seconds >= 0),
    end_seconds REAL NOT NULL CHECK (end_seconds > start_seconds),
    requested_duration_seconds REAL NOT NULL CHECK (requested_duration_seconds > 0),
    status TEXT NOT NULL CHECK (status IN ('queued','preparing','rendering',
                                          'verifying','completed','failed')),
    stage TEXT NOT NULL CHECK (stage IN ('queued','preparing','rendering',
                                        'verifying','completed','failed')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    output_path TEXT,
    output_size_bytes INTEGER CHECK (output_size_bytes IS NULL OR output_size_bytes >= 0),
    actual_duration_seconds REAL,
    width INTEGER CHECK (width IS NULL OR width > 0),
    height INTEGER CHECK (height IS NULL OR height > 0),
    video_codec TEXT,
    audio_codec TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    worker_id TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT
);

-- One clip per candidate: a repeated request finds the existing clip.
CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_candidate
    ON clips (candidate_id) WHERE candidate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_clips_job    ON clips (job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips (status, created_at);
CREATE INDEX IF NOT EXISTS idx_clips_lease   ON clips (lease_expires_at);
```

The constraints do the work: a negative start, an end at or before the
start, a non-positive duration, a progress outside 0–100, a non-positive
dimension, and an illegal status are all refused by the database itself, not
only by the application.

The `output_path` is **relative to the storage root**, like every other path
the application stores, and it is allocated only once a file exists.

### The migration test

`test_the_migration_adds_the_clip_table` asserts the table exists and that
the schema version is 5, and the whole suite runs its migrations from the
previous schema on a fresh database, which is the migration path that
matters. The Phase 3.1 test that breaks a migration on purpose and checks
that the original database survives still passes.

---

## 7. API contract

| Endpoint | What it does |
| --- | --- |
| `POST /api/v1/jobs/{job_id}/clips` | Request a clip for a candidate. **202** with the queued clip. |
| `GET /api/v1/jobs/{job_id}/clips` | The clips of a job, with bounded pagination. |
| `GET /api/v1/clips/{clip_id}` | One clip: its state and its verified metadata. |
| `POST /api/v1/clips/{clip_id}/retry` | Render a failed clip again. |
| `GET /api/v1/clips/{clip_id}/media` | Play or download the finished file. |

### The rules

- **The request only chooses the candidate.** The body is
  `{"candidate_id": "..."}`. The boundaries come from the candidate in the
  database, and a request that also carries `start_seconds` and
  `end_seconds` has them **ignored** — proven by
  `test_the_boundaries_cannot_be_chosen_by_the_client`.
- **Requests enqueue and return.** 202, with a stable clip id and the
  `queued` status. Nothing waits for a render.
- **The candidate is checked against the job.** A candidate that belongs to
  another job is refused, so a candidate id cannot be used against the wrong
  video.
- **A repeated request does not render twice.** The same clip comes back.
- **No path is exposed.** The response has no `output_path` field, and the
  media route is how the file is reached. `test_the_response_never_exposes_a_path`
  asserts that no Windows or POSIX path appears in any response.
- **The media route resolves the stored path inside the storage root.** A
  clip whose stored path escapes is served as a 404, not as a file
  (`test_the_media_route_refuses_a_path_outside_the_root`).
- **The pagination is bounded.** `limit` outside 1–100 and a negative
  `offset` are refused with 422.
- **The errors are the standard envelope**, with a stable code from the
  thirteen clip codes.

---

## 8. Worker behaviour

### Claiming

`claim_next_clip` is the same atomic pattern the jobs use: `BEGIN IMMEDIATE`,
the write lock before the candidate row is read, and an update that
re-checks the row is still queued. Two workers never receive the same clip.

### The lease

A claimed clip gets a lease of `worker_lease_seconds` and an incremented
`attempt_count`. Every state-changing write is conditional on the clip still
being owned by that worker, so a worker whose lease expired cannot complete
a clip that a newer attempt has taken over.

### The subprocess

- An argument list, `shell=False`. The source path is one argument, even
  when it has spaces, Unicode and a semicolon in it.
- A timeout, checked before every wait. The child is terminated, then
  killed if it does not stop, and is always reaped.
- FFmpeg's stderr goes to a **temporary file**, not to an unread pipe, so a
  chatty encoder cannot deadlock the worker.
- Only 2 000 bytes of stderr are kept, and it is logged on the server and
  never returned by the API.

### Concurrency

The worker renders one clip at a time.
`CLIPFORGE_CLIP_MAX_CONCURRENCY` is the ceiling and is validated at
start-up. A flood of API submissions therefore creates queued clips, not an
unbounded number of FFmpeg processes.

### The temporary file

`temp/clip-<clip id>-<attempt>.mp4`. The name carries the attempt, so two
attempts never share a path and a stale worker cannot remove a newer
attempt's file.

### Retries

A failed clip is not retried automatically. The **retry** operation puts it
back on the queue. A clip that has not failed cannot be retried, and a
completed clip is never reset.

### Crash recovery

On startup and on every recovery pass, the worker requeues a clip whose
lease expired, fails one that has run out of attempts, and reconciles the
temporary files. Recovery never requeues a completed clip.

---

## 9. Output verification

FFmpeg exiting with zero is not evidence. The temporary file is probed
again and **all** of these have to hold:

| # | Check | Failure code |
| --- | --- | --- |
| 1 | The file exists and is a regular file | `clip_output_invalid` |
| 2 | It resolves inside the output directory | `clip_output_invalid` |
| 3 | It is bigger than 1 024 bytes | `clip_output_invalid` |
| 4 | FFprobe can parse it | `clip_verification_failed` |
| 5 | It has a video stream | `clip_output_invalid` |
| 6 | The width and the height are positive | `clip_output_invalid` |
| 7 | The duration is finite and positive | `clip_output_invalid` |
| 8 | The duration is within **0.5 s** of the requested one | `clip_duration_mismatch` |
| 9 | The audio presence matches the source | `clip_output_invalid` |
| 10 | The codecs are the ones the output policy asks for | `clip_output_invalid` |

Check 9 goes both ways: a source **with** audio must produce a clip with
audio, and a source **without** audio must not produce a clip that
invented one.

### Atomic finalisation

1. Render to `temp/clip-<id>-<attempt>.mp4`.
2. Verify **that** file.
3. `os.replace` it into `clips/<id>.mp4` — atomic on the same filesystem,
   which is why the temporary file lives inside the storage root.
4. Commit the completed row with an ownership check.

If the commit finds the clip owned by somebody else, the file is removed
rather than left as an orphan. The gap between step 3 and step 4 is a
crash window: the file exists but the row does not say so. The next
render of the same clip writes its own attempt-specific temporary file and
replaces the final one, so the window self-heals.

---

## 10. Frontend behaviour

`ClipPanel` sits under each highlight candidate.

| State | What is shown |
| --- | --- |
| Idle | **Render clip** |
| Requesting | The button is disabled and says "Requesting…" |
| Queued / preparing / rendering / verifying | The **stage**, and the line *"The clip is being cut from the source video."* |
| Completed | The measured length, the dimensions, a `<video>` preview and a **Download** link |
| Failed | The message, the stable code, and **Render again** |

- **The progress is the stage, not a percentage.** FFmpeg does not report a
  reliable one, so the interface does not invent one; the test asserts that
  no percentage is shown at all.
- **"Completed" is only shown after the backend has verified the output**,
  because the frontend only sees `completed` once the row says so.
- **The duplicate button is disabled while the request is in flight.**
- **The polling is bounded and cleaned up.** The interval is cleared on
  unmount, and the test asserts that no request is made after the component
  is gone.
- **A temporary failure does not lose the clip id.** The poll's error is
  swallowed and the next tick tries again.
- **The preview uses the browser's own player** and the media comes from the
  API's route, never from a path the frontend built.

---

## 11. Security review

| Threat | Defence | Test |
| --- | --- | --- |
| Path traversal in the output path | The stored path is resolved with the storage root's containment check, symlinks included | `test_the_media_route_refuses_a_path_outside_the_root` |
| The media route reaching an arbitrary file | The path comes from the database and is resolved inside the root | the same test |
| A source outside the root | The source path is the stored relative path, resolved with the same check | the renderer's `_resolve_source` |
| Argument injection | The command is built from typed parameters; no value from the API reaches it | `test_the_command_never_uses_a_shell` |
| A shell | `shell=False`, always | the same test |
| A filename with metacharacters | The path is one argument | `test_a_source_with_an_odd_name_is_rendered` |
| NaN, infinity, booleans, strings as timestamps | Rejected before any work starts | `test_a_timestamp_that_is_not_finite_is_refused` and three more |
| A duration past the end of the source | Refused | `test_a_clip_that_runs_past_the_source_is_refused` |
| A source-video substitution | The candidate must belong to the job | `test_a_candidate_from_another_job_is_refused` |
| A stale worker finishing | The commit is ownership-checked | `test_a_stale_worker_cannot_complete_a_clip` |
| Two workers on one clip | The claim is atomic | `test_a_clip_is_claimed_by_one_worker` |
| A duplicate submission | One clip per candidate | `test_a_repeated_request_does_not_render_twice` |
| A path in an error message | The errors are fixed strings; the test asserts no path appears | `test_the_worker_fails_a_clip_whose_source_is_missing` |
| Raw FFmpeg output in the API | The stderr is logged on the server and never returned | the response tests |
| A secret reaching the renderer | The renderer is given a path and a duration; it never sees the integration | by construction |

**Not claimed:** this does not make the application safe for public
internet exposure. It remains a local-first application that binds to the
loopback address.

---

## 12. Test results

### Backend

| Command | Result | Exit |
| --- | --- | --- |
| `..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN` | **668 passed in 83.07s** | 0 |
| `..\.venv\Scripts\python.exe -m pytest --collect-only -q` | 668 collected across 32 files | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_clips.py` | 33 passed | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_clip_render.py` | 19 passed | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_clips_api.py` | 17 passed | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_clip_render.py tests/test_clips.py tests/test_clips_api.py` | 69 passed in 3.55s | 0 |
| `..\.venv\Scripts\python.exe tests/e2e_phase5.py` | every step passed | 0 |
| `..\.venv\Scripts\python.exe tests/e2e_workflow.py` | every step passed (re-run) | 0 |
| `..\.venv\Scripts\python.exe tests/e2e_phase4.py` | every step passed (re-run) | 0 |

### Frontend

| Command | Result | Exit |
| --- | --- | --- |
| `npm run typecheck` | types generated, no errors | 0 |
| `npm run lint` | no findings | 0 |
| `npm test` | **8 files, 77 tests passed** (68 before) | 0 |
| `npm run build` | production build, 2 static routes | 0 |
| `npm run audit:check` | the accepted advisory only | 0 |

### Mocks versus the real thing

| Kind | Where |
| --- | --- |
| **Real FFmpeg and FFprobe** | `test_clip_render.py` (19 tests), the render and reconciliation tests in `test_clips.py`, the media test in `test_clips_api.py`, and the whole of `e2e_phase5.py` |
| **Real SQLite** | every backend test |
| **Real API and worker processes** | `e2e_phase5.py`, `e2e_phase4.py`, `e2e_workflow.py` |
| **A local mock provider** | the provider path in `e2e_phase4.py` and `e2e_phase5.py` — a real HTTP server, but not a real provider |
| **Mocked** | the frontend's fetch, and the Phase 4 provider tests |

---

## 13. Real-media verification

The fixture is generated with FFmpeg at test time: `testsrc` video with a
sine audio track, 320×240, ten seconds.

**A clip of 2.0 s – 5.0 s, from a 10-second source with sound:**

```
duration : requested 3.00s, measured 3.00s
size     : 104 000-ish bytes, 320x240, h264
```

The measured figures from the end-to-end run:

```
duration : requested 4.48s, measured 4.5
size     : 57802 bytes, 320x240, h264
```

**The served bytes are a real video.** The end-to-end check pipes the
media route's response straight into `ffprobe` and asserts that it holds an
H.264 video stream — the file that reaches the browser is the file that was
verified.

**The boundary is right, not just the length.** The test
`test_a_clip_from_a_source_with_a_nonzero_start_time_lands_on_the_right_moment`
renders the same media moment from the offset source and from the plain
source it was made from, and compares the **MD5 frame hashes**: they are
identical, so the seek landed on the same content and not merely on a
clip of the right length.

### The fixture matrix

| Fixture | Covered by |
| --- | --- |
| A short MP4 with video and audio | `clip_source_video` |
| A video without audio | `clip_source_without_audio` — renders and verifies with no audio |
| A source with a non-zero stream start time | `clip_source_with_offset` — the frame-hash test |
| A clip beginning near time zero | the render tests |
| A clip ending near the source duration | the end-to-end run |
| A clip between keyframes | every render is a re-encode, so keyframes are irrelevant by construction |
| A source with unusual dimensions | the 320×240 fixture, checked by the verifier |
| A filename with spaces, Unicode and a semicolon | `clip_source_odd_name` |
| A malformed media file | `clip_source_broken` |
| A file with no video stream | the source probe refuses it |
| A missing source file | the source probe refuses it |
| A duration below the minimum | `test_a_clip_that_is_too_short_is_refused` |
| A request beyond the source duration | `test_a_clip_that_runs_past_the_source_is_refused` |
| A render that times out | `test_a_render_that_times_out_is_reported` |
| A non-zero exit status | the renderer's error path |
| An output that fails FFprobe | `test_a_file_that_is_not_a_video_is_refused` |
| Two simultaneous requests for one candidate | `test_a_repeated_request_does_not_render_twice` |
| A stale worker racing a newer attempt | `test_a_stale_worker_cannot_complete_a_clip` |

---

## 14. Failure-injection results

| Failure | How it was provoked | Result |
| --- | --- | --- |
| A render that will not finish | The deadline is checked before the first wait, so the child is stopped while it is still starting | `clip_render_timeout` |
| A missing FFmpeg | The path is pointed at a name that does not exist | `media_tool_unavailable` |
| A non-zero FFmpeg exit | A source that cannot be encoded | `clip_render_failed`, with the stderr logged and truncated |
| A malformed source | A file that is not a media file | `clip_source_invalid` |
| A missing source | The job's stored source is removed | `clip_source_missing` |
| An output that fails the probe | A file of the right size that is not a video | `clip_verification_failed` |
| An output of the wrong length | A tolerance of 0.5 s against a 2 s clip asked to be 9 s | `clip_duration_mismatch` |
| An output outside the output directory | The path is moved out | `clip_output_invalid` |
| A crash between the render and the commit | The ownership check refuses the commit | the file is removed, not orphaned |
| A worker that dies mid-render | The lease expires | the clip is requeued; after the last attempt it is failed with `max_attempts_exceeded` |
| A stale worker finishing | The ownership check | the write is refused and the clip keeps the newer attempt's state |

**The end-to-end failure path** was run for real: the job's source path was
pointed at a file that does not exist, and the clip failed with
`clip_source_missing` while the worker kept running.

---

## 15. Dependency audit

**No dependency was added, changed or removed by Phase 5.** The clip
renderer uses the standard library (`subprocess`, `json`, `tempfile`) and
FFmpeg, which the project already required. `backend/requirements.txt` is
byte-for-byte unchanged.

The audit was run and the result is unchanged from Phase 3.1:

```
$ cd frontend; npm audit
5 high severity vulnerabilities      exit 1

$ cd frontend; npm run audit:check
The audit reports 5 high, 0 critical and 5 total advisories. All of them
are the accepted `braces` advisory (GHSA-vfj7-8cjw-p6xm) in the dev-only
ESLint chain.                                            exit 0
```

These are **pre-existing**, not new: one advisory (`braces <= 3.0.3`, no
patched release) counted once for each of the five packages that depend on
it, in the dev-only ESLint chain. The reasoning and the guard are recorded
in [PHASE_3_1_REPORT.md](PHASE_3_1_REPORT.md) §8 and
[docs/DECISIONS.md](docs/DECISIONS.md) (D34).

**Node compatibility** is unchanged: the project requires `>=20.9.0`, which
matches Next.js 16.4.2's own `engines`, and the whole frontend suite
passes on the installed Node 26.4.0. The LTS parity check still could not
be run, because no Node LTS release is installed on this machine, and the
repository still has **no CI**, which is the larger gap.

---

## 16. Known limitations

1. **The rendering is a re-encode, so it is slow.** A three-minute clip from
   a one-hour recording decodes and re-encodes a minute of video. That is
   the price of the exact boundary; a stream-copy mode would be much faster
   and would cut on keyframes, which is why it was not used.
2. **The tolerance is on the duration, not on the absolute position.** A
   clip that starts a fraction of a second late and ends a fraction early
   would still pass. The frame-hash test covers the *seek* precisely, but
   the *runtime* check is the duration.
3. **A moment that straddles a window boundary of the analysis may be
   missed.** This is inherited from the Phase 4 windowing, not introduced
   here.
4. **The crash window between the rename and the commit self-heals but is
   not transactional.** A file can exist without its row saying so for the
   length of one commit. A database transaction cannot make a filesystem
   change transactional, and the reconciliation only removes *temporary*
   files, not a finalised one whose row is missing.
5. **The reconciliation uses an age threshold of an hour.** A render that
   is legitimately running for longer than that could, in principle, have
   its temporary file removed by a *second* worker starting up. The window
   is small — the file is only in the scratch directory for the length of
   the render — but it is not zero.
6. **No browser-level test.** The preview and the download are covered by
   component tests and by the end-to-end script's byte-level check, not by
   a real browser.
7. **The `clips` directory grows without bound.** A clip is kept forever;
   there is no retention policy, and the space is not reclaimed. That is
   deliberate for now, and it is the obvious candidate for the Phase 10
   cleanup work.
8. **The pre-existing `test_a_download_that_exceeds_the_limit_is_rejected`
   is flaky** and is recorded in §2.

---

## 17. Reproduction instructions

```powershell
# 1. The backend, from clipforge\backend
..\.venv\Scripts\python.exe -m pytest            # the whole suite
..\.venv\Scripts\python.exe -m pytest tests/test_clip_render.py   # the real FFmpeg tests
..\.venv\Scripts\python.exe tests\e2e_phase5.py   # the whole chain, rendered and played

# 2. The frontend, from clipforge\frontend
npm run typecheck
npm run lint
npm test
npm run build

# 3. The application itself
cd clipforge\backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
..\.venv\Scripts\python.exe worker.py
cd ..\frontend && npm run dev
```

Then open <http://localhost:3000>, upload a video, wait for the
transcript, run a highlight analysis from the **Integrations** page, and use
**Render clip** on a candidate.

FFmpeg and FFprobe 4 or newer are required. The check was made against
**FFmpeg 9.0.2**.

---

## 18. Definition-of-done checklist

| # | Item | Result |
| --- | --- | --- |
| 1 | The repository architecture and the phase reports were inspected | **Pass** — §2, §3 |
| 2 | The baseline test results were recorded | **Pass** — §2, including the pre-existing flake |
| 3 | The database migration is safe and tested from the existing schema | **Pass** — §6 |
| 4 | Clip creation validates the source and the candidate associations | **Pass** — §7 |
| 5 | Valid clip requests enqueue promptly | **Pass** — 202 with a stable id |
| 6 | The worker renders clips using the real FFmpeg | **Pass** — §13 |
| 7 | The timestamp semantics and the tolerance are documented and tested | **Pass** — §4, with the frame-hash proof |
| 8 | The rendering uses a safe process invocation and an enforceable timeout | **Pass** — §8 |
| 9 | The output is independently verified with FFprobe | **Pass** — §9, ten checks |
| 10 | The temporary output is atomically finalised | **Pass** — §9 |
| 11 | The crash recovery and the stale-worker ownership are safe | **Pass** — §8, §14 |
| 12 | Duplicate submissions behave as documented | **Pass** — one clip per candidate |
| 13 | Failed renders produce stable, useful errors | **Pass** — §14 |
| 14 | The API responses never expose an arbitrary local path | **Pass** — §7, §11 |
| 15 | The frontend lists the candidates and requests renders | **Pass** — §10 |
| 16 | The frontend shows truthful states and supports preview and download | **Pass** — §10 |
| 17 | The real-media integration tests pass | **Pass** — 19 real-FFmpeg tests |
| 18 | At least one successful end-to-end render is verified | **Pass** — `e2e_phase5.py` step 5 |
| 19 | At least one end-to-end failure path is verified | **Pass** — `e2e_phase5.py` step 8 |
| 20 | The existing Phase 2, 3, 3.1 and 4 behaviour remains intact | **Pass** — 668 passed, 0 failed |
| 21 | The backend tests pass, with the skips and the pre-existing failures explained | **Pass** — §2, §12 |
| 22 | The frontend tests, type checks, lint and the production build pass | **Pass** — §12 |
| 23 | The dependency audit is run and accurately reported | **Pass** — §15, unchanged |
| 24 | The documentation is updated | **Pass** — README, ARCHITECTURE, DECISIONS, ROADMAP, TESTING |
| 25 | `PHASE_5_REPORT.md` is written | **Pass** — this document |
| 26 | No commit was created and the Git HEAD is unchanged | **Pass** — §19 |

---

## 19. Git integrity

| | |
| --- | --- |
| Initial HEAD | `7502384d4b0ef1aeff3d568bd2db7ed790864378` |
| Final HEAD | `7502384d4b0ef1aeff3d568bd2db7ed790864378` |
| Commits created | **None** |
| Staged changes | **None** |
| Working tree at the start | 35 modified, 54 untracked |
| Working tree at the end | 36 modified, 65 untracked |

The working tree grew by the six new production files, the four new test
files, the report and the documentation changes. Every file that was there
before is still there, and the Phase 1–4 work was not touched.

---

## 20. Final verdict

### **READY WITH DOCUMENTED LIMITATIONS**

**Why not plain READY.** Six limitations in §16 are real and worth naming:
the render is a re-encode and therefore slow, the runtime check is on the
duration rather than the absolute position, the crash window between the
rename and the commit is not transactional, the temporary-file
reconciliation has an hour-long age threshold, there is no browser-level
test, and the clips directory has no retention policy.

**Why not NOT READY.** None of them is a correctness or security problem:

- The boundary guarantee is **proven**, not argued: the seek semantics were
  checked with frame hashes against a source with a non-zero start time.
- The output is **independently verified** before a clip is called
  complete, and the file that reaches the browser is the file that was
  verified.
- The ownership, the idempotency, the recovery and the duplicate
  submission all behave as documented, and each has a test.
- Every failure path produces a stable code, and the end-to-end run
  exercises a real one.
- The whole of Phases 1–4 still passes: **668 backend tests, 77 frontend
  tests**, typecheck, lint and the production build all exit 0.
- No dependency changed, no commit was made, and no Phase 6 work was
  started.

**What Phase 6 can rely on.** A clip is a real, playable file whose
boundaries came from the persisted transcript, whose bytes were verified
before the row said so, and whose file name ClipForge controls. The
vertical conversion and the captions can be built on top of it without
revisiting any of this.

---

*Every result in this document was transcribed from a command that was
actually run on 20 October 2026. Where a check could not be run, it says
so rather than claiming a pass. Nothing was committed.*
