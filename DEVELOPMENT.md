# QuantDinger — Development Guide

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Docker & Docker Compose | 20+ | required for the default setup |
| Python | 3.10+ | only if running backend outside Docker |
| Node.js | 22 LTS | only if you maintain the private QuantDinger-Vue or mobile source repos |

## Quick Start (Docker)

```bash
# 1. Clone
git clone https://github.com/<your-org>/quantdinger.git
cd quantdinger

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY to a random value:
#   SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
# The same project-root `.env` also contains image and port settings.

# 3. Launch
docker compose up -d --build

# 4. Open http://localhost:8888
```

The stack includes:

| Service | Port | Description |
|---------|------|-------------|
| `frontend` | 8888 | Nginx serving Vue SPA |
| `backend` | 5000 | Flask API (gunicorn) |
| `postgres` | 5432 | PostgreSQL 18 |
| `redis` | 6379 | Cache layer (LRU, 128 MB) |

## Project Structure

```
quantdinger/
├── backend_api_python/          # Flask API
│   ├── app/
│   │   ├── config/              # Settings, API keys, DB config
│   │   ├── data_providers/      # Market data fetchers (crypto, forex, …)
│   │   ├── data_sources/        # Native exchange and broker adapters
│   │   ├── routes/              # Flask Blueprints (REST endpoints)
│   │   ├── services/            # Business logic (strategy, trading, AI, …)
│   │   └── utils/               # DB helpers, auth, caching, logger
│   ├── migrations/              # SQL schema + seed data
│   ├── gunicorn_config.py       # Production WSGI config
│   ├── run.py                   # App entrypoint
│   ├── Dockerfile
│   └── requirements.txt
├── docs/                        # Changelog, architecture notes
├── docker-compose.yml           # frontend service pulls ghcr.io/.../quantdinger-frontend
├── docker-compose.ghcr.yml      # backend, frontend, and mobile pulled from GHCR (zero-clone deploy)
└── README.md
```

## Running Backend Locally (without Docker)

```bash
cp .env.example .env
cd backend_api_python
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
# Edit ../.env before starting the backend.
python run.py
```

The dev server starts on `http://localhost:5000` with auto-reload.

### Local stack on Windows (native PostgreSQL + Redis, no Docker)

`run.py` and `app/__init__.py` resolve the configuration as `../.env` when the backend
directory is named `backend_api_python`, so **the repository-root `.env` is the file the
backend reads**, and the Settings page writes back to that same file. A copy at
`backend_api_python/.env` is read by no code path — keep the two identical or delete the
copy, otherwise an edit there looks applied while the process reads the root file.

Local services used by this checkout:

| Service | Endpoint | Notes |
|---------|----------|-------|
| PostgreSQL 17 | `localhost:5432` | native service `postgresql-x64-17`; databases `quantdinger` and `quantdinger_test` |
| Redis | `localhost:6379` | native service `Redis`; cache on DB 0, Celery on DB 1 |
| Backend | `localhost:5000` | `python run.py` from `backend_api_python/` |
| Frontend | `localhost:8000` | Vite dev server (`VITE_DEV_PROXY_TARGET=http://127.0.0.1:5000`) |

`DATABASE_URL` for this stack is `postgresql://postgres:postgres@localhost:5432/quantdinger`
(the `postgres` superuser owns the local database; the `quantdinger` role exists but its
password is not the one any committed file expects).

Local-only switches worth knowing:

- `ENABLED_MARKETS` (CSV) is an allow-list: when set, only the listed markets are visible
  and the legacy `SHOW_CN_STOCK` / `SHOW_HK_STOCK` flags are ignored. Unset, `CNStock` is
  hidden and everything else is visible.
- `CELERY_TASKS_ENABLED=false` (the default) dispatches jobs to the bounded in-process
  thread pool sized by `AGENT_JOBS_MAX_WORKERS`, so agent jobs, backtests, strategy
  evolution and factor mining all run without a Celery worker.
- `FACTOR_MINING_MAX_BUDGET` and `FACTOR_MINING_CONCURRENT_LIMIT` are read by **no code**;
  the budget caps (400 default / 2000 max) and the one-active-job-per-user rule live in
  `app/services/factor_mining/`. Do not put them in `.env` expecting an effect.

#### Smoke-testing factor mining locally

Prerequisites: a `portfolio_strategy` row in `qd_script_sources` (its universe must resolve
at least 3 symbols over the requested range) and an admin JWT.

```bash
curl -s -X POST http://127.0.0.1:5000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"quantdinger","password":"<ADMIN_PASSWORD>"}'      # -> data.accessToken
curl -s http://127.0.0.1:5000/api/factor-mining/catalog   -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://127.0.0.1:5000/api/factor-mining/estimate -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sourceId":1,"market":"USStock","timeframe":"1D","startDate":"2023-01-03","endDate":"2023-06-30","budget":8,"seed":42}'
```

`estimate` returns `{"templateCount":8,"candidateBudget":N}` synchronously and writes
nothing; `POST /run` opens a job row, so use a small `budget` while iterating.

Note that every request message from this PostgreSQL instance arrives localized to GBK
(`lc_messages = 'Chinese (Simplified)_China.936'`), and `psycopg2` raises
`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd6` instead of the server's error.
When a database failure looks like a codec error, read the real one with
`psql -h localhost -U postgres -d quantdinger` or set `lc_messages = 'C'` in `postgresql.conf`.

## Frontend delivery targets

The main product SPA belongs to the separate **QuantDinger-Vue** repository (Vue 2.7 / Ant Design Vue). Its development entry is the Vite `dev` script, with port 8000 by default; its existing source-build path uses pnpm and the tracked `pnpm-lock.yaml`. A nested checkout is still owned by that repository and is not part of this repository's release source.

This repository also tracks `frontend-factor-mining/`, a separate Vue 3 / Element Plus page with its own npm toolchain and default development port 3000. It remains available for compatibility. It is not included in the default frontend image, and validating it does not validate the main product UI. New product features belong in QuantDinger-Vue.

Use the [numbered frontend module map](doc/web_doc/00-index.md) for ownership, the [test map](doc/web_doc/04-test-map.md) for exact checks, and the [delivery template](doc/web_doc/07-feature-delivery-template.md) for API/UI/contract coverage. Preserve the existing component library, authentication, request wrapper, tokens, and i18n of each target.

The main product's release workflow is maintained in its own repository:

```bash
# In QuantDinger-Vue
git tag v5.0.1
git push origin v5.0.1
```

The `release-frontend.yml` workflow there builds `linux/amd64 + linux/arm64` images via buildx and pushes them to `ghcr.io/openbyteinc/quantdinger-frontend`, tagged with the semver value, `{major}.{minor}`, and `latest`.

This repo's `docker-compose.yml` (and `docker-compose.ghcr.yml`) references that image by default. To pin a version while testing:

```bash
# Project-root .env (sibling of docker-compose.yml)
# Common: bump both sides together
echo "IMAGE_TAG=5.0.1" >> .env

# Or pin only frontend (testing a UI hotfix against stable backend)
# echo "FRONTEND_TAG=5.0.1" >> .env

docker compose pull frontend
docker compose up -d frontend
```

Resolution order: `FRONTEND_TAG` (or `BACKEND_TAG`) → `IMAGE_TAG` → `latest`.

The container reads `BACKEND_URL` at start time and substitutes it into the nginx config via the official `nginx:alpine` envsubst hook, so the same image works for compose, Railway, and direct `docker run`.

### Building frontend from local source

For iterating on Vue source (theme tweaks, debugging, customised UI), drop the source into the gitignored `./QuantDinger-Vue/` slot at the repo root and layer the `docker-compose.build.yml` override on top. The main `docker-compose.yml` only declares `image:` for frontend (so users who only pull never need the directory to exist); the override file adds the `build:` block:

```bash
# Expected layout — clone QuantDinger-Vue into ./QuantDinger-Vue/ at this repo root:
#   QuantDinger/
#     QuantDinger-Vue/                <- gitignored; clone goes here
#     backend_api_python/
#     docker-compose.yml
#     docker-compose.build.yml        <- enables local frontend build

git clone https://github.com/OpenByteInc/QuantDinger-Vue.git QuantDinger-Vue

# Build frontend from ./QuantDinger-Vue, pull everything else:
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build

# Rebuild after editing Vue source:
docker compose -f docker-compose.yml -f docker-compose.build.yml build frontend

# Apply runtime config changes only:
docker compose restart frontend
```

Tip: drop `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` into your root `.env` and plain `docker compose up --build` will pick up both files.

Key behaviour:

- **Without the override**: Compose pulls `ghcr.io/.../quantdinger-frontend:<tag>` as usual; `./QuantDinger-Vue/` does not need to exist.
- **With the override**: Compose builds from `./QuantDinger-Vue/` (or `FRONTEND_SRC_PATH` if set) and tags the result with whatever `FRONTEND_TAG` / `IMAGE_TAG` resolves to. The locally built image then satisfies plain `docker compose up` for subsequent runs until you `docker compose pull frontend` to overwrite it.

Source path override (keep the Vue clone somewhere else than `./QuantDinger-Vue/`):

```bash
FRONTEND_SRC_PATH=/abs/path/to/QuantDinger-Vue \
  docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Default backend behaviour is unaffected — it still builds from this repo's `backend_api_python/`. Only the frontend service uses the image/override-build split.

## Adding a New Data Source

1. Create `backend_api_python/app/data_sources/<name>.py` implementing a class
   with `get_ticker(symbol)` and `get_kline(symbol, timeframe, limit)`.
2. Register it in `data_sources/factory.py`.
3. If it serves the global market dashboard, add a fetcher in
   `data_providers/` and wire it into the fallback chain.

## Adding a New Exchange (Live Trading)

1. Create `backend_api_python/app/services/live_trading/<exchange>.py`
   inheriting from `BaseLiveTrading`.
2. Implement `place_order`, `cancel_order`, `get_balance`, etc.
3. Register in `live_trading/factory.py`.

## Environment Variables

See `backend_api_python/env.example` for the full list.  Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | **yes** | JWT signing key — must be changed from default |
| `ADMIN_USER` / `ADMIN_PASSWORD` | yes | Initial admin credentials |
| `TWELVE_DATA_API_KEY` | no | Twelve Data for forex/commodities |
| `ADANOS_API_KEY` | no | Optional Adanos Market Sentiment for US stock tickers |
| `OPENAI_API_KEY` or `OPENROUTER_API_KEY` | no | AI analysis features |
| `CACHE_ENABLED` | no | Set `true` to use Redis (auto-set in Docker) |

## Testing

```bash
cd backend_api_python
pip install pytest
pytest tests/ -v
```

## Troubleshooting

- **First-time Docker install or pull failures** - see the bilingual [Installation Troubleshooting](docs/deployment/INSTALL_TROUBLESHOOTING.md) guide for Docker Desktop proxy setup, Docker Hub pull errors, and Postgres data-version issues.

- **"apikey parameter is incorrect"** from Twelve Data — verify `TWELVE_DATA_API_KEY` in `.env`; Chinese stock data requires a paid plan.
- **Heatmap "暂无数据"** — usually caused by NaN in yfinance data; the global JSON encoder now sanitises all NaN/Inf to `null`.
- **Redis connection refused** — ensure `redis` service is running (`docker compose up -d redis`); set `CACHE_ENABLED=false` to fall back to in-memory cache.
