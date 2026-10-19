# Agent documentation (English)

This folder holds **agent-facing** material for coding assistants (Cursor, Claude Code, Codex, CLI bots, etc.).

| Document | Purpose |
|----------|---------|
| [MCP_SETUP.md](MCP_SETUP.md) | Wire Cursor / Claude Code / Codex / remote agents to a QuantDinger backend via the `quantdinger-mcp` MCP server (local stdio + remote HTTP) |
| [AGENT_QUICKSTART.md](AGENT_QUICKSTART.md) | Operator + integrator walkthrough: issue a token, call the Gateway, run paper trades |
| [cursor-mcp.example.json](cursor-mcp.example.json) | Minimal Cursor MCP configuration with replaceable endpoint and token placeholders |
| [agent-openapi.json](agent-openapi.json) | Machine-readable contract for `/api/agent/v1` (OpenAPI 3.0) |
| [../architecture/API_CONVENTIONS.md](../architecture/API_CONVENTIONS.md) | Shared HTTP conventions (envelopes, auth, Public/Internal tiers) |
| [../api/openapi.yaml](../api/openapi.yaml) | Human Web API spec (flask-smorest; migration in progress) |

## Permission model

| Scope | Purpose | Typical operations |
|-------|---------|--------------------|
| `R` | Read | Market data, research, account and run state |
| `W` | Configuration writes | Save strategy sources, create stopped deployments |
| `B` | Backtest / simulation compute | Submit or cancel backtests and factor-mining runs |
| `N` | Notifications | Manage and trigger signal alerts |
| `T` | Trading and runtime | Stop strategies, place orders, emergency stop |
| `C` | Admin | Administrators only |

Factor-mining submission and cancellation use `B`; vocabulary, estimates, jobs, proposals, and candidate reads use `R`.

Token permissions never bypass server-side live-trading controls, instrument allowlists, notional caps, expiry, rate limits, or auditing. Every `W/B/N/T` request with side effects should carry a unique `Idempotency-Key`.

**Language policy:** Machine-readable schemas, route names, scopes, environment variables, and tool identifiers remain in English as the canonical contract. Human setup guides are maintained in paired editions: [中文入口](README_CN.md) and this English entry.

Repository-level coding-agent instructions live in [`AGENTS.md`](../../AGENTS.md). Tool-specific entrypoints should load that file instead of duplicating repository rules under this documentation directory.
