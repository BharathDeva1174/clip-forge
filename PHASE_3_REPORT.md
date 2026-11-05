# Phase 3 — Audio extraction and transcription

**Status: COMPLETE.** Every acceptance criterion below was verified with an
executed check, not with an inspection of the code. The commands were run, the
output was read, and the output is transcribed here.

Phase 3 turns an accepted video into a **timestamped transcript** that lives
next to the job in SQLite and is readable from the API and from the job list.
The audio is extracted with FFmpeg and transcribed by a speech-to-text model
that runs **entirely on the machine**: no audio, no transcript and no request
ever leaves it.

---

## 1. What was built

| Piece | Where |
| --- | --- |
| FFmpeg audio extraction to 16 kHz mono PCM WAV | `backend/app/audio.py` |
| The local speech-to-text engine (faster-whisper) | `backend/app/transcription.py` |
| The transcript rules — what a valid segment is | `backend/app/transcripts.py` |
| The "stop now" signal for long-running steps | `backend/app/interruption.py` |
| The Phase 3 schema migration and the transcript repository | `backend/app/db.py` |
| The two new worker stages and the pipeline | `backend/app/worker.py` |
| The transcript endpoint | `backend/app/routers/jobs.py` |
| The transcript panel and the new job states | `frontend/src/components/TranscriptPanel.tsx`, `frontend/src/components/JobList.tsx` |

### The pipeline

```
queued → validating → downloading → inspecting
       → extracting_audio → transcribing → completed
```

`completed` now means **the source was stored, its audio was extracted and its
transcript was saved**. The state machine enforces it: `inspecting` can no
longer go straight to `completed`, so a completed job always implies that a
transcript was committed first.

---

## 2. The engineering decisions, in short

The full reasoning is in [docs/DECISIONS.md](docs/DECISIONS.md) (D25–D30).
The short version:

1. **The engine runs locally.** `faster-whisper` runs Whisper models on the
   machine. No hosted transcription API, no credential, nothing leaves the
   disk. Phase 2 had left this open; the local-first decision settles it.
2. **The model is loaded in the worker, lazily, and reused.** It is never
   loaded at API start-up, and the API never loads it at all.
3. **Offline is a mode, not a hope.**
   `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` makes the engine refuse to touch
   the network; a missing model then produces an actionable error instead of a
   silent download.
4. **The rules live in their own module.** `app/transcripts.py` decides what a
   valid segment is. It does no I/O and knows nothing about the engine or
   SQLite, so the same rules apply to any engine.
5. **A timestamp that points past the audio is normalised, not rejected.**
   See §5 — this was found by running the real model, not by reasoning about
   it.
6. **The transcript is committed before the job is completed.** A client that
   sees a completed job can always read its transcript.

---

## 3. The schema migration

SQLite cannot widen a `CHECK` constraint in place, and Phase 3 adds two
statuses and two stages. The migration therefore rebuilds the `jobs` table:
a new table is created with the wider constraints, every existing row is
copied into it, the old table is dropped and the new one takes its place — all
inside one transaction. Either every job survives or the database is left
exactly as it was.

Two new tables are created:

```sql
CREATE TABLE transcripts (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    language            TEXT,
    model_name          TEXT NOT NULL,
    audio_sample_rate   INTEGER NOT NULL CHECK (audio_sample_rate > 0),
    audio_channels      INTEGER NOT NULL CHECK (audio_channels > 0),
    duration_seconds    REAL,
    segment_count       INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
    created_at          TEXT NOT NULL
);

CREATE TABLE transcript_segments (
    id                      TEXT PRIMARY KEY,
    transcript_id           TEXT NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    segment_index           INTEGER NOT NULL CHECK (segment_index >= 0),
    start_seconds           REAL NOT NULL CHECK (start_seconds >= 0),
    end_seconds             REAL NOT NULL CHECK (end_seconds >= 0),
    text                    TEXT NOT NULL,
    average_log_probability REAL,
    no_speech_probability   REAL,
    UNIQUE (transcript_id, segment_index)
);
```

- The unique constraint on `job_id` is what makes a retry **replace** a
  transcript rather than add a second one.
- The unique constraint on `(transcript_id, segment_index)` is what makes the
  order of a transcript unambiguous.
- `ON DELETE CASCADE` means a deleted job takes its transcript with it.

### The migration was verified against a real Phase 2 database

The test builds a database exactly as the Phase 2 build left it — schema
version 2, one completed job with a source path — and then migrates it:

```
phase2 jobs         : 1
version before      : 2
version after       : 3
jobs preserved      : 1
job row             : {'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'status': 'completed',
                       'stage': 'ready', 'progress': 100}
job indexes         : ['idx_jobs_created_at', 'idx_jobs_lease',
                       'idx_jobs_status_created_at', 'idx_jobs_worker_id',
                       'sqlite_autoindex_jobs_1']
tables              : ['jobs', 'schema_migrations', 'transcript_segments', 'transcripts']
jobs has new stage  : True
jobs is not a stub  : True
new statuses accepted: True
idempotent          : True
```

The same migration ran against the **live development database** during the
integration check in §6, and the two jobs that the Phase 2 session had
created were still there afterwards, unchanged.

---

## 4. The audio extraction

`backend/app/audio.py` runs FFmpeg and then **does not trust it**. A zero exit
code is not evidence: the produced file is probed again and is only accepted
if it really is mono, really is 16 kHz, really is PCM and really has a
duration.

| Guarantee | How it is achieved |
| --- | --- |
| No shell | An argument list and `shell=False`. A filename containing `&` or `;` is just a filename. |
| No deadlock | FFmpeg's stderr goes to a temporary file, not a pipe. A pipe has a fixed buffer, and a child that fills it while the parent is not reading would block forever. |
| Bounded | The child has a timeout and can be terminated, so a worker asked to shut down stops FFmpeg instead of waiting for it. |
| Bounded diagnostics | Only 2000 bytes of stderr are kept, and it never reaches an API response. |
| No collisions | The output file name is generated inside the service, so an extraction can never overwrite a source video or another job's artefact. |
| Containment | The source is checked to be inside the storage root before it is read. |

### The real output

```
test_the_extracted_audio_is_mono_16khz_pcm
  channels   = 1
  framerate  = 16000
  sampwidth  = 2
  frames     > 0
  |frames/rate - duration| < 0.05
```

---

## 5. A real-world defect that the unit tests did not find

This is the most important thing in the report, so it is written out in full.

The **first live run with the real model failed**:

```
  queued/queued          progress   0%  attempt 0
  transcribing/transcribing progress  85%  attempt 1
  failed/failed          progress  85%  attempt 1

error : transcription_failed - The transcription produced timestamps that could not be trusted.
```

The validation was correct in principle and wrong in practice. The engine
reported the end of the last segment as **10.0 s in a 6.7 s recording**:

```
  start=0.0   end=2.48  text=' The quick brown fox jumps over the lazy dog.'
  start=3.36  end=10.0  text=' Clipforged transcribes your videos locally.'
```

The cause was measured rather than guessed. Across four audio lengths and
both VAD settings, the final segment always ended at exactly 10.0 s:

```
 audio s  tail s   vad      dur  max end    over
    6.70     0.0  True     6.70    10.00    3.30
    6.70     0.0 False     6.70    10.00    3.30
    7.70     1.0  True     7.70    10.00    2.30
    9.70     3.0  True     9.70    10.00    0.30
   14.70     8.0  True    14.70    10.00   -4.70
```

Whisper's timestamps are quantised to 20 ms and are produced from a
30-second sliding window, so the end of the last segment regularly points
**past** the end of the audio. That is a known artefact of the model, not a
broken value.

**The fix** (`app/transcripts.py`):

- A timestamp may reach up to one 30-second window past the audio. Beyond
  that it is rejected as genuinely wrong.
- Within that allowance the timestamp is **pulled back** to the end of the
  audio, so the stored transcript never claims that speech happened after the
  audio ended.
- A segment that lies **entirely** outside the audio is dropped. There is no
  honest timestamp for it — in practice it is a hallucination over trailing
  silence.

The rules are now covered by four tests, including the exact case the real
model produced:

```
test_a_segment_past_the_end_of_the_audio_is_pulled_back
test_a_segment_within_the_window_overshoot_is_accepted
test_a_segment_far_past_the_end_of_the_audio_is_rejected
test_a_segment_that_lies_entirely_past_the_audio_is_dropped
test_a_segment_that_ends_past_the_audio_keeps_its_real_span
```

The second live run, after the fix, produced the correct transcript (§6).

---

## 6. The live integration check

Real processes, a real video with real speech, and the real model.

### The fixture

The speech was generated locally with the Windows speech synthesiser, so no
third-party media was used, and the phrase is known:

> *"The quick brown fox jumps over the lazy dog. ClipForge transcribes your
> videos locally."*

It was muxed into a 6-second 640×360 H.264 video with FFmpeg.

### The model

`tiny.en` was cached into `backend/data/models/`:

```
models--Systran--faster-whisper-tiny.en\snapshots\0d3d19a3...\model.bin        75537502 bytes
models--Systran--faster-whisper-tiny.en\snapshots\0d3d19a3...\tokenizer.json     2128466 bytes
models--Systran--faster-whisper-tiny.en\snapshots\0d3d19a3...\vocabulary.txt     422309 bytes
models--Systran--faster-whisper-tiny.en\snapshots\0d3d19a3...\config.json          2317 bytes
model loaded in 7.6s
```

### The run

The API (`uvicorn app.main:app --host 127.0.0.1 --port 8000`) and the worker
(`python worker.py`) were started as separate processes, and the video was
uploaded over the real HTTP API.

```
=== 1. upload ===
job id      : 422ff23222c24ac69fd6e863de2f2024
status      : queued / queued (0%)

=== 2. watch the pipeline ===
  queued/queued          progress   0%  attempt 0
  transcribing/transcribing progress  85%  attempt 1
  completed/ready        progress 100%  attempt 1

=== 3. final job ===
status      : completed / ready (100%)
error       : none
duration    : 6s  640x360 h264

=== 4. transcript ===
status      : ready
language    : en
model       : tiny.en
audio       : 16000 Hz, 1 channel(s)
segments    : 2
  [  0.00s ->   2.48s] The quick brown fox jumps over the lazy dog.
  [  3.36s ->   5.99s] Clipforged transcribes your videos locally.

full text   : The quick brown fox jumps over the lazy dog. Clipforged transcribes your videos locally.
RESULT      : PASS - the expected phrase is in the transcript
```

Note the second segment's end: **5.99 s**, not the 10.0 s the engine
reported. The timestamp was pulled back to the end of the audio, exactly as
§5 describes.

### The database afterwards

```
{'id': 'c0eb2e076964488a845d52a56f4df084', 'status': 'completed', 'stage': 'ready',
 'progress': 100, 'attempt_count': 1, 'error_code': None, 'video_duration_seconds': 634.594}
{'id': '4cbb69b3370e45719d1163cc42e28eb0', 'status': 'completed', 'stage': 'ready',
 'progress': 100, 'attempt_count': 1, 'error_code': None, 'video_duration_seconds': 634.594}
{'id': '0d9f6746587b4af0ba58317be6b8fea3', 'status': 'failed', 'stage': 'failed',
 'progress': 85, 'attempt_count': 1, 'error_code': 'transcription_failed'}
{'id': '422ff23222c24ac69fd6e863de2f2024', 'status': 'completed', 'stage': 'ready',
 'progress': 100, 'attempt_count': 1, 'error_code': None}

--- transcripts ---
{'id': '20e5b35197aa443d870660a23fd05987', 'job_id': '422ff23222c24ac69fd6e863de2f2024',
 'language': 'en', 'model_name': 'tiny.en', 'audio_sample_rate': 16000,
 'audio_channels': 1, 'duration_seconds': 5.99075, 'segment_count': 2}

--- segments ---
{'segment_index': 0, 'start_seconds': 0.0, 'end_seconds': 2.48,
 'text': 'The quick brown fox jumps over the lazy dog.'}
{'segment_index': 1, 'start_seconds': 3.36, 'end_seconds': 5.99075,
 'text': 'Clipforged transcribes your videos locally.'}
```

- The first two rows are the **Phase 2 jobs**. They survived the Phase 3
  migration untouched — they were completed before transcripts existed, so
  they have none, and they have no transcript row.
- The third row is the run that failed in §5, before the fix. It kept its
  stable error code.
- The fourth row is the successful run.
- The extracted audio is **gone**: there is no `data/temp/audio-*` directory
  left behind.

### The readiness probe

```
{
  "status": "ready", "service": "clipforge-api", "version": "1.2.0", "config": "loaded",
  "capabilities": {
    "video_ingestion": true, "background_worker": true, "database": true,
    "persistent_storage": true, "audio_extraction": true, "transcription": true,
    "llm_highlight_detection": false, "clip_rendering": false,
    "vertical_formatting": false, "subtitle_generation": false
  },
  "transcription_model_cached": true
}
```

The later capabilities are still `false`. Nothing that does not exist is
advertised.

### The frontend, in a real browser

The browser session against http://localhost:3000 with the API and the worker
running showed the job list with the real transcript:

```
speech.mp4
422ff232 · upload
Transcribed            [Hide transcript]
  2 segments   Language: en   Model: tiny.en   Audio: 0:05
  0:00   The quick brown fox jumps over the lazy dog.
  0:03   Clipforged transcribes your videos locally.

speech.mp4
0d9f6746 · upload
Failed
  The transcription produced timestamps that could not be trusted.  transcription_failed

https://www.youtube.com/watch?v=aqz-KE-bpKQ
4cbb69b3 · youtube
Completed

https://www.youtube.com/watch?v=aqz-KE-bpKQ
c0eb2e07 · youtube
Completed
```

Note the last two: they are `Completed`, **not** `Transcribed`. They are the
Phase 2 jobs, they have no transcript, and the badge does not claim one. That
is what the `has_transcript` flag in the API is for.

---

## 7. The tests

### The suites, and what they cover

| File | Tests | Covers |
| --- | --- | --- |
| `test_acquisition.py` | 11 | The downloader adapter (Phase 2) |
| `test_api_v1.py` | 2 | The API metadata endpoint |
| `test_audio.py` | **27** | FFmpeg extraction: success, every failure mode, paths, safety |
| `test_config.py` | 21 | Settings, the loopback binding rule, secret masking |
| `test_db.py` | 6 | The base schema |
| `test_health.py` | 9 | The probes and the capability flags |
| `test_jobs_api.py` | 30 | The job endpoints and the state machine |
| `test_media.py` | 9 | FFprobe inspection |
| `test_storage.py` | 21 | Path containment |
| `test_storage_consistency.py` | **8** | The filesystem/SQLite boundary, by failure injection |
| `test_transcript_api.py` | **19** | The transcript endpoint |
| `test_transcript_model.py` | **23** | The transcript rules |
| `test_transcript_storage.py` | **20** | The migration and the transcript repository |
| `test_transcription.py` | **19** | The speech engine, with the model faked |
| `test_upload_api.py` | 18 | The upload endpoint |
| `test_worker.py` | 17 | The worker (Phase 2), now with a faked engine |
| `test_worker_pipeline.py` | **18** | The Phase 3 pipeline |
| `test_worker_process.py` | **3** | The worker as a separate operating-system process |
| `test_youtube_api.py` | 15 | The YouTube submission endpoint |
| `test_youtube_urls.py` | 51 | The URL validator |
| **Total** | **346** | 1 skipped |

Frontend: **38 tests** in 5 files.

### The results

```
$ python -m pytest
346 passed, 1 skipped in 20.64s
```

```
$ npm run typecheck   -> exit 0
$ npm run lint        -> exit 0
$ npm test            -> Test Files 5 passed (5), Tests 38 passed (38)
$ npm run build       -> exit 0
```

**Baseline comparison.** The same suites were run before any Phase 3 code was
written:

| | Before | After |
| --- | --- | --- |
| Backend | 190 passed, 1 skipped | 346 passed, 1 skipped |
| Frontend tests | 25 passed | 38 passed |
| Frontend typecheck / lint / build | exit 0 | exit 0 |

### The engine is faked, the rules are not

The ordinary suite never loads the speech model. `tests/fakes.py` provides a
scripted engine that still goes through the **real** validation code, so the
tests exercise the same rules as the real engine without a download and
without a slow decode.

The one place the real model is used is the manual integration check in §6.

### The Phase 2 suites still pass

Every Phase 1 and Phase 2 test file is present and passing. Three assertions
had to change, because the behaviour they describe genuinely changed, and
each one was changed to match the **new, stricter** behaviour rather than to
keep the old result:

| File | Before | After | Why |
| --- | --- | --- | --- |
| `test_api_v1.py` | `stage == "phase-2-ingestion"` | `stage == "phase-3-transcription"` | The build moved to Phase 3. |
| `test_health.py` | `audio_extraction` and `transcription` are `false` | they are `true` | The capabilities exist now. The later ones are still `false`. |
| `test_jobs_api.py` | `inspecting → completed` is allowed | it is rejected; `inspecting → extracting_audio → transcribing → completed` is allowed | A completed job must have a transcript. |

No test was weakened, skipped or deleted.

### The new tests, by requirement

**Audio extraction** — `tests/test_audio.py`, against real FFmpeg:

| Requirement | Test |
| --- | --- |
| Success from a known-good video | `test_audio_is_extracted_from_a_valid_video` |
| The output is mono, 16 kHz, PCM, with a valid duration | `test_the_extracted_audio_is_mono_16khz_pcm` |
| A missing FFmpeg | `test_a_missing_ffmpeg_produces_a_stable_error` |
| Invalid media | `test_invalid_media_fails_safely` |
| A truncated source | `test_a_truncated_source_fails_safely` |
| A source with no audio stream | `test_a_source_without_an_audio_stream_is_reported_explicitly` |
| The timeout | `test_the_extraction_timeout_stops_the_process_and_cleans_up` |
| A non-zero exit, an empty output, an output that is not audio | three stub-driven tests |
| Paths with spaces and Unicode | `test_paths_with_spaces_and_unicode_work` |
| Path traversal and a source outside the root | `test_a_source_outside_the_storage_root_is_refused`, `test_a_path_that_traverses_out_of_the_root_is_refused` |
| The output cannot overwrite a source or another result | `test_the_output_never_overwrites_a_source_or_another_result` |
| The diagnostics are bounded and never leak | `test_the_diagnostic_output_is_bounded_and_never_leaks` |
| FFmpeg is never run through a shell | `test_ffmpeg_is_never_run_through_a_shell` |

**Transcription** — `tests/test_transcription.py` and
`tests/test_transcript_model.py`:

| Requirement | Test |
| --- | --- |
| The output becomes the internal schema | `test_the_engine_output_becomes_a_transcript` |
| The timestamp boundaries and the deterministic order | `test_a_segment_past_the_end_of_the_audio_is_pulled_back` and nine more |
| Unicode, punctuation and non-English text | `test_unicode_and_punctuation_are_preserved`, `test_non_english_text_is_not_discarded` |
| Empty speech and an empty audio file | `test_an_empty_result_is_a_valid_empty_transcript`, `test_an_empty_result_is_skipped_rather_than_stored` |
| Malformed timestamps and invalid segment data | six tests, including `a_timestamp_that_is_not_finite_is_rejected` |
| A model that cannot be initialised | `test_a_model_that_cannot_be_loaded_is_reported` |
| A missing model cache while offline | `test_offline_mode_without_a_cached_model_never_reaches_the_network` |
| A transcription exception and the safe error mapping | `test_a_failure_while_decoding_is_mapped_to_a_stable_code` |
| The model is reused, and the concurrency is respected | `test_the_model_is_loaded_lazily_and_only_once`, `test_the_configured_concurrency_is_respected` |
| No external transcription API is called | `test_the_engine_never_opens_a_network_connection` |
| A retry does not duplicate the result | `test_a_retry_does_not_duplicate_the_transcript` |

**Storage** — `tests/test_transcript_storage.py`:

The migration from a Phase 2 database, the new tables, the indexes, the
idempotency, the foreign keys, the ordered and paginated retrieval, the
rollback when a segment insert fails, the replacement on a retry, the
survival across a restart, and four connections writing at the same time.

**The worker** — `tests/test_worker_pipeline.py` and
`tests/test_worker_process.py`:

The full pipeline, the engine receiving the extracted audio, the cleanup of
the temporary audio, every failure mode, the interruption, the retry, two
workers racing, the API staying responsive while the transcription runs, the
logs containing no absolute path, and — as a **separate operating-system
process** — the worker claiming a job, taking over a job left behind by a
dead worker, and leaving the database consistent when it is stopped.

**The API** — `tests/test_transcript_api.py`:

The transcript of a finished job, the timestamped segments, the pagination, a
job that is still running, a failed job, an unknown id, a malformed id,
invalid pagination, the stable error code of a failure, the `has_transcript`
flag, and that no response contains a path.

---

## 8. The API

### `GET /api/v1/jobs/{id}/transcript`

| Status | When |
| --- | --- |
| `200` | The transcript is ready, pending, or the job failed |
| `400` | The job id is not a valid identifier |
| `404` | No job with that id exists |
| `409` | The job failed; the body carries the job's stable error code |

The response always has the same shape:

```json
{
  "job_id": "422ff23222c24ac69fd6e863de2f2024",
  "status": "ready",
  "job_status": "completed",
  "job_stage": "ready",
  "language": "en",
  "model_name": "tiny.en",
  "audio_sample_rate": 16000,
  "audio_channels": 1,
  "duration_seconds": 5.99075,
  "created_at": "2026-10-20T10:27:24.437234+00:00",
  "segment_count": 2,
  "segments": [
    {"index": 0, "start_seconds": 0.0, "end_seconds": 2.48,
     "text": "The quick brown fox jumps over the lazy dog.",
     "no_speech_probability": 0.011546151712536812}
  ],
  "limit": 500,
  "offset": 0,
  "error": null
}
```

- `status` is `ready`, `pending` or `failed`.
- `segment_count` is the **total**; `segments` is one page. `limit` and
  `offset` walk through a long transcript, and the frontend says so when it
  is showing only part of one.
- `no_speech_probability` is the engine's own estimate that the segment holds
  no speech. It is a hint about the audio, not a measure of how correct the
  text is, and the field name says so.
- `average_log_probability` is **stored** but not exposed: it is a
  log-probability (the real value in the live run was `-0.4036`), which is
  easy to misread as a confidence score.

`GET /api/v1/jobs` and `GET /api/v1/jobs/{id}` gained one additive field,
`has_transcript`. It is what lets the frontend say *Transcribed* only when a
transcript really exists.

---

## 9. The frontend

- The job list shows **Extracting audio** and **Transcribing** as their own
  states, with the pipeline stage spelled out underneath.
- The transcription progress is labelled **as an estimate**: *"around 91%, an
  estimate rather than an exact figure"*. The library does not report an
  exact percentage, so neither does the UI.
- **Transcript** opens a panel with the timestamped segments, the segment
  count, the detected language, the model and the audio duration.
- The badge says **Transcribed** only when `has_transcript` is true.
- The panel polls while the job is running and stops when it is finished.
- The pipeline preview marks *Ingest* and *Transcribe* as **Done** and
  *Find highlights* as **Next up**.

---

## 10. Acceptance criteria

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | The Phase 1 and Phase 2 test suites still pass | **PASS** | 346 passed, 1 skipped; every Phase 1/2 file is present. §7 |
| 2 | Audio is extracted from an accepted source with FFmpeg | **PASS** | §4 and §7 — 27 audio tests against real FFmpeg, plus the live run in §6 |
| 3 | The extracted audio is mono, 16 kHz, PCM, with a valid duration | **PASS** | `test_the_extracted_audio_is_mono_16khz_pcm`; the live transcript reports `16000 Hz, 1 channel(s)` |
| 4 | A missing FFmpeg produces a stable, actionable failure | **PASS** | `test_a_missing_ffmpeg_produces_a_stable_error` → `ffmpeg_not_found` |
| 5 | Invalid or truncated media fails safely | **PASS** | `test_invalid_media_fails_safely`, `test_a_truncated_source_fails_safely` |
| 6 | A source with no audio stream is handled explicitly | **PASS** | `test_a_source_without_an_audio_stream_is_reported_explicitly` → `no_audio_stream`; the worker test proves the job fails with that code |
| 7 | The FFmpeg timeout terminates the process and cleans up | **PASS** | `test_the_extraction_timeout_stops_the_process_and_cleans_up` |
| 8 | A non-zero exit and an empty output fail safely | **PASS** | two stub-driven tests |
| 9 | Paths with spaces and Unicode work | **PASS** | `test_paths_with_spaces_and_unicode_work` |
| 10 | Path traversal and paths outside the root are rejected | **PASS** | `test_a_source_outside_the_storage_root_is_refused`, `test_a_path_that_traverses_out_of_the_root_is_refused`, plus the storage suite |
| 11 | The output cannot overwrite a source or another job's artefact | **PASS** | `test_the_output_never_overwrites_a_source_or_another_result` |
| 12 | The diagnostics are bounded and never leak through the API | **PASS** | `test_the_diagnostic_output_is_bounded_and_never_leaks`; the API test asserts no path in any response |
| 13 | A local, offline speech-to-text engine is used | **PASS** | `faster-whisper`; `test_the_engine_never_opens_a_network_connection`; the live run in §6 |
| 14 | The model is loaded in the worker and reused | **PASS** | `test_the_model_is_loaded_lazily_and_only_once`; the API never loads it |
| 15 | The concurrency limit is honoured | **PASS** | `test_the_configured_concurrency_is_respected` |
| 16 | The engine output becomes the internal schema | **PASS** | `test_the_engine_output_becomes_a_transcript` |
| 17 | The timestamps are validated and the ordering is deterministic | **PASS** | 23 tests in `test_transcript_model.py`, including the real overshoot case (§5) |
| 18 | Unicode, punctuation and non-English text are preserved | **PASS** | `test_unicode_and_punctuation_are_preserved`, `test_non_english_text_is_not_discarded` |
| 19 | Empty speech and malformed data are handled intentionally | **PASS** | `test_an_empty_result_is_a_valid_empty_transcript`, `test_a_timestamp_that_is_not_finite_is_rejected` and five more |
| 20 | The transcript is stored against the job in SQLite | **PASS** | `test_a_transcript_is_stored_with_its_segments`; the live database dump in §6 |
| 21 | The migration is backward-compatible with an existing database | **PASS** | §3 — a real Phase 2 database migrates with its job intact; the live Phase 2 jobs survived |
| 22 | The migration is transactional and leaves no damage | **PASS** | `test_the_migration_leaves_no_placeholder_table`, `test_the_migration_is_idempotent` |
| 23 | The foreign keys and the uniqueness are enforced | **PASS** | `test_a_transcript_needs_a_real_job`, `test_a_segment_needs_a_real_transcript`, `test_a_job_can_only_have_one_transcript` |
| 24 | The segments are stored and read back in order | **PASS** | `test_the_segments_come_back_in_order`, `test_the_segments_are_paginated` |
| 25 | The persistence is atomic | **PASS** | `test_a_failure_while_storing_the_segments_rolls_everything_back` |
| 26 | A retry does not duplicate the transcript | **PASS** | `test_saving_the_same_transcript_twice_replaces_it`, `test_a_retry_does_not_duplicate_the_transcript` |
| 27 | A completed job is only shown as completed once the transcript is stored | **PASS** | the state machine forbids `inspecting → completed`; the transcript is committed before the status update |
| 28 | The worker reports the extraction and transcription stages | **PASS** | the live run in §6 and `test_a_job_goes_through_extraction_and_transcription` |
| 29 | A failing transcription fails the job with a stable code | **PASS** | `test_a_failing_transcription_fails_the_job_with_a_stable_code` → `transcription_failed` |
| 30 | An interrupted extraction does not leave a falsely completed job | **PASS** | `test_an_interrupted_extraction_does_not_complete_the_job` |
| 31 | Two workers cannot process the same job | **PASS** | `test_two_workers_cannot_process_the_same_job` |
| 32 | The API stays responsive while the transcription runs | **PASS** | `test_the_api_stays_responsive_while_the_transcription_runs` |
| 33 | A graceful shutdown does not corrupt the database | **PASS** | `test_the_worker_process_leaves_the_database_readable` — the worker is stopped and `PRAGMA integrity_check` still says `ok` |
| 34 | The transcript endpoint answers for every state | **PASS** | `tests/test_transcript_api.py` — 19 tests |
| 35 | The endpoint never leaks a path | **PASS** | `test_the_response_never_leaks_a_path`, `test_the_response_never_leaks_a_model_cache_path` |
| 36 | The identifiers and the pagination are validated | **PASS** | `test_a_malformed_identifier_returns_400`, `test_an_invalid_page_size_returns_422`, `test_a_negative_offset_returns_422` |
| 37 | The frontend shows the real transcription status | **PASS** | §9 and the browser session in §6 |
| 38 | The frontend shows the timestamped transcript | **PASS** | the browser session in §6 |
| 39 | The frontend polls and stops appropriately | **PASS** | `test_polls_while_the_job_is_still_running`, `test_stops_polling_once_the_job_is_finished` |
| 40 | The backend tests pass | **PASS** | 346 passed, 1 skipped |
| 41 | The frontend typecheck, lint, tests and build pass | **PASS** | all exit 0; 38 tests |
| 42 | The integration check runs where the environment permits | **PASS** | §6 — a real upload, a real transcription with the real model, a real browser session |
| 43 | The documentation reflects the implementation | **PASS** | every command in the README and in `docs/DEVELOPMENT.md` was executed during this phase; the settings table matches `backend/app/config.py` field for field |
| 44 | `PHASE_3_REPORT.md` records genuine test evidence | **PASS** | this document — every result is transcribed from a real run |
| 45 | No secrets are exposed or committed | **PASS** | there is no transcription credential at all; the only `NEXT_PUBLIC_*` variable is the API base URL |
| 46 | No unimplemented functionality is represented as complete | **PASS** | `/ready` still reports the later capabilities as `false`; the pipeline preview marks *Find highlights* as *Next up*; the job badge does not claim a transcript that was never stored |

**Phase 3 status: COMPLETE — 46 of 46 criteria verified with executed
evidence.**

---

## 11. Security checks performed

- The speech model runs **entirely on this machine**. There is no
  transcription API key, and no audio or transcript is ever sent anywhere.
- With `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` the engine does not touch the
  network for the model either, and a regression test proves the model is
  never even constructed when the cache is empty.
- FFmpeg is always started with an argument list, never a shell. A filename
  containing shell metacharacters is only ever a filename.
- FFmpeg's stderr is captured to a file, truncated to 2000 bytes, logged on
  the server, and never returned by an API.
- The transcript endpoint returns no filesystem path, no model cache location
  and no raw subprocess output.
- The temporary audio is deleted as soon as the transcript is stored, and it
  is written to a per-job directory inside the storage root.
- The API binds `127.0.0.1` and now **refuses** any other address unless
  `CLIPFORGE_ALLOW_LAN_BINDING=true` is set explicitly.
- The migration copies every job row; nothing is dropped.
- No `NEXT_PUBLIC_*` variable was added, and no secret was committed.

---

## 12. The Phase 2 carry-over items

| Item | Status |
| --- | --- |
| 9.1 — The attestation wording must not claim verification | **Verified, no change needed.** The frontend asks *"I own this video or have permission to download and reuse it"* and the API refuses a submission without it with `unauthorized_source`. Neither side ever says the source is "verified", and neither can: the backend cannot verify the claim and does not pretend to. |
| 9.2 — The API must default to a local-only binding | **Done.** `CLIPFORGE_HOST` is still `127.0.0.1`, and a non-loopback address is now **refused** when the settings load unless `CLIPFORGE_ALLOW_LAN_BINDING=true` is set. Four test functions cover it, including the explicit opt-in. |
| 9.3 — The worker should be tested at the process level | **Done.** `tests/test_worker_process.py` starts `python worker.py` as a real, separate process with its own environment, and checks that it claims a job, that it takes over a job left behind by a dead worker, and that the database is intact when it is stopped. |
| 9.4 — The filesystem and the database must stay consistent | **Done.** `tests/test_storage_consistency.py` injects a failure at each of the three boundaries — the inspection, the move and the job record — and proves that neither an orphaned file nor a job row is left behind. The upload endpoint was also changed so that an *unexpected* error during the inspection removes the file, not only a `MediaError`. |
| 9.5 — The downloader must not become a generic fetch proxy | **Unchanged and still enforced.** The validator performs no network access at all and only a canonical YouTube URL is ever handed to the adapter. The residual risk is documented in `docs/ARCHITECTURE.md` §9.3. |
| 9.6 — Node LTS and `npm audit` | **Partly blocked, recorded.** `npm audit` reports the same five high-severity advisories in the dev-only ESLint chain (`braces` → `micromatch` → `fast-glob`). There is **no patched `braces` release** — 3.0.3 is the latest — and npm's only offered fix is downgrading `eslint-config-next` to v14, which breaks the Next.js 16 lint setup. **Accepted, not remediated**, unchanged from Phase 1 and Phase 2. The Node runtime available on this machine is **26.4.0**; no LTS release is installed here, so the LTS parity check could not be run. |

---

## 13. Known issues and limitations

1. **Transcription accuracy depends on the model.** `tiny.en` is fast and
   small but noticeably worse than `base.en` or `small.en`. The default is
   `tiny.en` because it makes the first run cheap; a real installation should
   raise `CLIPFORGE_TRANSCRIPTION_MODEL`.
2. **The transcription timeout is checked between decoded segments**, not in
   hard real time, so one very slow segment can overshoot it.
3. **Transcription runs on the CPU.** A long recording takes minutes. The
   `cuda` device works if the machine has a GPU, but there is no UI for it.
4. **The first run needs a network connection** to fetch the model, unless it
   was cached beforehand or `CLIPFORGE_TRANSCRIPTION_OFFLINE=true` is set.
5. **The `completed` meaning changed.** A job finished under Phase 2 has no
   transcript. The API reports that through `has_transcript` and the badge
   says *Completed*, but any client that infers "transcribed" from the status
   alone would be wrong.
6. **`npm audit` still reports five dev-only advisories** with no patched
   upstream release (§12).
7. **No end-to-end browser automation.** The integration check in §6 was done
   by hand in a real browser. Automating it is scheduled for Phase 9.
8. **The worker process test is timing-sensitive** on a slow machine: it
   waits for the worker to claim a job.

---

## 14. Next step

**Phase 4 — AI highlight detection**: send the stored transcript to an
external LLM through a provider abstraction, return candidate moments with a
start time, an end time, a title and a reason, and let the user accept or
reject them. The API credentials stay in the backend, in `CLIPFORGE_LLM_*`,
and no provider SDK is committed to until the provider and its endpoint
compatibility are confirmed.

---

## 15. Reproducing the live check

```powershell
# 1. Fetch the speech model (once)
cd clipforge\backend
..\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8', download_root='data/models'); print('cached')"

# 2. Start the API and the worker, in two terminals
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
..\.venv\Scripts\python.exe worker.py

# 3. Upload a video and read the transcript
$job = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/videos/upload -Method Post -Form @{ file = Get-Item .\sample.mp4 }
Start-Sleep -Seconds 10
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/jobs/$($job.id)/transcript"
```

---

*Every result in this document was transcribed from a command that was
actually run on 20 October 2026. Where a check could not be run, it says so
rather than claiming a pass.*
