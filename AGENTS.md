# AGENTS.md

ClipForge — a local-first app that turns long videos into short-form clips.
FastAPI + SQLite + a separate worker process on the back end; Next.js 16
(App Router) on the front.

**Read `docs/DEVELOPMENT.md` first** for setup, `docs/ARCHITECTURE.md` for
how the pieces are wired, and `docs/TESTING.md` for what each suite covers.
This file is the stuff that is easy to get wrong.

The per-phase reports (`PHASE_*_REPORT.md`, `UI_REVAMP_REPORT.md`) record what
was decided, what was verified and what is still limited. They are **kept
outside this repository** — local files, git-ignored, never to be re-added.
What survives here is [docs/ROADMAP.md](docs/ROADMAP.md) (phases and their
acceptance criteria) and [docs/DECISIONS.md](docs/DECISIONS.md) (the numbered
decisions). Read those two before changing an area, rather than re-deriving it.

## The published tree

The whole implementation is tracked: Phases 1–10 plus the reliability work after
Phase 10 (decisions D51–D56: lease renewal during provider calls, the analysis
deadline, cancellation that stops in-flight children, whole-job deletion).
`origin` is https://github.com/BharathDeva1174/clip-forge — a public repository,
so **anything committed here is published**.

What is deliberately *not* in the repository, and must stay that way:

- `.env`, `.env.local` and any other real environment file. Only the `*.example`
  templates are tracked, and they contain no key.
- `backend/data/` — the SQLite database, sources, clips, formats and cached
  models. Provider configurations and their credential references live in that
  database and in the OS credential store, so they are never published.
- Local run scratch: `logs/`, `monitor.md`, `monitor_*.log`, `run.bat`,
  `clips_export*/`, `scripts/monitor_yt_pipeline.py`, `.claude/`, `.qoder/`,
  `.openchamber/` and the `%SystemDrive%/` directory created by a bad quote.
- The per-phase write-ups: `PHASE_*_REPORT.md` and `UI_REVAMP_REPORT.md`. They
  stay on this machine as the verification record; they are not published and
  must not be `git add`ed back.

Never put an API key, token or provider endpoint belonging to this machine into
a tracked file, a local report or a test fixture. Before pushing, check the
staged set for key material — the repository has no secret to remove afterwards.

Do not reset, clean, stash, checkout or "restore" anything.

## Commands

The venv is at the **repository root**, not in `backend/`.

```powershell
# from clipforge\backend\
..\.venv\Scripts\python.exe -m pytest                 # all backend tests
..\.venv\Scripts\python.exe -m pytest tests/test_clips.py -k "lifecycle"
..\.venv\Scripts\python.exe run.py                    # API on 127.0.0.1:8000
..\.venv\Scripts\python.exe worker.py                 # the background worker

# from the repository root — starts API, worker and frontend, readiness-gated
.\scripts\start-local.ps1                             # then: Stop-ClipForge

# from clipforge\frontend\
npm test                                               # vitest, single run
npx vitest run src/components/JobList.test.tsx        # one file
npm run typecheck                                      # next typegen && tsc --noEmit
npm run lint
npm run build
npm run audit:check                                    # see the audit note below
```

**`npm run audit:check` fails on 31 October 2026, and that is the guard
working.** Its baseline (`ACCEPTED_ADVISORIES` in
`frontend/scripts/check-audit.mjs`) still lists the old dev-only `braces`
advisory, but `npm audit` now reports GHSA-g3hv-75wg-m249 instead — a
moderate denial-of-service in `minimatch` (`<11.0.1`) that reaches 16 dev-only
packages in the ESLint / `typescript-eslint` chain, and whose only
npm-suggested fix is a semver-major downgrade of `eslint` to v4. Nobody has
decided whether to fix it or accept it, so the baseline has deliberately not
been widened. `npm run lint` itself passes.

`npm run typecheck` runs `next typegen` first on purpose: Next 16 writes the
route types into `.next/types`, and a fresh checkout fails without it.

`backend/tests/e2e_*.py` are **scripts, not tests** — there are five of them
(`e2e_workflow`, `e2e_phase4`, `e2e_phase5`, `e2e_phase6`, `e2e_phase8`). They
start the real API and worker processes, use a throwaway storage directory,
and take minutes. Run them from `backend/`, one at a time.

## Three things that will waste your time

**1. A child process's stdout must go to a file, not a pipe.** A Windows
pipe holds about 4 KB; a server that fills it blocks while writing its log,
which looks exactly like the server having hung. All five `e2e_*.py`
scripts send the output to a file for this reason. Do the same in anything
new that spawns the API or the worker.

**2. Do not run the backend suite and the e2e scripts at the same time.**
`test_a_job_left_behind_by_a_dead_worker_is_recovered` is timing-sensitive
and fails under CPU contention — the worker process cannot start inside its
timeout. Run the suite alone.

**3. `frontend/AGENTS.md` is generated by `next dev`.** Deleting it just
re-creates the diff. Read it, do not remove it.

## The shape of the system

- **The API and the worker are separate processes** sharing one SQLite file.
  All media work (FFmpeg, FFprobe, transcription, rendering) happens in the
  **worker**. The API only queues and answers. Never run FFmpeg in a request
  handler.
- **Every long operation is its own lifecycle** with a status, a stage, a
  lease, an owner and an attempt count: `jobs`, `highlight_analyses`,
  `clips`, `clip_formats`. A worker claims work atomically, and every write
  is ownership-checked.
- **One timeline.** The transcript, the highlight candidates and the clip all
  sit on the **source media** timeline. A formatted clip's captions are the
  overlap of the segment with the clip, re-based onto the clip's own
  timeline and clamped to the clip's *measured* duration.
- **The AI provider key is in the OS credential store** (keyring). The
  database holds only a reference. Never put a key in the database, and
  never in a `NEXT_PUBLIC_*` variable — those are inlined into the browser
  bundle.
- **Storage** is a directory tree under the storage root: `uploads/`,
  `sources/`, `temp/`, `clips/`, `formats/`, `database/`. Paths in the
  database are relative to that root, and every path is resolved with a
  containment check.

## Changing the database

`backend/app/db.py` holds `MIGRATIONS` (a list of `(version, statements)`)
and `SCHEMA_VERSION = 7`. To add a table:

1. append a migration tuple to `MIGRATIONS`;
2. bump `SCHEMA_VERSION`;
3. add a repository function in the same module.

Tests assert the version against the **constant**, never a literal — keep it
that way, or the next schema change breaks them.

## Changing the frontend

- **Next 16 with Cache Components on** (`cacheComponents: true` in
  `next.config.ts`). A route whose parameter is only known at request time
  cannot be prerendered: the pathname cannot be resolved, so `usePathname()`
  in the shared layout suspends and the **build fails**. Either declare the
  route blocking (`export const instant = false`) or wrap the data access in
  `<Suspense>`. The guides are in `node_modules/next/dist/docs/`.
- **One API client**: `frontend/src/lib/api.ts`. No component calls `fetch()`
  directly, and there is no `any` anywhere in `frontend/src`.
- **Responses are normalised at the client boundary**, so a malformed answer
  cannot crash a panel. Keep new fields inside `normalize*` helpers rather
  than trusting the payload.
- **The API's `media_url` is relative to the API.** Use `mediaUrl()` from
  `lib/api.ts`; a raw `media_url` in a component points at the frontend's
  port and 404s.
- The header, the navigation and the footer live in the **root layout**; a
  page only decides what is in the middle.
- Tests sit next to the code: `src/**/*.test.tsx`. The suite is jsdom, so
  there is no layout engine — responsive behaviour cannot be asserted in a
  unit test; verify it in a browser.

## Conventions worth keeping

- The error envelope is fixed:
  `{"error": {"status", "code", "message"}}`. The `code` is stable and lives
  in `backend/app/errors.py`; branch on the code, never on the message.
- Backend tests are `backend/tests/test_*.py`; the fixtures are in
  `backend/tests/conftest.py`. The `real_model` marker means the test loads
  the real Whisper model and **skips unless the model is already cached** —
  the suite never downloads anything.
- Several fixtures **skip when FFmpeg is not on PATH**; that is a skip, not
  a pass, and it is worth saying out loud in a report.
- New records get their own lifecycle, their own file and their own lease.
  Do not bolt a stage onto the existing job state machine.
- The frontend uses the design tokens in `globals.css` (`--color-canvas`,
  `--color-ember-500`, `--color-ink-muted`, …). Do not hardcode colours.
- Decisions are numbered and recorded in `docs/DECISIONS.md`; `docs/ROADMAP.md`
  carries the phase list and the acceptance criteria each phase was verified
  against. All ten phases are complete, so a new decision — not a new phase —
  is what records a change of direction.
