# ClipForge — Quick Start

Get from an empty folder to a finished vertical clip on your own machine.
Roughly ten minutes, plus the time your videos take to transcribe.

ClipForge is **local-first**: the video is decoded, transcribed and cut on your
machine by a background worker, and nothing is uploaded anywhere. The only
outbound request the app can make is the one you start yourself, when you ask a
language model you configured to look for highlights.

---

## 1. What you need

| Tool | Version | Check with | Needed for |
| --- | --- | --- | --- |
| Node.js | 20.9+ (22 or 24 LTS recommended) | `node --version` | the dashboard |
| npm | 10+ | `npm --version` | the dashboard |
| Python | 3.11+ | `python --version` | the API and the worker |
| **FFmpeg + FFprobe** | 4+ | `ffprobe -version` | validation, audio, rendering |
| Git | any | `git --version` | the clone below |

FFmpeg is the one people are missing. On Windows:

```powershell
winget install Gyan.FFmpeg
```

Then **reopen the terminal** so the new `PATH` is picked up.

---

## 2. Clone

```bash
git clone https://github.com/BharathDeva1174/clip-forge.git
cd clip-forge
```

Prefer no Git? **Code → Download ZIP** on the repository page, then unzip into
a folder named `clipforge`. Everything below is identical.

The clone contains only source and documentation. No media, no database, no
model weights, no environment files — steps 3 and 4 create those, and each one
is git-ignored so it can never be committed by accident.

---

## 3. Backend (API + worker)

The virtual environment lives at the **repository root**, not inside `backend/`.

```powershell
cd backend
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

On macOS or Linux use `python3 -m venv ../.venv`, `source ../.venv/bin/activate`
and `cp .env.example .env`.

If PowerShell blocks activation, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the same
terminal and try again — or skip activation entirely and call
`..\.venv\Scripts\python.exe` where a guide says `python`.

`requirements-dev.txt` installs `faster-whisper`, so this pull is noticeably
bigger than a plain FastAPI one (ctranslate2, huggingface-hub, onnxruntime, av).

### The speech model

Not in the repository. The first transcription downloads it into
`backend/data/models/` and every run after that is offline. To fetch it up
front — e.g. for a machine that will be offline afterwards:

```powershell
cd backend
..\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8', download_root='data/models'); print('model cached')"
```

| Model | Size | Use it when |
| --- | --- | --- |
| `tiny.en` | ~75 MB | a quick check that the pipeline works |
| `base.en` | ~145 MB | the sensible default |
| `small.en` | ~480 MB | accuracy matters more than time |

Choose with `CLIPFORGE_TRANSCRIPTION_MODEL` in `backend/.env`. Only the `.en`
models are English; use the multilingual ones for other languages. Once cached,
`CLIPFORGE_TRANSCRIPTION_OFFLINE=true` makes the engine refuse the network
altogether, so a missing model fails loudly instead of downloading silently.

---

## 4. Frontend (dashboard)

```powershell
cd ..\frontend
npm install
Copy-Item .env.example .env.local
```

`.env.local` holds one value that matters, `NEXT_PUBLIC_API_BASE_URL`, and the
template already points it at `http://localhost:8000`. Those variables are
inlined into the browser bundle, so **never** put a key or a token in one.

---

## 5. Run

One command, from the repository root:

```powershell
.\scripts\start-local.ps1
```

It starts the API, then the worker, then the frontend, gating each step on
readiness rather than a fixed delay, and logs every process to a file under
`$env:TEMP\clipforge-local-launch\`. Stop it with:

```powershell
Stop-ClipForge
```

Use `-ApiPort` / `-FrontendPort` to move off 8000 and 3000, and `-Replace` to
clear ClipForge processes left over from an earlier run.

Three terminals works too, and is easier to watch:

```powershell
cd backend && ..\.venv\Scripts\python.exe run.py      # API on 127.0.0.1:8000
cd backend && ..\.venv\Scripts\python.exe worker.py   # the background worker
cd frontend && npm run dev                            # dashboard on :3000
```

**The worker is not optional.** The API only queues work; the worker is what
downloads, probes, extracts, transcribes and renders. If jobs sit at `queued`,
the worker is not running — `http://localhost:8000/ready` reports it.

| URL | What |
| --- | --- |
| http://localhost:3000 | the dashboard |
| http://localhost:8000/health | liveness |
| http://localhost:8000/ready | capabilities, model cache, worker heartbeat |
| http://localhost:8000/docs | OpenAPI explorer |

---

## 6. Your first clip

1. Open http://localhost:3000 and **upload** a video (or paste an authorised
   YouTube URL). The size limit is enforced while the body is still arriving, so
   an oversized file is refused rather than truncated.
2. Watch the job move through `downloading → inspecting → extracting_audio →
   transcribing`. Open the job to read the timestamped transcript when it lands.
   A `completed` job means *transcript stored*, not *clips exist*.
3. **Optional, for highlight detection only:** go to **Integrations**, add a
   preset (OpenCode Zen, Groq, Cerebras) or a custom OpenAI-compatible, OpenAI
   Responses or Anthropic endpoint, paste the key, **Test connection**, select
   it. The key goes to your OS credential store; the database keeps only a
   reference, and removing the integration removes the reference.
4. On the job page, **Find moments**, accept the candidates you want, then
   **Render clip**. Each clip is cut with an exact re-encode and verified with
   FFprobe before it is marked complete.
5. **Format** a finished clip to 9:16 — centre crop or fit-and-pad, burned-in
   captions from the transcript, and any font installed on this machine.
6. Trim a clip to make a **revision** (a new clip; the original is untouched),
   edit caption text (produces a distinct artifact), then **Export** the finished
   clips as a ZIP.

Everything works with no provider configured except step 3 onward: transcription,
rendering, formatting, editing and export never call a language model.

---

## 7. Check the install

```powershell
cd backend  && ..\.venv\Scripts\python.exe -m pytest      # backend suite
cd ..\frontend && npm run typecheck                        # types + route types
cd ..\frontend && npm test                                 # dashboard tests
cd ..\frontend && npm run lint                             # eslint
```

A few FFmpeg-dependent tests **skip** when FFmpeg is not on `PATH`, and
`test_real_model.py` skips unless the model is already cached. Skips are not
passes — read the summary line.

Do not run the suite and `backend/tests/e2e_*.py` scripts at the same time: the
e2e scripts start real API and worker processes, and one timing-sensitive
recovery test fails under CPU contention.

---

## 8. When something goes wrong

| Symptom | Cause and fix |
| --- | --- |
| `ffprobe` / `ffmpeg` not found | Install FFmpeg, then reopen the terminal so `PATH` refreshes. |
| Jobs stuck at `queued` | Start `python worker.py` from `backend/`; check `/ready` → `worker.running`. |
| Transcription fails with a missing-model error offline | Cache the model (step 3), then re-enable offline mode. |
| Port already in use | Change `CLIPFORGE_PORT`, or pass `-ApiPort` / `-FrontendPort` to the launcher (`npm run dev -- -p 3001` for the dashboard alone). |
| `SECRET_STORE_UNAVAILABLE` | The OS credential store is not usable; on Windows that is Credential Manager. |
| Highlight analysis fails with `ai_timeout` | A reasoning model needs a bigger `CLIPFORGE_AI_ANALYSIS_DEADLINE_SECONDS`, not more retries. |
| `Could not determine home directory` | `HOME`/`USERPROFILE` is unset in the process environment; set it. |
| Hydration warnings plus `bis_skin_checked` attributes | A browser extension (Bitdefender / Urban VPN) is rewriting the page — test in Incognito. |

A stale or interrupted job is not lost: expired leases are recovered by the next
worker, and cancelling is always available for work that has not finished.

---

## 9. Privacy in one paragraph

The API binds to loopback and refuses any other address unless you set
`CLIPFORGE_ALLOW_LAN_BINDING=true`. Media paths are contained to the storage
root under `backend/data/` (`uploads/`, `sources/`, `temp/`, `clips/`,
`formats/`, `models/`, `database/`), and client filenames are never used as
paths. Subprocesses are shell-free argument arrays with timeouts and
terminate-then-kill-reap. The AI transport requires HTTPS for remote hosts,
refuses redirects, bounds request and response size, and never leaks your key or
the provider's error body. Your video and audio never leave the machine; the
transcript text leaves only if you ask for highlight detection, and only to the
endpoint you configured.

---

## Next reading

| File | Covers |
| --- | --- |
| [README.md](README.md) | the full guide, configuration table, known limitations |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how the API, worker, database and frontend are wired |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | every command, per directory |
| [docs/TESTING.md](docs/TESTING.md) | what each suite covers |
| [docs/ROADMAP.md](docs/ROADMAP.md) | the ten phases and their acceptance criteria |
| [docs/DECISIONS.md](docs/DECISIONS.md) | numbered decisions with their reasoning |
