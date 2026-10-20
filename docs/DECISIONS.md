# Architecture decisions

Each entry records what was decided, why, and what it costs. Decisions that
are still open are listed at the end.

---

## D1 — Local-first, no cloud infrastructure

**Decision.** Everything runs on the user's own machine. The only external
service is the LLM provider, and it is optional and user-configured.

**Why.** The user asked for a local-first product and for no paid cloud
infrastructure in Phase 1. Local-first also removes an entire class of
problems: no media leaves the disk, there is no upload quota, no per-minute
cost for storage, and it works offline.

**Cost.** Transcription and rendering need local compute, so a machine without
a usable GPU will be slow. The UI has to be honest about progress, which is
one of the reasons background jobs come with a status model.

---

## D2 — Separate frontend and backend applications

**Decision.** A Next.js app on port 3000 and a FastAPI app on port 8000, in
separate directories with separate dependency trees.

**Why.** The API key for the LLM provider must never reach the browser. A
separate backend is the simplest way to guarantee that: the browser can only
reach the API, and the API is the only process that holds a secret. It also
matches the final product — a Python media pipeline and a React dashboard are
different tools.

**Cost.** Two processes to run in development, and CORS to configure. A single
Next.js route handler could have avoided both, at the price of putting the
media pipeline inside the Node process.

---

## D3 — Environment-based configuration, no config file

**Decision.** Configuration comes from environment variables, prefixed
`CLIPFORGE_` on the backend and `NEXT_PUBLIC_` on the frontend, with a
`CLIPFORGE_`-prefixed `backend/.env` for local development.

**Why.** It keeps secrets out of source control by default, works the same on
Windows and elsewhere, and needs no extra library on the frontend. The
backend uses `pydantic-settings` because it gives typed validation for free
and is the tool FastAPI's own documentation recommends.

**Cost.** `NEXT_PUBLIC_*` values are inlined at build time, so changing the
API URL requires a rebuild. That is acceptable for a local tool where the URL
rarely changes, and it is documented.

---

## D4 — SQLite for metadata, filesystem for media

**Decision.** Job and clip metadata goes in a local SQLite file; source videos,
audio, clips and subtitles go on the filesystem under
`CLIPFORGE_STORAGE_DIR`.

**Why.** The two data kinds want different things. Metadata wants queries,
constraints and transactions — SQLite gives that with no server to run. Media
wants to be large, streamed and served by the filesystem — a database blob
column would be the wrong tool. FFmpeg reads and writes files natively.

**Cost.** Two storage systems to keep consistent. The job row records the file
path, and the filesystem is the source of truth for the file itself. The
mitigation is to keep all writes inside the worker.

---

## D5 — Long-running work goes to a background worker

**Decision.** The API accepts a job, records it and returns immediately. A
background worker does the transcription, highlighting and rendering.

**Why.** Rendering a clip can take minutes. Holding an HTTP request open for
that long breaks proxy and browser timeouts, and it makes the API appear
unhealthy — health checks would queue behind a render, so the user would see
"backend offline" exactly when it is busy. Crashing mid-render would also
lose the work.

**Cost.** More moving parts: a queue, a job state machine, and a way for the
frontend to observe progress. The mitigation is that Phase 1 already models
the contract — `/ready` reports implemented capabilities, and the job table is
planned in Phase 2.

---

## D6 — Provider abstraction for the LLM, no SDK chosen yet

**Decision.** Phase 4 will talk to the LLM through a thin internal interface
(one method, one request shape, one response shape). No provider SDK is
committed to yet.

**Why.** The provider and its endpoint compatibility have not been confirmed,
and the choice affects error handling, streaming, token limits and cost. A
thin internal interface keeps the provider swappable and lets the cheapest
option win. Most providers speak a near-identical chat-completions dialect,
so the abstraction is small.

**Cost.** A small amount of extra code in Phase 4, and the risk that the
abstraction is too thin for a provider with unusual semantics. That risk is
accepted: the interface can grow when the provider is known.

---

## D7 — Health contract is a constant, not a setting

**Decision.** `/health` returns exactly `{"status":"ok","service":"clipforge-api"}`,
with the service name hard-coded.

**Why.** The frontend asserts on that exact shape. If the service name were
configurable, someone could change it and silently break the client. Making
it a constant turns that into a compile-time error instead of a runtime
surprise.

**Cost.** The contract cannot be changed without a coordinated frontend and
backend update. That is the point.

---

## D8 — CORS lists explicit origins and rejects wildcards

**Decision.** `CLIPFORGE_CORS_ORIGINS` is a comma-separated list. A `*` is
rejected when the settings are loaded.

**Why.** Credentials are enabled on the CORS middleware, and a wildcard origin
with credentials is invalid and unsafe. Rejecting the wildcard at load time
turns a subtle browser-level failure into a clear startup error.

**Cost.** Adding a second frontend origin means editing an environment
variable. Acceptable for a two-app local project.

---

## D9 — Secrets are `SecretStr`, never logged, never returned

**Decision.** Every credential is a `SecretStr` in the settings, and the
backend has a regression test asserting that no endpoint body contains it.

**Why.** Secrets leak easily: a debug log line, a `repr()` in an error, a
config dump. Masking at the type level means the default behaviour is safe.

**Cost.** Reading a secret requires an explicit `get_secret_value()` call,
which makes every use visible in review.

---

## D10 — `/ready` reports capabilities honestly

**Decision.** `/ready` lists every planned capability with a boolean, and all
of them are `false` in Phase 1.

**Why.** A readiness endpoint that returns a bare `{"status": "ok"}` cannot be
distinguished from a fully working system. Explicit flags let a client tell
"not implemented yet" from "not available right now", and they make it hard
for the UI to over-promise.

**Cost.** The response has to be kept in step with reality. A test asserts
that no capability is claimed while it is unimplemented.

---

## D11 — Test tooling chosen per ecosystem, with no new frameworks

**Decision.** Backend: pytest with FastAPI's `TestClient`. Frontend: Vitest
with React Testing Library and jsdom.

**Why.** Both are the tools the respective frameworks document and support
out of the box. No browser-testing framework was added: Phase 1 has nothing
to drive end to end, and adding one "to claim E2E coverage" would be
disproportionate. That is scheduled for Phase 9, when there is a real flow to
automate.

**Cost.** The integration check is manual. It is scripted in
[TESTING.md](TESTING.md) and its results are recorded in the Phase 1 report.

---

## D12 — Turbopack root pinned to the frontend directory

**Decision.** `frontend/next.config.ts` sets `turbopack.root` to the frontend
folder.

**Why.** Turbopack walks up the directory tree looking for a workspace root
and found an unrelated `package-lock.json` in the user's home directory. That
made it treat the home directory as the project root — it ignored the real
lockfile and warned about it on every build. Pinning the root fixes the
warning and keeps file watching scoped to the project.

**Cost.** If the frontend is later moved into a different layout, this needs
updating. It is derived from the config file's own location, so it travels
with the file.

---

## Open decisions

Not yet settled; each should be decided when its phase starts.

| Question | Decide in |
| --- | --- |
| Which speech-to-text engine? Offline (Whisper) or a hosted API? | Phase 3 |
| Which LLM provider and endpoint dialect? | Phase 4 |
| One worker process, or a queue table in SQLite with several workers? | Phase 5 |
| Subtitle burn-in with FFmpeg `drawtext`/ASS, or a rendered overlay? | Phase 6 |
| Does the API need authentication once it can run on a shared network? | Phase 9 |
| One-command launcher: PowerShell script, npm script, or a small Python CLI? | Phase 10 |
