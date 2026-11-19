# Alpha Finance Radar web

The React application serves the market dashboard, research reports, strategy workspace, chat and administration pages. FastAPI serves the production bundle from `web/dist`.

Run the following commands from `web/`. Install dependencies with `npm ci`.

| Command | Purpose |
| --- | --- |
| `npm run dev` | Start Vite for local development. The development server forwards `/api` requests to the running backend. |
| `npm test` | Run the frontend regression suite with Node's test runner. |
| `npm run lint` | Check frontend code with Oxlint. |
| `npm run build` | Build the production bundle after changing the UI. |

The [project documentation](../docs/README.md) describes individual pages, roles and data sources.
