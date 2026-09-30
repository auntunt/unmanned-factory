# Real application operator journey (synthetic rehearsal)

This suite uses the built production frontend served by the real FastAPI app,
real cookie authentication, CSRF, database and task HTTP endpoints. It launches
`scripts/preview_v3.py`, whose clearly labelled scripted provider works on a
disposable local Git repository. No model service, GitHub publication, production
data or external deployment is involved. One test deliberately injects a single
503 response to verify stale-data/retry presentation.

Run on a Linux development/CI host with the project's isolation requirements:
1. `uv sync --frozen --all-extras` at repository root
2. `npm ci && npm run build` in frontend
3. `npx playwright install --with-deps chromium`
4. `npm run test:e2e`

The backend is started automatically on loopback port 8790 and must not already
be running. The existing rehearsal creates only `.factory-preview/` data.
Use a fresh disposable checkout for a repeatable seeded run. The CI job is
independent of unit tests and uploads the HTML report, traces, failure videos
and labelled desktop/mobile screenshots even after failures.

Coverage: login, home setup paths, real overview, history search/filter/reload,
real scripted execution and verification records, failed refresh preserving
records, successful retry, narrow viewport, mobile navigation dismissal and
per-tab draft recovery. This is browser integration coverage, not a claim of
real-model, production-scale or deployment acceptance.
