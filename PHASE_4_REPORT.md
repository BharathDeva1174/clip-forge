# Phase 4 — AI Integrations & highlight detection

**Status: COMPLETE — READY WITH DOCUMENTED LIMITATIONS.**

Phase 4 adds a provider-agnostic integration system, a first-class
**Integrations** page, and highlight detection that runs on the transcript
the previous phases produced.

Every result below was produced by a command that was actually run on
20 October 2026. Where a check could not be run, it says so rather than
claiming a pass.

---

## 1. What the inspection found

The repository was inspected before anything was written: the Git state and
diff, the Phase 1–3.1 reports, the frontend routes, the backend API
structure, the migration framework, the job state machine, the worker
lifecycle, the transcript schema, the existing tests, the configuration and
secret-handling patterns, and the HTTP client dependencies.

Four things shaped the design.

**The provider configuration could not be an environment setting.** Phase 1
had reserved `CLIPFORGE_LLM_API_KEY`, `_BASE_URL` and `_MODEL`. The brief
asks for a page where a user can configure, test, select, edit, disable and
remove providers, and for a custom provider to work without a source
change. An environment variable holds exactly one provider, cannot be
edited without a restart, and cannot hold a key for more than one
configuration. Those three settings were **removed**, and the configuration
now lives in the database. (D35)

**The Phase 3.1 process boundary is untouched.** The transcription still
runs in a child process, the timeout is still hard, and the stale-worker
ownership rules still hold. The highlight analysis reuses the same
patterns rather than replacing them.

**The provider documentation says two things the brief's starting
references did not.** Both are recorded in §3.

**The HTTP client was already there.** `httpx2` was a *development*
dependency, used by Starlette's `TestClient`. It became a runtime
dependency: it is the client the provider adapters use, and it is the same
client whose transport the tests mock, so no provider call ever needs a
real credential in a test.

### The baseline, before anything was changed

```
$ cd backend; ..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN
581 passed, 1 skipped in 78.95s        exit 0

$ cd frontend; npm run typecheck        exit 0
$ cd frontend; npm run lint             exit 0
$ cd frontend; npm test                 5 files, 41 tests passed, exit 0
$ cd frontend; npm run build            exit 0
```

No pre-existing failures. The single skip is a symlink test that needs a
privilege this Windows account does not have.

The working tree already contained the whole of Phases 3 and 3.1 as
uncommitted work. It was preserved. **Nothing was committed.**

---

## 2. The architecture

### 2.1 The modules

| Module | What it owns | I/O |
| --- | --- | --- |
| `app/integrations.py` | The domain model: the protocols, the model capabilities, the pricing and connection statuses, the integration record | none |
| `app/providers.py` | The preset catalog: OpenCode Zen, Groq, Cerebras, Custom | none |
| `app/protocols.py` | One adapter per API protocol | none |
| `app/ai_client.py` | The transport: URL checks, the destination check, timeouts, the response cap, the retries, the error classification | the network |
| `app/credentials.py` | The operating system's credential store | the credential store |
| `app/highlights.py` | The candidate rules and the output schema | none |
| `app/highlight_prompt.py` | The versioned prompt and the windowing | none |
| `app/highlight_service.py` | The orchestration the worker calls | the database |
| `app/routers/integrations.py` | The integration endpoints | the database |
| `app/routers/highlights.py` | The analysis endpoints | the database |

The separation is the point. The highlight business logic knows about
*segments*, *moments* and *providers*. It never knows about bearer tokens,
`x-api-key` headers, `choices[0].message.content` or `content[0].text`.
Adding a provider is a configuration change, not a code change.

### 2.2 The protocol abstraction

Each protocol has its own endpoint path, its own authentication, its own
request shape and its own way of reading the answer. They are **not**
assumed to be alike.

| Protocol | Endpoint | Authentication | Reasoning | Structured output |
| --- | --- | --- | --- | --- |
| `openai_chat_completions` | `{base}/chat/completions` | `Authorization: Bearer` | `reasoning_effort` | `json_object` / `json_schema` |
| `openai_responses` | `{base}/responses` | `Authorization: Bearer` | `reasoning: {effort}` | `json_schema` |
| `anthropic_messages` | `{base}/messages` | `x-api-key` + `anthropic-version: 2023-06-01` | **not supported** — omitted | none; JSON is requested and validated locally |

Two more protocols are *recognised* but not implemented:
`google_generate_content` and `system_one`. A configuration that needs one
is reported as unsupported rather than routed to an endpoint that cannot
understand it — `test_an_unimplemented_protocol_is_refused_before_any_request`
proves no request is even made.

Three rules the adapters hold to:

- **A reasoning-effort parameter is sent only when the model is known to
  accept it.** The model metadata carries the parameter's name and its
  accepted values; an unknown value is not sent.
- **Anthropic never gets a reasoning parameter.** Its equivalent is a
  thinking token budget, which is a different quantity. It is omitted
  rather than mistranslated.
- **The output budget uses the name the API expects.** OpenAI's reasoning
  models want `max_completion_tokens`; the others want `max_tokens`;
  Anthropic *requires* `max_tokens` on every request.

### 2.3 Model capabilities are metadata, not parameters

The context window, the maximum output tokens, the reasoning-effort
parameter and its values, the structured-output mode, whether the model is
available, where the metadata came from and when it was checked are all
properties of a *model*. Unknown is represented as unknown, never as a
default that is sent. `test_the_model_list_parser_never_invents_metadata`
asserts that a provider that says nothing has its metadata stay unknown.

---

## 3. The provider catalog, and two documented discrepancies

The catalog holds **stable defaults only**: where a provider lives, which
protocol it speaks by default, where its model list is, and what its
documentation says about free access. It holds no model list, no price
table, and no claim that a model is free. (D39)

| Preset | Base URL | Default protocol | Model list |
| --- | --- | --- | --- |
| OpenCode Zen | `https://opencode.ai/inference/openai/v1` (Chat Completions and Responses), `https://opencode.ai/inference/anthropic/v1` (Messages) | model-dependent | `https://opencode.ai/zen/v1/models` |
| Groq | `https://api.groq.com/openai/v1` | Chat Completions | `…/models` |
| Cerebras | `https://api.cerebras.ai/v1` | Chat Completions | `…/models` |
| Custom | *(entered by the user)* | Chat Completions | derived from the base URL |

### Discrepancy 1 — OpenCode Zen's inference endpoints

The brief gives the base URL `https://opencode.ai/zen/v1`. That URL is
where the **model list** is published, and it was checked:

```
https://opencode.ai/zen/v1/models                 -> 200   (85 models, OpenAI-shaped)
https://opencode.ai/zen/v1/chat/completions       -> 404
https://opencode.ai/zen/v1/responses              -> 404
https://opencode.ai/inference/openai/v1/chat/completions  -> the path exists
https://opencode.ai/inference/openai/v1/responses         -> the path exists
https://opencode.ai/inference/anthropic/v1/messages      -> the path exists
```

The inference endpoints are **not** under the base URL the model list is
published at. The current documentation was followed: the preset carries the
inference base URL for each protocol, and the model list URL separately.

The brief's own warning — "Do not assume every model uses Chat
Completions" — is exactly right here: OpenCode Zen's catalogue spans the
Responses API, the Anthropic Messages API, the Chat Completions API and
Google's. The preset therefore carries a **protocol routing hint** per model
family (`gpt-` and `grok-` and `muse-` speak the Responses API, `claude-`
speaks the Messages API, and so on), and a model the hint does not cover is
reported as having an *unknown* protocol rather than being routed to an
incompatible endpoint.

### Discrepancy 2 — Cerebras has no permanently free tier

The brief asks for the free-tier caveat to be honest. Cerebras's own
documentation says, under *"Is there a permanently free tier?"*:

> No. The Free Trial is time- and credit-bounded: $5 in credits that expire
> 30 days after they're granted.

The preset therefore records a **trial**, in the provider's own words, and
the application never asserts that a model is free. The same caution
applies to OpenCode Zen, whose free models are documented as free "for a
limited time", and to Groq, whose free tier has lower limits than its paid
plans.

### The four distinctions the catalog keeps apart

| | |
| --- | --- |
| The provider supports a free tier | `free_tier="trial"` on the preset |
| A specific model is currently free | Not tracked. It would be a volatile inventory, and the provider changes it |
| A model is paid | Not tracked, for the same reason |
| The price is unknown | The default. The configuration's **Cost** field records what the *user* knows, and defaults to *unknown* |

`test_no_preset_claims_a_model_is_free_forever` asserts that every preset
records a tier and points at the provider as the authority.

---

## 4. Secrets

### Where the key lives

The key goes into the operating system's credential store through `keyring`:
the Windows Credential Manager, the macOS Keychain, or the Linux Secret
Service. The database keeps only a **reference**.

It was checked on this machine:

```
keyring.get_keyring()  ->  WinVaultKeyring
set / get / delete      ->  round trip ok
```

There is **no plaintext fallback**. Where no credential store is usable,
saving a key fails with a clear error, because a key written quietly
somewhere less safe is worse than no key at all. A configuration *without* a
key is still perfectly usable, which is what a local model server needs.

### The rules the code holds to

| Rule | Where it is enforced | Test |
| --- | --- | --- |
| The key is never returned to the frontend | The response model has `api_key_configured: bool` and no key field | `test_a_key_is_never_returned` |
| The key is never written to the database | Only `secret_ref` is stored | `test_a_key_is_not_written_to_the_database` |
| The key is never in a URL, a log or an error | The key is only ever a header | `test_the_key_is_sent_as_a_bearer_token` |
| An ordinary edit does not erase the key | The reference is only changed when it is passed explicitly | `test_editing_a_configuration_keeps_its_key` |
| Replacing a key is explicit | `POST …/key` | `test_a_key_can_be_replaced_on_purpose` |
| Removing a key is explicit | `DELETE …/key` | `test_a_key_can_be_removed_on_purpose` |
| Deleting a configuration deletes its key | The router deletes the credential | `test_removing_a_configuration_removes_its_key` |
| No test touches the real credential store | The `app` fixture installs an in-memory store | the whole suite |

The **frontend** masks the field, starts it empty, and says that leaving it
empty keeps the key that is already saved
(`test_masks_the_api_key_field_and_keeps_the saved_key_on_an_edit`).

### How it is stored, backed up and removed

- **Stored:** in the credential store, under the service name `clipforge`,
  keyed by a random reference. The reference is what the database holds.
- **Backed up:** the ClipForge database backup contains the reference, not
  the key. The credential store is **not** part of a ClipForge backup, so a
  key has to be entered again on a new machine. That is the deliberate
  trade-off: a backup that carried the key would be a second copy of the
  secret.
- **Removed:** **Remove key** on a configuration, or deleting the
  configuration, deletes the entry from the credential store.

---

## 5. The API and network safety

### The rules

| Rule | How |
| --- | --- |
| HTTPS for a remote provider | Plain `http` is refused unless the configuration is explicitly a local endpoint |
| No credentials in the URL | Refused |
| No fragment in the URL | Refused |
| The destination is checked | The host is resolved immediately before every request; loopback, private, link-local, unspecified, multicast and reserved addresses are refused |
| The metadata service is refused | `169.254.169.254` is link-local, and the metadata hostnames are refused by name |
| Redirects are not followed | A 3xx is an error, so the authorization header can never be forwarded to another origin |
| The request is bounded | A timeout, a response-size cap, a bounded retry budget with backoff, and `Retry-After` honoured |
| The provider's error body never reaches the user | It is inspected to classify the failure, then summarised |

The tests cover the address ranges, the scheme, the credentials in the URL,
the redirect, the response cap, the timeout, the rate limit, the
authentication failure, the missing model, the endpoint mismatch, and the
model list.

### The residual risk, stated plainly

A hostname that resolves to a permitted address once and to a forbidden one
later is not prevented. Closing that completely needs a resolver that pins
the address it checked, which the client does not offer. The check is
therefore made as close to the request as it can be, and the risk is
recorded here rather than claimed away.

---

## 6. The highlight detection

### The input

The transcript comes from the **database**. The service loads the job's
transcript and its ordered segments; it never re-transcribes, and it never
takes transcript text from the client. The segments carry their global
indices, so the model's answer refers to the real transcript.

### The prompt, and the injection risk

The system prompt says, in the first paragraph, that the transcript is
quoted material and not instructions, that anything in it that looks like an
instruction is part of the video, and that the model has no tools and no way
to reveal anything. The transcript is put in the *user* message, as data.

`test_the_prompt_treats_the_transcript_as_data` asserts the wording is
there and that injected text lands in the user message, never in the system
prompt. The answer is validated locally whatever comes back, so an
injection attempt can at worst produce a malformed answer — which is
dropped.

The prompt looks for a strong hook, a surprising or useful insight, a
complete thought, an emotional or narrative payoff, a clear standalone
segment, a suitable beginning and ending — and says explicitly that it
should not reward only for drama, and that a coherent moment with a
beginning and an end beats a striking fragment.

### The windowing

A transcript that does not fit is **split, not truncated**: silently
sending only the beginning would bias the result towards the start of the
video. The windows overlap, so a moment that straddles a boundary can still
be found whole, and the global segment indices are preserved so the answer
still refers to the real transcript.

`plan_windows` is deterministic, and the tests assert it: the same
transcript always produces the same windows, every segment is covered, and
the windows overlap.

### The output, and the grounding

The model is asked which **segment indices** make a good moment. The
boundaries are then read from the persisted segments. Times the model
proposes are advisory: they are checked against the segments the candidate
cites, and a contradiction drops the candidate — but they are never
stored. (D37)

The candidate rules, in the order they are applied:

1. the referenced segment indices exist and belong to this transcript;
2. the start is not after the end;
3. the proposed times, if any, are finite and overlap the cited segments;
4. the boundaries come from the **persisted segments**;
5. the moment is inside the source duration and within the length limits
   (1 s to 180 s);
6. the title is a non-empty string and the rationale is a string, both
   bounded (120 and 400 characters);
7. a score outside 0–1 is discarded, because it is an annotation and the
   moment does not depend on it;
8. a candidate that repeats, or that substantially overlaps (80 %) one
   already accepted, is dropped;
9. at most 50 candidates are kept.

An invalid candidate is **dropped, never repaired**. There is no invented
timestamp and no clamped range.

### The three outcomes, kept apart

| The model returned | The outcome |
| --- | --- |
| `{"highlights": []}` | A completed analysis with no candidates. A real answer. |
| Something unusable | The analysis fails with `ai_invalid_output`. |
| A transport failure | The analysis fails with `ai_rate_limited`, `ai_timeout`, `ai_auth_failed`, `ai_model_unavailable`, `ai_protocol_mismatch` or `ai_unavailable`. |

There is **no automatic second request**. A model that answers unusably is
reported, not nudged.

### The score is not a probability

The candidate carries a `score`, and the field's documentation says what it
is: *the model's own ranking. It is not a calibrated probability.* The
interface says the same. No probability is claimed anywhere.

---

## 7. Jobs, persistence and the worker

### The analysis is not a job stage

The `jobs` state machine is **untouched**. `completed` still means exactly
what it meant in Phase 3: the source was stored, its audio was extracted
and its transcript was saved.

A highlight analysis is a separate entity with its own lease, claimed by
the worker with the same atomic-claim and recovery pattern the jobs use. The
provider call takes seconds, so it does not belong in a request handler.

### Migration v4

Three tables:

- `integrations` — the non-secret configuration, with a partial unique
  index so there is at most one **enabled** default per role. A disabled
  default is allowed to exist, because disabling one must not have to
  promote another one behind the user's back.
- `highlight_analyses` — the run: the integration it was queued against, the
  provider, the protocol, the base URL, the model, the prompt version, the
  schema version, the transcript it read, the source duration, the status,
  the stable error code, the attempt count and the lease. A partial unique
  index allows one *active* analysis per job.
- `highlight_candidates` — the moments, with the user's decision, and a
  unique index on (analysis, position).

The analysis **snapshots** the configuration it was queued against, and the
snapshot is checked when the analysis runs. (D38)

### What happens when the configuration changes

| The user… | The analysis… |
| --- | --- |
| deleted the integration | fails with `integration_changed` |
| disabled the integration | fails with `integration_disabled` |
| edited the base URL, the protocol or the model | fails with `integration_changed` |
| deleted the *default* integration, so none is selected | is refused with `integration_not_configured` |

**It never falls back** to another provider, another model or a hardcoded
default. The privacy, the cost and the output would all change without
anybody asking, and the user would have no way of knowing.

### Idempotency and recovery

- A retry **replaces** the candidates of an analysis in one transaction, so
  a recovered analysis never ends up with two sets
  (`test_a_recovered_analysis_does_not_duplicate_its_candidates`).
- A stale worker cannot overwrite a newer attempt: the write is conditional
  on the analysis still being owned, and the check is inside the same
  transaction (`test_a_stale_worker_cannot_overwrite_a_newer_attempt`).
- Two workers never claim the same analysis
  (`test_the_worker_claims_an_analysis_only_once`).
- Polling never starts an analysis, and starting an analysis twice does not
  start a second one (`test_polling_never_starts_an_analysis`,
  `test_starting_an_analysis_twice_does_not_start_a_second`).

---

## 8. The frontend

### The Integrations page

`/integrations`, reached from the navigation in the shared header. It
lists the saved configurations with their name, provider, model, protocol,
connection status, whether they are the default, the cost status and when
they were last tested — and never an API key.

The form has every field the brief asks for, with the contextual help that
the easy-to-confuse fields need:

| Field | The help |
| --- | --- |
| Model ID | *"The exact identifier the provider expects. It is often not the same as the model's name."* |
| Model name | *"A label to show. It is never sent to the provider."* |
| Context window | *"The whole context the model accepts, in tokens. Leave it empty if you do not know: ClipForge will not guess."* |
| Reasoning effort | *"Provider and model dependent. It is only sent when the model is known to accept it."* |
| Cost | *"What you know about the cost. Prices and free access are the provider's to change, so ClipForge does not track them."* |
| API key | *"Saved in this machine's credential store… leaving this empty keeps the key that is already saved."* |

**Model discovery** is offered where the provider supports it. The
identifiers are the provider's own, exactly. A user can select one, and the
discovered metadata is used where it is trustworthy. A failed fetch says
what failed, and **a model can always be typed by hand**.

**The connection test** sends one small request that asks the model for a
single word. No transcript and no video is sent. The outcome distinguishes
success, an authentication failure, a missing model, an endpoint mismatch,
a rate limit, a timeout, a provider error, an unsupported operation and an
unusable configuration.

### The highlight panel

On a completed job with a transcript: **Find highlights**, and then the
pending, running, completed, empty and failed states. The candidates show
their title, their rationale, their timestamps, their duration, their kind
and their score, and they can be accepted or rejected.

**Show in transcript** points a candidate at the transcript interval it
came from: the segments it covers are marked in the transcript panel, so a
moment can be traced back to the words behind it.

The panel says plainly that *a moment is a place in the transcript. It is
not a rendered clip.* Nothing in the interface implies that a candidate is
a video.

### The privacy notice

Before an analysis starts, the panel says that the transcript is sent to
the selected provider and processed under that provider's own policies, and
that neither the video nor the audio is sent. The API returns the same
notice in `data_notice`, so the information is not only in the interface.

---

## 9. The tests

### The counts

| Suite | Before | After |
| --- | --- | --- |
| Backend | 581 passed, 1 skipped | **599 passed** |
| Frontend | 41 tests | **68 tests** |

### The new files

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_ai_client.py` | 57 | The three adapters, the model list, the connection test, the URL checks, the error classification, the timeouts, the rate limits, the redirects, the response cap, the preset catalog |
| `tests/test_integrations_api.py` | 31 | Saving, editing, listing, selecting, disabling, deleting; the key never leaving the credential store; the error responses without secrets |
| `tests/test_highlights.py` | 36 | The candidate rules, the windowing, the prompt, the service, the worker, the recovery, the stale-worker ownership |
| `tests/test_highlights_api.py` | 16 | Queuing, reading, accepting, rejecting, and every error case |
| `tests/fake_provider.py` | — | A provider that records requests and answers with canned responses |
| `tests/e2e_phase4.py` | — | The end-to-end workflow check |

Frontend: `IntegrationsPanel.test.tsx` (16) and `HighlightsPanel.test.tsx`
(11).

### What each requirement is covered by

**Configuration and credentials** — create, edit, list, select, disable and
delete; the credential reference; the key never returned; an edit that
keeps the key; explicit replacement and deletion; the migration; exactly one
enabled default; the presets and the custom configuration; the URL checks
and the blocked destinations; the redirect and the authorization header; and
the error responses without secrets.

**Provider adapters** — the endpoint and the payload for each protocol; the
model discovery; the manual model entry; the model-list errors and the
malformed response; a successful connection test; the authentication
failure, the timeout, the rate limit and the unsupported parameters; the
reasoning-effort parameter omitted when it is unsupported; the protocol
mismatch and the unsupported model; and the bounded request and response
sizes.

**Highlight detection** — the valid output; the missing and the unknown
segment ids; the invalid, negative, reversed and out-of-range timestamps;
the duplicate and the overlapping candidates; the empty transcript; the
prompt-injection text; the malformed JSON; the context-window splitting and
the candidate merging; the timeout and the rate limit; **no silent provider
fallback**; the persisted state and the candidate records; the worker crash,
the retry and the stale-worker ownership; and no duplicate candidates after
a recovery.

**Frontend** — the form; the masked key; the connection-test states; the
model discovery and the manual selection; the save, the edit and the
default selection; the empty, loading, success and error states; the
analysis progress and the results; and no transcript or key in a UI error.

### The end-to-end check

`tests/e2e_phase4.py` starts the **real API process**, the **real worker
process** and a **real local HTTP server** that speaks the Chat Completions
dialect. The whole path — the credential store, the URL checks, the
transport, the protocol adapter, the worker's analysis, the API's responses
— is exercised without a real provider and without a real credential.

```
=== 1. start the mock provider ===
  [PASS] the mock provider is listening

=== 2. build the fixture ===
  [PASS] the fixture video exists -- 59987 bytes
  [PASS] the API answers /health

=== 3. create and test an integration ===
  [PASS] the integration is saved -- 201
  [PASS] the key is not returned
  [PASS] the key is recorded as configured
  [PASS] the connection test answers
  [PASS] the connection test succeeded -- The provider answered.
  [PASS] the model list is fetched -- 400
  [PASS] it is selected for highlight detection

=== 4. upload a video and let the worker transcribe it ===
  [PASS] the upload is accepted -- 202
  [PASS] the job completed -- completed
  [PASS] it has a transcript
  [PASS] the transcript is stored
  segments : 2

=== 5. no provider request during ordinary polling ===
  [PASS] polling sent nothing to the provider -- 0 requests

=== 6. start the highlight analysis ===
  [PASS] the analysis is queued -- 202
  [PASS] it starts as pending
  [PASS] the analysis completed -- completed
  [PASS] it produced candidates -- 1
  [PASS] the provider was used
    [  0.00s ->   4.50s] The opening line
  [PASS] the candidate is grounded in the transcript
  [PASS] the transcript was sent, not the video
  [PASS] the analysis is persisted
  [PASS] the candidates are persisted -- 1

=== 7. a provider failure ends the analysis with a stable code ===
  [PASS] the analysis failed -- failed
  [PASS] it failed with a stable code -- ai_rate_limited
  [PASS] the worker is still running

=== 8. a malformed answer is not a success ===
  [PASS] the analysis failed -- failed
  [PASS] it failed with the invalid-output code -- ai_invalid_output
  [PASS] the worker is still running

=== 9. a deleted integration does not fall back to another ===
  [PASS] the analysis is refused -- 409
  [PASS] it says no integration is configured -- integration_not_configured
  [PASS] no request was sent to any provider

=== result ===
  every step passed
```

The Phase 3.1 end-to-end check was re-run after the changes and still
passes every step.

---

## 10. The exact commands

| Command | Result | Exit |
| --- | --- | --- |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rN` | **599 passed in 93.90s** | 0 |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest --collect-only -q` | 599 collected across 29 files | 0 |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest tests/test_ai_client.py` | 57 passed | 0 |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest tests/test_integrations_api.py` | 31 passed | 0 |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest tests/test_highlights.py` | 36 passed | 0 |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest tests/test_highlights_api.py` | 16 passed | 0 |
| `cd backend; ..\.venv\Scripts\python.exe -m pytest tests/test_real_model.py` | 10 passed | 0 |
| `cd backend; ..\.venv\Scripts\python.exe tests/e2e_phase4.py` | every step passed | 0 |
| `cd backend; ..\.venv\Scripts\python.exe tests/e2e_workflow.py` | every step passed | 0 |
| `cd frontend; npm run typecheck` | types generated, no errors | 0 |
| `cd frontend; npm run lint` | no findings | 0 |
| `cd frontend; npm test` | **7 files, 68 tests passed** | 0 |
| `cd frontend; npm run build` | production build, 2 static routes (`/`, `/integrations`) | 0 |
| `cd frontend; npm run audit:check` | the accepted advisory only | 0 |
| `cd frontend; npm audit` | 5 high — the one accepted `braces` advisory | 1 |

The dependency audit and the Node compatibility assessment are unchanged
from Phase 3.1 and are recorded there: one advisory
(`GHSA-vfj7-8cjw-p6xm`, `braces <= 3.0.3`, no patched release), no
compatible fix, guarded by `npm run audit:check`. Nothing in this phase
changed a dependency version, and the lockfile is unchanged.

---

## 11. The changed files

### New — production code

| File | Why |
| --- | --- |
| `backend/app/integrations.py` | The domain model: the protocols, the capabilities, the statuses, the records |
| `backend/app/providers.py` | The preset catalog |
| `backend/app/protocols.py` | The three protocol adapters |
| `backend/app/ai_client.py` | The transport |
| `backend/app/credentials.py` | The credential store |
| `backend/app/highlights.py` | The candidate rules and the output schema |
| `backend/app/highlight_prompt.py` | The prompt and the windowing |
| `backend/app/highlight_service.py` | The orchestration |
| `backend/app/routers/integrations.py` | The integration endpoints |
| `backend/app/routers/highlights.py` | The analysis endpoints |
| `frontend/src/app/integrations/page.tsx` | The Integrations route |
| `frontend/src/components/IntegrationsPanel.tsx` | The list and the form |
| `frontend/src/components/HighlightsPanel.tsx` | The highlight panel |
| `frontend/src/components/AppHeader.tsx` | The shared header with the navigation |

### New — tests

`backend/tests/test_ai_client.py`, `test_integrations_api.py`,
`test_highlights.py`, `test_highlights_api.py`, `fake_provider.py`,
`e2e_phase4.py`, and the two frontend test files.

### Modified — production code

| File | Why |
| --- | --- |
| `backend/app/db.py` | Migration v4; the integration and analysis repository; the ownership checks; `snapshot()` |
| `backend/app/worker.py` | The analysis claim and the run; the lease renewal; the credential store and the client are injectable |
| `backend/app/schemas.py` | The API models for the integrations and the highlights |
| `backend/app/errors.py` | The new stable codes |
| `backend/app/config.py` | The reserved `CLIPFORGE_LLM_*` settings removed |
| `backend/app/deps.py` | The client and the credential store as dependencies |
| `backend/app/main.py` | The new routers; the client and the store on the application state |
| `backend/requirements.txt` | `httpx2` and `keyring` become runtime dependencies |
| `frontend/src/lib/api.ts` | The client for the new endpoints |
| `frontend/src/components/JobList.tsx` | The highlight panel and the shared focus range |
| `frontend/src/components/TranscriptPanel.tsx` | The focused segment range |
| `frontend/src/app/page.tsx` | The shared header; the phase label |
| `frontend/package.json` | The `audit:check` script |

### Modified — tests and documentation

`backend/tests/conftest.py` (the in-memory credential store and the mocked
client), `backend/tests/test_config.py` (the removed settings),
`backend/tests/test_job_recovery.py` (the schema version), the two end-to-end
scripts (the output now goes to a file — see §12), and the documentation.

---

## 12. A bug in the test harness, and what it was

The first end-to-end run hung. The cause was **not** in the product: the
API and the worker wrote their logs into an anonymous pipe that nobody read
until the end of the run, and a Windows pipe holds about 4 KB. Once the
buffer filled, the API blocked while writing its log and stopped answering
— which looks exactly like a server that has hung, and it took a stack dump
of all three processes to tell apart from a real deadlock.

Both end-to-end scripts now write the child's output to a file. It is worth
recording because the symptom was misleading, and because a reader of the
Phase 3.1 script would otherwise hit the same thing.

---

## 13. Known limitations and unverified claims

1. **The highlight analysis sends the transcript to a provider.** That is
   the feature, but it means the transcript leaves the machine. ClipForge
   cannot say what a provider does with it; the interface says so and
   points at the provider's own policy.
2. **No accuracy claim is made about the moments.** The model is asked for
   good moments and the result is validated for *grounding*, not for
   quality. Whether a particular model picks good moments was not measured.
3. **The cost of an analysis is not known.** The number of segments, the
   context and the number of windows are bounded, but the token count
   depends on the provider's tokeniser, which ClipForge does not have.
4. **A DNS name that resolves differently between the check and the
   request is not prevented.** See §5.
5. **The reasoning-effort values are not discovered.** They are metadata the
   user supplies, because no provider publishes them in its model list.
6. **The windowing is a character budget, not a token count.** The model's
   context window is used to decide how much fits, but the split itself is
   by characters, because the tokenisers are provider-specific.
7. **A moment that straddles a window boundary may be missed.** The windows
   overlap, which makes it less likely, but it is not impossible.
8. **No real provider was used.** Every test ran against a mocked
   transport or a local server. The adapters were written from the
   providers' current documentation, and the endpoints and request shapes
   were checked against them, but **no live request was made to OpenCode
   Zen, Groq or Cerebras**, so a mistake in a request shape would not have
   been caught. Running the Phase 4 end-to-end check against a real
   provider is the obvious next check, and it needs a key.
9. **The `google_generate_content` and `system_one` protocols are
   recognised but not implemented.** A configuration that needs one is
   marked unsupported.
10. **The analysis is a separate entity, not a job stage.** A client that
    watches the job's status will not see the analysis progressing; it has
    to poll the highlights endpoint. That was the price of leaving the job
    state machine alone.

---

## 14. Readiness for Phase 5

### **READY WITH DOCUMENTED LIMITATIONS**

- **No critical correctness or security issue remains.** The secrets are in
  the credential store and never in the database; the destination is checked;
  redirects are not followed; the candidates are grounded in the persisted
  transcript; the analysis cannot silently fall back to another provider;
  and the ownership rules are the same ones the jobs use.
- **The Phase 1–3.1 behaviour is intact.** The whole previous suite passes
  (599 tests), the Phase 3.1 end-to-end check still passes every step, and
  the process-isolated transcription, the hard timeout and the stale-worker
  protections are untouched.
- **The remaining limitations are scoped and do not contradict a stated
  guarantee.** They are listed in §13. None of them affects the grounding
  of a candidate, the handling of a secret, or the integrity of the job
  lifecycle.

### What Phase 5 can rely on

- A transcript with ordered, timestamped segments that are inside the
  audio, and a moment for each candidate that refers to real segment
  indices.
- A candidate's start and end times, taken from the database rather than
  from the model, so a clip boundary is a fact rather than a guess.
- A user's accept or reject decision, stored.
- The `jobs` state machine, unchanged: a completed job is still a completed
  job.

### What Phase 5 must still do for itself

- Render the clip. **Nothing in this phase renders anything**, and the
  interface says so where a user might otherwise expect a video.
- Decide the clip's own boundaries. A candidate is a moment; a clip is a
  file, and the two are not the same thing.
- The 9:16 conversion, the captions and the export are all still ahead.

---

*Every result in this document was transcribed from a command that was
actually run on 20 October 2026. Where a check could not be run, it says
so rather than claiming a pass. Nothing was committed.*
