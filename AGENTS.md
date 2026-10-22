# QuantDinger Repository Instructions

This is the canonical instruction file for coding agents working in this repository. Keep tool-specific entrypoints thin and do not duplicate these rules elsewhere.

## Repository map

- `backend_api_python/`: Flask API, services, trading runtime, migrations, and backend tests.
- `mcp_server/`: the tenant-scoped MCP client/server package backed by QuantDinger APIs.
- `docs/`: product, architecture, deployment, API, Agent Gateway, and trading documentation.
- `docker-compose*.yml` and `scripts/`: deployment topology and lifecycle tooling.
- The main product frontend belongs to the separate **QuantDinger-Vue** repository (Vue 2.7 / Ant Design Vue). Supply its checkout through `FRONTEND_SRC_PATH`; the local-build override defaults to `./QuantDinger-Vue`. A locally nested checkout remains a separate repository and must not be committed here.
- `frontend-factor-mining/`: the tracked, standalone Vue 3 / Element Plus factor-mining UI. It is retained for compatibility and is not the main product SPA or the default Compose frontend image. New product UI work targets QuantDinger-Vue; do not create another frontend to avoid changing the product UI.
- `doc/web_doc/`: numbered frontend delivery, route, component, contract, test, and acceptance documentation. Start at [the frontend module map](doc/web_doc/00-index.md).

## Read by task

Read only the documentation relevant to the change:

- When project-local skills are installed, read `.agents/README.md` for their trigger rules and client entrypoints. Skills supplement these repository instructions; they do not replace architecture, trading safety, or verification requirements. Load only the skills needed for the current task.

- Agent Gateway or MCP: [Agent documentation](docs/agent/README.md), [Agent OpenAPI](docs/agent/agent-openapi.json), and [MCP package documentation](mcp_server/README.md).
- Strategy API, backtests, or trading behavior: [Strategy development guide](docs/trading/STRATEGY_DEV_GUIDE.md) and [live-trading safety](docs/trading/LIVE_TRADING_SAFETY.md).
- Runtime ownership, concurrency, Kafka, workers, or durable state: [architecture index](docs/architecture/README.md) and the task-specific document it links.
- HTTP contracts: [API conventions](docs/architecture/API_CONVENTIONS.md) and the applicable OpenAPI document.
- Installation or operations: the root [README](README.md) and the applicable guide under `docs/deployment/`.
- User-visible changes or UI-consumed API changes: [frontend module map](doc/web_doc/00-index.md), [state and API contracts](doc/web_doc/03-state-and-api.md), [information architecture](doc/web_doc/09-information-architecture.md), [layout and visual baseline](doc/web_doc/10-layout-and-visual-baseline.md), and the affected module document. Factor mining uses [module 06](doc/web_doc/06-factor-mining.md).

## Frontend delivery

- Assess frontend impact for changes to pages, components, interactions, or UI-consumed APIs. Implement affected consumers together; if unaffected, record consumer and compatibility evidence instead of making unnecessary page changes.
- Plan each user flow from entry through input, submission, API, result, and refresh verification. Include its route, reused/new components, fields/errors/authentication/pagination, five UI states, i18n, responsive behavior, and accessibility. If no persistence is needed, state the correct refresh behavior. Use the [delivery workflow](doc/web_doc/07-delivery-workflow.md) and [template](doc/web_doc/07-feature-delivery-template.md).
- Keep API, UI, and contract coverage separate. An API implementation or backend test does not demonstrate that its controls are available in the product UI.
- Assign frontend and backend implementation against the same contract; inspect both actual source roots. Use the agent roles available in the current environment, and have a browser verifier check the connected flow.
- Reuse the target frontend's existing components, tokens, request/authentication wrapper, and localization. Do not mix the standalone Vue 3 UI with the main Vue 2 product conventions.
- Read the affected numbered module document before changes and update it in the same batch. Numbers only increase; these documents describe current behavior. Put progress and evidence in the plan or acceptance report.
- Preserve the standalone UI until a replacement has passed its documented parity, browser, and deployment checks and removal is explicitly scoped. Retiring an obsolete description does not authorize deleting source.

## Architecture and contract invariants

- Preserve the process ownership documented in the architecture index. Do not move persistent trading loops into the HTTP backend or let evaluator processes submit exchange orders directly.
- PostgreSQL is the durable source of truth. Kafka transports versioned events; Redis cache data is evictable, while the jobs Redis has a separate durability role. Do not silently substitute one for another.
- Keep tenant isolation, idempotency, leases, fencing, and audit behavior intact when changing distributed or trading workflows.
- `/api/agent/v1` is the Agent Gateway boundary. Update `docs/agent/agent-openapi.json` and its alignment tests whenever that HTTP contract changes.
- MCP capabilities must retain the same authentication, scopes, idempotency, limits, and live-trading safeguards as the backing API.

## Safety and repository hygiene

- Never commit real secrets, production `.env` files, API keys, or database passwords. Use `.env.example` and placeholders.
- Do not weaken live-trading safeguards or bypass explicit authorization and human review unless the user requests and scopes that change.
- Do not add upgrade-time data rewrites for one-off local cleanup. Use an explicit, reviewed migration only when shipped user data must change.
- Preserve unrelated working-tree changes.
- Use English for source-code comments. Keep user-facing application copy in the existing localization system rather than hardcoding it in source files.
- Keep machine-readable contracts and identifiers in English. When a human guide has English and `_CN` editions, update both when the change affects both audiences.
- Write every commit message in Chinese, keeping the Conventional Commits type token in English (for example `fix: 修复…`), so changelog tooling and history scans keep working.

## Verification

- Run focused backend tests from `backend_api_python/` with `python -m pytest tests/<test_file>.py -q`.
- Agent contract coverage lives in `backend_api_python/tests/test_ai_agent_contract_alignment.py` and related `test_agent_*.py` files.
- MCP tests live in `mcp_server/tests/`.
- Validate Compose changes with `docker compose config --quiet` before exercising the affected services.
- Match verification depth to risk; trading, migrations, tenancy, and distributed ownership require targeted regression tests.
- Follow the [frontend test map](doc/web_doc/04-test-map.md) and [acceptance checklist](doc/web_doc/05-acceptance-checklist.md). Run non-fixing lint, typecheck, unit tests, build, and actual browser verification for UI changes. Missing source, scripts, or test infrastructure are blocking evidence, not passing checks.
- Preserve the distinction between mock UI checks, test-backend integration, and a deployed image. Record the frontend target/revision, route, desktop/mobile screenshots, console/network findings, accessibility results, and uncovered items. Report backend, frontend, integration, and deployment status separately.
