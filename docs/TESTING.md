# Testing

Two independent suites, one per application. Neither needs an API key, an
internet connection, a GPU or a video file.

| Suite | Runner | Location | Needs the other app running? |
| --- | --- | --- | --- |
| Backend | pytest + httpx (`TestClient`) | `backend/tests/` | No |
| Frontend unit + component | Vitest + React Testing Library | `frontend/src/**/*.test.ts(x)` | No |

---

## 1. Backend tests

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python -m pytest
```

Useful variations:

```powershell
python -m pytest -v                          # one line per test
python -m pytest tests/test_health.py        # one file
python -m pytest -k "cors"                   # by name
python -m pytest --tb=short                  # shorter tracebacks
```

### What each test covers

**`backend/tests/test_health.py`**

| Test | Verifies |
| --- | --- |
| `test_health_returns_200` | `GET /health` answers 200 |
| `test_health_returns_the_exact_contract` | The body is exactly `{"status": "ok", "service": "clipforge-api"}` — no extra keys, no drift |
| `test_ready_reports_a_loaded_configuration` | `GET /ready` answers 200 with `status=ready`, the service name, `config=loaded` and a version |
| `test_ready_does_not_claim_unimplemented_capabilities` | Every capability flag is `false` — the API never claims transcription, rendering or a database exists |
| `test_unknown_route_returns_404` | An unknown path answers 404 |
| `test_cors_allows_the_configured_frontend_origin` | A preflight from `http://localhost:3000` is allowed |
| `test_cors_does_not_allow_an_unknown_origin` | A preflight from an unknown origin gets no allow-origin header |
| `test_responses_never_leak_configured_secrets` | A configured LLM key never appears in any response body |

**`backend/tests/test_api_v1.py`**

| Test | Verifies |
| --- | --- |
| `test_api_v1_returns_200` | `GET /api/v1` answers 200 |
| `test_api_v1_returns_the_expected_metadata` | The body carries the API name, version, stage (`phase-1-foundation`) and the docs path |

**`backend/tests/test_config.py`**

| Test | Verifies |
| --- | --- |
| `test_defaults_are_safe_for_local_development` | The defaults are port 8000, `development`, and the two localhost origins |
| `test_an_invalid_port_is_rejected` | A non-numeric port raises a validation error |
| `test_an_out_of_range_port_is_rejected` | A port above 65535 is rejected |
| `test_a_wildcard_cors_origin_is_rejected` | `*` is rejected at load time, because it is invalid with credentials |
| `test_environment_variables_are_loaded` | `CLIPFORGE_PORT` and `CLIPFORGE_CORS_ORIGINS` are picked up from the environment |
| `test_secret_values_are_masked_when_dumped` | A secret is readable through `get_secret_value()` but absent from `model_dump_json()` and `repr()` |

### Reading failures

| Symptom | Likely cause |
| --- | --- |
| `ModuleNotFoundError: No module named 'app'` | You are not in `backend/`, or `pythonpath` was lost from `backend/pyproject.toml` |
| `ModuleNotFoundError: No module named 'fastapi'` | The virtual environment is not active, or dependencies were not installed |
| `ValidationError` on almost every test | An environment variable in your shell is invalid — check `CLIPFORGE_PORT` and `CLIPFORGE_CORS_ORIGINS` |
| `test_health_returns_the_exact_contract` fails | The `/health` contract changed. That is a breaking API change; update the frontend and the docs together |
| `test_cors_*` fails | `CLIPFORGE_CORS_ORIGINS` in your environment does not include `http://localhost:3000` |

The suite is deterministic: no network, no clock dependency, no shared state
between tests. It runs in well under a second.

---

## 2. Frontend tests

```powershell
cd clipforge\frontend
npm test
```

Useful variations:

```powershell
npm run test:watch               # re-run on change
npm test -- health               # filter by file name
npm test -- --reporter=verbose   # detailed output
```

### What each test covers

**`frontend/src/lib/health.test.ts`** — the health-check logic, with a stubbed
`fetch`:

| Test | Verifies |
| --- | --- |
| `reports an online backend` | A 200 with the expected payload yields the `online` state, and the right URL is called |
| `reports an offline backend when the request fails` | A thrown network error yields `offline` and names the base URL |
| `reports an offline backend for a non-2xx response` | A 503 yields `offline` with the status code in the reason |
| `reports an offline backend for an unexpected payload` | A 200 with the wrong shape yields `offline` |
| `strips trailing slashes from the base URL` | `http://host:8000/` produces `http://host:8000/health` |

**`frontend/src/components/HealthIndicator.test.tsx`** — the rendered component:

| Test | Verifies |
| --- | --- |
| `shows a checking state while the request is in flight` | The loading state is rendered before the answer arrives |
| `reports an online backend` | The component renders *Backend online* and the service name |
| `reports an unreachable backend and recovers when the retry succeeds` | The failure state and its reason are rendered, and pressing *Retry* moves the component to the online state |

The component tests use a deferred promise and a stubbed global `fetch`, so
they are deterministic and never touch the network.

### Reading failures

| Symptom | Likely cause |
| --- | --- |
| `Cannot find module '@/...'` | The `@/*` alias is missing from `vitest.config.mts` |
| `document is not defined` | The environment is not `jsdom` — check `test.environment` in `vitest.config.mts` |
| A test hangs | A promise that never settles; the deferred-promise pattern in the component tests avoids this |
| `getByText` cannot find an element | The rendered copy changed — update the test to the new text, and check the component |

---

## 3. Static checks

Run from `clipforge\frontend\`:

```powershell
npm run typecheck   # tsc --noEmit
npm run lint        # ESLint (flat config, eslint-config-next)
npm run build       # production build
```

`npm run build` also runs TypeScript over the project, so it covers the type
check as well. `next lint` no longer exists in Next.js 16 — ESLint runs
directly.

---

## 4. Integration checks (manual, both apps running)

These are not automated because they need two live processes. They were run
manually in Phase 1; see [PHASE_1_REPORT.md](../PHASE_1_REPORT.md) for the
recorded results.

Start the backend and the frontend, then:

```powershell
# Backend liveness
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing | Select-Object -ExpandProperty Content
# -> {"status":"ok","service":"clipforge-api"}

# Backend readiness
Invoke-WebRequest http://127.0.0.1:8000/ready -UseBasicParsing | Select-Object -ExpandProperty Content

# API metadata
Invoke-WebRequest http://127.0.0.1:8000/api/v1 -UseBasicParsing | Select-Object -ExpandProperty Content

# Unknown route must be 404
Invoke-WebRequest http://127.0.0.1:8000/nope -UseBasicParsing -SkipHttpErrorCheck | Select-Object StatusCode

# Frontend page loads
Invoke-WebRequest http://localhost:3000 -UseBasicParsing | Select-Object StatusCode
```

Then open http://localhost:3000 in a browser and confirm:

- the header shows **Backend online** (green) while the backend runs;
- stopping the backend and reloading shows **Backend unreachable** with a
  *Retry* button;
- restarting the backend and pressing *Retry* returns it to **Backend online**.

---

## 5. What is not tested yet

- No end-to-end browser automation. The Phase 1 integration check was done by
  hand in a real browser; adding a browser-testing framework is planned for
  Phase 9.
- No tests for upload, transcription, LLM calls, rendering or captions — none
  of that exists yet.
- No load or performance tests.
