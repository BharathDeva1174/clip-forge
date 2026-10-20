# Development guide

Windows PowerShell, exact commands. Every command says which directory to run
it in.

| Directory | What lives there |
| --- | --- |
| repository root (`clipforge\`) | Docs, `.gitignore`, `.editorconfig`, `.env.example` |
| `backend\` | The FastAPI application, its tests and its `requirements*.txt` |
| `frontend\` | The Next.js application and its tests |

Commands are shown for PowerShell. In `cmd.exe` use `copy` instead of
`Copy-Item`, and in bash use `cp`.

---

## 1. First-time setup

### 1.1 Check the toolchain

```powershell
node --version
npm --version
python --version
```

Requirements: Node.js 20.9 or newer (22 LTS / 24 LTS recommended), npm 10 or
newer, Python 3.11 or newer.

### 1.2 Backend

```powershell
cd clipforge\backend

python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements-dev.txt

Copy-Item .env.example .env
```

> If `Activate.ps1` is blocked, run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the same
> terminal and activate again. You can also skip activation entirely and use
> `..\.venv\Scripts\python.exe` in place of `python`.

### 1.3 Frontend

```powershell
cd clipforge\frontend

npm install

Copy-Item .env.example .env.local
```

### 1.4 Sanity check

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python -m pytest

cd ..\frontend
npm run typecheck
npm run lint
npm test
```

---

## 2. Running the applications

Two terminals. Run the backend first so the frontend has something to talk to.

### Terminal 1 — backend

```powershell
cd clipforge\backend
..\.venv\Scripts\Activate.ps1
python run.py
```

Expected output:

```
Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Equivalent explicit form (useful for changing host or port):

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`--reload` restarts the server when a `.py` file changes. Stop it with
`Ctrl+C`.

### Terminal 2 — frontend

```powershell
cd clipforge\frontend
npm run dev
```

Expected output:

```
- Local:  http://localhost:3000
✓ Ready in ...
```

Stop it with `Ctrl+C`.

### URLs

| URL | What |
| --- | --- |
| http://localhost:3000 | Frontend |
| http://localhost:8000/health | `{"status":"ok","service":"clipforge-api"}` |
| http://localhost:8000/ready | Readiness + capability flags |
| http://localhost:8000/api/v1 | API metadata |
| http://localhost:8000/docs | OpenAPI explorer |

### Production build of the frontend

```powershell
cd clipforge\frontend
npm run build
npm run start
```

`npm run start` serves the production build on http://localhost:3000.

---

## 3. Everyday commands

### Backend (from `clipforge\backend\`)

```powershell
..\.venv\Scripts\Activate.ps1

python -m pytest                    # all tests
python -m pytest -v                 # verbose
python -m pytest tests/test_health.py
python -m pytest -k "cors"          # by name
python -m uvicorn app.main:app --reload --port 8000
```

Add a dependency:

```powershell
pip install <package>
# then update backend/requirements.txt with the pinned version, and
# backend/constraints.txt with the new resolution.
```

### Frontend (from `clipforge\frontend\`)

```powershell
npm run dev                         # dev server
npm run build                       # production build
npm run start                       # serve the production build
npm run lint                        # ESLint
npm run typecheck                   # next typegen && tsc --noEmit
npm test                            # Vitest, single run
npm run test:watch                  # Vitest, watch mode
npm run test -- health              # Vitest, filtered by file name
```

Add a dependency:

```powershell
npm install <package>
npm install -D <package>            # dev dependency
```

`package-lock.json` is committed. Commit it whenever `package.json` changes.

---

## 4. Configuration

### Backend — `clipforge\backend\.env`

```
CLIPFORGE_ENVIRONMENT=development
CLIPFORGE_DEBUG=false
CLIPFORGE_LOG_LEVEL=INFO
CLIPFORGE_HOST=127.0.0.1
CLIPFORGE_PORT=8000
CLIPFORGE_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
CLIPFORGE_STORAGE_DIR=data
# CLIPFORGE_LLM_API_KEY=...        # reserved for Phase 4
```

### Frontend — `clipforge\frontend\.env.local`

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

> `NEXT_PUBLIC_*` values are inlined into the browser bundle at build time.
> Restart `npm run dev` (or rebuild) after changing one. Never put a secret in
> a `NEXT_PUBLIC_*` variable.

### Changing the API port

1. Set `CLIPFORGE_PORT=8010` in `backend/.env`.
2. Set `NEXT_PUBLIC_API_BASE_URL=http://localhost:8010` in
   `frontend/.env.local`.
3. Restart both applications.

---

## 5. Troubleshooting

### `python` is not recognised

Install Python 3.11+ and enable *Add python.exe to PATH*, or use the launcher:

```powershell
py -3.11 -m venv .venv
```

### `Activate.ps1` is blocked

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Or skip activation:

```powershell
..\.venv\Scripts\python.exe -m pytest
```

### `Port 8000 is already in use`

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object OwningProcess
Stop-Process -Id <OwningProcess> -Force
```

Or change `CLIPFORGE_PORT` and `NEXT_PUBLIC_API_BASE_URL` as described above.

### The frontend shows "Backend unreachable"

1. Is the backend running? Open http://localhost:8000/health.
2. Does `NEXT_PUBLIC_API_BASE_URL` match the backend's port?
3. Did you change `.env.local` after starting the dev server? Restart it.
4. Is the browser console showing a CORS error? Add the exact browser origin
   to `CLIPFORGE_CORS_ORIGINS`. Wildcards are rejected on purpose.

### `npm run build` fails while downloading fonts

`next/font/google` fetches Geist at build time. Without internet access,
remove the `next/font` imports from `src/app/layout.tsx` and use a system font
stack instead.

### The backend will not start

Read the traceback. The usual causes are a missing dependency
(`pip install -r requirements-dev.txt`), a malformed `.env`, or an invalid
value — a non-numeric `CLIPFORGE_PORT` or a `*` in `CLIPFORGE_CORS_ORIGINS`
both fail fast at startup, by design.

### Tests fail after changing `config.py`

The settings are validated on every load. If you added a required field
without a default, update `backend/.env.example` and the defaults in
`backend/app/config.py`.

---

## 6. Git

The repository was initialised with Git in Phase 1. `backend/.venv`,
`frontend/node_modules`, build output, `.env` files and generated media are
ignored.

```powershell
git status
git add -A
git commit -m "..."
```

Never commit an `.env` file. `git check-ignore -v backend/.env` should print
the rule that ignores it.
