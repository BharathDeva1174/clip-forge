# ClipForge — Phase 6 report: vertical formatting and captions

**Status:** complete
**Final verdict:** **READY WITH DOCUMENTED LIMITATIONS**
**Date:** 21 October 2026
**HEAD at the time of writing:** `7502384d4b0ef1aeff3d568bd2db7ed790864378` — unchanged, nothing committed, nothing staged

---

## 1. Summary

### What was implemented

A **formatting pipeline** that turns a finished Phase 5 clip into a
portrait, captioned, standalone MP4 that can be uploaded to a short-form
platform as it is.

The user opens a completed clip, presses **Make a short-form version**,
picks a resolution and a layout, turns the captions on or off, adjusts how
they look, and asks for it. The render happens in the worker; the interface
follows it through its stages, shows the finished video, and offers it for
download.

Specifically:

| Capability | Delivered |
| --- | --- |
| Portrait 9:16 output | 720 × 1280 and 1080 × 1920, both enforced to be 9:16 |
| Two layout modes | `center_crop` (fill and cut) and `fit_pad` (show it all, pad the rest) |
| Aspect ratio preserved | `force_original_aspect_ratio` — neither mode can stretch |
| Burned-in captions | Generated from the persisted transcript; the font, size, colours, outline, shadow, position and margins are configurable |
| Clip-relative timing | The overlap of the segment with the clip, converted onto the clip's own timeline |
| Boundary handling | A segment that crosses either boundary contributes only the part that is inside |
| Validated settings | Every dimension, colour, number and font name is refused if it cannot be used |
| A separate artifact | Its own table, its own file, its own lifecycle, its own lease |
| Retries | Only a failed artifact can be rendered again, with the settings it was created with |
| Preview and download | A controlled media route that resolves inside the storage root |
| Independent verification | FFprobe reads the output back and checks it against the configuration |
| Real rendering | The real FFmpeg, the real FFprobe, the real worker |
| A frontend workflow | The `FormatPanel` component, wired into the clip panel |
| Recovery | Leases, ownership checks, expired-lease recovery, orphan cleanup |

### What a user can now do

1. Cut a clip from a video (Phase 5).
2. Open the finished clip and press **Make a short-form version**.
3. Choose **720 × 1280** or **1080 × 1920**.
4. Choose **centre crop** (fill the screen, cut the sides) or **fit and
   pad** (show the whole frame, fill the rest with a colour).
5. Turn the captions on, pick a font from the ones installed on the
   machine, and adjust the size, the colours, the outline and the position.
6. Press **Render the short-form version** and watch it go through
   *queued → rendering → verifying → completed*.
7. Play the result and download the MP4.
8. Go back to the landscape clip, which has not changed.

### What was deliberately left out of scope

Everything the brief placed out of scope, and nothing else:

- No face detection, no face tracking, no active-speaker framing, no
  automatic reframing.
- No word-level karaoke animation — the transcript is segment-level, and
  the system does not pretend otherwise.
- No timeline editor, no multiple tracks, no transitions, no effects.
- No publishing to any platform.
- No cloud processing.
- No new LLM-provider integration and no model benchmarking.
- No new dependency. `backend/requirements.txt` is **byte-for-byte
  unchanged by this phase** — its last modification is from the Phase 3/4
  work that was already uncommitted, and the caption rendering uses the
  `subtitles` filter that is already compiled into the FFmpeg the
  application depends on.

### The one thing worth knowing up front

**Formatting makes no AI request at all.** The captions come from the
transcript that is already in the database, and the transcription model is
not called again. The formatting path is deterministic media processing.

---

## 2. The architecture

### 2.1 New files

| File | What it is |
| --- | --- |
| `backend/app/formatting.py` | The formatting settings: validation, normalisation, the fingerprint |
| `backend/app/captions.py` | The caption timeline, the text cleaning, the wrapping, the SRT document, the filter-path escaping, the colour conversions |
| `backend/app/format_render.py` | The FFmpeg command, the bounded run, and the independent verification |
| `backend/app/format_service.py` | The whole job: resolve, probe, caption, render, verify, finalise, commit |
| `backend/app/formats.py` | The formatted-clip domain model |
| `backend/app/fonts.py` | The fonts this machine has, read out of the font files |
| `backend/app/routers/formats.py` | The five endpoints and the font list |
| `backend/tests/test_formatting.py` | 87 tests |
| `backend/tests/test_format_render.py` | 23 tests, with the real FFmpeg |
| `backend/tests/test_format_worker.py` | 14 tests |
| `backend/tests/test_formats_api.py` | 29 tests |
| `backend/tests/e2e_phase6.py` | The end-to-end workflow check |
| `frontend/src/components/FormatPanel.tsx` | The formatting controls |
| `frontend/src/components/FormatPanel.test.tsx` | 15 tests |

### 2.2 Modified files

| File | What changed |
| --- | --- |
| `backend/app/db.py` | Migration 6, the `clip_formats` table, and the artifact repository |
| `backend/app/worker.py` | `run_formats_once()`, the format runner, the lease recovery, the reconciliation |
| `backend/app/config.py` | Three new settings |
| `backend/app/errors.py` | Twelve new `FORMAT_*` codes |
| `backend/app/storage.py` | The `formats/` directory, `new_format_path()`, `new_format_work_dir()` |
| `backend/app/schemas.py` | The request and response models |
| `backend/app/main.py` | The router registration |
| `frontend/src/lib/api.ts` | The format client, and the list responses hardened so a surprising answer cannot crash the interface |
| `frontend/src/components/ClipPanel.tsx` | The `FormatPanel` mounted under a finished clip |
| `backend/tests/test_clips.py` | The schema-version assertion now follows `SCHEMA_VERSION` |
| `README.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/ROADMAP.md`, `docs/TESTING.md`, `backend/.env.example` | The documentation |

### 2.3 The schema

Migration 6 adds one table:

```sql
CREATE TABLE IF NOT EXISTS clip_formats (
    id                       TEXT PRIMARY KEY,
    clip_id                  TEXT NOT NULL REFERENCES clips (id) ON DELETE CASCADE,
    job_id                   TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    config_fingerprint       TEXT NOT NULL,
    settings_json            TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN (...)),
    stage                    TEXT NOT NULL CHECK (stage IN (...)),
    progress                 INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    output_path              TEXT,
    output_size_bytes        INTEGER,
    actual_duration_seconds  REAL,
    width                    INTEGER, height INTEGER,
    video_codec              TEXT, audio_codec TEXT, pixel_format TEXT,
    captions_enabled         INTEGER NOT NULL DEFAULT 1 CHECK (captions_enabled IN (0, 1)),
    captions_applied         INTEGER NOT NULL DEFAULT 0 CHECK (captions_applied IN (0, 1)),
    caption_count            INTEGER NOT NULL DEFAULT 0,
    attempt_count            INTEGER NOT NULL DEFAULT 0,
    worker_id                TEXT, lease_expires_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    started_at TEXT, completed_at TEXT,
    error_code TEXT, error_message TEXT,
    UNIQUE (clip_id, config_fingerprint)
);
```

With four indexes: on `(clip_id, created_at)`, on `(job_id, created_at)`,
on `(status, created_at)` and on `lease_expires_at`.

`SCHEMA_VERSION` moved from 5 to **6**.

**The relationships.** A formatted clip points at the Phase 5 clip it came
from, and at the job, so the interface can group them and the ownership is
unambiguous. The Phase 5 clip table is **not** touched: its lifecycle, its
semantics and its rows are exactly as they were.

### 2.4 The lifecycle

The formatted clip uses **the same state machine as the clip**, deliberately.
Two nearly-identical state machines would be a trap. The statuses and the
transition table come from `app.clips` and are re-exported under
context-appropriate names from `app.formats`.

```
queued → preparing → rendering → verifying → completed
                  ↘         ↘          ↘
                          failed
```

- `completed` is reachable **only** from `verifying`.
- `failed` is reachable from any non-terminal status.
- A failed artifact can be put back on the queue by an explicit retry.
- The Phase 5 clip's state machine is untouched.

### 2.5 The idempotency design

The settings are normalised and hashed; the hash is the
`config_fingerprint`. One clip with one fingerprint is one artifact,
enforced by the unique index.

- The **same** settings → the same artifact. A repeated request, an HTTP
  retry or a double-click returns the artifact that already exists (HTTP
  200) instead of rendering twice (HTTP 202 for a new one).
- A **different** setting → a **different** artifact. Changing the
  resolution or the caption style never replaces a finished result; the old
  one is kept, and both are listed.

The normalisation matters: without it the same settings written in a
different order would hash differently, and the idempotency would leak.

The settings are also stored as an **immutable snapshot**
(`settings_json`), so a retry renders what the user asked for even if the
defaults have since changed.

### 2.6 The worker

`Worker.run_formats_once()` claims the oldest queued artifact, renders it,
and returns whether there was anything to do. It is called from the same
loop as the other work, **after** the job, the transcript and the analysis
have been offered their turn, so formatting never starves them.

The claim is a `BEGIN IMMEDIATE` transaction that selects the oldest
`queued` row and moves it to `preparing` with the worker's id and a lease
in the same transaction. Every subsequent write is ownership-checked: a
worker that has lost the artifact to a newer attempt finds that its update
returns `None` and it does not touch the output file.

### 2.7 The rendering pipeline

```
resolve the Phase 5 clip ─► probe it (duration, dimensions, audio)
        │
        ▼
build the caption events from the persisted transcript
        │
        ▼
write the SRT into temp/format-<id>-<attempt>/
        │
        ▼
run FFmpeg with that directory as its working directory
        │
        ▼
verify the temporary file with FFprobe
        │
        ▼
os.replace() into formats/<id>.mp4
        │
        ▼
commit the completed row, with the ownership check
```

The **measured** duration of the source clip is what the captions are
constrained to, and what the output's duration is checked against — not the
duration that was asked for.

---

## 3. The caption correctness

This is the part of Phase 6 that is easy to get quietly wrong, so it was
spelled out carefully.

### 3.1 The two timelines

Confirmed in the actual schema before anything was written:

- `transcript_segments.start_seconds` / `end_seconds` are on the **source
  media timeline** — the transcript is of the source video's audio.
- `clips.start_seconds` / `end_seconds` are copied from the highlight
  candidate, which came from those same segments, so they are on the **same
  timeline**.
- The caption file has to be on the **clip's** timeline, which starts at
  zero when the clip begins.

So the conversion is the overlap:

```
overlap_start  = max(segment_start, clip_start)
overlap_end    = min(segment_end,   clip_end)

if overlap_end <= overlap_start:  the segment contributes nothing

relative_start = overlap_start - clip_start
relative_end   = overlap_end   - clip_start
```

### 3.2 The rules, in the order they are applied

1. The overlap with the clip is computed.
2. A segment with no overlap contributes nothing.
3. The overlap is converted onto the clip's timeline.
4. The result is constrained to the clip's **verified playable duration** —
   the measured length of the rendered file.
5. The times are rounded to whole milliseconds, and an event that rounds to
   nothing is dropped.
6. Empty or whitespace-only text contributes nothing.

Every one of these has a test:

| Case | Expected |
| --- | --- |
| A segment inside the clip | Its relative time is kept |
| A segment starting before the clip | Clipped to zero |
| A segment ending after the clip | Clipped to the duration |
| A segment crossing both boundaries | Clipped to the whole clip |
| A segment entirely before the clip | Nothing |
| A segment entirely after the clip | Nothing |
| A segment with zero duration | Nothing |
| A segment with reversed timestamps | Nothing |
| A segment with a NaN timestamp | Nothing |
| A segment with an infinite timestamp | Nothing |
| Empty or whitespace-only text | Nothing |
| A clip with no transcript | Nothing |
| Captions disabled | Nothing |
| A clip measured shorter than requested | The captions stop at the measured length |

### 3.3 The rounding policy

The subtitle format has millisecond resolution, so the times are rounded to
whole milliseconds with `round()` — to the nearest, not down.

The order matters, and it is **round first, then clamp**:

1. `relative_start` and `relative_end` are computed as seconds.
2. They are rounded to milliseconds.
3. They are clamped into `[0, duration_ms]`, where `duration_ms` is the
   measured playable duration.
4. An event whose end is not strictly after its start is dropped.

Rounding first and clamping second means the clamping can never *extend* an
event — it can only shorten it. Clamping first and rounding second could
round an event that ends exactly at the duration up to a millisecond
*past* it, which is the failure the brief warns about.

### 3.4 The subtitle format

**SRT**, not ASS.

SRT has no markup at all: the text is text. The transcript is the user's
speech, and it lands in a file that a renderer parses. ASS would give
per-caption styling, but it would also give the text a syntax — `{...}`
override blocks, `\N` escapes — and the text would then have to be escaped
against it, which is a way of corrupting legitimate speech to prevent an
injection. **SRT removes the surface instead of defending it.**

The styling is render-wide anyway: the font, the size, the colours, the
outline, the shadow, the position and the margins are all choices the user
makes once. libass's `force_style` option expresses exactly that, and its
value is built from validated settings.

The two things SRT cannot express are handled at **generation** time, where
the application has full control:

- **The maximum characters per line** — the text is wrapped to that width.
- **The maximum lines** — the text is cut at that many lines, and the cut
  is marked with an ellipsis so it is not mistaken for the whole sentence.

Both are tested, and both are applied by the application, not by libass.

### 3.5 The text handling

| Concern | What is done |
| --- | --- |
| Control characters | Removed (`\x00`–`\x08`, `\x0b`, `\x0c`, `\x0e`–`\x1f`, `\x7f`) |
| Whitespace | Runs are collapsed to a single space; the text is trimmed |
| Unicode | Preserved as-is, including punctuation (`—`, `¿`, `…`) and non-Latin scripts |
| A very long caption | Capped at 2000 characters, with a mark |
| The number of events | Capped at 2000 per clip |
| The file size | Capped at 2 MB; a bigger document is refused rather than written |

A caption containing `{`, `}`, a comma, a backslash or `\N` is **text**. It
cannot become a styling tag, an extra field or a command, because SRT has
no syntax to inject into. A test asserts that a hostile caption leaves the
document with exactly one event.

### 3.6 What is not claimed

**The captions are segment-level, not word-level.** The transcript stores
one timestamp range per segment, and that is what is used. The system does
not invent word timings and does not pretend to have them. The report says
so, the interface says so, and nothing in the code claims otherwise.

### 3.7 The missing-transcript case

A clip with no transcript, or with no transcript text overlapping it,
renders **successfully without captions** — and the artifact says so.

`captions_enabled` (what was asked for) and `captions_applied` (what was
actually burned in) are **two separate fields**. When they disagree, the
interface says: *"No caption text overlapped this clip, so it was rendered
without captions."* Nothing silently claims captions that are not there.

---

## 4. The subtitle-file safety

### 4.1 What FFmpeg actually does — measured, not assumed

Before any of this was designed, the real FFmpeg was asked. The candidates
were rendered against the real binary and the frames were compared:

| Form | Result |
| --- | --- |
| `subtitles=filename='C\:/path/file.srt'` | **Works** — the frames differ from the uncaptioned render |
| `subtitles='C\:/path/file.srt'` | Works |
| `subtitles=filename=C\\:/path/file.srt` (unquoted) | **Fails** — `No option name near '...'` |
| `subtitles=filename='C:\path\file.srt'` (raw) | **Fails** — the colon is read as an option separator |

Then the hostile paths, each rendered for real:

| The path contains | Result |
| --- | --- |
| A space | Works |
| A comma | Works |
| Square brackets | Works |
| An ampersand | Works |
| A semicolon | Works |
| Unicode | Works |
| An equals sign | Works |
| A dollar sign | Works |
| **A single quote** | **Fails, and cannot be made to work** |

The single quote is the important one. FFmpeg's subtitle filter opens the
file with the name it parsed, and the quote is simply **not there any more**,
whichever escaping is used — `\'` and the `'\''` continuation idiom were
both tried, and both produced a filter that pointed at `withquote.srt`.

### 4.2 The design that follows from that

The subtitle file is written into a directory the application controls, and
**FFmpeg is run with that directory as its working directory**. The filter
therefore refers to the file by a plain, server-generated name
(`captions.srt`), and:

- the directory — the part the user's machine controls, and the part that
  might contain a quote, a comma or a space — **never reaches the filter**;
- the file name is chosen by the application and contains only `[a-z0-9-.]`.

The escaping helper `escape_filter_path()` still exists, is still the thing
that builds the filter value, and is still tested against every hostile
case above. It **refuses** a path it cannot express rather than producing a
filter that silently points at the wrong file. That refusal is the reason
the working-directory arrangement exists.

### 4.3 The other rules

- The file is written as **UTF-8**, which is what libass expects.
- The name is **attempt-specific**: `temp/format-<id>-<attempt>/captions.srt`.
  Two attempts never share one.
- The file is written by the application. **No user-supplied path or
  filename ever reaches a filter.**
- The file is deleted when the attempt ends, successfully or not.

---

## 5. The rendering

### 5.1 The filters

**`center_crop`:**

```
scale=W:H:force_original_aspect_ratio=increase,crop=W:H
```

`increase` scales the frame to **cover** the canvas; `crop` then takes the
excess off, centred.

**`fit_pad`:**

```
scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2:color=0xRRGGBB
```

`decrease` scales the frame to **fit inside** the canvas; `pad` fills the
rest, centred, with the configured colour.

`force_original_aspect_ratio` is what makes stretching impossible — the
scale is computed from the source's aspect ratio, never forced. The
explicit `W:H` at the end of both chains is what makes the output exactly
the configured resolution.

Rotation metadata needs no special handling: FFmpeg's autorotation applies
the rotation at the decoder level, so the filter chain sees the **displayed**
dimensions and the aspect ratio is the one the viewer sees.

### 5.2 The command

```
ffmpeg -nostdin -hide_banner -loglevel error -y
       -i <source>
       -vf "<the chain>"
       -map 0:v:0 [-map 0:a:0?]
       -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p
       [-c:a aac -b:a 128k]
       -movflags +faststart -f mp4 <output>
```

- An **argument array**, never a shell string. `shell=False`.
- The audio map is optional (`0:a:0?`), so a source with no sound formats
  successfully, and one with sound keeps it.
- `-pix_fmt yuv420p` and `libx264` — what phones and browsers expect.
- `+faststart` — the moov atom is moved to the front, so the file plays
  without downloading the whole thing.
- **No stream copy.** A transformation needs a re-encode; a stream copy
  would cut on keyframes and break the boundary guarantee.

### 5.3 The bounded run

`render_format()`:

- The child is started with `stdin=DEVNULL`, `stdout=DEVNULL` and
  **stderr to a temporary file** — on Windows an anonymous pipe holds about
  4 KB, and a child that fills it blocks, which looks exactly like a hang.
- The timeout is enforced; on expiry the child is **terminated**, then
  **killed**, then **reaped**.
- At most 2000 bytes of diagnostics are kept.
- A missing FFmpeg is reported as `media_tool_unavailable`, not as a
  crash.
- The child's working directory is the attempt's directory.

### 5.4 The verification

FFmpeg exiting with zero is **not** evidence. `verify_format_output()`
reads the file back with FFprobe and checks:

| # | Check | Failure code |
| --- | --- | --- |
| 1 | The file exists and is a regular file | `format_output_invalid` |
| 2 | It resolves inside the output directory | `format_output_invalid` |
| 3 | It is larger than 1024 bytes | `format_output_invalid` |
| 4 | FFprobe can parse it | `format_verification_failed` |
| 5 | There is a video stream | `format_output_invalid` |
| 6 | The dimensions are **exactly** the configured ones | `format_output_invalid` |
| 7 | The duration is finite and positive | `format_output_invalid` |
| 8 | The duration is within the tolerance of the clip's measured duration | `format_duration_mismatch` |
| 9 | The video codec is H.264 | `format_output_invalid` |
| 10 | The pixel format is yuv420p | `format_output_invalid` |
| 11 | The audio presence matches the source | `format_output_invalid` |
| 12 | The audio codec is AAC | `format_output_invalid` |
| 13 | The container is a valid MP4 (implied by the above and by the probe) | — |

The tolerance is `format_duration_tolerance_seconds`, default **0.5 s**.

### 5.5 Was the caption really burned in?

**This is proved at the media level, not asserted.**

The same input is rendered **twice** — once with the subtitles filter and
once without — and the MD5 of every frame is compared. If the captions were
not burned in, the frames would be identical.

- `test_the_captions_are_actually_burned_in` — the captioned render must
  differ from the uncaptioned one.
- `test_a_caption_outside_the_clip_is_not_burned_in` — a caption whose time
  falls outside the video must leave the two renders **identical**. This is
  the control: it proves the difference is the caption and not the encode.
- `e2e_phase6.py` does the same against the bytes the **API served**,
  comparing them with the frames of the uncaptioned Phase 5 clip.

In the end-to-end run, **105 frames differed** from the uncaptioned clip.

The production verifier records `caption_count` and `captions_applied` on
the artifact, and checks that the subtitle filter was in the command it
ran — but it does **not** re-render to compare. That is a deliberate
trade-off, recorded in §11.

---

## 6. The API

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/v1/clips/{clip_id}/formats` | Queues a formatted clip, or returns the existing one for the same settings |
| `GET` | `/api/v1/clips/{clip_id}/formats` | The formatted clips of a clip, paginated |
| `GET` | `/api/v1/formats/{format_id}` | One formatted clip: its state, its settings and its measurements |
| `POST` | `/api/v1/formats/{format_id}/retry` | Puts a **failed** artifact back on the queue |
| `GET` | `/api/v1/formats/{format_id}/media` | Streams the verified file |
| `GET` | `/api/v1/fonts` | The fonts this machine has |

### 6.1 The status codes

| Situation | Status |
| --- | --- |
| A new artifact was queued | **202** |
| The artifact for these settings already exists | **200** |
| An unknown clip | 404 |
| The clip has not been rendered | 409 |
| An unknown setting | 400 (`format_invalid_settings`) |
| An unknown enum value | 422 (the request validator) |
| An unknown artifact | 404 |
| Retrying something that has not failed | 409 |
| The media of an artifact with no file | 404 |

### 6.2 The safety

- The request carries **the settings and nothing else**. The boundaries
  come from the clip, which came from the candidate, which came from the
  transcript. A test posts `start_seconds` and `end_seconds` and asserts
  they are ignored.
- The media route resolves the stored path **inside the storage root**. A
  stored path of `../outside.mp4` serves a 404; a test proves it.
- The response **never contains a filesystem path**. A test asserts that
  neither `output_path` nor a Windows drive letter nor `/home/` appears in
  the body.
- The error messages are fixed strings with stable codes. A test asserts
  that a failure message contains no `C:\`.
- The pagination is bounded: `limit` between 1 and 100, `offset` at least 0.
- The existing envelope, the CORS posture and the local-only protections
  are unchanged.

---

## 7. The frontend

`FormatPanel` is mounted under a finished clip in `ClipPanel`. The original
clip's preview and download are untouched and remain available.

### 7.1 What it does

| Requirement | How |
| --- | --- |
| Choose the resolution | A select with 720 × 1280 and 1080 × 1920 |
| Choose the layout | A select explaining both modes in words |
| Enable or disable the captions | A checkbox; the styling controls disappear when it is off |
| Adjust the caption styling | The font, the size, the text colour, the outline colour, the outline thickness and the position |
| Choose the padding colour | Shown only in *fit and pad* |
| See the configuration | The summary under the button |
| Submit | **Render the short-form version** |
| See the lifecycle | The stage is shown, and the panel polls while it is working |
| Understand a failure | The message and the stable code, and a **Render again** button |
| Preview | A `<video>` element pointed at the media route |
| Download | A download link pointed at the same route |

### 7.2 The UI rules that were followed

- **Duplicate submissions are prevented.** The button is disabled and reads
  *"Requesting…"* while the request is in flight. A test asserts that at
  most one POST is sent.
- **Polling is bounded and stops.** It only runs while an artifact is in a
  working stage, and the interval is cleared when the component unmounts. A
  test unmounts the component, advances the clock by nine seconds and
  asserts that **no further request was made**.
- **No invented progress.** The interface shows the *stage*, not a
  percentage. The backend's `progress` column exists and is bounded, but
  the interface does not pretend to know more than the stage.
- **The artifact id and the error context survive.** A failed request
  leaves the artifact's id, its code and its message on screen.
- **The accessible names are clean.** Every control has an explicit
  `htmlFor`/`id` pair, so the label is the name and the help text does not
  become part of it.
- **The fonts are the ones installed.** The list comes from
  `GET /api/v1/fonts`, which reads the family names out of the font files
  themselves.

### 7.3 The frontend tests

15 tests, covering the default settings, changing the resolution and the
layout, turning the captions on and off, the styling controls, the font
list, the request, the duplicate-submission guard, the queued stage, the
finished artifact with its preview and download, **the case where a clip was
rendered without captions**, the failure with its stable code, the retry, an
error from the server, **that an answer that is not a list does not take the
panel down**, and that polling stops on unmount.

### 7.4 A bug the tests found

The full suite failed **once**, and then passed. A flake that appears once
and then goes away is a bug that has not been fixed, so it was chased down
rather than re-run until it was green.

The cause: `FormatPanel` fetches its list **on mount**, and the API client
returned `payload.items` without checking that it was there. The clip
panel's existing test answers *every* GET with a clip payload, so
`listFormats` received an object with no `items` and returned `undefined`,
and the panel then called `.some()` on it. Whether the test failed depended
on whether the fetch resolved before the test finished — a race.

The fix is at the boundary, where it belongs: the client now returns an
empty list for anything that is not a list, so **a surprising response
cannot crash the interface** — for the clips, the formatted clips and the
fonts alike. A regression test was added for it, and the suite was then run
**five times in a row** to confirm the race is gone.

This is the same bug class the Phase 5 report recorded for the clip panel.
It is fixed once, in the shared place, rather than twice in two components.

---

## 8. The configuration

Three new settings, all with the existing conventions:

| Setting | Default | Meaning |
| --- | --- | --- |
| `CLIPFORGE_FORMAT_FFMPEG_TIMEOUT_SECONDS` | `600` | How long one formatting render may run |
| `CLIPFORGE_FORMAT_DURATION_TOLERANCE_SECONDS` | `0.5` | How far the measured length may differ from the clip's |
| `CLIPFORGE_FORMAT_ENCODER_PRESET` | `veryfast` | The FFmpeg encoder preset |

The formatting **profile** is per-request, not global: the resolution, the
layout, the padding colour and the caption style are chosen by the user and
stored on the artifact.

### 8.1 The validation

| Field | Accepted | Refused |
| --- | --- | --- |
| `width`, `height` | A positive even integer, 64–2160 × 64–3840, and **9:16 within 0.5 %** | Zero, negative, odd, landscape, square, oversized |
| `layout` | `center_crop`, `fit_pad` | Anything else |
| `padding_colour` | `#RRGGBB` | A name, a short hex, `0x…`, an empty string |
| `font_family` | Letters, digits, spaces, hyphens, underscores and dots | A comma, a quote, a semicolon, a bracket, control characters |
| `font_size` | 8–200 | Negative, zero, 500, a fraction, a string, a boolean |
| `outline_thickness`, `shadow` | 0–10 | Anything outside |
| `alignment` | 1–3 (bottom, middle, top) | Anything outside |
| `margin_vertical` | 0–4000 | Anything outside |
| `max_characters_per_line` | 8–100 | Anything outside |
| `max_lines` | 1–10 | Anything outside |

A boolean is **not** accepted where a number is expected — `True` is not
`1` here. Non-finite values cannot arrive: the values are integers, and
the one float-ish path (a colour) is a pattern.

No raw FFmpeg filter expression is ever accepted from a client. The
interface never shows one.

---

## 9. The fonts

The captions need a font, and not every machine has the same one. The
fonts are **detected**: the TrueType and OpenType files in the system font
directories are read, and the family name is taken from each file's own
`name` table.

That is a small amount of binary parsing — about 80 lines of `struct` — and
it is worth it: a name taken from the file's metadata is the name libass
will actually resolve.

On this machine it finds **195 families**, including Arial, Segoe UI and
Verdana. (It found 281 before a bug was fixed: the string offset in a
`name` table is relative to the table, not to the file, and the first
version read it from the wrong place and produced garbage. There is now a
test that the detected name for a known font is the right one.)

Only the fonts that could **actually be used** are offered: the caption
style is a comma-separated list of libass options, so a name with a comma in
it cannot be expressed, not even inside the quotes. The font list is
filtered by the same validation the settings use, so the interface can
never offer a font that the form would then refuse.

If the font directories cannot be read, the list is empty and the interface
falls back to Arial. Nothing here is allowed to fail the application.

---

## 10. The tests and the evidence

### 10.1 The exact commands

```powershell
# Backend, from clipforge\backend
..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN

# Frontend, from clipforge\frontend
npm test
npm run typecheck
npm run lint
npm run build
npm run audit:check

# The end-to-end workflow checks
..\.venv\Scripts\python.exe tests\e2e_workflow.py
..\.venv\Scripts\python.exe tests\e2e_phase4.py
..\.venv\Scripts\python.exe tests\e2e_phase5.py
..\.venv\Scripts\python.exe tests\e2e_phase6.py
```

### 10.2 The baseline, measured before anything was changed

| Check | Baseline | Result |
| --- | --- | --- |
| Backend | `667 passed, 1 skipped` | exit 0 |
| Frontend | `77 tests` | exit 0 |
| Type check | — | exit 0 |
| Lint | — | exit 0 |
| Production build | — | exit 0 |
| Audit check | 5 `braces` advisories (dev-only ESLint chain) | exit 0 |

The one skip is the privileged-symlink test, which cannot run as this
user. The Phase 5 report recorded 668 passed and 0 skipped; the difference
is that same test, which skips when the privilege is unavailable. It is
not a difference in behaviour.

### 10.3 The final results

| Check | Baseline | Final | Exit |
| --- | --- | --- | --- |
| **Backend** | 667 passed, 1 skipped | **820 passed, 1 skipped** | 0 |
| **Frontend** | 77 tests | **92 tests** | 0 |
| **Type check** | 0 | **0** | 0 |
| **Lint** | 0 | **0** | 0 |
| **Production build** | 0 | **0** | 0 |
| **Audit check** | 5 advisories | **5 advisories** (the same dev-only `braces` ones) | 0 |
| `e2e_workflow.py` | — | **every step passed** | 0 |
| `e2e_phase4.py` | — | **every step passed** | 0 |
| `e2e_phase5.py` | — | **every step passed** | 0 |
| `e2e_phase6.py` | — | **every step passed** | 0 |

### 10.4 The new backend tests

| File | Tests | What it covers |
| --- | --- | --- |
| `test_formatting.py` | 87 | The defaults, the resolutions, every invalid dimension, the layout modes, the colours, the caption settings, the fingerprint, the settings round trip, the whole caption timeline, the text handling, the SRT document, the filter-path escaping |
| `test_format_render.py` | 23 | The filters, the style, the command, **real renders**, both layout modes, a source with no sound, the caption burn-in, the caption outside the video, the Unicode caption, the missing FFmpeg, the timeout, the failed render, and every verification failure |
| `test_format_worker.py` | 14 | The migration, the claim, the stale-owner rejection, the lease recovery, the attempts limit, the retry rules, the reconciliation, the cleanup, and **a real render from a real clip** |
| `test_formats_api.py` | 29 | Queuing, the idempotency, the distinct-configuration rule, the invalid settings, the pagination, the response safety, the retry, the media route, the containment, and the font list |

**153 new backend tests.** 667 + 153 = 820 ✓

### 10.5 The real-FFmpeg tests

`test_format_render.py` uses the real FFmpeg and the real FFprobe. The
fixtures are generated locally with `lavfi` — a landscape video with sound,
and one without — so nothing is downloaded.

Verified against the real output:

- the exact portrait dimensions (720 × 1280);
- the expected duration;
- the correct audio presence, in both directions;
- that FFprobe can parse it;
- **that the captions are burned in**, by comparing frames;
- that a caption outside the video leaves the output unchanged;
- both layout modes;
- Unicode captions.

The end-to-end script adds the real API output on top of that.

### 10.6 The end-to-end check

`backend/tests/e2e_phase6.py` starts the **real API process** and the
**real worker process** and drives the whole chain over HTTP. The only
stand-in is the AI provider, which is a **real local HTTP server** speaking
the Chat Completions dialect — no credential, no external service. The
formatting step itself makes no AI request at all, and that is what the
check demonstrates.

Every step passed:

| Step | Check |
| --- | --- |
| The fixture | The video exists |
| The API | `/health` answers |
| The upload | Accepted, 202 |
| The job | Completed, with a transcript |
| The analysis | Completed, with candidates |
| The clip | Queued, then completed — 640 × 360, 4.5 s |
| The format request | **Returns at once**, 202, `queued` |
| The work | The worker formats it — 720 × 1280, 4.5 s, 163479 bytes, h264/yuv420p |
| The portrait | Exactly 720 × 1280 |
| The duration | Within 0.5 s of the clip |
| The captions | 2 captions, `captions_applied` is true |
| The media route | 200, `video/mp4`, more than 1024 bytes |
| The served bytes | A portrait H.264 video, confirmed with FFprobe |
| **The burn-in** | **105 frames differ** from the uncaptioned clip |
| The second format | A different artifact, 1080 × 1920, `fit_pad`, no captions |
| Both kept | `total` is 2 |
| The original | Still completed, still playable, still 640 wide |
| The failure | A deliberate break fails with `format_source_missing` |
| The retry | 200, back on the queue |
| The worker | Still running |
| The cleanup | No temporary format directory is left |

### 10.7 The regressions

The whole Phase 3/3.1/4/5 surface was re-run: the backend suite, the
frontend suite, the type check, the lint, the production build, the audit
check, and all three earlier end-to-end scripts. Everything passed.

**One test was changed**, and it is worth being explicit about why:
`test_the_migration_adds_the_clip_table` asserted `version == 5` — a
hardcoded number. The schema is now 6, so the assertion was changed to
`version == SCHEMA_VERSION`. The test still asserts what it meant to: that
the schema is at the version the code says it is. **No test was weakened or
removed.**

**One source file outside the new code was changed**: `frontend/src/lib/api.ts`
now returns an empty list for a response that is not a list, instead of
`undefined`. That is the fix for the bug in §7.4, and it hardens the clip
list as well — the same class of crash the Phase 5 report recorded.

The frontend suite was run **five times in a row** after that fix, all 92
tests passing each time, because a single green run does not prove a race
is gone.

### 10.8 The pre-existing flaky test

The Phase 5 report recorded a flaky, network-dependent test:
`test_a_download_that_exceeds_the_limit_is_rejected` in
`backend/tests/test_acquisition.py`. Its current status was **verified, not
assumed**: it passed in this phase's baseline run and in the final run, and
in the three end-to-end scripts. The file has not been touched since HEAD.
It remains network-dependent and can still fail without a network.

### 10.9 The audit check

`npm run audit:check` exits 0. It reports **5 high, 0 critical, 5 total**
advisories, and the script confirms that **all of them are the accepted
`braces` advisory** (GHSA-vfj7-8cjw-p6xm) in the **dev-only** ESLint
chain. This is the same set the Phase 5 report recorded. No dependency was
upgraded, downgraded or replaced to make it pass — the check passes on its
own terms, with the advisory acknowledged.

### 10.10 Browser verification

**No browser automation was performed in this phase.** The environment has
no browser-testing framework, and adding one is out of scope. The UI is
covered by 15 component tests that exercise the real component with a
stubbed `fetch`; the API, the worker and the media are covered by the
end-to-end script.

The formatting panel was **not** manually exercised in a browser during
this phase. This is an honest gap, and it is recorded in §11.

---

## 11. The security and the safety

### 11.1 Subprocesses

- **Argument arrays**, `shell=False`, always.
- No command string is ever assembled from configuration or from the
  transcript.
- The input path is the resolved Phase 5 clip, inside the storage root.
- The output path is inside the attempt's directory.
- The only string that reaches a filter is the **application's own file
  name** and a style built from validated settings.
- The timeout, the terminate/kill/reap sequence and the bounded
  diagnostics are as in Phase 5.

### 11.2 The subtitle injection surface

- **SRT has no markup**, so there is nothing to inject into.
- The text cannot create an extra event: the whitespace is collapsed, so a
  blank line cannot appear inside a caption, and the event count is
  bounded.
- The file is written by the application, with a server-generated name, in
  a server-controlled directory.
- The file size and the event count are bounded.
- The style string is built from validated values; a font name with a
  comma, a quote or a bracket is refused at validation time.

### 11.3 Paths

- The source is resolved with the existing containment check.
- The output path is built from the artifact's own id.
- The stored output path is **relative to the storage root**, and the media
  route resolves it with the same containment check.
- The API never returns a filesystem path.

### 11.4 The worker

- The claim is transactional.
- Every write is ownership-checked, and a stale worker's update returns
  `None` and does not touch the file.
- The retry is explicit and only valid for a failed artifact.
- The attempts are bounded, and an artifact that runs out of attempts is
  failed rather than retried for ever.
- The temporary cleanup is **narrow**: only directories that match the
  formatting naming pattern, that are older than the age limit, and that
  live inside the scratch directory. A live attempt's directory is too
  young to be removed, and a test proves that a fresh directory and a
  finished clip are both left alone.

### 11.5 The crash-recovery policy

The filesystem and SQLite cannot be committed atomically together. The
policy:

| Moment | What happens |
| --- | --- |
| The render is running | The artifact is owned by the worker, with a lease |
| The worker dies | The lease expires; the recovery requeues it (or fails it, if it is out of attempts) |
| The file is promoted, the commit does not happen | The file is an orphan in `formats/`. The next attempt re-renders and replaces it with the same name (`<artifact id>.mp4`), so the orphan is overwritten rather than accumulated. |
| The file is promoted and the commit succeeds, but the worker is stale | `update_format` returns `None`; the worker **deletes** the file it promoted, because it belongs to that attempt |

The Phase 5 caveat — an orphaned final file after a crash between the
replacement and the commit — is **not** reproduced without a recovery plan:
the file name is derived from the artifact id, so a retry converges on the
same path. This is documented rather than solved by a sweeping cleanup,
which would risk deleting a live attempt's output.

### 11.6 The local-only posture

Unchanged. No cloud processing, no external upload, no new network access
in the formatting path, no change to the credential store and no change to
the provider integrations.

---

## 12. The performance and the resources

- No video file is ever loaded into Python's memory. The work is
  subprocesses and files.
- The output dimensions are bounded (2160 × 3840 at the very most, and the
  practical presets are 720 × 1280 and 1080 × 1920).
- The subtitle file is bounded at 2 MB and 2000 events.
- The caption text is bounded at 2000 characters per event.
- The render is **off the request threads**: the API queues and returns.
- The formatting concurrency is the worker's own: one artifact at a time,
  after the other work has had its turn. A long render does not block
  ingestion, polling or the other work.
- The temporary files are cleaned up after every attempt, and the
  reconciliation removes the ones that are long gone.
- The output files stay local.

---

## 13. The known limitations

### 13.1 The honest ones

1. **The production verifier does not re-render to prove the burn-in.** It
   checks the structure, the dimensions, the codec, the pixel format, the
   duration and the audio, and it records how many caption events were
   written. The *media-level* proof that the captions are in the picture is
   in the test suite and in the end-to-end script, which render twice and
   compare the frames. Doing it in production would double every render.
   This is a trade-off, and it is stated rather than hidden.

2. **The burn-in proof compares frames; it does not read the text.** The
   proof is that a captioned render differs from an uncaptioned one. That
   does not check *which* words are on screen. Reading them would need OCR,
   and no such claim is made.

3. **A missing font is not detected at render time.** libass silently falls
   back to another font. The interface offers the fonts that are
   installed, so this is unlikely, but it is not enforced. The mitigation
   is the font list, not a guarantee.

4. **The browser was not exercised manually in this phase.** The 14
   component tests cover the panel's behaviour, and the end-to-end script
   covers the API and the media, but nobody looked at the panel in a real
   browser during this phase. This is the clearest gap in the evidence.

5. **The caption styling is render-wide.** There is no per-caption
   styling, because SRT has no markup. The wrapping and the line cap are
   the application's, not libass's.

6. **The frame rate is the source's.** It is not forced to a constant, which
   is predictable but means two clips can have different frame rates. A
   constant frame rate would cost a re-timing pass for no benefit here.

7. **The aspect ratio is enforced to be 9:16 within 0.5 %.** A user who
   wants a different portrait ratio — 4:5, say — cannot have one. The
   brief asked for 9:16, so this is deliberate, but it is a limit.

8. **There is no cancellation.** A queued or running format cannot be
   cancelled. This is the same gap the rest of the application has, and it
   is listed in the architecture as not yet existing.

9. **The render is not idempotent at the file level.** A retry re-renders
   the whole file. There is no resume.

### 13.2 The environment-dependent skips

- The privileged-symlink test is skipped (1 skip in the backend suite).
  It cannot run without the privilege, and it has nothing to do with this
  phase.
- The real-model tests are unaffected. The transcription model was not
  called again, so no new model dependency was introduced.
- The `test_a_download_that_exceeds_the_limit_is_rejected` test remains
  network-dependent. It passed here, but it can fail without a network.

### 13.3 The carried-forward items

- The **Node LTS parity check** is still not runnable: no LTS is installed
  and the repository has no CI. This was carried from Phase 3.1 and is not
  a Phase 6 regression.
- The **`braces` advisory** in the dev-only ESLint chain is unchanged and
  acknowledged.

---

## 14. The definition of done

| # | Item | Done |
| --- | --- | --- |
| 1 | The existing architecture and working tree inspected | ✅ |
| 2 | The baseline test results recorded | ✅ 667 passed / 1 skipped / 77 frontend |
| 3 | A new validated formatting configuration | ✅ `app/formatting.py` |
| 4 | Portrait 9:16 rendering | ✅ 720 × 1280 and 1080 × 1920, both enforced |
| 5 | Centre crop without stretching | ✅ `force_original_aspect_ratio=increase` + `crop` |
| 6 | Fit padding without stretching | ✅ `force_original_aspect_ratio=decrease` + `pad` |
| 7 | Burned-in captions from the persisted transcript | ✅ No model called again |
| 8 | Clip-relative timing correct | ✅ The overlap, then the round, then the clamp |
| 9 | Start/end boundary clipping tested | ✅ Both, and both at once |
| 10 | The subtitle generation is safe for Windows paths and untrusted text | ✅ SRT, a server-generated name, a tested escaping helper |
| 11 | The output is a separate artifact | ✅ `clip_formats`, its own file, its own lifecycle |
| 12 | The original Phase 5 clips remain unchanged | ✅ Proved by a test and by the end-to-end script |
| 13 | The artifact state, the retries and the ownership checks | ✅ The same state machine as the clips |
| 14 | The file/database crash recovery is documented | ✅ §11.5 |
| 15 | The API endpoints are implemented and tested | ✅ 6 endpoints, 29 tests |
| 16 | The frontend controls and the status UI | ✅ `FormatPanel`, 15 tests |
| 17 | The real FFmpeg/FFprobe output tests pass | ✅ 23 tests, all passing |
| 18 | The caption rendering is checked beyond the subtitle file | ✅ **105 frames differ** in the end-to-end run |
| 19 | The end-to-end formatting test passes | ✅ Every step, exit 0 |
| 20 | The Phase 3/4/5 regressions are checked | ✅ All three scripts and both suites |
| 21 | The type check, the lint, the frontend tests and the build | ✅ 0, 0, 91, 0 |
| 22 | The audit check is run and documented | ✅ Exit 0, the same 5 dev-only advisories |
| 23 | `PHASE_6_REPORT.md` is complete | ✅ This file |
| 24 | The git status and the changed files are reported | ✅ §15 |
| 25 | No existing work was discarded | ✅ Nothing reset, nothing cleaned |
| 26 | No commit was created | ✅ |

---

## 15. The git status

**HEAD:** `7502384d4b0ef1aeff3d568bd2db7ed790864378` — **unchanged**. Nothing
was committed and nothing was staged.

The working tree carries the uncommitted Phase 3, 3.1, 4 and 5 work, which
was already there before this phase and was preserved untouched.

```
36 modified, 80 untracked, 0 staged
```

The 80 untracked files include the 15 new Phase 6 files:

```
backend/app/captions.py
backend/app/fonts.py
backend/app/format_render.py
backend/app/format_service.py
backend/app/formats.py
backend/app/formatting.py
backend/app/routers/formats.py
backend/tests/e2e_phase6.py
backend/tests/test_format_render.py
backend/tests/test_format_worker.py
backend/tests/test_formats_api.py
backend/tests/test_formatting.py
frontend/src/components/FormatPanel.tsx
frontend/src/components/FormatPanel.test.tsx
PHASE_6_REPORT.md
```

The modified files are listed in §2.2. The Phase 5 report's file list is
unchanged except for the ones named here.

---

## 16. The final verdict

### **READY WITH DOCUMENTED LIMITATIONS**

### Why

**The evidence is strong on the things that matter.** The caption timing —
the part of this phase that is easiest to get quietly wrong — is derived
from the overlap of the segment with the clip, tested for every boundary
case, rounded consistently and clamped to the *measured* duration, so a
caption can be neither before zero nor after the end. The captions come
from the persisted transcript, with no second call to the model and no AI
request at all. The rendering is real FFmpeg, the verification is an
independent FFprobe read-back that checks the exact dimensions, the codec,
the pixel format, the duration and the audio, and the burn-in is proved at
the **media level** — 105 frames of the served file differ from the
uncaptioned clip, with a control test that proves the difference is the
caption and not the encode. The worker integrates with the existing lease,
ownership and recovery patterns without touching them, the Phase 5 clip is
proved untouched, the end-to-end chain passes every step, and the whole
Phase 3/3.1/4/5 surface still passes.

**The limitations are real but bounded, and none of them is a correctness
failure in the delivered feature:**

- The production verifier checks the *structure* of the output, while the
  *burn-in* is proved in the test suite. The evidence exists; it is not
  re-derived on every render.
- The font fallback is not detected at render time. The interface offers
  the installed fonts, so a user is not left guessing.
- The browser was not manually exercised in this phase. The component tests
  and the end-to-end script cover the behaviour, but nobody looked at it in
  a browser.
- The styling is render-wide, and the frame rate is the source's.

**Why not plain `READY`:** because the browser was not manually verified in
this phase, and because the burn-in is verified by the tests rather than
by the running application. Both are narrow, both are stated, and neither
is a defect in what was built — but a reader should not have to discover
them.

**Why not `NOT READY`:** every requirement in the brief that is applicable
is met and demonstrated with executed evidence. The scope boundaries were
respected: no face tracking, no karaoke, no editor, no publishing, no new
dependency, no new AI integration.

### One last note

This is an unauthenticated local application. It is **not** production
readiness for a public deployment, and it is not claimed to be. Phase 6 is
about reliable local functionality, and that is what has been delivered and
verified.
