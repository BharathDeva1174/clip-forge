# ClipForge backend

FastAPI · SQLite (WAL) · a separate worker process · FFmpeg / FFprobe ·
faster-whisper · Python 3.11+

The engine of [ClipForge](../README.md). Two processes — an API and a worker —
share one SQLite file and a storage directory on this machine.

```
backend/
├── run.py            # starts the API on 127.0.0.1:8000
├── worker.py         # starts the background worker (a shim over app.worker.main)
├── app/
│   ├── main.py       # app factory: CORS, body-size limit, logging, error envelope
│   ├── config.py     # typed settings from backend/.env (CLIPFORGE_ prefix)
│   ├── db.py         # schema + migrations (SCHEMA_VERSION) + every repository query
│   ├── routers/      # videos, jobs, transcripts, highlights, clips, formats,
│   │                 # integrations, health, api metadata
│   ├── worker.py     # the loop: claim → process → renew lease → publish
│   ├── acquisition.py, youtube.py, uploads.py, media.py    # ingestion
│   ├── audio.py, transcription.py, transcription_host.py,
│   │   transcription_process.py                            # speech to text
│   ├── highlight_service.py, highlight_prompt.py, ai_client.py,
│   │   protocols.py, providers.py, credentials.py          # AI highlight detection
│   ├── clip_service.py, clip_render.py                     # cutting clips
│   ├── format_service.py, format_render.py, captions.py,
│   │   fonts.py, formatting.py                             # 9:16 + burned captions
│   ├── export_service.py                                   # bounded ZIP export
│   ├── storage.py, errors.py, schemas.py, deps.py,
│   │   logging_config.py                                   # plumbing
│   ├── worker_heartbeat.py, interruption.py                 # what the status view
│   │                                                      # reads, and how a stop
│   │                                                      # reaches a running step
│   └── jobs.py, transcripts.py, highlights.py, clips.py,
│       formats.py, integrations.py                          # lifecycle models
└── tests/            # 41 test_*.py plus five e2e_*.py scripts
```

## The one rule that matters

**The API never touches media.** It queues work and answers questions. FFmpeg,
FFprobe, the transcription model and the provider calls all run in the worker.
Never call them from a request handler.

Every long operation is a row with its own lifecycle, stage, lease, owner and
attempt count — `jobs`, `highlight_analyses`, `clips`, `clip_formats`. The
worker claims atomically (`BEGIN IMMEDIATE`), renews the lease while it works,
and every write is ownership-checked, so a slow or dead worker cannot corrupt
the result of a newer attempt.

## Running it

From `backend/`, using the virtual environment at the **repository root**:

```powershell
..\.venv\Scripts\python.exe run.py       # API
..\.venv\Scripts\python.exe worker.py    # worker (separate terminal)
```

| URL | What |
| --- | --- |
| `http://127.0.0.1:8000/health` | liveness |
| `http://127.0.0.1:8000/ready` | capabilities, model cache, worker heartbeat |
| `http://127.0.0.1:8000/api/v1` | API metadata |
| `http://127.0.0.1:8000/docs` | OpenAPI explorer |

Configuration is environment-only, read from `backend/.env` (copy
`.env.example`). The API binds to loopback unless
`CLIPFORGE_ALLOW_LAN_BINDING=true`.

## Tests

```powershell
..\.venv\Scripts\python.exe -m pytest                     # the suite
..\.venv\Scripts\python.exe -m pytest tests/test_clips.py # one file
```

`real_model` tests skip unless the Whisper model is already cached, and several
fixtures skip when FFmpeg is not on `PATH` — a skip is not a pass.

`tests/e2e_*.py` are **scripts, not tests**: they start real API and worker
processes against a throwaway storage directory and take minutes. Run them from
`backend/`, one at a time, and never alongside the suite. Send their output to a
file, never a pipe — a full 4 KB pipe blocks the child and looks like a hang.

## Data

Everything lives under `CLIPFORGE_STORAGE_DIR` (default `backend/data/`, git
ignored): `uploads/`, `sources/`, `temp/`, `clips/`, `formats/`, `models/`,
`database/`. Database paths are relative to that root and every path is
resolved with a containment check. Provider API keys are **not** in the
database — they are in the OS credential store, and the row keeps only a
reference.
