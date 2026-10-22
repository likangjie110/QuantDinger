# Distributed Runtime Deployment and Scaling

This guide is the operational source of truth for the current event-driven
runtime. It describes what the repository runs today, how to release it safely,
and which boundaries must change before a multi-host 100,000-strategy cluster.

## Current production topology

```mermaid
flowchart LR
    CLIENT["Web, mobile, API, MCP"] --> API["Backend API"]
    API --> PG[("PostgreSQL")]
    API --> CACHE[("Redis cache")]
    API --> JOBS[("Redis jobs")]

    CONTROL["Trading workers\ncontrol and realtime owners"] --> PG
    CONTROL -->|"closed bars"| KAFKA[("Kafka")]
    KAFKA --> DISPATCH["Strategy dispatcher group"]
    DISPATCH -->|"strategy shard batches"| KAFKA
    KAFKA --> EVAL["Strategy evaluator group\nhot bar runtimes"]
    EVAL -->|"inbox, checkpoints, intents"| PG

    PG -->|"fenced pending orders"| CONTROL
    EXCHANGE["Exchanges and brokers"] <--> CONTROL
    CONTROL -->|"durable execution events"| PG
    PG --> GRID["Owned grid actor mailbox"]
    GRID --> CONTROL

    KAFKA --> AUDIT["Kafka audit worker"] --> PG
    JOBS --> CELERY["Celery workers"] --> PG
```

PostgreSQL remains the business source of truth. Kafka provides ordered runtime
transport; its offsets do not replace event inboxes, actor mailboxes, leases,
fencing tokens, idempotency keys, or order reconciliation.

## Strategy execution paths

| Strategy class | Current owner | Trigger and recovery path |
| --- | --- | --- |
| Ordinary closed-bar Strategy API V2 | `strategy-evaluator-worker` | Kafka shard batch, durable inbox, runtime lease, hot checkpoint |
| Grid with resting exchange orders | `trading-worker` | Private fill stream or REST reconciliation, durable grid actor mailbox |
| Martingale, DCA, tick-driven, and session-driven runtime | `trading-worker` | Realtime local scheduler protected by strategy lease and fencing |
| Pending live order submission | `trading-worker` | PostgreSQL claim, runtime fencing validation, stable client order ID |
| AI, backtest, report, and maintenance job | `celery-worker` | Durable Redis jobs queue and task retry policy |
| Finite factor-mining job | `celery-mining-worker` | Dedicated `mining` queue on the same durable jobs Redis; existing agent-job task and PostgreSQL job record |

Market identity must include venue/provider, market, market type, instrument ID,
and timeframe. A symbol string such as `BTC/USDT` is not a globally unique price
stream and must never be used as the only routing key.

## Factor-mining queue capacity

Factor mining keeps the `celery` process role and the existing
`quantdinger.tasks.agent_job(job_id)` entrypoint. New `factor_mining` jobs are
published explicitly to `mining`; the general worker continues consuming only
`jobs,ai,maintenance`. This isolates worker slots and container resource budgets
without changing trading ownership, job IDs, cancellation, or result storage.
With `CELERY_TASKS_ENABLED=false`, standalone development still uses the existing
local executor and does not provide this queue isolation.

The source and GHCR Compose files both start `celery-mining-worker`. The local
frontend build override inherits it. The production override applies the same
non-root, read-only filesystem and dropped capabilities as other Celery workers.
Set the following in the project-root `.env`:

```dotenv
CELERY_MINING_REPLICAS=1
CELERY_MINING_CONCURRENCY=1
CELERY_MINING_PREFETCH=1
CELERY_MINING_DB_POOL_MIN=1
CELERY_MINING_DB_POOL_MAX=4
CELERY_MINING_MEMORY_LIMIT=2g
CELERY_MINING_CPU_LIMIT=1.0
FACTOR_MINING_JOB_LEASE_SEC=120
FACTOR_MINING_JOB_MAX_ATTEMPTS=3
```

CPU and memory limits apply per container; DB pools apply per child process.
Estimate the mining DB connection ceiling as replicas × concurrency × pool max.
The general worker keeps `CELERY_CONCURRENCY`. Both workers still share PostgreSQL,
jobs Redis, storage, and the host, so queue separation does not prove capacity or
eliminate shared bottlenecks. Measure memory, queue wait time, job duration and
database connections before raising mining concurrency or replicas.

Mining ownership is persisted on the PostgreSQL job row. Each finite attempt claims
an execution token and renewable lease; a replacement receives a new token and stale
writes are rejected. The defaults above cap a job at three attempts. Existing
maintenance dispatches expired running jobs and queued rows older than 60 seconds;
tenant-scoped submit/detail/list requests can trigger the same bounded recovery.
The local executor also uses this ownership path, while the broker path remains
the dedicated `mining` queue. Recovery preserves earlier run audit rows, starts a
new run under the same job ID and reuses recorded AI proposals where available.

The existing one-shot migration role applies the idempotent P1 DDL appended to
`migrations/init.sql` before starting this backend/worker revision. The matching
`20261019_factor_mining_p1.sql` fragment is used for isolated schema checks.
Candidate identity now includes market and timeframe; candidate publication and successful run/job states commit together.
The previous application revision's two-column candidate conflict target is not
compatible with this schema: do not roll it back after migration without an
explicit compatibility migration. No candidate content or IDs are rewritten.

Both Celery container health checks now inspect the local node
`celery@<container hostname>` and require exactly its expected active queues:

```bash
docker compose exec -T celery-worker python -m app.commands.worker_health celery --queues jobs,ai,maintenance
docker compose exec -T --index 1 celery-mining-worker python -m app.commands.worker_health celery --queues mining
```

Repeat the mining check for every replica index. A missing reply, broker error,
wrong queue, or mixed mining/general subscriptions fails the check. The legacy
`worker_health celery` command retains its heartbeat check. `/api/health/ready`
checks PostgreSQL and the broker, while `/api/health/workers` summarizes recorded
heartbeats; neither HTTP endpoint proves that a mining consumer is available.

During an upgrade, start and verify the new mining consumers before enabling new
producers. Let pre-upgrade mining messages already in `jobs` finish on the general
worker; do not purge queues, rewrite messages, or rewrite job records. Before
rolling back producers, drain the `mining` queue and its active/reserved work while
keeping its compatible worker running. Old workers consuming only
`jobs,ai,maintenance` cannot drain `mining`. Follow the existing graceful drain
procedure and the P1 schema compatibility boundary above.

Validate configuration before starting services; these checks do not start a
worker or benchmark throughput:

```bash
docker compose --env-file .env.example -f docker-compose.yml config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.build.yml config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.production.yml config --quiet
docker compose --env-file .env.example -f docker-compose.ghcr.yml -f docker-compose.production.yml config --quiet
```

Configuration references: [Celery 5.6 routing](https://docs.celeryq.dev/en/v5.6.3/userguide/routing.html),
[worker queue inspection](https://docs.celeryq.dev/en/v5.6.3/userguide/workers.html#queues-list-of-active-queues),
and [Compose service resource settings](https://docs.docker.com/reference/compose-file/services/).

## Safe release procedure

1. Freeze schema-changing deployments and take a verified PostgreSQL backup.
2. Save the current image tags and environment files for rollback.
3. Pull or build all images from the same release version.
4. Run the database migration and Kafka topic initializer.
5. Recreate the services and verify health, worker heartbeats, leases, consumer
   lag, pending-order backlog, grid actor backlog, and error logs.

Prebuilt-image deployment:

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml run --rm migration
docker compose -f docker-compose.ghcr.yml run --rm kafka-init
docker compose -f docker-compose.ghcr.yml up -d --remove-orphans
docker compose -f docker-compose.ghcr.yml ps
```

Source deployment:

```bash
git pull --ff-only
docker compose build backend
docker compose run --rm migration
docker compose run --rm kafka-init
docker compose up -d --remove-orphans
docker compose ps
```

Do not use `down -v` during an upgrade. It deletes local state volumes. A schema
migration must complete before workers using the new schema are admitted.

Minimum post-release checks:

```bash
curl -f http://127.0.0.1:5000/api/health
curl -f http://127.0.0.1:5000/api/health/ready
curl -f http://127.0.0.1:5000/api/health/workers
docker compose logs --since=10m trading-worker strategy-dispatcher-worker strategy-evaluator-worker kafka-audit-worker
```

For a GHCR installation, add `-f docker-compose.ghcr.yml` to the log command.
Verify at least one controlled paper strategy through start, bar evaluation,
order intent, fill projection, stop, worker restart, and ownership recovery.

## Scaling on one host

Set replica counts in the project-root `.env` and recreate only the affected
roles:

```dotenv
TRADING_WORKER_REPLICAS=4
STRATEGY_DISPATCHER_REPLICAS=2
STRATEGY_EVALUATOR_REPLICAS=8
```

```bash
docker compose up -d --force-recreate \
  trading-worker strategy-dispatcher-worker strategy-evaluator-worker
```

Scale one role at a time and observe CPU, memory, PostgreSQL connections and
query latency, Kafka consumer lag, exchange rate-limit errors, event inbox age,
pending-order age, and actor retry/dead counts. Kafka partition counts cap useful
consumer parallelism; adding more evaluator replicas than assigned partitions
does not create more throughput.

## Moving to multiple hosts

The default Compose stack is a single-host topology. Do not copy it unchanged
to several servers: every host would create its own PostgreSQL, Redis, Kafka,
and local volumes.

A real multi-host deployment needs:

- one shared PostgreSQL primary with backups, connection control, tested restore,
  and read replicas for analytical/read-heavy endpoints;
- shared cache Redis and a separate durable jobs Redis;
- a multi-broker Kafka cluster with replication and externally resolvable
  advertised listeners;
- an orchestrator or equivalent service manager for unique identities, health,
  rolling drain, restart, anti-affinity, secrets, and network policy;
- private networking from workers to stateful services and controlled egress to
  exchanges and brokers;
- centralized metrics, logs, alerts, and consumer-lag monitoring.

Only stateless or lease-owned worker roles should be scaled horizontally. Move
PostgreSQL, Redis, and Kafka out of the application Compose stack before adding
application hosts.

## Is adding servers enough for 100,000 strategies?

No. The current ownership, event, inbox, fencing, and actor boundaries make
horizontal expansion possible, but 100,000 live strategies is a capacity target,
not a configured guarantee. Additional servers increase evaluator and realtime
compute only after shared bottlenecks are removed.

Before claiming 100,000-strategy capacity, complete and benchmark at least:

1. independent shared market ingestion and bar-clock services;
2. account/credential-partitioned execution gateways with endpoint-aware rate
   limits and backpressure;
3. production Kafka replication, partition sizing, retention, and replay drills;
4. PostgreSQL partitioning/retention for high-volume event, log, and trade data,
   plus read-path isolation and connection pooling;
5. strategy resource classes, tenant quotas, sandbox budgets, and admission
   control;
6. 10,000, then 30,000, then 100,000 synthetic strategy load tests with minute
   boundary burst measurements;
7. restart, broker disconnect, rebalance, database failover, duplicate-event,
   stale-owner, and disaster-recovery tests;
8. production dashboards and alerts for end-to-end bar-to-decision and
   intent-to-order latency.

Capacity must be published as a measured envelope for each strategy class and
timeframe. A lightweight one-minute moving-average strategy, a multi-symbol
portfolio, and a tick-driven grid do not consume equivalent resources.

## Rollback boundary

Application images can be rolled back only when the previous version supports
the migrated schema and current event versions. Keep migrations additive across
the release window, retain Kafka topics during rollback, and drain evaluator and
trading workers before replacing them. Never roll back by restoring an old
database snapshot while new workers or exchange sessions are still active.
