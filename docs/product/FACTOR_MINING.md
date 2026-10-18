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
- Screens candidates down a graded ladder (L1-L6), from "legal" to "still has out-of-sample
  evidence on the frozen window" and finally "still carries something the caller's own library
  does not".
- Files results in the candidate library `qd_factor_candidates`: one row per distinct expression,
  deduplicated across runs, with `run_id` pointing at the run that judged it last.
- Keeps everything tenant-scoped, cancelable per job, and auditable afterwards -- config, data
  fingerprint, counts, and a per-candidate reject reason all land in the database.

**What this module deliberately does not do**

| Non-goal | Detail |
| --- | --- |
| No strategy-runtime integration | Mined candidates live only in `qd_factor_candidates`. A strategy can still only reference the built-in factor registry `_FACTORS` (`app/services/factors/registry.py`); there is no user-scoped path from a strategy to the mining output. |
| No write-back to the factor registry | Mining output never becomes a new built-in factor; `_FACTORS` stays a static, code-maintained table. |
| No billing | This feature is **entirely free of charge**: it neither debits credits nor writes a ledger. Resources are bounded only by the concurrency and budget caps (section 9). |
| No replayable market panel | Only the data fingerprint `snapshotId` is stored, never the market panel itself, so a past run cannot be re-run offline against identical data. A v3 AI run can replay its **proposals** (`replayJobId`, section 8) without calling the model, but the replay still refetches the panel and reports `snapshotChanged` when the fingerprint moved. |

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

Fields come in groups, and a market's set is the union of the groups that market has:

| Group | Fields | Markets |
| --- | --- | --- |
| `price_volume` | `open`, `high`, `low`, `close`, `volume` | every market |
| `derivatives` | `funding_rate`, `open_interest`, `term_structure`, `roll_cost` | `Crypto` only |

`fields_for(market)` returns that set, in group order. The market is matched **exactly** as the run
spells it (`Crypto`, `USStock`): there is no case folding and no alias resolution, so `crypto` --
or an empty or unknown market -- falls back to the `price_volume` group alone. That fallback is
deliberate and points the refusing way: a misspelled market loses fields rather than admits them,
and a field outside `fields_for(market)` is refused at L1 as `unknownField` anyway.

A group's presence says the **data source can answer** for the field, not that every symbol has it:
a symbol whose derivative series comes back empty keeps its frame with the field as a column of
NaN, and the L2 coverage gate decides what that gap means. `term_structure` and `roll_cost` come
from Deribit's BTC and ETH monthly dated futures: the venue stamps each daily candle at that day's
08:00 UTC open, so a candle's close is knowable only at tick + 1 day, and both series are indexed
by that later instant. The main leg is the nearest monthly contract with at least 7 days to expiry
and the far leg is the next one; `term_structure` annualizes the far/main spread over the expiries'
calendar-day gap, and `roll_cost` is the same spread un-annualized. Only BTC and ETH have these
contracts, so every other `Crypto` symbol keeps a NaN column. Both legs are Deribit contracts, so
currency differences mostly cancel, but a member may quote on another venue (a USDT perpetual, for
example), which makes this a cross-venue rather than a strict same-venue spread. Written out,
`term_structure = (far / main - 1) * 365 / d`, where `d` is the calendar-day gap between the two
expiries, and `roll_cost = far / main - 1` is not annualized; a contract joins the pool only
between its listing and expiry. Because only BTC and ETH members carry these fields, a candidate
that depends on either one passes L2's coverage gate only when BTC/ETH members fill at least the
gate's share of the active universe (0.60 by default); `min_coverage` can be overridden by the
run's parameters.

`GET /catalog` serves this list for a market: `?market=Crypto` returns the two groups' nine fields,
and no `market` (or one this build does not know) returns the five price-volume fields.

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

Operator arity, spelled out because a DSL editor needs it: every time-series operator takes one
argument **except the binary `ts_corr`**; every cross-sectional operator takes one; the unary
arithmetic operators take one, the binary arithmetic and comparison operators take two; `const`
takes none, `clip` takes one, and `where` takes three.

### 3.3 Constraint constants

| Constant | Value | Meaning |
| --- | --- | --- |
| `MAX_DEPTH` | 8 | Maximum tree depth, the root counting as 1 |
| `MAX_NODES` | 24 | Maximum node count; leaves and operator nodes both count |
| `WINDOW_MIN` | 1 | Lower bound of the window parameter `n` |
| `WINDOW_MAX` | 252 | Upper bound of the window parameter `n` |
| `MAX_LOOKBACK` | 504 | Upper bound on the sum of windows along any root-to-leaf path -- that is, the history this expression needs |

**Why the depth bound is 8, and what it does not change.** It was 6. A cross-over grafts one
expression's subtree onto another expression's path, so the child's depth is the two halves' added
up -- and with no margin above the deepest legal expression, the deepest candidates cannot be
combined at all: the deepest template expression (the base of `conditional_filter` /
`volume_price_confirm` with its smoothing variant) sits at depth 6, exactly the old bound, and the
raised one leaves that family 2 levels of room. The bound is a
**validation policy, not part of what an expression means**, which is why `DSL_VERSION` was *not*
incremented and every existing `expression_hash` is unchanged: the same expression fingerprints
identically before and after, so no stored candidate is re-keyed by this. What does move is which
expressions are *legal*: trees of depth 7 or 8 that used to be refused as
`factorMining.reject.invalid:tooDeep` are now accepted, and a seed whose legal margin was tight may
now produce a different number of rejected children.

### 3.4 Look-ahead protection

- **No negative shift.** The window parameter `n` of `lag` / `delta` is bounded by
  `WINDOW_MIN=1`, so there is no `n <= 0` spelling and no way to move a future bar into the
  present.
- **Windows only look backwards.** The rolling-family operators and `ts_corr` use
  `min_periods = n`, so a window reports only once it is full and is missing (NaN) before that;
  `lag`, `delta`, and `ts_pct_change` get the same leading-`n` gap from `shift` / `pct_change`
  instead, which have no `min_periods` notion at all.
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
| **L2 quick screen** | Coverage, then IC observation count, then signal strength (`\|rankIC\|`) | coverage >= 0.60; observations >= 30; `\|rankIC\|` >= 0.02 | `factorMining.reject.lowCoverage` / `factorMining.reject.insufficientObservations` / `factorMining.reject.weakIc` |
| **L3 stability** | ICIR, fold sign agreement, first/second half agreement, average turnover | ICIR >= 0.30; fold agreement >= 0.60; halves agree in sign; turnover <= 0.70 | `factorMining.reject.weakIcir` / `factorMining.reject.unstableFolds` / `factorMining.reject.unstableHalves` / `factorMining.reject.highTurnover` |
| **L4 redundancy** | Absolute correlation against every comparison factor that was scored: the registry's factors against the built-in bar, the caller's own frozen pool (names carrying the `pool:` namespace) against the pool bar | built-ins < 0.85; pool < 0.85 | `factorMining.reject.duplicateBuiltin` / `factorMining.reject.duplicateInPool` / `factorMining.reject.unverifiableRedundancy` |
| **L5 frozen out-of-sample** | In-sample and out-of-sample `rankIC` agree and clear the strength bar; deflated-Sharpe probability | `\|OOS rankIC\|` >= 0.01 and same sign as IS; DSR probability >= 0.95 | `factorMining.reject.weakOutOfSample` / `factorMining.reject.selectionBias` |
| **L6 pool increment** | With the caller's whole frozen pool regressed out of the candidate on each session, is the residual still correlated with the forward returns, and on the same side as the raw IC | `\|residual IC\|` >= `min_abs_rank_ic` (0.02) **and** the residual IC carries the raw IC's sign | `factorMining.reject.noIncrementalValue` |

**Details that are easy to get wrong:**

- **L4 ignores non-finite correlations, per namespace.** Only finite values count as evidence. A
  NaN means "the engine could not measure it" -- a fundamental factor carrying no data on a
  price-volume run, or a pool member whose stored expression no longer rebuilds -- and skipping one
  is **not** a clearance. So the rule is grouped: a namespace (built-ins, pool) that was asked
  about and holds **no finite value at all** makes L4 unverifiable, and the candidate is rejected
  with `factorMining.reject.unverifiableRedundancy` rather than waved past the ladder's only
  redundancy checkpoint. A namespace whose names were never asked about demands nothing, or every
  run without a pool would fail closed; a mapping with nothing in it at all was never asked
  anything either, and that is unverifiable too. Built-ins are judged first, so a candidate that
  duplicates both is reported as the built-in duplicate it also is.
- **L4's two bars are separate.** `max_builtin_correlation` and `max_pool_correlation` both default
  to 0.85, but a run may weigh its own library differently from the registry's factors, so the two
  are read apart and the reject keys stay apart -- duplicating a built-in and duplicating what the
  caller already mined send a reader to different places.
- **L6 only retires on a measurement.** The increment has three states, and only one of them is a
  rejection: `true` (carries information), `false` (measured, carries nothing the pool does not --
  the only state a candidate may be retired on) and "no answer". A pool that is empty, or one
  nothing could be read off, leaves the candidate **frozen** and records the reason in the
  increment's own block: `factorMining.poolIncrement.noPool` and `factorMining.poolIncrement.unmeasured`.
  Neither is a reject key and neither appears in a candidate's `reason`. This is deliberately the
  opposite of L4's fail-closed rule: a candidate let through at L4 joins the pool every later run
  compares against, so a duplicate there compounds, while one let through here has already cleared
  L2-L5 and a reader can retire it later on evidence.
- **An unmeasurable turnover is not a low one.** The `factorMining.reject.highTurnover` test is
  written as `not (turnover <= max_turnover)`, so both NaN and `inf` fail it (fail-closed).
- **Absent fold consistency is skipped.** `foldIcSigns` is written by the search engine (the IC
  series split into at most 4 equal folds, each fold contributing the sign of its mean). A missing
  series -- or one that yielded fewer than two folds -- means "not evaluated", so that check is
  skipped.
- **L5's `trials` is the whole ledger.** The deflated-Sharpe denominator is **every candidate the
  run evaluated, rejections included**, not the survivor count. Using survivors would underprice
  the search itself and understate exactly the selection bias the correction exists for.
- **Thresholds are overridable.** A `POST /run` payload may carry a `thresholds` object whose keys
  are the `GateThresholds` field names (snake_case): `min_coverage`, `min_ic_observations`,
  `min_abs_rank_ic`, `min_icir`, `min_fold_consistency`, `max_turnover`,
  `max_builtin_correlation`, `max_pool_correlation`, `min_oos_abs_rank_ic`,
  `min_deflated_sharpe_probability`. `min_abs_rank_ic` is the run's one signal-strength bar and
  L6 reads the residual against the same value. An
  unrecognised key or a `null` keeps the default. A value must read as its own field's type: the
  integer-count threshold `min_ic_observations` accepts `30` and `30.0` but refuses `0.5` (which
  `int()` would silently turn into `0` and switch L2's observation floor off), and no threshold
  accepts a boolean or a non-finite number. A payload that is not an object, or a value that fails
  these rules, returns 400 with `factorMining.thresholdsInvalid` rather than a coerced bar.

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

**The pool increment is read on the search window.** L6 measures what a survivor adds to the
caller's frozen pool over the search window's own cross-sections, never over the tail: an increment
is a search-period judgment, and the frozen segment is the evidence that judges the candidate, not
evidence for it. The pool's members are rebuilt from their stored expressions against the very
field panels the search scored on, which is what makes a member comparable at all; the measurement
reads the search window's prepared frames, so bars a source returns past the window's end cannot
reach it.

**Every trial is on the ledger, failures included.** Each evaluated candidate writes one row into
`qd_factor_trials`, passes and rejections alike. That ledger's size is the deflated-Sharpe
denominator used at L5.

**A ledger row keeps only what the gates read.** The trial's `metrics` carries `coverage`,
`rankIc`, `icir`, `foldIcSigns`, `averageTurnover`, `stability`, the `executionAssumptions` block,
and `icObservations` -- the **length** of the IC series, under its own key, because L2 asks how many
observations there were and never what they were. The rest (the IC series itself, the group and long/short curves, the per-date cross-sections, the correlation matrix) is kept on the **candidate** only for a candidate that reached the ledger's pass side -- survivors, and the ones the frozen review then judged; a candidate rejected at L1--L4 never gets a candidate row, so its ledger row is all that remains of it. The ledger has one row per evaluation, so an untrimmed
row is what makes a run's record grow with the budget: at 200 symbols one is 0.78 MB, and a budget
of 400 would persist roughly a third of a gigabyte per run -- three times over, since the record is
written to `qd_factor_trials`, `qd_factor_mining_runs.result_json`, and `qd_agent_jobs.result`.

**Data fingerprint.** `summary.snapshotId` is a real fingerprint of the panel's contents: symbols
are visited in sorted order and each one's canonical OHLCV bytes are mixed into a `sha256` (the
symbol name is hashed too). It is a **fingerprint, not a replayable copy** -- v1 stores the digest
and no market snapshot.

**Warmup.** The fetch reaches back before the search start by an amount that depends on the search
mode, because the two modes know different things about what they will evaluate. **Enumeration**
knows exactly: every candidate comes from a template, so the run measures them before it fetches --
the same template selection, budget, and seed the search loop expands -- and asks for `max(40,
min(504, longest lookback + 5))` bars, converted to calendar days. Budgeting the DSL's 504 ceiling
instead would cost about 3.5x the provider history -- on a daily policy that is the difference
between a one-year and a four-month searchable window. **Evolution** knows nothing of the kind: a
crossover grafts an arbitrary subtree, with any window the DSL accepts, onto an arbitrary path, so
it takes the ceiling (`MAX_LOOKBACK=504` bars ≈ 953 calendar days) instead. Where a market-data
policy caps the fetched range, that ceiling gets the evolved run **refused outright** -- the shared
backtest range check raises before a bar is requested, and the job fails with
`strategyV2.backtestRangeLimit` -- rather than searched over a shorter window.

**Determinism and reproducibility.** The same panel plus the same run config -- every field of the
request, `searchMode` included -- produces the same trials and the same candidates in the same
order. `(templateIds, budget, seed)` is only part of it: the mode decides which sources contribute
candidates and how much history is fetched, and in evolution `population`, `generations` and
`eliteRatio` decide what it breeds from. Template expansion is seeded, the evolution stream draws
from `random.Random(seed)` alone, the gate ladder is pure, and nothing iterates a set.

## 7. HTTP surface

Prefix `/api/factor-mining`; the OpenAPI tag is **`FactorMining`**. Every route requires
authentication (`Authorization: Bearer <jwt>`). All responses use the standard envelope
`{"code": 1, "msg": "common.success", "data": ...}`; on error `code: 0`, `msg` is the i18n key from
section 11, and `data` is `null`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/catalog` | The DSL's own vocabulary: `fields`, `operators`, `templates`; optional `?market=` picks the field set (section 3.1) |
| `POST` | `/estimate` | Static price of a search, returning **exactly** `{templateCount, candidateBudget}` and nothing else; no source read, no panel, no job |
| `POST` | `/run` | Submit a mining run; **HTTP 202**, returns the job receipt |
| `GET` | `/jobs` | The caller's mining jobs, newest first, **without** `result` |
| `GET` | `/jobs/<job_id>` | One job, with `progress` and the full `result` |
| `POST` | `/jobs/<job_id>/cancel` | Cancel; the runner stops at its next progress report |
| `GET` | `/jobs/<job_id>/proposals` | An AI job's proposals in generation order (section 8); a running job returns the rows written so far |
| `GET` | `/candidates` | The candidate library, filtered and paged |
| `GET` | `/candidates/<candidate_id>` | One candidate, with `metrics` and `outOfSample` |

**`POST /run` request body.** The server reads only these keys and drops the rest, so a client's own
key never lands in the job store's request:

`sourceId`, `sourceName`, `market`, `timeframe`, `startDate`, `endDate`, `freezeRatio`, `budget`,
`seed`, `templateIds`, `groups`, `holdingPeriod`, `commission`, `slippage`, `neutralizeIndustry`,
`thresholds`, `searchMode`, `population`, `generations`, `eliteRatio`, `aiRounds`,
`aiProposalsPerRound`, `researchNotes`, `replayJobId`

Required: `sourceId` (positive integer), `market` (a known market key), and `startDate` / `endDate`
(`YYYY-MM-DD`). `budget` defaults to 400 and `timeframe` to `1D`.

**Search modes.** `searchMode` is `enumeration` (the default, the template expansion),
`evolution` (that same expansion plus the stream bred from it), or `ai` (pure model proposals,
section 8); any other value is refused with `factorMining.invalidParameter` rather than read as the
default, because the modes search different candidate sets over different amounts of history.
`population` (default 32), `generations` (20) and `eliteRatio` (0.25) are the breeding settings,
and they are **required — non-empty — of a run that breeds**: a value this layer invented would be
a search nobody asked for. They are read as an integer, an integer and a number respectively, and
bounds are enforced on the same key: `population` and `generations` must be >= 1, and `eliteRatio`
must fall in `[0, 1)` (a share of 1 or more leaves the breeding loop no room for children, and the
run would search the templates, breed nothing, and still be recorded as a success). In enumeration
and ai modes all three are optional and also bounded when supplied.

`aiRounds` (default 2, 1–3), `aiProposalsPerRound` (default 10, 1–20), `researchNotes` (plain
text, at most 8000 characters) and `replayJobId` (a job id) belong to the AI mode alone: a non-AI
run that carries a non-empty one is refused with `factorMining.invalidParameter`, not silently
ignored. The replay contract is in section 8.

**`/estimate` is unchanged by any of this.** It validates the same payload -- the mode, the
breeding settings and the AI fields included -- and still answers with **exactly**
`{templateCount, candidateBudget}`: two keys, and no echo of the mode, which the caller already
knows because it is what it submitted. For an AI run `templateCount` is `0` and
`candidateBudget` is `min(aiRounds * aiProposalsPerRound, budget)`: the AI stream is the whole
search, and the run budget still caps it.

**Job receipt / job object** (one field set for both):

```json
{"jobId": "<uuid>", "status": "queued", "progress": {},
 "config": {"sourceId": 12, "market": "USStock", "timeframe": "1D",
            "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42},
 "result": null, "hasResult": false, "error": null,
 "createdAt": "2026-10-13T09:00:00+00:00", "startedAt": null, "finishedAt": null}
```

**`config` is not withheld from the list view.** It is the subset of the run fields (the list at the
top of section 7) that the request actually carried, read out of the job's stored `request` by an
explicit whitelist -- that mapping also holds the job store's own `__userId` / `__billing` channel,
and neither leaves. It cannot be dropped the way `result` is: with the result withheld, `config` is
the only thing telling a poller which market, window and budget a listed job ran. A row with no
stored request reads as an empty object rather than an error; a job without a config is still worth
listing.

**`error` is one line.** A failed job's stored error is the exception plus a full traceback, but
that stack names the worker's absolute paths and module layout, and the job store already writes it
to the job's own log line -- so only the **first line** leaves. The guarantee is the job store's
(`agent_jobs.get_job`) and holds for **every job kind** -- backtest and strategy evolution
included -- rather than being special to this feature. A caller gets the reason it can act on
(usually one of the `factorMining.*` keys above); the stack stays with the operator. A job that
never failed keeps the `null` / `''` it was written with.

**The shape of `progress`** is `{"stage": "search" | "review", "evaluated": <int>, "total": <int>}`.
The search reports every ten candidates, and the stage switches to `review` when the frozen-segment
pass starts. It is `{}` before the job has run.

**List view versus detail view, for jobs.** `/jobs` answers every item with `result: null` and
`hasResult` saying whether there is one; the run record -- every surviving candidate's full metrics
and every trial's ledger row -- is served by `/jobs/<job_id>` alone. A page of twenty full records
is more JSON than a client polling for "the one that finished" can use. `POST /run`'s receipt is
the same envelope, and a queued job has no result yet, so `hasResult` is `false` there. `config` and
`progress` are served by both views.

**`GET /jobs` parameters:** `limit` (1-50, default 20).
**`GET /candidates` parameters:** `runId` (integer), `status` (`search` / `frozen` / `rejected`),
`market`, `limit` (1-200, default 50), `offset` (>= 0, default 0). An unreadable integer returns
400 with `factorMining.invalidQuery`.

**`?runId=` is not a stable view of "this run's candidates".** The library deduplicates by
`(user_id, expression_hash)`, and `runId` points at the run that judged an expression **last**, so
re-running an expression moves its row out of the older run's view and into the newer one. The
filter answers "which run last judged these rows", which is what the library is for; it does not
answer "what did run 7 evaluate".

**List view versus detail view.** `/candidates` **omits** `metrics` and `outOfSample` -- a
candidate's metrics carry its whole IC series and curves, which a page of fifty does not need. Read
`/candidates/<id>` for those two blocks.

**Candidate fields:** `id`, `userId`, `runId`, `expressionHash`, `expression`, `origin`,
`templateId`, `market`, `timeframe`, `status`, `reason`, `createdAt`, `updatedAt`; the detail view
adds `metrics` and `outOfSample`.

**`status` values:** `search` (cleared L1-L4, awaiting the frozen review), `frozen` (cleared L5 as
well, and not retired at L6), `rejected` (stopped at some stage; the key is in `reason` -- an L6
retirement carries `factorMining.reject.noIncrementalValue`, and an increment that could not be
measured retires nobody).

**`GET /catalog`:** `fields` is the market's own set, resolved through the same `fields_for` the
validator uses, so what the editor offers and what L1 accepts cannot drift apart. The optional
`market` query argument names the market the caller is about to run; absent, empty or unrecognised
returns the price-volume group (section 3.1). `operators` gives **names only**, with no arity and no
windowed flag -- section 3.2 above is the authoritative table. Each entry of `templates` is
`{templateId, hypothesisI18nKey, category, parameterGrid}`.

**The label inside the correlation study.** When a mined candidate enters L4's correlation study,
it is displayed under the factor name **`"expression"`** (the built-ins follow it). So
`metrics.factorCorrelation.factors[0]` is always `"expression"` and `matrix[0][1:]` holds its
correlation against each built-in.

## 8. AI proposals and the failure-feedback loop

`searchMode="ai"` replaces the template expansion with model proposals. The job asks the
server-configured LLM for hypotheses and expressions, validates every expression against the same
field and operator whitelist as any other candidate, and sends the legal ones through the
unchanged L1-L6 ladder. The template library is still given to the model as prior examples, but
it does not enter the candidate stream: every AI candidate has `origin = "ai"` and an empty
`templateId`. The trial ledger, candidate pool, frozen review and pool-increment stage are the
same as in the other modes.

**Request fields.** `aiRounds` (default 2, 1-3), `aiProposalsPerRound` (default 10, 1-20),
`researchNotes` (plain text, at most 8000 characters) and `replayJobId` (a job id) are the AI
fields of `POST /run`; carrying a non-empty one on a non-AI run is refused with
`factorMining.invalidParameter`. The model and provider are **not** request fields: they come from
the server's model configuration. The run's candidate budget is
`min(aiRounds * aiProposalsPerRound, budget)` -- the AI stream is the whole search, and the run
budget still caps it. `/estimate` returns that product as `candidateBudget` and `0` as
`templateCount`.

**One round, at most two model calls.** Each round's prompt carries the market, timeframe and data window,
the field catalogue listed in group order, the operator catalogue (arity and window), the candidate budget,
the cross-run failure summary, the previous round's feedback, the optional research notes, and up
to ten template examples as priors. Research notes and all historical material are delimited as
**data**: instructions inside them are not executed. The model must answer with strict JSON:
`{"proposals": [{"hypothesis": "...", "expression": {...}}]}`. The call has a 60-second timeout
and is retried once (two attempts per round) on transport or JSON failure; a round that still
fails fails the job with `factorMining.aiProposalFailed`, keeping the proposals already written.

**Validation, deduplication, and the ladder.** A proposal is not a candidate until it passes L1:
each expression is normalized with `allowed_fields=fields_for(market)`, so the model cannot use a
field or operator the template search could not. A validation failure is stored as
`status = "l1_rejected"` with the DSL error code in `reason`; a repeated fingerprint within the
run is `status = "duplicate"` with `factorMining.reject.duplicateProposal`. Neither is evaluated
nor written to the trial ledger. A legal proposal is written as `status = "proposed"` and becomes
a seed; once its evaluation outcome is known the row is marked `evaluated` (its L1-L6 verdict is in
the trial/candidate ledger, not in the proposal's `reason`). A round with no legal proposal stops
the stream early rather than spending another call; a run whose rounds all produce nothing fails
with `factorMining.aiNoValidProposals`. Only the first
`min(aiRounds * aiProposalsPerRound, budget)` legal proposals in `(round, ordinal)` order are
evaluated; legal proposals the model returned beyond that cap stay recorded as `proposed` and are
never evaluated.

**Failure feedback.** Feedback has two layers. Within a run, after a round's seeds have been
evaluated, the engine's in-memory outcomes are aggregated by stage and reject reason, with up to
10 failed expressions (hash, stage, reason and the expression itself) and up to 5 near misses
ordered by their relative distance to the threshold; the block is capped at 6000 characters and
becomes the next round's `Previous round feedback`. It must read the engine, not
`qd_factor_trials`: the service writes trials only after `engine.run` returns. Across runs, a live
run fetches the summary once before the first round from the caller's 5 most recent **successful**
runs on the same market and timeframe -- stage/verdict/reason counts plus up to 10 failed
expressions, capped at 6000 characters -- and every round's prompt carries it, space permitting.
A replay calls no model and reads no history. Failed and cancelled runs are not used as history.

**What is stored.** `qd_factor_proposals` holds one row per proposal (`run_id`, `round`,
`ordinal`, expression, hash, hypothesis, model, provider, prompt hash, DSL version, source,
status, reject reason). `qd_factor_mining_runs.job_id` (an added column) maps a job to its run, so
proposals are readable while the job runs and after it fails. Research notes are not copied into
`config_json` or the proposal rows: they travel with the job request (`qd_agent_jobs.request`,
tenant-scoped), and the summary stores only their SHA-256 and length. Prompts are not stored;
only their hashes are.

**AI summary.** `GET /jobs/<job_id>` adds `result.summary.ai` to an AI run's result:

```json
{"enabled": true, "rounds": 2, "proposals": 17, "l1Rejected": 3, "duplicates": 2,
 "evaluated": 12, "model": "...", "provider": "...", "modelFallbackUsed": false,
 "promptHashes": ["..."], "researchNotesSha256": "...", "researchNotesLength": 1234,
 "replayOf": "", "snapshotChanged": false}
```

`rounds` is the number of rounds actually run; `model` / `provider` identify the last answering
batch (empty on a replay, which calls no model). Non-AI runs do not carry `summary.ai`.

**Proposal endpoint.** `GET /api/factor-mining/jobs/<job_id>/proposals?limit=50&offset=0` returns
`{"items": [...]}` sorted by `(round, ordinal)`; `limit` is clamped to 200. Each item is
`{id, runId, round, ordinal, hypothesis, expression, expressionHash, status, reason, model,
provider, promptHash, dslVersion, source, createdAt}`. A running job returns the rows written so
far (possibly none); an unknown job, another tenant's job, a non-mining job, or a job that has not
opened its run all read as 404 `factorMining.jobNotFound`.

**Replay.** `replayJobId` names one of the caller's own **successful** AI runs. A replay request
needs only `searchMode="ai"` and `replayJobId`; the recorded run supplies the market, window,
budget, source and AI bounds. A client may restate `market`, `timeframe`, `startDate`, `endDate`,
`budget` or `thresholds`, but only with effective values equal to the record -- a disagreement is
`factorMining.aiReplayIncompatible`. `researchNotes` may be supplied afresh for the audit record;
it is not sent to a model. The replay copies the recorded proposals into a new run with
`source = "replay"`, re-runs L1 against the current DSL (a `dslVersion` mismatch is
`factorMining.aiReplayIncompatible`), and evaluates only the legal ones. The proposer is never
called on this path. Because the panel is refetched, `snapshotChanged` says whether its
fingerprint differs from the original run's: the proposals and their L1 statuses are reproduced,
but the metrics are a new measurement. An unknown or other-tenant `replayJobId` is
`factorMining.aiReplayNotFound`. A successful run with no recorded proposals (for example a
non-AI run) has nothing to replay and fails the new job with
`factorMining.aiNoValidProposals`.

**Limits.**

| Item | Value |
| --- | --- |
| Rounds | Default 2, maximum 3 (`aiRounds`) |
| Proposals per round | Default 10, maximum 20 (`aiProposalsPerRound`) |
| Valid proposals per run | The first `min(aiRounds * aiProposalsPerRound, budget)` in `(round, ordinal)` order are evaluated; later ones stay `proposed` |
| Model call | 60 s timeout; 1 retry (2 attempts per round) |
| Research notes | 8000 characters |
| Prompt | 24000 characters per round; optional material is truncated before the field/operator catalogue and constraints |
| Cross-run history | The 5 most recent successful runs |
| Feedback block | 6000 characters |

**Known limits.** The model and provider are deployment configuration, not per-run choices. The
feature does not fetch papers or reports and has no file/PDF ingestion; only pasted plain-text
notes are read. Proposal quality depends on the configured provider and model, and an AI proposal
still has to pass the unchanged L1-L6 ladder -- AI mode changes where hypotheses come from, not
what counts as evidence. Simulation/paper-run monitoring is not implemented. The feature remains
free of charge; each round is at most two model calls and the run is bounded as above.

## 9. Jobs, concurrency, and budget

**Durable execution.** A mining job travels through the shared `qd_agent_jobs` table with
`kind = "factor_mining"`, carried by the Celery durable path (`app/tasks/agent_jobs.py` registers
the kind). The in-process pool and Celery both end up calling one implementation,
`run_mining_job`; there are not two code paths.

**Two records, written at different moments.** The job row (`qd_agent_jobs`) is the timeline the API
polls. The run row (`qd_factor_mining_runs`) is the audit record, and it is opened **before the
search starts**, so a crash mid-run still leaves a row saying which config was being searched.

**Concurrency cap.** One tenant may have **1 active mining job** at a time. Hitting it returns 400
with `factorMining.concurrentLimit`. The gate counts that tenant's `queued` / `running` rows **of
`kind = "factor_mining"`** -- the Agent Gateway's tenant-wide cap still counts every kind, but this
feature's own limit is its own: a running backtest is not a mining slot, and an unswept ghost from
another feature (which this feature's sweep does not retire) cannot block mining forever.

**Budget caps.**

| Item | Value |
| --- | --- |
| `DEFAULT_BUDGET` | 400 |
| `MAX_BUDGET` | 2000 (above it, `factorMining.budgetExceeded`) |
| Minimum usable budget | 1 (below 1, or unreadable, `factorMining.budgetInvalid`) |

`budget` is a **cap, not a promise**: when the template set expands to fewer than 400 distinct
candidates, the run evaluates exactly what expanded (102 for the default template set). An AI
run's stream is capped at `min(aiRounds * aiProposalsPerRound, budget)` for the same reason, and
the model may produce fewer legal proposals than either bound; the run evaluates what passed L1.

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

## 10. Data tables

| Table | Granularity | Key constraints |
| --- | --- | --- |
| `qd_factor_mining_runs` | One row per run | Config, data fingerprint, three counts, status, and `job_id` (the job that drives the run); `user_id` is a foreign key to `qd_users` |
| `qd_factor_candidates` | One row per distinct expression | `UNIQUE (user_id, expression_hash)`, so it deduplicates across runs; `run_id` is `ON DELETE SET NULL` -- a candidate is an asset that outlives runs, so only its pointer may go with one |
| `qd_factor_trials` | One row per evaluation | `run_id` is `ON DELETE CASCADE` -- a ledger detached from the run that priced it means nothing |
| `qd_factor_proposals` | One row per AI proposal | `UNIQUE (run_id, round, ordinal)`, ordered by `(round, ordinal)`; `run_id` is `ON DELETE CASCADE`; `user_id` scopes every read, and `status` / `reject_reason` carry the proposal's own audit state (section 8) |

Every write is tenant-scoped and **fails audibly**: writing against a run that is not the caller's
raises (`factorMining.runNotFound`) instead of returning a row count for rows that were never
written. That silent version would lose a 250-trial ledger without a trace.

**Where the pool increment lives.** A survivor's increment is written into its `outOfSample` block
-- the `out_of_sample_json` column on `qd_factor_candidates`, served as `outOfSample` by
`/candidates/<id>` -- under `poolIncrement`:

| Key | Meaning |
| --- | --- |
| `residualIc`, `rawIc` | Mean residual IC and mean raw IC over the measured sessions; `null` when nothing was measured (a partial mean is not a reading) |
| `poolSize` | The pool's size after the run's cap and self-exclusion |
| `compared` | The member names a panel **could** be rebuilt for -- what the reading actually covers |
| `skipped` | The members that could not be rebuilt; **skipping is not a pass** |
| `complete` | `true` exactly when `skipped` is empty, i.e. the reading covers the whole pool |
| `hasIncrementalValue` | `true` / `false` / `null` (not measured -- three states a consumer has to tell apart) |
| `reason` | Empty when the candidate carries value; `factorMining.reject.noIncrementalValue` when it was measured and does not; `factorMining.poolIncrement.noPool` / `.unmeasured` when there is no verdict |
| `usedCrossSections`, `skippedCrossSections` | Sessions the reading averages over, and sessions it had to drop |
| `minAbsRankIc` | The bar the residual IC was read against, restated so a stored reading explains its own verdict |

**A consumer reads `complete` and the two counts, never `hasIncrementalValue` alone.** A truncated
pool still judges, deliberately: the alternative is a stage that falls silent the moment one stored
expression stops rebuilding, and a `false` there is decided by the members that *were* measured.
What may not happen is the truncation going unmentioned -- so a reader that shows
`hasIncrementalValue` without `complete`, `poolSize` and `compared` renders "this library was
compared by half" and "nothing adds anything to this library" identically, and the second is the
only one the reading supports.

## 11. i18n keys

The front-end copy lives in another repository. These are **all** the `factorMining.*` keys the
backend can emit; the backend emits keys only and never renders copy.

**API and service layer (28 keys)**

| Key | When it fires |
| --- | --- |
| `factorMining.runFailed` | An unexpected server-side failure while submitting (500) |
| `factorMining.estimateFailed` | An unexpected server-side failure while pricing a search (500) |
| `factorMining.jobNotFound` | The job does not exist, is not the caller's, or is not a mining job |
| `factorMining.candidateNotFound` | The candidate does not exist or is not the caller's; also when the id is not an integer |
| `factorMining.invalidQuery` | A query parameter is not a readable integer |
| `factorMining.invalidParameter` | A run field cannot be read as the type it is priced with: an integer field (`seed` / `groups` / `holdingPeriod` / `population` / `generations` / `aiRounds` / `aiProposalsPerRound`) that is not an integer, an `eliteRatio` that is not a number, a `commission` / `slippage` too large to convert, a `searchMode` this build does not know, a `timeframe` outside the supported shape (`<digits><m\|h\|d\|w>`), a `population` / `generations` below 1, an `eliteRatio` outside `[0, 1)`, an `aiRounds` outside `1-3`, an `aiProposalsPerRound` outside `1-20`, a `researchNotes` that is not a string or exceeds 8000 characters, any AI field on a non-AI run (`aiRounds` / `aiProposalsPerRound` / `researchNotes` / `replayJobId`), or one of `population` / `generations` / `eliteRatio` missing or blank in an evolution run |
| `factorMining.thresholdsInvalid` | `thresholds` is not an object, or a threshold cannot be read as its own field's type (a fractional `min_ic_observations`, a boolean, a non-finite value) |
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
| `factorMining.aiProposalFailed` | The model call failed on transport or JSON after its one retry; proposals already written are kept |
| `factorMining.aiNoValidProposals` | Every round of an AI run produced zero legal proposals |
| `factorMining.aiReplayNotFound` | `replayJobId` names no job of the caller's, or one that is not a mining job |
| `factorMining.aiReplayIncompatible` | The recorded run is not a successful run, its DSL version differs, or a restated run field disagrees with the record |
| `factorMining.runNotFound` | A write targeted a run that does not exist or is not the caller's |

**Parameterized forms (3, so a consumer must not match on a bare key)**

| Form | Source |
| --- | --- |
| `factorMining.templateNotFound:<template_id>` | `templateIds` contained an unknown template id |
| `factorMining.fingerprintInvalid:<index>` | The `<index>`-th candidate's fingerprint is not a 64-character sha256 hex digest |
| `factorMining.reject.invalid:<reason>` | The specific failure a DSL validation carried on `ExpressionError.code` (`unknownField` / `tooDeep` / `tooManyNodes` / `unknownOperator` / `badArity` / `badNode` / `badWindow` / `lookbackExceeded`) |

**Reject reasons (15 keys)**

| Key | Stage |
| --- | --- |
| `factorMining.reject.duplicateProposal` | AI proposal stream (a repeated fingerprint within the run; status `duplicate`, never evaluated) |
| `factorMining.reject.invalid` | L1 (the trial's and candidate's `reason` column holds this **bare key**; the `:<reason>` form appears only on the `ExpressionError.code` the DSL raises) |
| `factorMining.reject.lowCoverage` | L2 |
| `factorMining.reject.insufficientObservations` | L2 |
| `factorMining.reject.weakIc` | L2 |
| `factorMining.reject.weakIcir` | L3 |
| `factorMining.reject.unstableFolds` | L3 |
| `factorMining.reject.unstableHalves` | L3 |
| `factorMining.reject.highTurnover` | L3 |
| `factorMining.reject.duplicateBuiltin` | L4 |
| `factorMining.reject.duplicateInPool` | L4 |
| `factorMining.reject.unverifiableRedundancy` | L4 |
| `factorMining.reject.weakOutOfSample` | L5 |
| `factorMining.reject.selectionBias` | L5 |
| `factorMining.reject.noIncrementalValue` | L6 |

**Pool increment readings (2 keys, deliberately *not* rejections)**

| Key | Where it appears |
| --- | --- |
| `factorMining.poolIncrement.noPool` | `outOfSample.poolIncrement.reason`, when the caller has no frozen library yet -- the question was never asked |
| `factorMining.poolIncrement.unmeasured` | `outOfSample.poolIncrement.reason`, when a comparison was drawn and nothing could be measured off it, or when the review had no search window behind it |

Neither key retires a candidate and neither appears in a candidate's `reason` or a trial's `reason`:
they are the two "no answer" states that sit beside the verdict in the increment's own block
(section 10), and an operator sent to the wrong one of the two looks for a library in the wrong place.

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

## 12. Known non-goals and open items

- **No billing.** The feature debits nothing and writes no ledger; resources are bounded only by
  the concurrency and budget caps.
- **`queued` ghosts are not swept.** Only `running` stale jobs are collected; jobs stuck `queued`
  are out of scope (deferred).
- **`/catalog`'s `operators` carries names only** -- no arity and no windowed flag. The table in
  section 3.2 is the authoritative source.
- **No strategy-runtime integration.** Mining output cannot be referenced by a strategy, and there
  is no user-scoped read path to it.
- **No external research ingestion.** AI mode reads only what the prompt carries: the catalogue,
  the run parameters, the failure summaries and the caller's pasted plain-text notes. Papers,
  reports, file uploads and PDF parsing are out of scope.
- **Model choice is deployment configuration.** A run cannot name a model or provider; it uses the
  server's configuration, and an unconfigured or failing provider fails the AI job
  (`factorMining.aiProposalFailed`) rather than falling back to templates. Proposal quality is
  therefore a property of the deployment, not a guarantee of the feature.
- **No simulation/paper-run monitoring.** Checking a mined candidate on a simulated forward run is
  still out of scope.
- **Replay is proposal replay, not data replay.** `replayJobId` never calls the model and
  reproduces the proposal set and its L1 statuses, but it refetches the panel; `snapshotChanged`
  reports when the fingerprint moved.
- **Fingerprint only.** There is no replayable panel copy, so past runs cannot be re-run offline.
- **Term structure and roll cost remain out of scope for traditional futures**, which would need a
  multi-contract futures data face. Crypto dated futures are covered: `term_structure` and
  `roll_cost` are in the `Crypto` whitelist and sourced from Deribit's BTC/ETH monthly contracts
  under the point-in-time rules in section 3.1.

Related: [Strategy workflow](STRATEGY_WORKFLOW.md), [Backtest Center](BACKTEST_CENTER.md),
[Strategy API V2 development](../trading/STRATEGY_DEV_GUIDE.md),
[live-trading safety](../trading/LIVE_TRADING_SAFETY.md).
