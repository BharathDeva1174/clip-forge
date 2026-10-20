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

## D13 — The worker is a separate operating-system process

**Decision.** `backend/worker.py` is its own process. It is not a FastAPI
`BackgroundTask`, not an in-process thread and not an in-memory queue.

**Why.** The brief requires a durable worker, and the reasons are technical as
well as contractual:

- A background task dies with the API process. Jobs would be lost on every
  restart, and the API would have to be running for the pipeline to work at
  all.
- An in-memory queue has no durable state. The database *is* the queue, so a
  job survives a crash of either process.
- A separate process can be killed, restarted and inspected on its own, and
  its memory and CPU use do not compete with the API's event loop.
- Long downloads need process control: a timeout, a clean shutdown and the
  ability to terminate a child process. That is much easier to reason about
  when the worker owns the child.

**Cost.** Three processes to start in development instead of one, and the
worker needs the same configuration and the same database file as the API.
The startup documentation covers it, and the worker uses the same
`get_settings()` the API does, so there is nothing to keep in sync by hand.

---

## D14 — The job state machine is explicit and centrally enforced

**Decision.** The status set, the stage set and the allowed transitions live
in `app/jobs.py`. Every write goes through `assert_transition()`, and the
database adds `CHECK` constraints on the status, stage and source type.

**Why.** "Prevent invalid transitions" is only enforceable if the rules are
written down once. A transition table makes the rules reviewable, and the
`CHECK` constraints make the database the last line of defence rather than the
only one.

**Cost.** A status change needs a change in two places: the transition table
and, if it is a new status, the `CHECK` constraint and a migration. That is
deliberate friction.

---

## D15 — At-least-once processing with idempotent steps

**Decision.** The worker guarantees at-least-once, not exactly-once. A job
may be processed twice if a worker dies after doing the work but before
recording the result.

**Why.** Exactly-once would need a distributed transaction between the
filesystem and the database, which SQLite does not offer and which is not
worth building for a local tool. At-least-once is achievable and safe as long
as each step is idempotent, so the steps are written that way: a source that
is already stored is re-inspected rather than re-downloaded, and a job record
is never duplicated.

**Cost.** A rare double inspection. The document says "at-least-once"
explicitly rather than implying a stronger guarantee.

---

## D16 — Leases with expiry, and recovery at startup

**Decision.** A claimed job carries `lease_expires_at`. The worker renews the
lease while it works, and recovers expired leases at startup and every few
minutes. A recovered job is requeued unless it has used up its attempts, in
which case it fails with `max_attempts_exceeded`.

**Why.** A worker that is killed — a crash, a closed terminal, a power cut —
would otherwise leave a job in `validating` forever, because the row says a
worker owns it and no worker is coming. A lease with an expiry makes the
ownership self-healing: any worker can take over once the lease has passed,
and the job can never be stuck.

**Cost.** A worker that is merely very slow could have its job taken over.
That is why the lease is renewed during a download and why it is generous by
default (five minutes).

---

## D17 — Only transient failures are retried

**Decision.** `acquisition_failed`, `acquisition_timeout` and
`media_tool_unavailable` are retried up to `worker_max_attempts`. Everything
else — invalid media, an unsupported codec, a video that is too long — fails
immediately and permanently.

**Why.** "Never retry a permanently invalid input indefinitely" is a
requirement, and it is also the obvious thing to get wrong: a retry loop
around a file that is not a video burns CPU and delays the real error the
user needs to see. The error code decides, not a blanket policy.

**Cost.** A new error kind has to be classified. The set of retryable codes
is one constant in `app/worker.py`.

---

## D18 — The upload limit is enforced while the body streams

**Decision.** ClipForge parses the multipart body itself with
`python-multipart`, driving the parser from the request stream, and stops as
soon as the byte limit is passed. A `Content-Length` above the limit is
rejected by a middleware before the body is read at all.

**Why.** Starlette's own form parser spools the whole file to a temporary
file *before* the endpoint runs, and it has no limit on file part size. An
oversized upload would therefore be written to disk in full before the
application could refuse it — the exact resource consumption the brief asks
to avoid. Driving the same library directly keeps the streaming behaviour and
adds the limit.

**Cost.** About 150 lines of parsing code, and a dependency on
`python-multipart` at a specific version. The alternative was a naive body
parser, which the brief explicitly warns against.

---

## D19 — FFprobe is the only authority on what a video is

**Decision.** The extension allowlist is a cheap first filter; FFprobe decides
whether the file is really a video, how long it is and how big it is. The
client's MIME type is never consulted.

**Why.** An extension is a claim, not a fact. `evil.mp4` can be anything, and
`clip.mp4` can be a text file. FFprobe reads the container, so it is the only
thing that can be trusted. The MIME type is even less reliable than the
extension, since browsers and operating systems disagree about it.

**Cost.** A subprocess per upload. FFprobe reads container metadata and does
not decode frames, so it is fast; the timeout bounds it anyway.

---

## D20 — YouTube URLs are validated by exact hostname comparison

**Decision.** `app/youtube.py` accepts only the hostnames in
`CLIPFORGE_YOUTUBE_ALLOWED_HOSTS`, compared exactly, and normalises every
accepted form to `https://www.youtube.com/watch?v=<id>`. It rejects other
schemes, credentials in the URL, non-default ports, malformed ids and unknown
paths. It never touches the network.

**Why.** Substring matching is how `youtube.com.attacker.example` gets
through. Exact comparison makes the whole class of host-confusion attacks
impossible. Keeping the module pure also means the rule can be tested
exhaustively without a network.

**Cost.** A new YouTube host — a regional domain, say — needs a
configuration change. That is a feature, not a bug: the allowlist is meant to
be explicit.

---

## D21 — yt-dlp, without cookies or a login, behind an adapter

**Decision.** The downloader is `yt-dlp`, started as `python -m yt_dlp` with
an argument list and no shell, given only a validated canonical URL. It is
never given cookies, a username or a playlist. It sits behind the
`SourceAcquirer` interface, and the whole YouTube path can be switched off
with `CLIPFORGE_YOUTUBE_ACQUISITION_MODE=disabled`.

**Why.** yt-dlp is the de-facto standard, it is pure Python, it is
released under the Unlicense, and it works with the interpreter that is
already installed. Not giving it credentials means it can only reach
material that is publicly available without signing in — which is exactly
the boundary ClipForge wants. The adapter keeps the choice replaceable: a
different acquisition mechanism is one class.

**Cost — and this is stated rather than hidden:** the URL the *user* supplies
is fully under our control, but the redirects yt-dlp follows internally are
not. YouTube serves media from `googlevideo.com` hosts that YouTube chooses,
and yt-dlp resolves and follows those itself. This is inherent to how YouTube
works and is why the mode is configurable. A stricter design would need a
downloader with a controllable HTTP client, at the cost of losing most of
yt-dlp's format handling.

---

## D22 — The caller must confirm they are authorised

**Decision.** The YouTube submission carries `authorized: true`, and the
frontend presents it as an explicit checkbox: *"I own this video or have
permission to download and reuse it."* A submission without it is refused
with `unauthorized_source`.

**Why.** A publicly viewable video is not automatically authorised for
downloading and reuse. Making the user state their rights is a small,
honest control that also puts the responsibility where it belongs.

**Cost:** one extra field, and a checkbox the user has to tick. The backend
cannot verify the claim, and does not pretend to.

---

## D23 — Source files are kept when processing fails

**Decision.** A source is deleted only when the upload itself failed. A
source whose *job* failed stays on disk.

**Why.** The upload was valid; the failure was in processing. Throwing away
a multi-gigabyte file because, say, the download of a different job timed out
would be hostile, and the file may still be wanted.

**Cost:** disk space that the user has to reclaim by hand. The layout and the
cleanup command are documented.

---

## D24 — Job responses never contain a filesystem path

**Decision.** The job schema has `source_reference` (the display name or the
canonical URL) but no path, no worker id and no lease information.

**Why.** A path is an internal detail and, on a shared machine, a small
information leak. It also makes the API harder to change: a path is an
implementation detail of the storage layout.

**Cost:** the frontend cannot show where the file lives. It does not need to.

---

## Open decisions

Not yet settled; each should be decided when its phase starts.

| Question | Decide in |
| --- | --- |
| Which speech-to-text engine? Offline (Whisper) or a hosted API? | Phase 3 |
| Which LLM provider and endpoint dialect? | Phase 4 |
| One worker process, or a queue table with several workers? | Phase 5 |
| Subtitle burn-in with FFmpeg `drawtext`/ASS, or a rendered overlay? | Phase 6 |
| How should a job be cancelled, and what does it mean for a running download? | Phase 8 |
| Does the API need authentication once it can run on a shared network? | Phase 9 |
| One-command launcher: PowerShell script, npm script, or a small Python CLI? | Phase 10 |
