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
| `npm run audit:check` | `npm audit` filtered to the accepted advisories — it **fails today**: see [../AGENTS.md](../AGENTS.md) |

## Layout

```
src/
├── app/
│   ├── layout.tsx             # the shell: header, navigation, footer
│   ├── page.tsx               # dashboard: add a video + the library
│   ├── globals.css            # the design tokens every component uses
│   ├── jobs/[jobId]/page.tsx  # one video's workflow (a blocking route)
│   ├── status/page.tsx        # readiness, worker heartbeat, capabilities
│   └── integrations/page.tsx  # the AI provider configurations
├── components/
│   ├── AppHeader.tsx          # navigation and the live health indicator
│   ├── HealthIndicator.tsx    # the backend dot in the header
│   ├── VideoSourcePanel.tsx   # the upload and YouTube forms
│   ├── JobList.tsx            # the library; polls while a job is running
│   ├── JobWorkflow.tsx        # source → transcript → moments → clips → short-form
│   ├── TranscriptPanel.tsx    # the timestamped transcript
│   ├── HighlightsPanel.tsx    # the analysis and its candidates
│   ├── ClipPanel.tsx          # render a clip from an accepted candidate
│   ├── ClipEditor.tsx         # title, trim into a revision, caption overrides
│   ├── FormatPanel.tsx        # portrait formatting with burned-in captions
│   ├── ClipGrid.tsx           # a job's clips and their short-form versions
│   ├── MediaPreview.tsx       # a preview that says so when it cannot play
│   ├── ExportButton.tsx       # the ZIP of the finished clips
│   ├── ServiceStatusPanel.tsx # the status view
│   ├── IntegrationsPanel.tsx  # provider configurations and their keys
│   ├── PipelinePreview.tsx    # the original five-step plan (no longer mounted)
│   └── ui/                    # Button, StatusBadge, Stepper, EmptyState,
│                              # ErrorState, WorkflowIndicator — the shared
│                              # primitives that keep one vocabulary
└── lib/
    ├── api.ts            # the one typed client for the ClipForge API
    ├── config.ts         # NEXT_PUBLIC_API_BASE_URL handling
    ├── health.ts         # health-check logic, independent of React
    ├── jobs-event.ts     # a one-line event bus between components
    ├── source-display.ts # how to talk about a job's source
    └── use-polling.ts    # the polling rule every view follows
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
- **One API client.** No component calls `fetch()` directly and there is no
  `any` in `src/`; `lib/api.ts` owns every request.
- Answers are **normalised at the client boundary**, so a surprising payload
  becomes an `unknown` status or a clamped progress figure rather than a blank
  panel.
- The API's `media_url` is **relative to the API**, not to this app. Pass it
  through `mediaUrl()` before putting it in a `src` or an `href`; the raw value
  points at the frontend's port and 404s.
- `/jobs/[jobId]` is a **blocking route** (`export const instant = false`).
  Under Next 16's Cache Components its id is only known at request time, so it
  cannot be prerendered, and a shared layout that reads the pathname with
  `usePathname()` would suspend and fail the build.
- Colours, spacing and type come from the tokens in `app/globals.css`. The
  header, navigation and footer live in `app/layout.tsx`; a page decides only
  what is in the middle.

## Tests

`npm test` runs Vitest against `src/**/*.test.ts(x)`. The health-check logic
and the API client are tested with a stubbed `fetch` and a fake
`XMLHttpRequest`; the components are tested for their loading, success and
failure states. No test touches the network.

See [../docs/TESTING.md](../docs/TESTING.md) for the full picture.
