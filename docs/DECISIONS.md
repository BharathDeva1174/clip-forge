# Architecture decisions

Each entry records what was decided, why, and what it costs. Decisions that
are still open would be listed at the end; as of 31 October 2026 there are
none, and the final section is the log of the ones that have since been
resolved.

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

**Since Phase 6.** Every one of the ten flags is `true`, because every one of
the ten capabilities ships; `test_ready_reports_exactly_the_capabilities_that_exist`
in `backend/tests/test_health.py` is the guard that replaced the Phase 1
"all false" assertion.

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

**Since Phase 9.** No browser-testing framework was ever added: Phase 9
attempted it and recorded honestly that repeatable browser automation is not
possible on this install (no Playwright/Cypress/Selenium, and the built-in
preview cannot attach). The end-to-end evidence is the `e2e_*.py` scripts,
which drive the real API and worker over HTTP.

---

## D12 — Turbopack root pinned to the frontend directory

**Decision.** `frontend/next.config.ts` sets `turbopack.root` to the frontend
folder.

**Why.** Turbopack walked up the directory tree looking for a workspace root
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

## D25 — The speech-to-text engine runs locally, with faster-whisper

**Decision.** Transcription uses `faster-whisper`, a local implementation of
the Whisper models. The model weights are downloaded once into a cache and
then used entirely offline. There is no hosted transcription API and no
transcription credential.

**Why.** Phase 2 left this open between an offline engine and a hosted API,
and the local-first decision (D1) settles it: the product is about turning
*your own* videos into clips, and sending the audio to a third party to do it
would be a large and surprising disclosure. `faster-whisper` is also
quantised enough to run on a CPU, which most development machines have.

**Cost.** Transcription is slow on a CPU, and the model weights are a
one-off download of 75 MB (`tiny.en`) to 480 MB (`small.en`). The first
transcription needs a network connection unless the model was cached
already. A hosted API would have been faster and simpler, at the price of
the user's content leaving the machine.

---

## D26 — The model is loaded in the worker, lazily, and reused

**Decision.** The speech model is constructed on the first transcription and
kept for the lifetime of the worker process. It is never loaded at API
start-up, and the API never loads it at all.

**Why.** Loading a model takes seconds and hundreds of megabytes. Doing it
per request would make every API call expensive; doing it at API start-up
would put a heavy dependency in the process that has to answer quickly. The
worker already exists to own the slow work, and it processes one job at a
time, so one loaded model serves every job.

**Cost.** The first job after a worker restart is slower than the rest, and
the worker holds the model's memory for its whole life.

---

## D27 — Strict offline mode is a setting, not the default

**Decision.** By default the engine may fetch the model on first use.
`CLIPFORGE_TRANSCRIPTION_OFFLINE=true` makes it refuse to touch the network
at all, and a model that is not in the cache then produces an actionable
error instead of a silent download.

**Why.** Making offline the only option would make the first run fail for
every new user. Making it impossible to be offline would make the
local-first promise conditional on the model's particular file layout. The
setting lets the user choose, and it makes the guarantee testable: the
engine's model constructor is called with `local_files_only=True`, and a
regression test proves the model is never even constructed when the cache is
empty.

**Cost:** one more setting, and a user who wants a hard guarantee has to
turn it on.

---

## D28 — The transcript rules live in their own module

**Decision.** `app/transcripts.py` owns what a valid segment is. It performs
no I/O and knows nothing about SQLite or about the speech engine.

**Why.** The rules are the part most likely to be wrong, and they are the
part that decides whether the stored transcript can be trusted. Keeping them
separate from the engine and from the database means the same rules apply to
any engine, and they can be tested without either.

**Cost:** one more module, and the engine output has to be converted into the
internal shape before it can be checked.

---

## D29 — A timestamp that points past the audio is normalised, not rejected

**Decision.** Whisper's timestamps are quantised to 20 ms and produced from a
30-second window, so the end of the last segment regularly points past the
end of the audio. Such a timestamp is pulled back to the end of the audio. A
timestamp further out than one window is rejected. A segment that lies
entirely outside the audio is dropped.

**Why.** This was found by running the real model, not by reasoning about
it: the first live run failed with *"The transcription produced timestamps
that could not be trusted"* because the engine reported the end of the last
segment as 10 s in a 6.7 s recording. Rejecting that would have made the
whole feature useless on real input. Rejecting it *and* keeping the
overshoot would have stored a transcript that claims speech after the audio
ends. Pulling it back keeps the transcript internally consistent, and the
one-window allowance is generous enough for the model's real behaviour while
still catching a genuinely broken value.

**Cost:** the stored end time is not exactly what the engine said. It is
exactly as long as the audio, which is the truth.

---

## D30 — `completed` means the transcript is stored

**Decision.** A job cannot reach `completed` without passing through
`extracting_audio` and `transcribing`, and the transcript is committed before
the status is updated. The state machine enforces it: `inspecting` can no
longer go straight to `completed`.

**Why.** A client that sees a completed job must be able to read its
transcript. Leaving the order up to the worker's discipline would make the
guarantee a convention rather than a rule, and the whole point of the
explicit state machine (D14) is to make such rules enforceable.

**Cost:** the meaning of `completed` changed in Phase 3, so a job that was
finished under Phase 2 has no transcript. The API reports that honestly
through `has_transcript` rather than letting a client infer it.

---

## D31 - The inference runs in a child process, so the timeout is real

**Decision.** The speech model runs in a child process of the worker, not in
the worker itself. `CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS` is a hard
limit: when it passes, the child is terminated, then killed, then reaped,
and the job fails with `transcription_timeout`. The old in-process engine
remains available as `CLIPFORGE_TRANSCRIPTION_ISOLATION=inline` and is
documented as having a soft timeout.

**Why.** Phase 3's timeout was checked *between decoded segments*, and the
report said so plainly. Two things were found while checking that claim:

1. `WhisperModel.transcribe()` does not only decode. It decodes the whole
   audio file, runs the voice-activity filter and computes the mel features
   **before** it returns the generator. All of that was outside the timeout
   check entirely, so a slow decode could block the worker with no deadline
   in force at all.
2. The decoder itself runs in C++ and releases the GIL. A Python-level
   interruption — a signal, an exception, a thread cancel — cannot reach it.

So neither a smaller timeout nor a thread would have fixed it. The only way
to stop a stalled inference is to stop the process that is doing it, and the
worker is already a separate process, so the architecture already had the
right shape: one more process boundary.

**Cost.** Two more modules and a small JSON protocol over a pipe. The child
has to be started, kept, reused and reaped, and the worker has to stop it
on shutdown. In exchange, "a stalled inference cannot hold the worker" is a
property that can be tested at the process level rather than argued about.

---

## D32 - The normalising duration is the extracted file's, and it is never optional

**Decision.** The duration that transcript timestamps are normalised against
is the duration of the extracted WAV, measured by FFprobe when the file was
produced. The worker passes it to the engine, and the engine prefers it over
anything the model reports.

**Why.** The normalisation rules of D29 only work if a duration is known. The
rule "a segment that lies entirely outside the audio is dropped" is inside an
`if duration is not None` branch, so with an unknown duration a segment
claiming to be at 9 999 s would have been stored verbatim. The engine's own
duration is usually right, but it is reported by the model, and it is not
always there.

The source video's nominal duration is the wrong value in any case: a video
whose audio track is shorter, or which was trimmed, would produce a
transcript that claims speech after the audio ended.

**Cost.** One more keyword argument on the engine interface, and the fakes
have to accept it.

---

## D33 - A model name is validated as configuration, before anything is fetched

**Decision.** `CLIPFORGE_TRANSCRIPTION_MODEL` is checked when the settings
are read. A name that is a known Whisper model, a model repository id such
as `owner/name`, or the path of an existing local model directory is
accepted. Anything else is refused with a message that lists the supported
names. The check never loads a model and never touches the network.

**Why.** A typo such as `tiny.eng` used to be accepted silently and turned
into a download attempt for a repository that does not exist, which is a
confusing way to learn about a typo. A configuration mistake should be
reported as a configuration mistake, early, and in words that say what to
type instead.

**Cost.** The list of known models has to be kept up to date. It is a set of
names, not a version, and a name that is not in it can still be used as a
repository id or a local path.

---

## D34 - The one dependency advisory is accepted, recorded and guarded

**Decision.** The five high-severity advisories that `npm audit` reports are
one advisory — GHSA-vfj7-8cjw-p6xm against `braces` — counted once for
every package that depends on it. It is accepted here, and guarded by
`npm run audit:check`, which fails the
moment a *different* advisory appears.

**Why.** There is no patched `braces`: the advisory covers every published
version and 3.0.3 is the latest release. The only fix npm offers is a
semver-major downgrade of `eslint-config-next` to v14, which was checked
rather than assumed: v14 declares `peer eslint@"^7.23.0 || ^8.0.0"` (npm
refuses the install against the project's ESLint 9 outright) and its
`core-web-vitals` export is an eslintrc-style object with a single `extends`
key, not the flat-config array that `eslint.config.mjs` spreads. Applying
the offered fix would have broken `npm run lint` entirely.

The advisory is not dismissed because it is a development dependency. It is a
real denial-of-service bug in a glob expander. What makes it acceptable here
is that the only thing that reaches it is the project's own ESLint
configuration, not user input, and that it is not in the shipped bundle. The
guard exists so that this judgement cannot quietly become a habit.

**Cost.** One script and one npm script.

---

## D35 - The provider configuration is data, not configuration

**Decision.** A provider configuration is a row in SQLite, managed from the
Integrations page. The API key for it lives in the operating system's
credential store, and only a *reference* to it reaches the database. The
reserved `CLIPFORGE_LLM_API_KEY` / `_BASE_URL` / `_MODEL` settings are
removed.

**Why.** The brief asks for a page where a user can configure, test, select,
edit, disable and remove providers, and for a custom provider to work
without a source change. An environment variable cannot do that: it holds
exactly one provider, it cannot be edited without restarting, and it cannot
hold a key for more than one configuration at a time. It was also the
"hardcode one provider" shape the brief warns against.

The key is a separate question from the configuration, and the two are
stored differently on purpose. A key is a secret; the rest is not. Keeping
the key out of the database means a copy of the database -- a backup, a
sync, a commit -- never carries a secret.

**Cost.** The settings module no longer has any secret in it, so the
"secret is masked when dumped" rule now applies to the credential store
rather than to `Settings`. `keyring` becomes a runtime dependency, and the
application needs a credential store to save a key at all: where there is
none, saving a key fails clearly rather than falling back to plaintext.

---

## D36 - The protocol is a property of the configuration, and the metadata is the model's

**Decision.** Every configuration names its API protocol explicitly. The
protocol decides the endpoint path, the authentication, the request shape
and where the answer is read from. Model capabilities -- the context window,
the output limit, the reasoning-effort parameter and its values, the
structured-output mode -- are *model metadata*, and an optional parameter is
only ever sent when the model is known to accept it.

**Why.** The three protocols differ in every way that matters, and
pretending otherwise is how a request that would otherwise work gets
rejected. OpenAI's Chat Completions takes a bearer token and a `messages`
list; the Responses API takes `instructions` and `input` and spells
reasoning as an object; Anthropic's Messages API takes an `x-api-key` header
plus a version header, puts the system prompt in its own field, and
*requires* a maximum token count. Anthropic has no reasoning-effort
parameter at all -- its equivalent is a thinking token budget, which is a
different quantity -- so it is omitted rather than mistranslated.

**Cost.** Three adapters instead of one, and a metadata model that has to be
filled in honestly. Unknown metadata stays unknown: it is never guessed and
never sent.

---

## D37 - The model selects segments; the boundaries come from the database

**Decision.** The model is asked which *transcript segments* make a good
moment. The start and end times are then read from the persisted segments.
Times the model proposes are treated as advisory: they are checked against
the segments the candidate cites, and a contradiction drops the candidate,
but they are never stored.

**Why.** The brief is explicit that the model must not be trusted to invent
authoritative timestamps. Asking for segment indices and deriving the
boundaries makes the transcript the authority, which is the only thing in the
system that was actually measured. A model that cites a segment that does
not exist has not understood the task, and its answer is dropped.

**Cost.** The model has to count segments, which is harder than reading
timestamps. The prompt shows the segment numbers, and the answer is
validated locally whatever comes back.

---

## D38 - A queued analysis fails clearly when its configuration changes

**Decision.** An analysis records the provider, protocol, base URL and model
it was queued against. If the integration is deleted, disabled or edited
before the analysis runs, the analysis **fails** with a stable code. It
never falls back to another provider, another model or a hardcoded default.

**Why.** Falling back would change the privacy, the cost and the output
without anybody asking, and the user would have no way of knowing. The
brief says so, and it is also the only behaviour a user can reason about:
"the analysis I queued runs against the configuration I chose, or it says
why it did not".

**Cost:** a queued analysis can fail because of something the user did
afterwards. The failure says exactly what happened, and retrying picks up
the new configuration.

---

## D39 - The preset catalog holds defaults, not inventories

**Decision.** A preset records where a provider lives, which protocol it
speaks by default, where its model list is, and what its documentation says
about free access. It does not carry a model list, a price table, or a
claim that a model is free. A model family may carry a *protocol routing
hint* -- which API the family speaks -- and a model the hint does not cover
is reported as having an unknown protocol, never routed to an incompatible
endpoint.

**Why.** Model inventories and prices change without notice, and a stale
copy in the source is worse than no copy: it would be presented as
authority. The free-tier distinction the brief asks for is real: a provider
may have a free tier, a specific model may be free *for a limited time*, a
model may be paid, or the price may simply be unknown. ClipForge records
the tier it was told about and says that the provider decides.

Two things were found by reading the providers' current documentation, and
both are recorded in the Phase 4 report: OpenCode Zen's inference endpoints
are not under the base URL its model list is published at, and Cerebras's
documentation states plainly that there is no permanently free tier.

**Cost:** the catalog needs a maintenance note, and a user may have to
choose a protocol for a model the hint does not cover.

---

## D40 - A clip is cut from the candidate, and re-encoded

**Decision.** A clip's boundaries are the persisted candidate's own
validated times, and the clip is **re-encoded** rather than stream-copied.
The output is verified with FFprobe before the clip is marked complete.

**Why.** Two separate guarantees.

The *grounding* is inherited: the candidate's times came from the
persisted transcript segments, and the renderer takes them from the
candidate rather than from the request. A client cannot introduce a
timestamp, and the model's own numbers never reach the renderer without
having been through the transcript validation first.

The *accuracy* needs the re-encode. A stream copy cuts on keyframes, so the
file would start earlier and end later than the candidate asks for, and it
would do so silently. Re-encoding is slower and it is the price of a clip
that starts where it was told to. The `-ss` before `-i` seek was verified
against a file with a non-zero start time: it lands on the *media* timeline,
not the file timeline, which is what the candidate's times are on.

**Cost:** a clip takes real time to render, and the rendered file is larger
than a copied one would be.

---

## D41 - A clip is not completed because FFmpeg exited with zero

**Decision.** The render goes to a temporary path that belongs to that
attempt alone, the temporary file is probed and checked, and only then is it
renamed into place and the row committed — with an ownership check.

**Why.** A zero exit status is not evidence that anything was produced. A
run can succeed and write a file that is empty, truncated, or missing its
video stream, and the database would then say "completed" about a file that
cannot be played. The verification is a separate step that looks at the
file, not at the exit status.

The ordering matters as much as the check. Verifying *after* the rename
would leave a bad file visible in the clips directory; committing *before*
the verification would leave the database pointing at an unverified file.
Verifying the temporary file and then renaming it means a half-written file
is never visible as a clip.

The ownership check on the commit is the same rule the jobs use: a worker
whose lease expired cannot claim a success, and the file it produced is
removed rather than left as an orphan.

**Cost:** two filesystem steps and a transaction instead of one, and the
reconciliation of abandoned temporary files has to exist.

---

## D42 - One clip per candidate, and a retry is an explicit operation

**Decision.** A candidate can have one clip, enforced by a partial unique
index. A repeated request returns the clip that already exists. A failed
clip is put back on the queue by an explicit retry operation, which is the
only way a terminal clip moves.

**Why.** A repeated click, or an HTTP retry, must not render the same moment
twice — that would cost the user two renders and leave two copies. Deduplicating
on the *candidate* is exact: two genuinely different candidates are never
merged, which rounding the timestamps and comparing them would eventually
do.

A completed clip is never requeued. The recovery that requeues a clip whose
worker died must not be able to re-render a clip that already succeeded.

**Cost:** a user cannot ask for the same moment twice, and a failed clip
needs one extra click.

---

## D43 - The captions are an SRT file, and the styling comes from libass

**Decision.** The caption file is an **SRT** document, and the appearance
comes from libass's `force_style` option, whose value is built from the
validated settings.

**Why.** SRT has no markup at all: the text is text. The transcript is the
user's speech, and a caption file is a place where untrusted text lands.
ASS would give per-caption styling, but it also gives the text a syntax --
`{...}` override blocks, `\N` escapes -- and the text would have to be
escaped against it, which is a way of corrupting legitimate speech to
avoid an injection. SRT removes the surface instead of defending it.

The styling does not need per-caption markup: the font, the size, the
colours, the outline, the position and the margins are all *render-wide*
choices, which is exactly what `force_style` expresses. The one thing SRT
cannot express is a hard maximum on the number of lines, so the wrapping
and the line cap are done when the file is **written**: the text is broken
into lines of at most *N* characters and cut at *M* lines, with a mark so
the cut is not mistaken for the whole sentence.

**Cost:** the caption styling is render-wide rather than per caption, and
the line wrapping is the application's rather than libass's.

---

## D44 - The subtitle file is referred to by its name, not by its path

**Decision.** The caption file is written into a directory the application
controls, and FFmpeg is run with that directory as its working directory, so
the filter refers to the file by a plain, server-generated name.

**Why.** This was measured rather than assumed. FFmpeg's subtitle filter
parses its filename option with the filter syntax, in which a colon
separates an option from its value, a comma separates filters, brackets
group them and a quote opens and closes a value. A Windows path has a colon
in its drive letter and backslashes everywhere, so it has to be escaped --
and the escaping was tested against the real FFmpeg for paths with spaces,
commas, brackets, Unicode and a semicolon.

A path with a **single quote** turned out to be impossible: FFmpeg opens
the file with the name it parsed, and the quote is simply not there any
more, whichever escaping is used. The arrangement above makes the question
moot, because the directory -- which is the part the user's machine
controls -- never reaches the filter at all. The escaping helper still
exists, is still tested, and refuses a path it cannot express rather than
producing a filter that silently points at the wrong file.

**Cost:** the renderer has to set the child's working directory, and the
subtitle file cannot live anywhere else.

---

## D45 - A formatted clip is a separate artifact, and the settings are fingerprinted

**Decision.** A formatted clip is its own row, with its own file, its own
lifecycle and its own lease. One clip with one configuration is one
artifact, enforced by a unique index on `(clip_id, config_fingerprint)`.

**Why.** The landscape clip is still what the user cut; the portrait one is
a derivative. Overwriting the original would take away something the user
had, and overwriting the *formatted* artifact when a setting changes would
be worse: a finished result would quietly change under the user's feet.

The fingerprint is the hash of the **normalised** settings, so the same
settings always produce the same artifact -- a repeated click, or an HTTP
retry, does not render twice -- and a different setting always produces a
different one. The normalisation matters: without it, the same settings
written in a different order would be a different artifact.

**Cost:** a user cannot ask for the same configuration twice, and each
distinct configuration keeps its own file.

---

## D46 - The dashboard's counts come from one batched query set, not from a request per job

**Decision.** The job response carries how many highlight candidates, how
many clips and how many formatted artifacts the job has, and the status of
its most recent analysis. The API gathers them for the whole page in a
fixed number of queries.

**Why.** The dashboard wants to say at a glance what a video has become.
The obvious way to get that is to ask the analysis endpoint and the clips
endpoint for every job — which is two requests per row, so a library of
fifty jobs is a hundred requests every time the page loads. That is the
"runaway background requests" problem the phase brief warns about, and it
would be worse on a laptop running everything locally.

The codebase already had the answer: `job_ids_with_transcripts` gathers the
transcript flag for a whole page in one query, because a client must be
able to say truthfully whether a transcript exists. The counts are the
same kind of fact and use the same pattern.

The "most recent analysis" is chosen by the same rule as
`latest_analysis_for_job` — `created_at` descending, then `id`
descending — so the count never disagrees with the analysis the detail
endpoint reports. A test asserts that they agree.

**Cost:** the job response is a little larger, and the counts are a second
place where the interface could get them wrong. The tests keep the two
endpoints honest.

---

## D47 - An unrecognised status is called unknown, not guessed

**Decision.** The API client normalises every job it receives. A status
that is missing, or one this build does not know, becomes the sentinel
`unknown` — not `queued`, not `completed`. The interface says "Unknown
status" and stops polling.

**Why.** The alternative is to rewrite an unknown status into one the
interface *does* know, and that is a lie: a job would be shown as queued
when nothing says it is. The `unknown` sentinel costs a few lines and keeps
the interface honest about the one thing it genuinely does not know.

It is also what stops the polling: a status the build cannot describe will
not describe itself later either, so asking about it for ever would be
waste.

**Cost:** the `JobStatus` type carries one value the backend does not
define. It is documented as a client-side state.

---

## D48 - The route that needs a request-time id is a blocking route

**Decision.** `/jobs/[jobId]` declares `export const instant = false`.

**Why.** Next 16 runs with Cache Components on, and a dynamic route whose
parameter is not known at build time cannot be prerendered: the pathname
cannot be resolved, so `usePathname()` in the shared header suspends, and
the build fails with *"URL data in a Client Component outside of Suspense"*.

The documented options are a Suspense boundary or a blocking route. The
workflow of a video genuinely depends on a request-time id and on a
request-time API, so declaring it a blocking route says exactly what is
true: the navigation into it is allowed to wait. The header keeps its
`usePathname()` and the layout keeps its static shell.

**Cost:** the route is not prerendered, so its first paint waits for the id.

---

## D49 - The media routes are resolved in the client, not in the API

**Decision.** The API keeps answering with a `media_url` that is relative
to itself. The client resolves it against the API's address in one place —
`mediaUrl()` — and every preview and every download uses the result.

**Why.** A relative `media_url` is right for a client on the same origin
and wrong for one on a different port. The frontend runs on 3000 and the
API on 8000, so the browser resolved `/api/v1/clips/<id>/media` against
the *frontend*, got a 404, and showed an empty black rectangle where the
video should have been. The same was true of every download link.

The obvious alternative — making the API answer with an absolute URL —
would bake the API's own address into every response, and it would be
wrong the moment the API moved or was reached through a proxy. The
frontend already knows where the API is: it is the one thing it has to be
configured with. So the resolution belongs there, in one place, where it
can be changed once.

**The cost of getting it wrong was invisible to the tests.** The component
tests asserted the *relative* URL — the bug — and passed. Only the
browser, playing the file, showed that nothing was there. The five
expectations that encoded the bug now assert the absolute URL.

**Cost:** one more thing for the client to get right, and a URL that the
frontend has to build rather than copy.

---

## D50 - The worker leaves a heartbeat, so the status view can see it

**Decision.** The worker writes a small JSON file into the storage root
every few seconds. The readiness report says whether the worker was seen
recently, and the status view says so in words.

**Why.** The API and the worker are separate processes, so the API cannot
see the worker at all. A job can sit in the queue for ever with nothing
to explain why, and the roadmap asks the status view to cover *both* the
API and the worker. The worker is the half that does the work, so it is
the half that matters.

It is a **file** and not a row in the database, and that is the whole
design: the heartbeat is about the *process*, not about a job; it changes
constantly; and it must not need a migration, a transaction or a schema
change to read. A missing note and a stale note mean the same thing to a
user — the worker is not running — and that is the answer the user needs.
Writing it is best-effort: a worker that cannot write the note keeps
working, because losing the note is much better than losing the worker.

**Cost:** the storage root holds one more file, and the answer is
eventually consistent — the status view can be up to half a minute out of
date.

---

## D51 - Lease renewal tolerates a busy database, and publication re-checks ownership

A provider call can run for minutes. The analysis lease is renewed on a
background thread during the call, but that thread used to treat **any**
error — including a transient `database is locked` under contention — as
"ownership lost" and stopped renewing for good, so healthy work lost its
lease and got requeued (observed live: an analysis needed a second attempt
because one renewal error doomed the first attempt).

The renewer now distinguishes three cases. A renewal that answers "you do
not own this row" stops immediately. A transient error **after** a renewal
has succeeded is retried until the original lease would have run out. A
renewal that has **never** succeeded stops immediately, because ownership
was never confirmed. On top of the thread, the runner re-checks ownership
synchronously once, just before publishing, and treats a refused
completion write (`update_analysis` returning `None`) as a lost analysis —
a fast provider can finish inside the renewer's first tick, so the thread
alone cannot rule out a takeover.

**Cost:** a takeover that happens mid-run is now detected at publication
instead of possibly never being detected; the trade is one extra small database
transaction per analysis.

---

## D52 - Recovery messages say the lease expired, not that a worker died

When a lease expires, the recovery path used to write *"A worker stopped
while this … was running."* Under real conditions that was false: the
worker named in the row was alive, and the lease had merely run out during
a long provider call. The message for jobs, analyses, clips and formatted
clips now says what happened — the lease expired and the work was queued
again for a running worker to pick up. The stable error code
(`lease_expired`) is unchanged.

**Cost:** none. The old sentence was simply wrong about the world.

---

## D53 - The heartbeat is written through a per-process temporary file

The heartbeat's fixed `.tmp` name could be held by a reader or a scanner,
which turned one slow write into a `PermissionError` every five seconds —
observed in the worker log as a traceback loop. The temporary name now
includes the worker's pid, and repeated write failures log one full
warning every five minutes instead of one per attempt. A successful write
resets the throttle.

**Cost:** a stuck heartbeat file now leaves at most one `.tmp`-suffixed
file per worker behind, and repeated failures are quiet in the log.

---

## D54 - The launcher refuses to stack ClipForge on top of itself

Nine `worker.py` processes and two `run.py` APIs were found running at
once, all sharing one database and one heartbeat file. The result was
lease churn, a `/ready` report that flapped between worker ids, and
analyses losing their leases mid-provider-call. `scripts/start-local.ps1`
now detects ClipForge python processes before starting anything and
refuses to run unless they are stopped — or `-Replace` is passed, in
which case it stops them itself. The launcher also **rotates** its logs to
`*.prev.log` instead of deleting them, so the log of the run that went
wrong survives the restart that diagnoses it.

**Cost:** starting a second stack on purpose now needs `-Replace`; the
previous run's logs stay on disk until the next start overwrites them.

---

## D55 - Cancellation stops the child processes and reaches the queued work

Cancellation used to mean "no further state transitions and no result
publication": a job marked cancelled left its in-flight yt-dlp download,
FFmpeg render or formatted render running to completion, and its queued
clips, formatted clips and pending analyses were still claimed and
rendered afterwards by the worker. The work was thrown away at the
ownership check, minutes later.

Cancellation now reaches the work itself, in three layers. **Queued
children** of a cancelled job are failed in the same transaction that
cancels the job, with the stable `job_cancelled` code, so the worker never
claims them. **Claimed clips and formatted clips** are driven by a tick
that runs before the render, every quarter of a second during it, and
before verification: the tick renews the artifact's lease (which clip and
format renders never had — a render longer than the lease used to be
stealable mid-flight) and raises `WorkCancelled` when the parent job is
cancelled, which the renderers' existing `finally` answers by terminating
and reaping the FFmpeg child. **A cancelled parent job is terminal**, so
the artifact is recorded as `failed`/`job_cancelled` rather than queued
for a retry nobody would want; a worker *shutdown* during a render stays a
plain interruption and leaves the artifact for lease recovery. Downloads
already stopped their child through the acquisition loop's own tick and
`finally`, and transcription already stops through the process
supervisor — both are unchanged.

A cancel landing inside the bounded (≤ 60 s) verification step is still
not interrupted mid-probe; the tick catches it at the next boundary.

**Cost:** three small UPDATEs per cancellation, and one lease renewal per
tick during renders — both negligible next to the media work they guard.

---

## D56 - A finished job can be deleted, whole

The library grew two things the product was missing: the videos are shown
in **sections** (in progress / processed / failed or cancelled — a job is
in progress exactly while the worker could still touch it), and a
**delete** for finished work.

`DELETE /api/v1/jobs/{id}` removes a job and, through the schema's
`ON DELETE CASCADE` with foreign keys enforced, the whole tree under it —
transcript and segments, analyses and candidates, clips, formatted clips
and caption edits — in one transaction. A job that a worker could still be
working on is refused with the stable `job_not_deletable` code, re-checked
inside the delete's own transaction so a job claimed in between cannot be
half-removed; cancel first, then delete. Before the rows go, the stored
relative paths (the source, every clip, every formatted clip) are read
and, after the delete, resolved through the storage root's containment
check and unlinked — a path that escapes the root, or a file that is
already gone, is logged and skipped rather than followed. The downloader's
and the formatters' scratch directories under `temp/` go with them.

**Cost:** deletion is forever, and the interface says so before it
happens; the file reclamation is best-effort, so a locked file can leave
an orphan that the next deletion attempt will not find — the database
remains the truth.

---

## Resolved decisions (formerly open)

Each entry records what was decided and when. They are kept here so the
reasoning outlives the per-phase reports, which are kept outside this
repository.

| Question | Decided in | Resolution |
| --- | --- | --- |
| Which LLM provider and endpoint dialect? | Phase 4 | A provider-agnostic abstraction with one method and one adapter per protocol: OpenAI Chat Completions, OpenAI Responses and Anthropic Messages. No provider SDK is committed to. Custom endpoints are supported. Two protocols are recognised but not implemented (`google_generate_content`, `system_one`) and are reported as unsupported rather than routed. |
| One worker process, or a queue table with several workers? | Phase 5 | One worker process. It polls jobs, highlight analyses, clips and formats in the same loop, claiming each with `BEGIN IMMEDIATE`. No queue table and no multi-worker coordination beyond the database leases. |
| Subtitle burn-in with FFmpeg `drawtext`/ASS, or a rendered overlay? | Phase 6 | SRT captions with libass's `force_style` for render-wide styling. FFmpeg's `subtitles` filter is used with the working directory set to the attempt's scratch directory, so only a server-generated filename reaches the filter. ASS was rejected because it gives the user's speech a syntax to inject into. |
| Should the model be quantised differently per machine, or stay `int8`? | Phase 4 / 3.1 | Derived from the device: `int8` on CPU, `float16` on CUDA, `default` on auto. Configurable via `CLIPFORGE_TRANSCRIPTION_COMPUTE_TYPE`. The model runs locally with `faster-whisper`; no hosted transcription API. |
| How should a job be cancelled, and what does it mean for a running download? | Phase 8 | `POST /api/v1/jobs/{id}/cancel` sets `status=cancelled`, releases the lease and clears `worker_id` in one transaction. A cancelled job is never claimed. Cancellation does **not** terminate an in-flight FFmpeg/yt-dlp mid-step — it prevents further state transitions and result publication, and cancelled results are never published. This is a documented limitation. **Amended 2026-10-31 (D55):** cancellation now stops in-flight children (yt-dlp, FFmpeg clip and format renders) within one tick, fails the job's queued children with `job_cancelled`, and clip/format leases are renewed during renders. A cancel inside the bounded verification step lands at the next tick boundary. |
| Does the API need authentication once it can run on a shared network? | Phase 9 | No authentication added. The API binds loopback by default and refuses any other address unless `CLIPFORGE_ALLOW_LAN_BINDING=true` is set explicitly. It is a local tool, not a service; exposing it to a shared network is documented as a risk the user opts into. |
| One-command launcher: PowerShell script, npm script, or a small Python CLI? | Phase 10 | A PowerShell script, `scripts/start-local.ps1`, starts the API → worker → frontend on user-specified ports, waits for TCP + HTTP readiness (not fixed delay), logs each process to a file (never a pipe), and exposes `Stop-ClipForge` for cleanup. |
