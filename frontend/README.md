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
| `npm run typecheck` | `tsc --noEmit` |
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
│   ├── HealthIndicator.tsx  # live backend status (client component)
│   ├── PipelinePreview.tsx  # the five-step plan
│   └── VideoSourcePanel.tsx # disabled video input (Phase 2)
└── lib/
    ├── config.ts         # NEXT_PUBLIC_API_BASE_URL handling
    └── health.ts         # health-check logic, independent of React
```

## Configuration

`NEXT_PUBLIC_API_BASE_URL` — the base URL of the ClipForge API. Default:
`http://localhost:8000`.

`NEXT_PUBLIC_*` values are inlined into the browser bundle at build time, so
restart the dev server after changing one, and never put a secret in them.

## Tests

`npm test` runs Vitest against `src/**/*.test.ts(x)`. The health-check logic is
tested with a stubbed `fetch`, and the indicator component is tested for its
loading, success, failure and retry states. No test touches the network.

See [../docs/TESTING.md](../docs/TESTING.md) for the full picture.
