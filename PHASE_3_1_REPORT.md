# Phase 3.1 — Transcription hardening & pre-Phase-4 gate

**Status: COMPLETE — READY WITH DOCUMENTED LIMITATIONS.**

Phase 3.1 was a test-driven reliability and verification phase. Its job was
to find out whether the Phase 3 pipeline could be trusted, fix what was not
trustworthy, and prove the fixes. It did not add a product feature, and it
did not start Phase 4.

Every result below was produced by a command that was actually run on
20 October 2026, and the output is transcribed from that run. Where a check
could not be run, it says so rather than claiming a pass.

---

## 1. What the initial inspection found

The Phase 3 report was treated as a set of claims to be checked, not as
facts. Four of its claims did not survive the check.

| Phase 3 claim | What the code actually did | Verdict |
| --- | --- | --- |
| "The transcription timeout is checked between decoded segments, not in hard real time, so one very slow segment can overshoot it." | True, and **worse than stated**: the deadline was only checked inside the `for segment in segments` loop, and `WhisperModel.transcribe()` does the audio decode, the voice-activity filter and the mel-feature extraction *before* it returns that generator. All of that work was outside the timeout check entirely. | Understated |
| "The model is loaded in the worker, lazily, and reused." | True for the in-process engine. The model is loaded lazily and reused. | Confirmed |
| "A timestamp that points past the audio is normalised, not rejected." | True — but only when the engine could report a duration. With no duration, **no** timestamp was checked against the audio at all. | Partly true |
| "`npm audit` reports the same five high-severity advisories … There is no patched `braces` release." | The "no patched release" claim was right. The claim that the offered fix "breaks the Next.js 16 lint setup" had never been checked; it was checked in this phase (§8). | Confirmed, and now verified |

The baseline, before anything was changed:

```
$ cd backend; ..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN
346 passed, 1 skipped in 22.43s                                   exit 0

$ cd frontend; npm run typecheck                                   exit 0
$ cd frontend; npm run lint                                        exit 0
$ cd frontend; npm test                                            5 files, 38 tests passed, exit 0
$ cd frontend; npm run build                                       exit 0
```

There were **no pre-existing failures**. The single skip is a symlink test
that needs a privilege this Windows account does not have; it was skipped
before this phase and is still skipped.

The working tree already contained the whole of Phase 3 as uncommitted work
(16 new files, 32 modified). That work was preserved. Nothing was reverted,
and nothing was committed.

---

## 2. Every gap, and what happened to it

Thirteen gaps were found. Ten were fixed, two were accepted with a
documented reason, and one is a limitation that is now visible.

| # | Gap | Where | Outcome |
| --- | --- | --- | --- |
| 1 | The timeout was soft, and did not even cover the decode, the VAD or the feature extraction | `app/transcription.py` | **Fixed** — the model moved to a child process (§3) |
| 2 | A stalled inference could occupy the worker for ever | `app/transcription.py` | **Fixed** — the child is terminated and reaped |
| 3 | The worker never stopped the inference child on shutdown | `app/worker.py` | **Fixed** — `terminate()` is called on shutdown, on timeout, on failure and on cancellation |
| 4 | The normalising duration was optional, so with no duration *no* timestamp was checked against the audio | `app/transcripts.py`, `app/worker.py` | **Fixed** — the FFprobe duration of the extracted WAV is always supplied |
| 5 | A stale worker could delete and replace the transcript of a newer attempt, and could fail a job another worker had taken over | `app/db.py`, `app/worker.py` | **Fixed** — ownership is checked inside the writing transaction |
| 6 | `release_for_retry` could move a *terminal* job back onto the queue | `app/db.py` | **Fixed** — it only moves an active job |
| 7 | The transcript endpoint could read the job and the transcript at two different instants | `app/routers/jobs.py` | **Fixed** — both are read in one snapshot |
| 8 | The model cache check matched a substring: a request for `base` was satisfied by a cached `base.en` | `app/transcription.py` | **Fixed** — the match is exact |
| 9 | A model name was never validated, so `tiny.eng` became a silent download attempt | `app/transcription.py` | **Fixed** — validated as configuration, before anything is fetched |
| 10 | Nothing bounded the number of segments or the length of a segment's text | `app/transcripts.py` | **Fixed** — both are bounded, and both are documented and configurable |
| 11 | The panel said "No transcript yet" for a job that was finished before transcripts existed | `frontend/src/components/TranscriptPanel.tsx` | **Fixed** |
| 12 | A long unbroken word in a segment could push the panel sideways | `frontend/src/components/TranscriptPanel.tsx` | **Fixed** — the text wraps |
| 13 | The "nothing leaves the machine" claim was aspirational: the engine *asked* the model library not to download, but nothing stopped a network call | `app/transcription_host.py` | **Fixed** — the inference process cannot open a socket at all (§4) |

### Accepted, not fixed

| Gap | Why it is accepted |
| --- | --- |
| The five `npm audit` advisories | There is no patched `braces` and the only offered fix breaks the lint setup. Verified, recorded and now guarded by `npm run audit:check`. §8. |
| A worker killed with `SIGKILL` leaves its inference child running until the child notices the closed pipe | The child exits when its pipe closes, which happens as soon as the worker dies, and it cannot outlive the current transcription. A prompt, portable "kill the whole tree" needs platform-specific job objects or `PR_SET_PDEATHSIG`, which is more machinery than the problem warrants. Documented in `docs/TESTING.md` §7. |

### One bug found in this phase's own work

The first version of the supervisor had a **deadlock**: the worker stops the
engine from inside the progress callback, and the callback ran while the
engine held its request lock, so `terminate()` waited for a lock the same
thread already held. It was found by re-reading the code after the first
green run, fixed with a separate lock for the child, and is now covered by
`test_the_engine_can_be_stopped_from_the_progress_callback` and
`test_the_worker_releases_the_job_when_it_is_stopped_mid_transcription`.

---

## 3. The implemented timeout contract

### Where the deadline is enforced

The speech model does **not** run inside the worker. It runs in a **child
process** of the worker:

```
worker process                        inference process
────────────────                      ──────────────────
engine.transcribe()  ──configure────►  load the model
                   ──transcribe────►  decode the audio
              ◄──segment / progress──  report what it found
                   ◄──done─────────    finished
                   ──terminate─────►   stop, then kill, then reap
```

The parent (`app/transcription_process.py`) owns the deadline. The child
(`app/transcription_host.py`) speaks a small line-delimited JSON protocol
over its standard input and output.

### The contract, in full

> `CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS` is a **hard** timeout. A
> transcription that has not finished within that number of seconds is
> stopped. The child process that was doing the inference is terminated, then
> killed if it does not stop, then reaped. The job fails with the stable code
> `transcription_timeout` and a terminal status. No partial transcript is
> committed. The worker carries on with the next job. Nothing — no child
> process, no pipe, no temporary file — is left behind.

**The bound.** The limit plus a short termination grace period: the child is
asked to stop and is given 2 seconds, then killed and given 2 more. On
Windows `terminate()` is `TerminateProcess`, so the child stops at once. The
timeout covers the *whole* job: starting the inference process, loading the
model and decoding the audio.

**Why a process and not a smaller number or a thread.** Three facts, all
checked against the installed `faster-whisper` 1.2.1:

1. `WhisperModel.transcribe()` is not lazy about the expensive part. It
   decodes the whole audio file (`decode_audio`), runs the voice-activity
   filter and computes the mel features *before* it returns the generator
   (`faster_whisper/transcribe.py`, lines 386–578). The Phase 3 deadline was
   checked only inside the loop over that generator, so the decode, the VAD
   and the feature extraction were **outside** the timeout entirely.
2. The decoder itself is `ctranslate2`, which is C++. It releases the GIL, so
   a Python-level interruption — an exception, a signal, a thread cancel —
   cannot reach it.
3. The worker is already a separate operating-system process, so the
   architecture already had the right shape: one more process boundary.

Lowering the timeout would not have fixed any of this. It would have made
the *soft* deadline arrive sooner while the work it was supposed to cover
still ran unguarded.

### The guarantees, and how each is held

| Guarantee | How it is achieved | Evidence |
| --- | --- | --- |
| A stalled inference cannot hold the worker | The parent owns the deadline and kills the child | `test_a_stalled_inference_is_stopped_and_reported_as_a_timeout`, `test_a_stalled_inference_is_stopped_and_the_worker_survives` |
| The worker survives | The child is a separate process; killing it does not touch the worker | `test_a_stalled_inference_is_stopped_and_the_worker_survives` (the worker then completed a second job) |
| The job ends in a terminal state | The failure is recorded as `failed` / `transcription_timeout` | `test_a_stalled_inference_is_stopped_and_the_worker_survives`, the end-to-end check step 7 |
| No partial transcript is committed | The transcript is only written after a complete, validated result | the same test asserts `get_transcript(...) is None` |
| No orphan child | The parent reaps the child; the child also exits when its pipe closes | `test_a_timed_out_transcription_leaves_no_child_process_behind`, the end-to-end check step 9 |
| The child is stopped on shutdown | `Worker._terminate_children()` now includes the engine | `test_the_worker_stops_its_inference_process_when_it_is_shut_down` |
| A graceful shutdown does not hang or fail the job | The engine is stopped from the progress callback without a lock cycle | `test_the_worker_releases_the_job_when_it_is_stopped_mid_transcription` |
| The model is loaded once, not once per job | The child is kept between jobs | `test_the_child_is_started_once_and_reused` |
| One inference process, not one per job | There is exactly one child, and the request lock serialises the requests | `app/transcription_process.py` |
| Nothing secret or bulky in the arguments | The configuration and the audio path travel over the pipe | `test_the_environment_does_not_leak_into_the_inference_process` |
| Bounded IPC | The message queue is bounded; the child's stderr goes to a temporary file of which only a truncated tail is logged | `app/transcription_process.py` |
| FFmpeg interruption still works | Unchanged: the extractor still terminates its own child | `tests/test_audio.py` (27 tests, unchanged) |

### The soft timeout is still available, and is labelled as such

`CLIPFORGE_TRANSCRIPTION_ISOLATION=inline` runs the model inside the worker
again. Its timeout is checked between decoded segments, so one slow
inference can overshoot it. The setting, the README and the decision log all
say so. It exists for environments where a child process cannot be started;
it is **not** the default.

---

## 4. Model and offline operation

### Choosing a model

`CLIPFORGE_TRANSCRIPTION_MODEL` is read by `TranscriptionSettings` and is
now **validated as configuration**: a known Whisper model size, a model
repository id such as `owner/name`, or the path of an existing local model
directory is accepted; anything else is refused with a message that lists
the supported names. The check loads nothing and touches no network, so it
is safe to run while the API starts up.

| Model | Size | Character |
| --- | --- | --- |
| `tiny.en` | ~75 MB | The default, because it makes the first run cheap. Noticeably weaker on accents, names and background noise. |
| `base.en` | ~145 MB | A good default for real recordings. |
| `small.en` | ~480 MB | Clearly more accurate, especially on noisy speech. |

A larger model is **not** automatically better. It is slower, it needs more
memory, and on clean, well-spoken audio the difference between `tiny.en` and
`base.en` is often small, while on noisy or accented audio it is large. The
multilingual sizes are needed for languages other than English and cost more
again. **No word-error rate is claimed anywhere in this repository, because
none has been measured.**

### Offline behaviour

| Question | Answer |
| --- | --- |
| Where is the model? | `CLIPFORGE_TRANSCRIPTION_CACHE_DIR`, or `<storage>/models` by default |
| What happens when it is not cached? | `transcription_model_unavailable`, with a message that says what to do |
| How is the cache prepared? | `python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', download_root='data/models')"` while online |
| How is an implicit download prevented? | `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` — the engine refuses to *start the inference process at all* when the model is not cached, so no download can be attempted |
| What does the user see? | The stable code `transcription_model_unavailable` and a message that names the fix |
| Can the transcription itself reach the network? | **No.** The inference process replaces `socket.connect` and `socket.create_connection` with a function that raises |

That last point is new in this phase, and it is what turns "we do not send
anything" from a promise into something the process enforces. The
`test_the_inference_process_refuses_to_use_the_network` test proves the
guard works, and the whole real-model suite (§5) runs with it active and
passes — the model is loaded from the cache and used with no network
available to it.

### The model is loaded lazily, in the worker, and reused

- The API never loads the model: `/ready` only looks at the cache directory
  (`test_the_api_never_loads_the_model`).
- The worker starts the inference process on the first transcription and
  keeps it, so the model is loaded once (`test_the_child_is_started_once_and_reused`,
  `test_the_model_is_loaded_once_and_reused`).
- The configuration reaches the child through the pipe, not through the
  environment, so the parent's settings are the only truth
  (`test_the_environment_does_not_leak_into_the_inference_process`).

### Secrets and paths

The inference process receives the model name, the device, the cache
directory and the audio path over a pipe. No secret is in that data, none
appears in a process argument list, and the child never sees the API's
environment. The API's readiness probe and job responses remain free of
paths and secrets, as the Phase 3 tests already proved.

---

## 5. Timestamp normalisation and the real-model results

### The rules, and where they came from

The Phase 3 rules (D29) are **preserved**, and the five regression tests
that cover the real defect are **preserved and still pass**
(`tests/test_transcript_model.py`, unchanged: the overshoot is pulled back,
the window overshoot is accepted, the far overshoot is rejected, the
entirely-past segment is dropped, the real span is kept).

The full matrix now has a named, deterministic test for every case
(`tests/test_transcript_normalisation.py`, 28 tests):

| Case | Behaviour | Test |
| --- | --- | --- |
| Negative start | The whole transcript is rejected | `test_a_negative_start_is_rejected` |
| `NaN` / infinity | The whole transcript is rejected | `test_a_nan_timestamp_is_rejected`, `test_an_infinite_timestamp_is_rejected` |
| End before start | The whole transcript is rejected | `test_a_segment_that_ends_before_it_starts_is_rejected` |
| Starts at or beyond the audio's end | The segment is dropped | `test_a_segment_that_starts_exactly_at_the_end_of_the_audio_is_dropped` |
| Ends beyond the audio's end | The end is pulled back to the end of the audio | `test_a_segment_that_ends_past_the_audio_is_pulled_back` |
| Empty or whitespace-only text | The segment is dropped | `test_a_segment_with_no_text_is_dropped` |
| Duplicate segments | Kept, in a deterministic order | `test_duplicate_segments_are_kept_in_a_stable_order` |
| Overlapping segments | Kept, ordered by start | `test_overlapping_segments_are_kept_and_ordered` |
| Zero-duration segment | Dropped: it cannot be placed in the audio | `test_a_zero_duration_segment_is_dropped` |
| Very short segment | Kept: it still carries speech | `test_a_very_short_segment_is_kept` |
| Malformed output (not a list, no timestamps, text that is not a string) | The whole transcript is rejected | `test_output_that_is_not_a_list_is_rejected` and three more |
| Absurdly long text | The segment is dropped | `test_a_segment_with_an_absurdly_long_text_is_dropped` |
| Valid output | Not modified | `test_valid_output_is_not_modified` |

Two decisions are worth stating plainly, because they are judgement calls:

- **An impossible timestamp rejects the whole transcript** rather than
  dropping that one segment. This is the Phase 3 contract ("a transcript
  that claims to be complete must be complete") and it is *stricter* than
  dropping, so it cannot fabricate anything. It was kept deliberately.
- **A segment that cannot be placed is dropped, never invented.** A segment
  that lies entirely outside the audio, and a segment with no span at all,
  are dropped. There is no honest timestamp for either.

### The duration the timestamps are normalised against

This is the one real change to the rules. The duration is now **the
duration of the extracted WAV, measured by FFprobe when the file was
produced**, and the worker passes it to the engine. The engine prefers it
over anything the model reports.

Before this phase, the normalising duration came from the model's own
report, and the whole of the clamping sat inside an
`if duration is not None:` branch. With no duration, **no** timestamp was
checked against the audio: a segment claiming to be at 9 999 s would have
been stored verbatim. That is now impossible, because the duration is always
known.

It is also the *right* duration. The source video's nominal duration is not
the audio's duration — the live run in §10 shows a 3.37 s transcript in a
5 s video — and using it would have produced exactly the "speech after the
audio ended" that D29 exists to prevent.

### The real-model test matrix

`tests/test_real_model.py` — 10 tests, marked `real_model`, **all executed
and all passed**. They skip when the model is not already cached, so they
never download anything. The model used is the configured default,
`tiny.en`, from `backend/data/models`.

Every fixture is generated on this machine: the speech comes from the
Windows speech synthesiser and the audio is assembled with FFmpeg. No
third-party media is used.

| Fixture | Result |
| --- | --- |
| Clear ordinary English speech | 1 segment, `en`, duration 3.37 s, text *"The quick brown fox jumps over the lazy dog."* |
| Speech with pauses between the clauses | Segments produced, in order, `end >= start` for every one |
| Short speech followed by four seconds of silence | Segments produced; the speech is at the start, the silence is not claimed as speech |
| Speech at the very beginning and end | The last segment ends inside the audio |
| Speech with a 220 Hz tone underneath | A transcript is produced; the duration matches; every segment is inside the audio |
| A different voice (`Microsoft Zira Desktop`) at a slower rate | Segments produced, language `en` |
| Three seconds of pure silence | **Zero segments**, and the duration is right — an empty transcript, not a failure |
| A corrupted file that claims to be audio | **A failure** (`transcription_failed`), never an empty success |

Structural assertions, not word-perfect ones: a small model on synthetic
speech is not a word-accuracy benchmark. The one content assertion is that
a known speech fixture produces a non-empty transcript containing a
recognisable word of the known phrase.

**No word-error rate is claimed.** None was measured, and none is stated in
this report or anywhere in the repository.

---

## 6. Database migration, crash recovery and idempotency

All of these are tested against a real, temporary SQLite database. No test
touches the developer's data.

| Requirement | Evidence |
| --- | --- |
| A job only completes after its transcript is durably committed | The state machine refuses `inspecting → completed`; the transcript is written before the status is updated. `test_a_job_cannot_go_from_inspecting_to_completed`, `test_a_completed_job_always_has_its_transcript` |
| A failed extraction or transcription leaves no transcript and no completed job | `test_a_failed_transcription_leaves_no_transcript_and_no_completed_job`, `test_a_failed_transcription_leaves_no_half_written_transcript` |
| A crash between the transcript commit and the job completion is recovered safely | The transcript survives; the job is requeued; the next attempt **replaces** the transcript rather than adding one. `test_recovering_a_lease_does_not_duplicate_anything` |
| A stale worker cannot overwrite the result of a newer attempt | **This was broken and is now fixed.** `test_a_stale_worker_cannot_replace_the_transcript_of_a_newer_attempt` |
| A stale worker cannot fail a job another worker owns | **Also broken, now fixed.** `test_a_stale_worker_cannot_fail_a_job_that_somebody_else_owns` |
| Lease recovery creates no duplicate transcripts or segments | One transcript row, one set of segments, after a recovery and a second attempt |
| Retries are idempotent | `test_a_retry_does_not_duplicate_the_transcript` (Phase 3's test, updated to use a realistic recovery path) |
| Terminal states stay terminal | `test_a_terminal_job_is_not_put_back_on_the_queue`, `test_a_failed_job_is_not_put_back_on_the_queue` |
| A job whose attempts are used up fails permanently | `test_a_job_whose_attempts_are_used_up_is_failed_permanently` |
| Legacy Phase 2 jobs stay valid and are distinguished | `test_a_legacy_completed_job_has_no_transcript_and_says_so`: `has_transcript` is `false` and the transcript endpoint answers `pending` with no segments |
| The migration from the previous schema preserves jobs and media metadata | `test_a_phase_2_database_migrates_without_losing_anything` (Phase 3, unchanged) and `test_the_migration_does_not_destroy_existing_sources` |
| A failing migration leaves the original database recoverable | **New.** `test_a_failing_migration_leaves_the_database_as_it_was`: the migration is broken on purpose; the database is still at version 2, the job is intact, `PRAGMA integrity_check` says `ok`, and the migration succeeds afterwards |
| Upload and temporary artefact cleanup stay safe at the filesystem/database boundary | `tests/test_storage_consistency.py` (8 tests, unchanged) |
| Connections and subprocesses are closed on shutdown | `test_the_worker_leaves_the_database_readable_after_a_shutdown`, `test_the_worker_stops_its_inference_process_when_it_is_shut_down` |

The two ownership bugs are the most important findings of this workstream.
Before the fix, a worker whose lease had expired could delete the transcript
that a newer worker had just written, and could mark the newer attempt's
job as failed. Both writes are now conditional on the job still being owned,
and the check happens **inside** the same transaction as the write, so there
is no window between the check and the change.

---

## 7. Security and resource-limit review

| Area | Finding | Outcome |
| --- | --- | --- |
| FFmpeg argument construction | An argument list, `shell=False`. A filename with `&` or `;` is only ever a filename. | Unchanged, still tested |
| Bounded subprocess runtime | FFmpeg, the downloader and the inference process all have a deadline and are terminated. | Improved — the inference process is now bounded too |
| Safe temporary files | Names are generated by the application, inside the storage root, and removed afterwards. The child's stderr goes to a `TemporaryFile` that is closed with the child. | Unchanged |
| Path traversal | Every path is resolved and checked for containment. | Unchanged, still tested |
| No transcript contents or absolute media paths in ordinary logs | The worker logs the job id and `source.name`; the API never returns a path. The child's stderr is logged truncated and only on failure. | Unchanged, still tested |
| Audio duration limit | `CLIPFORGE_AUDIO_MAX_DURATION_SECONDS`, 4 h by default. | Unchanged |
| Audio file size limit | `CLIPFORGE_AUDIO_MAX_BYTES`, 2 GiB by default. | Unchanged |
| **Segment count** | **Was unbounded.** A malformed engine output could make the worker collect an unbounded number of segments. | **Fixed** — `CLIPFORGE_TRANSCRIPTION_MAX_SEGMENTS`, default 50 000, configurable, documented |
| **Transcript text size** | **Was unbounded per segment.** One malformed result could be arbitrarily long. | **Fixed** — a segment longer than 2 000 characters is dropped as malformed; the constant is documented in `app/transcripts.py` |
| API pagination | `MAX_PAGE_SIZE` 200 for jobs, `MAX_TRANSCRIPT_PAGE_SIZE` 1 000 for segments, both validated by FastAPI. | Unchanged |
| Decoded audio in memory | The engine decodes the whole file: roughly 2.9 MB per minute, so the 4-hour default can need about 700 MB. | **Documented**, not changed — the limit is already configurable |
| Loopback-only binding | `CLIPFORGE_HOST` defaults to `127.0.0.1` and a non-loopback address is refused unless `CLIPFORGE_ALLOW_LAN_BINDING=true`. | Unchanged, still tested |
| Upload validation and YouTube URLs | Unchanged; both suites still pass. | Unchanged |
| **Network access from the inference process** | **Was not enforced.** The engine passed `local_files_only`, but nothing stopped a network call. | **Fixed** — the child cannot open a socket |

No new limit was introduced without a documented rationale and, where it
makes sense, a setting. The two new limits are the segment count and the
per-segment text length; the first is configurable, the second is a
constant that exists only to bound memory.

---

## 8. Dependency audit and Node compatibility

### The audit

```
$ cd frontend; npm audit
5 high severity vulnerabilities
exit 1
```

The full report (`npm audit --json`) shows the chain and the versions:

```
eslint-config-next@16.4.2
└── @next/eslint-plugin-next@16.4.2
    └── fast-glob@3.3.1
        └── micromatch@4.0.8
            └── braces@3.0.3        ← the advisory
```

```
advisory : GHSA-vfj7-8cjw-p6xm  (npm advisory id 1240992)
title    : braces vulnerable to stack-exhaustion denial of service
           through deeply nested patterns
severity : high   CVSS 3.1: 7.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H)
range    : <=3.0.3
```

**The five advisories are one advisory**, counted once for every package
that depends on `braces`. The chain is entirely inside the development
dependency `eslint-config-next`.

### Is there a fix?

**No compatible fix exists, and this was verified rather than assumed.**

- `braces` has **no patched release**: the advisory covers `<=3.0.3` and
  3.0.3 is the latest published version. `npm view braces versions` ends at
  3.0.3.
- `fast-glob@3.3.3` (the newest) still depends on `micromatch@^4.0.8`,
  which still depends on `braces@^3.0.2`. Bumping it changes nothing.
- `next` and `@next/eslint-plugin-next` are both already at their latest
  published version, 16.4.2.
- The only fix npm offers is `npm audit fix --force`, which would install
  `eslint-config-next@14.2.35`. **What that would do was checked**, in a
  scratch directory, and it breaks the build in two ways:
  1. v14 declares `peer eslint@"^7.23.0 || ^8.0.0"`, so npm **refuses** the
     install against the project's ESLint 9:
     `npm error code ERESOLVE … peer eslint@"^7.23.0 || ^8.0.0" from eslint-config-next@14.2.35`.
     Forcing it means downgrading ESLint to 8, which is itself deprecated.
  2. v14's `core-web-vitals` export is an **eslintrc-style object** with a
     single `extends` key, not the flat-config array that
     `frontend/eslint.config.mjs` spreads:
     `typeof: object`, `isArray: false`, `keys: extends`.
     `...nextVitals` on a non-iterable throws, so `npm run lint` would not
     run at all.

`npm audit fix --force` was therefore **not** run. The lockfile is
unchanged, the dependency versions are unchanged, and the project still
builds, lints and tests.

### The mitigation

The advisory is not dismissed because it is a development dependency. It is
a real denial-of-service bug in a glob expander. What makes it acceptable
here is narrow and worth stating precisely:

- the only thing that reaches `braces` is `fast-glob`, used by the Next.js
  ESLint plugin to expand the **project's own** glob patterns — not user
  input, not a request body, not a filename from an upload;
- it runs on a developer's machine during `npm run lint`, not in the
  shipped application, and not in production;
- an exploit needs a deeply nested brace pattern to be expanded, which
  would have to be committed to the repository's own ESLint configuration.

So the practical mitigation is to make sure the exception cannot grow:

```
$ cd frontend; npm run audit:check
The audit reports 5 high, 0 critical and 5 total advisories. All of them are
the accepted `braces` advisory (GHSA-vfj7-8cjw-p6xm) in the dev-only ESLint
chain.
exit 0
```

`frontend/scripts/check-audit.mjs` walks the `via` chain of every finding
and **fails** if any advisory other than the accepted one appears, or if the
accepted one moves to a different package. A new advisory is a red build.

### Node compatibility

| | |
| --- | --- |
| Next.js version | 16.4.2 (the latest published) |
| Next's `engines.node` | `>=20.9.0` |
| The project's `engines.node` | `>=20.9.0` |
| The README's documented range | Node.js 20.9+, 22 LTS / 24 LTS recommended |
| Node installed on this machine | **v26.4.0** (npm 11.14.1) |
| CI configuration | **There is none.** The repository has no `.github` directory and no CI workflow file. |

The documented range and Next.js's own requirement agree, and the whole
frontend suite — typecheck, lint, 41 tests and the production build — passes
on the installed Node 26.4.0. Two things are worth recording honestly:

- **The LTS parity check could not be run.** No Node LTS release is
  installed on this machine, so the claim "this works on 22 LTS" is not
  backed by an executed check here. It rests on Next.js's own `engines`
  field and the documented range, not on a run.
- **Node 26.4.0 is not an LTS release.** Everything passes on it, but it is
  not the version the README recommends, and a CI workflow that pins an LTS
  version would be a genuine improvement. The repository has no CI at all,
  which is the larger gap; it is scheduled for Phase 9.

The Node runtime was **not** upgraded or downgraded.

---

## 9. The commands, and what each returned

Every command was run from the repository root or from `backend/`
respectively. Exit statuses are the real ones.

### Backend

| Command | Result | Exit |
| --- | --- | --- |
| `..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN` | **458 passed, 1 skipped in 72.78s** | 0 |
| `..\.venv\Scripts\python.exe -m pytest --collect-only -q` | 459 tests collected across 26 files | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_transcript_storage.py tests/test_job_recovery.py` | **33 passed in 1.36s** (migration, ownership, recovery) | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_transcription_timeout.py tests/test_transcription_process.py` | **18 passed in 46.77s** (process-level timeout and recovery) | 0 |
| `..\.venv\Scripts\python.exe -m pytest tests/test_real_model.py` | **10 passed** (the real local model) | 0 |
| `..\.venv\Scripts\python.exe tests/e2e_workflow.py` | **every step passed** (the full workflow over HTTP) | 0 |

Backend test counts by file, after this phase:

| File | Tests | | File | Tests |
| --- | --- | --- | --- | --- |
| `test_acquisition.py` | 11 | | `test_transcript_api.py` | 21 |
| `test_api_v1.py` | 2 | | `test_transcript_model.py` | 23 |
| `test_audio.py` | 27 | | `test_transcript_normalisation.py` | **28** |
| `test_config.py` | 21 | | `test_transcript_storage.py` | 20 |
| `test_db.py` | 6 | | `test_transcription.py` | 20 |
| `test_health.py` | 9 | | `test_transcription_config.py` | **40** |
| `test_job_recovery.py` | **13** | | `test_transcription_process.py` | **15** |
| `test_jobs_api.py` | 30 | | `test_transcription_timeout.py` | **3** |
| `test_media.py` | 9 | | `test_upload_api.py` | 18 |
| `test_real_model.py` | **10** | | `test_worker.py` | 17 |
| `test_storage.py` | 21 | | `test_worker_pipeline.py` | 18 |
| `test_storage_consistency.py` | 8 | | `test_worker_process.py` | 3 |
| | | | `test_youtube_api.py` | 15 |
| | | | `test_youtube_urls.py` | 51 |
| | | | **Total** | **459** |

### Frontend

| Command | Result | Exit |
| --- | --- | --- |
| `npm run typecheck` (`next typegen && tsc --noEmit`) | types generated, no errors | 0 |
| `npm run lint` (`eslint`) | no findings | 0 |
| `npm test` (`vitest run`) | **5 files, 41 tests passed** (38 before this phase) | 0 |
| `npm run build` | production build, 2 static routes | 0 |
| `npm audit` | 5 high (one advisory) | 1 |
| `npm run audit:check` | the accepted advisory only | 0 |

### The kinds of test, distinguished

| Kind | What it uses | Where |
| --- | --- | --- |
| Unit | fakes, no processes, no model | most of the suite |
| Mocked integration | the real API, the real database, the real worker, a faked engine | `test_worker_pipeline.py`, `test_job_recovery.py` |
| Process-level | real operating-system processes, a stub inference host | `test_transcription_process.py`, `test_transcription_timeout.py`, `test_worker_process.py` |
| Real model | the actual local `faster-whisper` model | `test_real_model.py` |
| Full end-to-end | the real API, the real worker, the real database, real FFmpeg, the real model, over HTTP | `tests/e2e_workflow.py` |

### The skip

One test is skipped: a symlink test that needs a privilege this Windows
account does not have. It was skipped before this phase. **No test was
weakened, skipped or deleted to make this phase pass.** The three Phase 3
assertions that were adjusted are listed in §11.

---

## 10. End-to-end workflow evidence

`backend/tests/e2e_workflow.py` starts the **real API process** and the
**real worker process**, uses a throwaway storage directory, and drives
everything over HTTP. The transcript below is the real output.

```
ClipForge end-to-end workflow check
  storage : ...\Temp\clipforge-e2e\storage
  database: ...\clipforge-e2e\storage\database\clipforge.db

=== 1. build the fixture ===
  [PASS] the fixture video exists -- 42793 bytes
  [PASS] the API answers /health

=== 2. upload a video through the real HTTP API ===
  [PASS] the upload is accepted -- 202
  job id  : 4c5f22c5fb02468b8bb6877c65ad6be
  status  : queued / queued

=== 3. watch the pipeline ===
  status  : completed / ready (100%)
  [PASS] the job completed
  [PASS] the job has a transcript

=== 4. the transcript ===
  [PASS] the transcript endpoint answers
  status  : ready
  language: en   model: tiny.en
  audio   : 16000 Hz, 1 channel(s)
  duration: 3.373938s
  segments: 1
    [  0.00s ->   2.00s] The quick brown fox jumps over the lazy dog.
  [PASS] the transcript is ready
  [PASS] the audio is 16 kHz mono
  [PASS] there is at least one segment
  [PASS] the expected phrase is in the transcript
  [PASS] no segment claims speech outside the audio

=== 5. the temporary artefacts are gone ===
  [PASS] no extracted audio is left behind -- []
  [PASS] the source video is still there

=== 6. a legacy job without a transcript ===
  [PASS] the legacy job is answered
  [PASS] the legacy job does not claim a transcript
  [PASS] the legacy transcript endpoint answers
  [PASS] the legacy transcript is pending, not ready
  [PASS] the legacy transcript has no segments

=== 7. a controlled timeout ===
  job id  : 837acd3c666b4c339c8d2399defc566c
  status  : failed   error: transcription_timeout
  [PASS] the job reached a terminal state -- failed
  [PASS] the failure is the timeout -- transcription_timeout
  [PASS] the job is not left transcribing
  [PASS] a timed-out job has no transcript -- 409
  [PASS] the worker is still running

=== 8. the worker carries on ===
  job id  : faf7dd0cdd324286bdf5eea0a6e5fc73
  status  : completed / ready
  [PASS] the next job completed -- completed
  [PASS] the worker is still running

=== 9. nothing is left behind ===
  [PASS] no extracted audio is left behind -- []
  [PASS] no inference process is left behind -- []

=== result ===
  every step passed
```

Mapping the thirteen required steps onto that run:

| Required step | Where it is shown |
| --- | --- |
| 1. A clean, temporary test data directory | The storage directory is created fresh under `%TEMP%` and removed at the start |
| 2. Upload a small valid video with known speech over the real HTTP API | Step 2 |
| 3. Source validation and durable job creation | Step 2 — the upload is validated by FFprobe and the job is created before the worker sees it |
| 4. The audio is extracted as 16 kHz mono PCM WAV | Step 4 — the stored transcript reports `16000 Hz, 1 channel(s)`; the file itself is probed by `app/audio.py` before it is used, and 27 tests in `test_audio.py` check the file directly |
| 5. Transcription runs locally and offline | Steps 3–4 — the real model, with the inference process unable to open a socket |
| 6. The transcript and segments are persisted | Step 3 (`has_transcript` is true) and step 4 |
| 7. The transcript API returns the right timestamps and text | Step 4 |
| 8. The frontend shows the transcript and the job state | **Not exercised by this script.** The UI is covered by component tests (`TranscriptPanel.test.tsx`, 12 tests) — the panel's ready, pending, processing, empty, failed and legacy states. A browser-level check was not run in this phase. |
| 9. A failure path, preferably a controlled timeout | Step 7 — a real timeout, with a real model load that exceeds a 1-second limit |
| 10. The worker survives and processes another job | Steps 7 and 8 |
| 11. Temporary artefacts and child processes are cleaned up | Steps 5 and 9 |
| 12. An older completed job without a transcript is handled | Step 6 |
| 13. Repeat the workflow without internet access | **Partly.** The machine has internet, so the whole machine could not be taken offline. What *was* done: the inference process is forbidden from opening a socket at all, the real-model suite runs with that guard active and passes, and the end-to-end run uses the local model cache. **What remains unverified:** that the API and the worker processes themselves would work with the network physically removed — they make no network calls in this workflow apart from the optional YouTube download, which was not exercised. |

---

## 11. Changed files, and why

### New — production code

| File | Why |
| --- | --- |
| `backend/app/transcription_process.py` | The supervisor: starts the inference process, owns the deadline, terminates and reaps the child, keeps the model loaded between jobs, and normalises the child's output into a transcript. |
| `backend/app/transcription_host.py` | The inference process itself: loads the model, decodes, streams the segments back, and refuses to use the network. |

### New — tests and helpers

| File | Why |
| --- | --- |
| `backend/tests/test_transcription_process.py` | 15 tests: the timeout, the cleanup, the failures, the reuse of the model, the network guard, the shutdown path. |
| `backend/tests/test_transcription_timeout.py` | 3 process-level tests: a stalled inference, a worker that survives it, a graceful shutdown. |
| `backend/tests/test_transcription_config.py` | 40 tests: the model name, the cache, the offline behaviour, the isolation mode. |
| `backend/tests/test_transcript_normalisation.py` | 28 tests: the normalisation rules, one per case. |
| `backend/tests/test_job_recovery.py` | 13 tests: ownership, lease recovery, retries, terminal states, legacy jobs, a failed migration. |
| `backend/tests/test_real_model.py` | 10 tests: the real model against the matrix. |
| `backend/tests/host_stub.py` | A stub inference host: the same protocol, able to stall, crash, fail or be slow. |
| `backend/tests/worker_stub.py` | A worker entry point for the process-level tests, with the inference host pointed at the stub. |
| `backend/tests/real_model_fixtures.py` | Builds the audio fixtures locally, with the speech synthesiser and FFmpeg. |
| `backend/tests/e2e_workflow.py` | The end-to-end workflow check. |
| `frontend/scripts/check-audit.mjs` | Fails if a dependency advisory other than the accepted one appears. |

### Modified — production code

| File | Why |
| --- | --- |
| `backend/app/transcription.py` | The `isolation` and `max_segments` settings, the model-name validator, the exact cache match, the `duration_seconds` argument, the `TRANSCRIPTION_INVALID_OUTPUT` code, `terminate()`, `build_engine()` dispatch. |
| `backend/app/transcripts.py` | The documented rule list, the `max_segments` bound, the per-segment text bound, and the zero-duration rule applied consistently. |
| `backend/app/errors.py` | The new `transcription_invalid_output` code. |
| `backend/app/worker.py` | The ownership guards, the FFprobe duration passed to the engine, the engine stopped on shutdown, the startup configuration report. |
| `backend/app/db.py` | `expected_worker_id` in `update_job` and `save_transcript`, ownership in `renew_lease`, the active-status guard in `release_for_retry`, and the `snapshot()` read transaction. |
| `backend/app/routers/jobs.py` | The transcript endpoint reads the job and the transcript in one snapshot. |
| `backend/app/routers/health.py` | The cache check uses the shared function instead of building an engine. |
| `frontend/src/components/TranscriptPanel.tsx` | The legacy-completed state, and long text that wraps. |

### Modified — tests

| File | Why |
| --- | --- |
| `backend/tests/fakes.py` | The fake engine accepts the duration and can be stopped. |
| `backend/tests/test_worker_pipeline.py` | The overriding engines accept the new argument; the retry test now uses a realistic recovery path. |
| `backend/tests/test_transcription.py` | The engine factory tests now cover both isolation modes. |
| `backend/tests/test_transcript_api.py` | Two tests: the pagination is stable across polls, and polling starts no work. |
| `frontend/src/components/TranscriptPanel.test.tsx` | Three tests: the legacy state, a long segment, and a failure that is not dressed up as an empty transcript. |
| `backend/pyproject.toml` | The `real_model` marker, so the model tests can be selected or skipped. |

### Modified — documentation

`README.md`, `.env.example`, `backend/.env.example`, `docs/ARCHITECTURE.md`
(§8.3 and §8.4, the duration and the process boundary), `docs/DECISIONS.md`
(D31–D34), `docs/ROADMAP.md` (Phase 3.1), `docs/TESTING.md` (the new suites,
the matrix, the end-to-end check, and an honest "not tested yet").

### The Phase 3 assertions that were changed

Three, and each because the behaviour genuinely changed:

| File | Before | After | Why |
| --- | --- | --- | --- |
| `test_transcription.py` | `build_engine` returns a `FasterWhisperEngine` | two tests: the default is the process engine, `inline` is the fallback | the default engine changed |
| `test_worker_pipeline.py` | the retry test released a **completed** job | it now reproduces a crash between the transcript commit and the job completion | `release_for_retry` no longer moves a terminal job, and the new path is the realistic one |

Two Phase 3 tests (`test_a_cached_model_is_recognised` and
`test_offline_mode_uses_a_cached_model`) needed **no** change: they were
passing before and they pass now. What changed is the *implementation* they
exercise, and the new behaviour it needed — a request for `base` must not be
satisfied by a cached `base.en` — is covered by a new test in
`test_transcription_config.py`.

No test was weakened to make it pass.

---

## 12. Remaining known limitations

1. **The `braces` advisory is still open.** It has no upstream fix, it is
   dev-only, and it is now guarded rather than ignored. §8.
2. **The Node LTS parity check was not run.** No LTS release is installed on
   this machine, so "works on 22 LTS" rests on Next.js's `engines` field
   rather than on an executed check. The repository also has **no CI at
   all**, which is the larger gap.
3. **A worker killed with `SIGKILL` leaves its inference child until the
   child notices the closed pipe.** It cannot outlive the current
   transcription, and the automated test proves it exits, but the exit is not
   instantaneous.
4. **The real-model suite runs on one model** (`tiny.en`) and on locally
   synthesised speech. It says nothing about `base.en` or `small.en` on real
   recordings.
5. **No accuracy figure exists.** No word error rate has been measured, and
   none is claimed.
6. **The end-to-end check does not drive the browser.** It proves the API,
   the worker, the database and the model; the UI is covered by component
   tests. There is still no browser automation.
7. **A long recording needs a lot of memory** — roughly 2.9 MB per minute
   for the decoded audio, so the four-hour default can need about 700 MB.
   The limit is configurable; the default was not changed, because that is a
   product decision and not a reliability fix.
8. **The offline claim is enforced inside the inference process**, not at
   the machine level. The machine could not be taken offline in this
   environment, so "the whole application works with the network removed"
   was not verified end to end. §10, step 13.
9. **The inline isolation mode still has a soft timeout.** It is the
   documented fallback, not the default.
10. **The transcript is paginated at 1 000 segments per request.** A very
    long recording is walked with `offset`; the frontend shows the first
    page and says how many there are in total.

---

## 13. Phase 4 readiness

### **READY WITH DOCUMENTED LIMITATIONS**

The reasoning, against the three readiness outcomes:

- **No unresolved critical correctness or security issue remains.** The two
  data-integrity bugs found in this phase — a stale worker overwriting a
  newer attempt, and a terminal job being requeued — are fixed and covered
  by tests. The timeout is now enforced at the process level, not asserted.
  The two new resource limits are documented and configurable.
- **The hard-timeout claim is backed by process-level evidence.** Not by
  argument: the tests start a real worker, make the inference stall on
  purpose, and observe the job ending in a terminal state with
  `transcription_timeout`, the worker still running, the next job
  completed, and no orphan process left behind. The end-to-end run repeats
  it against the real model and the real pipeline.
- **No partial transcript is ever committed**, and a job is never left
  marked `transcribing`.
- **The transcript that Phase 4 will build on is trustworthy**: its
  timestamps are inside the audio, its ordering is deterministic, its
  segments satisfy the database constraints, an empty recording is empty
  rather than wrong, and malformed output is a failure rather than a
  success.
- **The remaining limitations are scoped and do not contradict any stated
  guarantee.** They are listed in §12. None of them affects the correctness
  of a stored transcript, the enforceability of the timeout, or the
  integrity of the job lifecycle.

### What Phase 4 can rely on

- A transcript is stored against its job, with ordered, timestamped
  segments, before the job is marked completed.
- Every segment is inside the audio it describes, with `end >= start >= 0`
  and a non-empty text.
- The transcript is exactly what the configured local model produced,
  normalised by documented rules and nothing else.
- A failed or timed-out job has **no** transcript, and says so with a
  stable error code.
- `has_transcript` is truthful, including for jobs that were finished
  before transcripts existed.

### What Phase 4 must still do for itself

- Decide the LLM provider and its endpoint dialect (still an open
  decision, D-series).
- Handle the provider's own timeouts and retries; the timeout contract
  proved here is for *local* inference, and an external API needs a
  different one.
- Decide what to do about `average_log_probability`, which is stored but
  deliberately not exposed, because it is a log-probability and is easy to
  misread as a confidence score.

Nothing in Phase 4 needs to compensate for unreliable transcription or
unreliable job recovery, because those are now either fixed or explicitly
documented.

---

*Every result in this document was transcribed from a command that was
actually run on 20 October 2026. Where a check could not be run, it says
so rather than claiming a pass. Nothing was committed.*

---

## Addendum — 31 October 2026: the audit baseline grew by one accepted advisory

`npm audit` started reporting a second advisory in the same dev-only ESLint
chain: **GHSA-g3hv-75wg-m249 (moderate) against `minimatch` < 11** (globparts
amplification). Every ESLint tool sits on minimatch 10 through
`@eslint/config-array`, and the only fix npm offers is downgrading `eslint`
to v4 — the same dead end the `braces` advisory hit. It is lint tooling:
nothing from it ships to the browser or runs in the backend. The advisory
(1244012) is accepted and recorded in `scripts/check-audit.mjs` alongside
`braces` (1240992); the gate still fails on anything new.
