# ClipForge frontend

Next.js 16 (App Router) · React 19 · TypeScript · Tailwind CSS 4

The user-facing part of [ClipForge](../README.md). It talks to the local
FastAPI API and nothing else — no provider keys, no database, no FFmpeg.

```powershell
npm install
Copy-Item .env.example .env.local
npm run dev          # http://localhost:3000
```

## Scripts

| Script | Does |
| --- | --- |
| `npm run dev` | Dev server with hot reload |
| `npm run build` | Production build |
| `npm run start` | Serve the production build |
| `npm run lint` | ESLint |
| `npm run typecheck` | `next typegen && tsc --noEmit` |
| `npm test` | Vitest, single run |
| `npm run test:watch` | Vitest, watch mode |

## Layout

```
src/
├── app/
│   ├── layout.tsx        # metadata, fonts, page shell
│   ├── page.tsx          # landing / dashboard shell
│   └── globals.css       # Tailwind theme tokens
├── components/
│   ├── HealthIndicator.tsx  # live backend status
│   ├── JobList.tsx          # job list, polls while a job is running
│   ├── PipelinePreview.tsx  # the five-step plan
│   └── VideoSourcePanel.tsx # upload and YouTube forms
└── lib/
    ├── api.ts            # typed client for the ClipForge API
    ├── config.ts         # NEXT_PUBLIC_API_BASE_URL handling
    ├── health.ts         # health-check logic, independent of React
    └── jobs-event.ts     # a one-line event bus between components
```

## Configuration

`NEXT_PUBLIC_API_BASE_URL` — the base URL of the ClipForge API. Default:
`http://localhost:8000`.

`NEXT_PUBLIC_*` values are inlined into the browser bundle at build time, so
restart the dev server after changing one, and never put a secret in them.

## Behaviour worth knowing

- The upload uses `XMLHttpRequest` rather than `fetch`, because only
  `XMLHttpRequest` reports upload progress. A multi-gigabyte upload with no
  progress feedback looks like a hang.
- The job list polls every 3 seconds **only while at least one job is
  non-terminal**, and stops as soon as every job has finished. A manual
  *Refresh* button is always available.
- A form that queues a job dispatches a `clipforge:jobs-changed` event so the
  list refreshes immediately, instead of waiting for the next poll.

## Tests

`npm test` runs Vitest against `src/**/*.test.ts(x)`. The health-check logic
and the API client are tested with a stubbed `fetch` and a fake
`XMLHttpRequest`; the components are tested for their loading, success and
failure states. No test touches the network.

See [../docs/TESTING.md](../docs/TESTING.md) for the full picture.
