# Automatic factor mining

Automatic factor mining automates the research loop: propose a hypothesis, build an expression,
screen it down a graded ladder, review it out of sample, and file what survives in a candidate
library. It covers the **research process**, not returns. A mined factor is **not a promise of
future profit**: even a frozen-window pass is one out-of-sample measurement, not a forecast.

This page is for users and for the maintainers of the front-end copy. It documents the module
boundaries, the expression DSL, the template library, the screening ladder, the time semantics,
the HTTP surface, the job and budget limits, and every i18n key.

## 1. What this module is, and what it is not

**What it does**

- Expands 8 financial-hypothesis templates into a deterministic batch of candidate expressions,
  scores each one, and keeps a complete trial ledger.
- Screens candidates down a graded ladder (L1-L5), from "legal" to "still has out-of-sample
  evidence on the frozen window".
- Files results in the candidate library `qd_factor_candidates`: one row per distinct expression,
  deduplicated across runs, with `run_id` pointing at the run that judged it last.
- Keeps everything tenant-scoped, cancelable per job, and auditable afterwards -- config, data
  fingerprint, counts, and a per-candidate reject reason all land in the database.

**What v1 deliberately does not do**

| Non-goal | Detail |
| --- | --- |
| No strategy-runtime integration | Mined candidates live only in `qd_factor_candidates`. A strategy can still only reference the built-in factor registry `_FACTORS` (`app/services/factors/registry.py`); there is no user-scoped path from a strategy to the mining output. |
| No AI proposals | Every v1 candidate comes from expanding the template library's parameter grids. No model proposes hypotheses (left to v3). |
| No write-back to the factor registry | Mining output never becomes a new built-in factor; `_FACTORS` stays a static, code-maintained table. |
| No billing | This feature is **entirely free of charge**: it neither debits credits nor writes a ledger. Resources are bounded only by the concurrency and budget caps (section 8). |
| No replayable copy | Only the data fingerprint `snapshotId` is stored, never the market panel itself, so a past run cannot be replayed offline. |

## 2. Reproduce one mining run

Prerequisites: a **portfolio-type** Strategy V2 script source (`sourceId`) whose universe resolves
to at least 3 usable symbols over the requested range.

```bash
# 1) Price the search first: no source read, no panel, no job.
curl -s -X POST http://127.0.0.1:5000/api/factor-mining/estimate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sourceId": 12, "market": "USStock", "timeframe": "1D",
       "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42}'
# -> {"code":1,"msg":"common.success","data":{"templateCount":8,"candidateBudget":50}}

# 2) Submit the job and take its receipt (HTTP 202).
curl -s -X POST http://127.0.0.1:5000/api/factor-mining/run \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sourceId": 12, "market": "USStock", "timeframe": "1D",
       "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42}'
# -> {"code":1,...,"data":{"jobId":"<uuid>","status":"queued",...}}

# 3) Poll to a terminal state.
curl -s http://127.0.0.1:5000/api/factor-mining/jobs/<jobId> -H "Authorization: Bearer $TOKEN"
# status is one of queued | running | succeeded | failed | cancelled

# 4) Read the candidate library.
curl -s "http://127.0.0.1:5000/api/factor-mining/candidates?status=frozen&limit=50" \
  -H "Authorization: Bearer $TOKEN"
```

Once the job reaches `succeeded`, `GET /jobs/<jobId>` carries in `result` the data fingerprint
`summary.snapshotId`, the three counts `summary.trials` / `summary.candidates` / `summary.frozen`,
and the `split` block naming the search and frozen windows.

## 3. The expression DSL

A candidate factor is a JSON expression tree: leaves are field names, internal nodes are
`{"op": ..., "args": [...], "params": {...}}`. **A number is never a bare leaf** -- a literal
enters an expression through the zero-arity `const` operator.

### 3.1 Field whitelist

Five price-volume fields only: `open`, `high`, `low`, `close`, `volume`.
(`PRICE_VOLUME_FIELDS`)

### 3.2 Operators (31)

| Family | Count | Operators |
| --- | --- | --- |
| Time series (windowed, parameter `n`) | 11 | `lag` `delta` `ts_mean` `ts_sum` `ts_std` `ts_min` `ts_max` `ts_rank` `ts_zscore` `ts_pct_change` `ts_corr` |
| Cross-section | 3 | `cs_rank` `cs_zscore` `cs_demean` |
| Constant | 1 | `const` (parameter `value`) |
| Unary arithmetic | 5 | `neg` `abs` `sign` `log` `sqrt` |
| Binary arithmetic | 5 | `add` `sub` `mul` `div` `safe_div` |
| Comparison | 4 | `gt` `lt` `ge` `le` |
| Other | 2 | `where` (three-arity), `clip` (parameters `lo` / `hi`) |

### 3.3 Constraint constants

| Constant | Value | Meaning |
| --- | --- | --- |
| `MAX_DEPTH` | 6 | Maximum tree depth, the root counting as 1 |
| `MAX_NODES` | 24 | Maximum node count; leaves and operator nodes both count |
| `WINDOW_MIN` | 1 | Lower bound of the window parameter `n` |
| `WINDOW_MAX` | 252 | Upper bound of the window parameter `n` |
| `MAX_LOOKBACK` | 504 | Upper bound on the sum of windows along any root-to-leaf path -- that is, the history this expression needs |

### 3.4 Look-ahead protection

- **No negative shift.** The window parameter `n` of `lag` / `delta` is bounded by
  `WINDOW_MIN=1`, so there is no `n <= 0` spelling and no way to move a future bar into the
  present.
- **Windows only look backwards.** Every time-series operator requires `min_periods = n`, so a
  window reports only once it is full and is missing (NaN) before that.
- **Missing stays missing.** NaN inputs propagate, and undefined results (division by zero, zero
  dispersion, `log` of a non-positive value) are NaN rather than `inf`.
- **Point-in-time.** Rewriting a later bar can never change an earlier bar's output.

### 3.5 Normalization and fingerprint

`normalize_expression` re-spells an expression canonically (keys sorted, empty `params` dropped)
and then computes `sha256(canonical + ":v" + DSL_VERSION)` as its **fingerprint**, which is the
`expression_hash` column. Spelling variants of one expression therefore share a fingerprint, which
is what makes cross-run deduplication work.

## 4. The template library (8 templates)

A template pairs a financial hypothesis with a parameterized expression skeleton and an integer
parameter grid. Expansion walks every grid point in `TEMPLATES` declaration order; each grid point
yields its **base expression** plus "denoised" variants (the base wrapped in `ts_mean` at windows
3 and 5). Candidates are deduplicated by fingerprint and then truncated to `budget`.

| Template id | Category | Hypothesis | Parameter grid | Grid points |
| --- | --- | --- | --- | --- |
| `momentum` | price | Recent winners keep winning | `N in {5,10,20,60,120}` | 5 |
| `reversal` | price | Recent winners pull back in the short run | `N in {3,5,10,20}` | 4 |
| `low_volatility` | price | Calm names do better risk-adjusted | `N in {10,20,60}` | 3 |
| `liquidity` | volume | Volume against its own baseline flags a change in attention | `N in {5,10,20}`, `M in {20,60}` | 6 |
| `liquidity_level` | volume | The absolute level of volume, ranked across symbols, carries information of its own | `N in {5,10,20}` | 3 |
| `volume_price_confirm` | price_volume | Momentum is more reliable when volume confirms it | `N in {5,20,60}`, `M in {5,20}` | 6 |
| `risk_adjusted_momentum` | price | Momentum per unit of volatility beats raw momentum | `N in {20,60,120}` | 3 |
| `conditional_filter` | price_volume | Momentum only works while volume sits in a high regime | `N in {5,20}`, `M in {5,20}` | 4 |

34 grid points in total. Expanding the default template set (all 8) with the default
`budget=400` and default `seed=42` yields **102** distinct candidates -- when the template set
cannot fill the budget, `estimate` reports that real number in `candidateBudget` rather than 400.

`liquidity_level` is the eighth template, added by a review decision: `liquidity` measures how much
volume has risen against its own baseline, while `liquidity_level` measures where the absolute
level sits in the cross-section. Those are different hypotheses, so each gets its own template.

## 5. The screening ladder and its default thresholds

Every candidate walks down from L1, and **the first gate it fails is its reject reason**; nothing
further is checked.

| Stage | What it checks | Default bar | Reject key on failure |
| --- | --- | --- | --- |
| **L1 legality** | DSL shape, fields, operators, parameter ranges -- and the evaluation itself (division by zero, overflow, a scalar handed to a panel operator) | -- | `factorMining.reject.invalid` |
| **L2 quick screen** | Coverage, then IC observation count, then signal strength (`\|rankIC\|`) | coverage >= 0.60; observations >= 30; `\|rankIC\|` >= 0.02 | `.lowCoverage` / `.insufficientObservations` / `.weakIc` |
| **L3 stability** | ICIR, fold sign agreement, first/second half agreement, average turnover | ICIR >= 0.30; fold agreement >= 0.60; halves agree in sign; turnover <= 0.70 | `.weakIcir` / `.unstableFolds` / `.unstableHalves` / `.highTurnover` |
| **L4 built-in redundancy** | Absolute correlation against every built-in factor that was scored | < 0.85 | `.duplicateBuiltin` / `.unverifiableRedundancy` |
| **L5 frozen out-of-sample** | In-sample and out-of-sample `rankIC` agree and clear the strength bar; deflated-Sharpe probability | `\|OOS rankIC\|` >= 0.01 and same sign as IS; DSR probability >= 0.95 | `.weakOutOfSample` / `.selectionBias` |

**Details that are easy to get wrong:**

- **L4 ignores non-finite correlations.** Only finite values count as evidence. A NaN means "the
  engine could not measure it" -- fundamental factors carry no data on a price-volume run --
  and skipping one is **not** a clearance. So when **nothing finite survives the filter**, L4
  cannot run at all and rejects with `.unverifiableRedundancy` rather than waving the candidate
  past the ladder's only redundancy checkpoint.
- **An unmeasurable turnover is not a low one.** The `.highTurnover` test is written as
  `not (turnover <= max_turnover)`, so both NaN and `inf` fail it (fail-closed).
- **Absent fold consistency is skipped.** `foldIcSigns` is written by the search engine (the IC
  series split into at most 4 equal folds, each fold contributing the sign of its mean). A missing
  series means "not evaluated", so that check is skipped.
- **L5's `trials` is the whole ledger.** The deflated-Sharpe denominator is **every candidate the
  run evaluated, rejections included**, not the survivor count. Using survivors would underprice
  the search itself and understate exactly the selection bias the correction exists for.
- **Thresholds are overridable.** A `POST /run` payload may carry a `thresholds` object whose keys
  are the `GateThresholds` field names (snake_case): `min_coverage`, `min_ic_observations`,
  `min_abs_rank_ic`, `min_icir`, `min_fold_consistency`, `max_turnover`,
  `max_builtin_correlation`, `min_oos_abs_rank_ic`, `min_deflated_sharpe_probability`. An
  unrecognised key or a `null` keeps the default.

## 6. Time semantics and bias controls

**Point-in-time universe.** The universe is resolved by `resolve_candidates` over the **whole
requested range** (not the search window), so the search panel and the frozen panel carry the same
members and the same point-in-time membership.

**Signal and execution timing.** A signal is computed from bar *t*'s close. Entry is the **next
bar's open** and exit is the open `holdingPeriod` bars after that, giving a forward return of
`exit / entry - 1`. `holdingPeriod` defaults to 5.

**Search and frozen windows are strictly isolated.** The requested range is cut by **trading
sessions**: the trailing `freezeRatio` (default 0.3, which must fall inside `(0, 0.5)`) of the
sessions form the frozen window, which starts the **day after** the last search session, so the two
windows share no bar. The search's data-source calls stop at the search end; the **frozen window is
read exactly once**, inside `review_frozen`, and only when something survived. No candidate can
ever touch the bars that are going to judge it.

**Every trial is on the ledger, failures included.** Each evaluated candidate writes one row into
`qd_factor_trials`, passes and rejections alike. That ledger's size is the deflated-Sharpe
denominator used at L5.

**Data fingerprint.** `summary.snapshotId` is a real fingerprint of the panel's contents: symbols
are visited in sorted order and each one's canonical OHLCV bytes are mixed into a `sha256` (the
symbol name is hashed too). It is a **fingerprint, not a replayable copy** -- v1 stores the digest
and no market snapshot.

**Warmup.** The fetch reaches back before the search start by an amount driven by **the candidates
this run will actually evaluate** (the same template selection, budget, and seed): `max(40,
min(504, longest lookback + 5))` bars, converted to calendar days. Budgeting the DSL's 504 ceiling
instead would cost about 3.5x the provider history -- on a daily policy that is the difference
between a one-year and a four-month searchable window.

**Determinism and reproducibility.** The same panel plus the same `(templateIds, budget, seed)`
produces the same trials and the same candidates in the same order: template expansion is seeded,
the gate ladder is pure, and nothing iterates a set.

## 7. HTTP surface

Prefix `/api/factor-mining`; the OpenAPI tag is **`FactorMining`**. Every route requires
authentication (`Authorization: Bearer <jwt>`). All responses use the standard envelope
`{"code": 1, "msg": "common.success", "data": ...}`; on error `code: 0`, `msg` is the i18n key from
section 10, and `data` is `null`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/catalog` | The DSL's own vocabulary: `fields`, `operators`, `templates` |
| `POST` | `/estimate` | Static price of a search, returning `{templateCount, candidateBudget}`; no source read, no panel, no job |
| `POST` | `/run` | Submit a mining run; **HTTP 202**, returns the job receipt |
| `GET` | `/jobs` | The caller's mining jobs, newest first |
| `GET` | `/jobs/<job_id>` | One job, with `progress` and `result` |
| `POST` | `/jobs/<job_id>/cancel` | Cancel; the runner stops at its next progress report |
| `GET` | `/candidates` | The candidate library, filtered and paged |
| `GET` | `/candidates/<candidate_id>` | One candidate, with `metrics` and `outOfSample` |

**`POST /run` request body.** The server reads only these keys and drops the rest, so a client's own
key never lands in the job store's request:

`sourceId`, `sourceName`, `market`, `timeframe`, `startDate`, `endDate`, `freezeRatio`, `budget`,
`seed`, `templateIds`, `groups`, `holdingPeriod`, `commission`, `slippage`, `neutralizeIndustry`,
`thresholds`

Required: `sourceId` (positive integer), `market` (a known market key), and `startDate` / `endDate`
(`YYYY-MM-DD`). `budget` defaults to 400 and `timeframe` to `1D`.

**Job receipt / job object** (one field set for both):

```json
{"jobId": "<uuid>", "status": "queued", "progress": {}, "result": null,
 "error": null, "createdAt": "2026-10-13T09:00:00+00:00",
 "startedAt": null, "finishedAt": null}
```

**`GET /jobs` parameters:** `limit` (1-50, default 20).
**`GET /candidates` parameters:** `runId` (integer), `status` (`search` / `frozen` / `rejected`),
`market`, `limit` (1-200, default 50), `offset` (>= 0, default 0). An unreadable integer returns
400 with `factorMining.invalidQuery`.

**List view versus detail view.** `/candidates` **omits** `metrics` and `outOfSample` -- a
candidate's metrics carry its whole IC series and curves, which a page of fifty does not need. Read
`/candidates/<id>` for those two blocks.

**Candidate fields:** `id`, `userId`, `runId`, `expressionHash`, `expression`, `origin`,
`templateId`, `market`, `timeframe`, `status`, `reason`, `createdAt`, `updatedAt`; the detail view
adds `metrics` and `outOfSample`.

**`status` values:** `search` (cleared L1-L4, awaiting the frozen review), `frozen` (also cleared
L5), `rejected` (stopped at some stage; the key is in `reason`).

**`GET /catalog`:** `operators` gives **names only**, with no arity and no windowed flag -- section
3.2 above is the authoritative table. Each entry of `templates` is
`{templateId, hypothesisI18nKey, category, parameterGrid}`.

**The label inside the correlation study.** When a mined candidate enters L4's correlation study,
it is displayed under the factor name **`"expression"`** (the built-ins follow it). So
`metrics.factorCorrelation.factors[0]` is always `"expression"` and `matrix[0][1:]` holds its
correlation against each built-in.

## 8. Jobs, concurrency, and budget

**Durable execution.** A mining job travels through the shared `qd_agent_jobs` table with
`kind = "factor_mining"`, carried by the Celery durable path (`app/tasks/agent_jobs.py` registers
the kind). The in-process pool and Celery both end up calling one implementation,
`run_mining_job`; there are not two code paths.

**Two records, written at different moments.** The job row (`qd_agent_jobs`) is the timeline the API
polls. The run row (`qd_factor_mining_runs`) is the audit record, and it is opened **before the
search starts**, so a crash mid-run still leaves a row saying which config was being searched.

**Concurrency cap.** One tenant may have **1 active job** at a time. Hitting it returns 400 with
`factorMining.concurrentLimit`. Note that the gate counts every `queued` / `running` row that user
has in the shared job table, with no `kind` filter -- so another running job of theirs occupies the
slot too.

**Budget caps.**

| Item | Value |
| --- | --- |
| `DEFAULT_BUDGET` | 400 |
| `MAX_BUDGET` | 2000 (above it, `factorMining.budgetExceeded`) |
| Minimum usable budget | 1 (below 1, or unreadable, `factorMining.budgetInvalid`) |

`budget` is a **cap, not a promise**: when the template set expands to fewer than 400 distinct
candidates, the run evaluates exactly what expanded (102 for the default template set).

**Cancellation.** `POST /jobs/<id>/cancel` marks the job cancelled and the runner stops by raising
`JobCancelledError` at its **next progress report**. The run row is then closed as `cancelled`
rather than `failed`, because showing a user's own cancellation as an engine failure would be
wrong.

**Ghost-job collection.** A job whose worker process died stays frozen in `running`. `GET /jobs`
sweeps while listing: any job still `running` whose `startedAt` is older than
`max(120, CELERY_TASK_TIME_LIMIT or 3600) + 300` seconds is failed with
`factorMining.workerInterrupted`. This is not cosmetic -- the concurrency gate counts active jobs,
so an unswept ghost would permanently block that tenant's submissions.

**Open item:** a ghost stuck in `queued` is **out of the sweep's scope** (deferred).

## 9. Data tables

| Table | Granularity | Key constraints |
| --- | --- | --- |
| `qd_factor_mining_runs` | One row per run | Config, data fingerprint, three counts, status; `user_id` is a foreign key to `qd_users` |
| `qd_factor_candidates` | One row per distinct expression | `UNIQUE (user_id, expression_hash)`, so it deduplicates across runs; `run_id` is `ON DELETE SET NULL` -- a candidate is an asset that outlives runs, so only its pointer may go with one |
| `qd_factor_trials` | One row per evaluation | `run_id` is `ON DELETE CASCADE` -- a ledger detached from the run that priced it means nothing |

Every write is tenant-scoped and **fails audibly**: writing against a run that is not the caller's
raises (`factorMining.runNotFound`) instead of returning a row count for rows that were never
written. That silent version would lose a 250-trial ledger without a trace.

## 10. i18n keys

The front-end copy lives in another repository. These are **all** the `factorMining.*` keys the
backend can emit; the backend emits keys only and never renders copy.

**API and service layer (21 keys)**

| Key | When it fires |
| --- | --- |
| `factorMining.runFailed` | An unexpected server-side failure while submitting (500) |
| `factorMining.jobNotFound` | The job does not exist, is not the caller's, or is not a mining job |
| `factorMining.candidateNotFound` | The candidate does not exist or is not the caller's; also when the id is not an integer |
| `factorMining.invalidQuery` | A query parameter is not a readable integer |
| `factorMining.sourceRequired` | `sourceId` is missing or not a positive integer |
| `factorMining.sourceNotFound` | The `sourceId` script source does not exist or is not the caller's |
| `factorMining.sourceCodeRequired` | The script source exists but its code is empty |
| `factorMining.dateRangeRequired` | `startDate` / `endDate` missing or unparseable |
| `factorMining.dateRangeTooShort` | Fewer than 2 trading sessions in the range, so no split is possible |
| `factorMining.invalidMarket` | `market` is not a known market key |
| `factorMining.budgetInvalid` | `budget` is unreadable or below 1 |
| `factorMining.budgetExceeded` | `budget` is above 2000 |
| `factorMining.concurrentLimit` | The caller already has an active job |
| `factorMining.workerInterrupted` | Failure reason written when a stale job is swept |
| `factorMining.freezeRatioInvalid` | `freezeRatio` is unreadable or outside `(0, 0.5)` |
| `factorMining.searchFreezeOverlap` | The split left the frozen window overlapping the search window (a structural guard that should not fire) |
| `factorMining.universeTooSmall` | The universe resolved to fewer than 3 members |
| `factorMining.usableUniverseTooSmall` | Fewer than 3 symbols remained usable after fetching |
| `factorMining.noMarketData` | The fetch returned no market data at all |
| `factorMining.missingIcSeries` | The research engine returned no IC series (an upstream fault) |
| `factorMining.runNotFound` | A write targeted a run that does not exist or is not the caller's |

**Parameterized forms (3, so a consumer must not match on a bare key)**

| Form | Source |
| --- | --- |
| `factorMining.templateNotFound:<template_id>` | `templateIds` contained an unknown template id |
| `factorMining.fingerprintInvalid:<index>` | The `<index>`-th candidate's fingerprint is not a 64-character sha256 hex digest |
| `factorMining.reject.invalid:<reason>` | The specific failure a DSL validation carried on `ExpressionError.code` (`unknownField` / `tooDeep` / `tooManyNodes` / `unknownOperator` / `badArity` / `badNode` / `badWindow` / `lookbackExceeded`) |

**L1-L5 reject reasons (12 keys)**

| Key | Stage |
| --- | --- |
| `factorMining.reject.invalid` | L1 (the trial's and candidate's `reason` column holds this **bare key**; the `:<reason>` form appears only on the `ExpressionError.code` the DSL raises) |
| `factorMining.reject.lowCoverage` | L2 |
| `factorMining.reject.insufficientObservations` | L2 |
| `factorMining.reject.weakIc` | L2 |
| `factorMining.reject.weakIcir` | L3 |
| `factorMining.reject.unstableFolds` | L3 |
| `factorMining.reject.unstableHalves` | L3 |
| `factorMining.reject.highTurnover` | L3 |
| `factorMining.reject.duplicateBuiltin` | L4 |
| `factorMining.reject.unverifiableRedundancy` | L4 |
| `factorMining.reject.weakOutOfSample` | L5 |
| `factorMining.reject.selectionBias` | L5 |

**Template hypotheses (8 keys)**

- `factorMining.template.momentum`
- `factorMining.template.reversal`
- `factorMining.template.low_volatility`
- `factorMining.template.liquidity`
- `factorMining.template.liquidity_level`
- `factorMining.template.volume_price_confirm`
- `factorMining.template.risk_adjusted_momentum`
- `factorMining.template.conditional_filter`

`GET /catalog` returns these keys directly as `templates[].hypothesisI18nKey`, so the front end
never has to hardcode the template list.

## 11. Known non-goals and open items

- **No billing.** The feature debits nothing and writes no ledger; resources are bounded only by
  the concurrency and budget caps.
- **`queued` ghosts are not swept.** Only `running` stale jobs are collected; jobs stuck `queued`
  are out of scope (deferred).
- **`/catalog`'s `operators` carries names only** -- no arity and no windowed flag. The table in
  section 3.2 is the authoritative source.
- **No strategy-runtime integration.** Mining output cannot be referenced by a strategy, and there
  is no user-scoped read path to it.
- **No AI proposals.** Every candidate comes from expanding template parameters (revisit in v3).
- **Fingerprint only.** There is no replayable panel copy, so past runs cannot be re-run offline.

Related: [Strategy workflow](STRATEGY_WORKFLOW.md), [Backtest Center](BACKTEST_CENTER.md),
[Strategy API V2 development](../trading/STRATEGY_DEV_GUIDE.md),
[live-trading safety](../trading/LIVE_TRADING_SAFETY.md).
