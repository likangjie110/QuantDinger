# 全自动量化因子挖掘（Factor Mining）设计规格

> 状态：待评审 | 日期：2026-10-13 | 适用仓库：QuantDinger（main @ 209decd）

## 1. 现状（已核实）

| 事实 | 位置 |
| --- | --- |
| 因子目录是**代码级不可变注册表**：61 个手写因子（54 技术 + 7 基本面） | `backend_api_python/app/services/factors/registry.py:157` |
| TA-Lib 目录适配（枚举既有函数，非挖掘） | `app/services/factors/talib_adapter.py` |
| `FactorResearchEngine` 做**单因子**时点化截面诊断（IC/ICIR/分组/成本/换手/覆盖率/单调性/稳定性/与内置因子相关性） | `app/services/strategy_v2/factor_research.py:11` |
| 唯一入口 `POST /api/backtest/factor-research`，一次只收一个 `factorId` | `app/routes/backtest_center.py:222`、`:234` |
| 结果落库 `qd_factor_research_runs` | `migrations/20260716_factor_research_runs.sql` |
| 最接近“自动搜索”的 `strategy_evolution` 搜的是**策略参数**，不是因子 | `app/services/strategy_evolution/` |
| 调度器无任何因子相关 job | `app/celery_app.py:62` |

**结论**：候选自动生成、批量评估、去重、冻结、复盘的闭环不存在。

## 2. 目标与非目标

### 2.1 目标（v1 验收）

1. 给定市场/资产池、周期、区间与预算，一次作业自动完成：表达式候选生成 → 逐级评估淘汰 → 与内置因子去重 → 冻结段样本外复评 → 全量试验留痕。
2. 任意结论可复现：同 seed + 同配置 + 同数据指纹 → 同候选集与同指标。
3. 全部候选（含失败）可查询，试验次数用于选择偏差校正。

**全自动覆盖的是研究流程，不承诺挖出的因子持续盈利。**

### 2.2 非目标

- **不接入策略运行时**：`StrategyV2BacktestRunner`（`runtime.py:1700`）与 `StrategyV2LiveSession`（`runtime.py:2250`）都没有 `user_id`，`context.factor()` 直连全局 `get_factor()`。让策略调用挖掘因子需要把用户作用域贯穿回测与实盘评估进程，触碰多租户隔离不变量 → 独立高风险计划。
- **不做 AI/LLM 提案**：属第三版。
- **不把通过因子写回 `_FACTORS`**：那是不可变代码注册表，回写源码不可测试、不可回滚、污染仓库。
- v1 不做跨市场字段适配（加密资金费率/持仓量、期货期限结构）。

## 3. 关键概念

| 概念 | 定义 |
| --- | --- |
| 原料 | 行情、财务、资金费率、持仓量等原始数据 |
| 候选因子 | 由原料经算子构造的**计算表达式** |
| DSL 表达式 | JSON 表达式树；唯一候选表示。模板、搜索算法、后续 AI 都输出同一种表达式 |
| 试验 trial | 一次评估尝试（含失败）；是选择偏差校正的分母 |
| 冻结 frozen | 通过全部门槛并完成样本外复评的候选 |

## 4. 仓库适配决策（对通用建议的偏离）

通用技术建议（Parquet + DuckDB + SQLite + 独立后台进程 + Java 后端托管 API）不含本仓库上下文，逐条替换：

| 通用建议 | 本仓库采用 | 依据 |
| --- | --- | --- |
| 元数据 SQLite | PostgreSQL 新表 + `migrations/*.sql` | `AGENTS.md`：PG 是 durable source of truth |
| Parquet/DuckDB 数据层 | 复用 Strategy V2 数据面：`resolve_candidates` / `fetch_frames` / `fundamental_data.enrich_panel`，数据版本用按标的逐个规范化后合并的 sha256 指纹（**不复用** `MarketDataSnapshotStore`：它只规范化扁平 OHLCV 帧，多标的面板只能得到日历指纹，且会写入没有任何加载路径的文件） | `app/services/strategy_v2/service.py:438,466`、`snapshot.py:18`；自建数据层会破坏时间口径一致性 |
| 独立后台任务进程 | `agent_jobs.submit_job(kind="factor_mining")`（celery-worker 执行，本地回退线程池） | `docs/architecture/README.md` 进程所有权：celery-worker owns 有限、可重试任务；`app/utils/agent_jobs.py:81` |
| 自建评估器 | 复用 `FactorResearchEngine` 口径 | 单因子诊断已在使用，口径不一致则研究结论不可比 |
| 自建滚动验证与统计 | 复用 `strategy_evolution.statistics.deflated_sharpe` / `probability_of_backtest_overfitting` / `monte_carlo_bootstrap` 与 `walk_forward.build_walk_forward_plan` | `statistics.py:61,96,129`、`walk_forward.py:66` |
| 面向用户文案 | 返回 i18n 键 `factorMining.*` | 与 `strategyEvolution.*`、`factor.*` 一致；文案在外部前端仓库 |

## 5. 模块与文件职责

```
backend_api_python/app/services/factor_mining/
    __init__.py     公开导出
    dsl.py          表达式 AST：节点 schema、字段白名单、约束校验、规范化 JSON、sha256 指纹
    operators.py    算子白名单与向量化面板代数（缺失/零分母/窗口语义在此固定）
    evaluator.py    表达式 → 因子面板求值（含公共子表达式缓存）
    templates.py    金融假设模板库 + 参数网格 + 预算内确定性候选展开
    gates.py        评估阶梯门槛（纯函数：指标 → 判决 + 淘汰原因键）
    engine.py       编排：数据准备 → 生成 → 求值 → 逐级淘汰 → 冻结段复评 → 汇总
    service.py      应用服务：user 作用域、作业提交/取消/进度、查询
    storage.py      PG 仓储（runs / candidates / trials）
backend_api_python/app/routes/factor_mining.py   Human HTTP 门面
backend_api_python/migrations/20261013_factor_mining.sql
```

分层遵循 `docs/architecture/MODULE_BOUNDARIES.md`：routes 只做解析/鉴权/映射；services 承载用例；DB 细节不外溢到 route。

## 6. DSL 规格

### 6.1 节点 schema

```json
{"op": "ts_mean", "args": ["close"], "params": {"n": 20}}
```

- 叶子：字符串字段名（必须在字段白名单内）。数值字面量不作为叶子，需要常量时写零元算子 `const`（见 6.3）。
- 内部节点：`op` 必须在算子白名单内；`args` 为子节点（或字段名）列表；`params` 为标量参数。
- 规范化：键序固定（`op` → `args` → `params`）、`params` 键排序、数字统一 `repr`；指纹 = `sha256(canonical_json)`。
- 新增算子不改 schema，只加白名单条目并递增 `DSL_VERSION`。

### 6.2 字段白名单（v1，量价）

`open`、`high`、`low`、`close`、`volume`（来自 Strategy V2 frame）。派生量（如 vwap）用表达式合成，不新增字段。

基本面字段（`pe_ratio`、`return_on_equity`、`market_cap` 等）**留到 v1.1**：启用前须先验证 `fundamental_data.enrich_panel` 的点时可用性（按实际发布时点），否则字段存在但口径不可靠。

> 规则：只能使用实际拥有、且时间口径可靠的数据。没有历史订单簿，生成器就不得提出依赖买卖盘深度的候选 —— 白名单机制在 L1 强制这一点。

### 6.3 算子白名单与语义

**时序（按 symbol 列滚动，`min_periods = n`，窗口不足 → NaN）**

| 算子 | 语义 |
| --- | --- |
| `lag(x, n)` | `x.shift(n)`，要求 `n >= 1` |
| `delta(x, n)` | `x - lag(x, n)` |
| `ts_mean / ts_sum / ts_min / ts_max(x, n)` | 标准滚动聚合 |
| `ts_std(x, n)` | 滚动标准差，`ddof = 1` |
| `ts_rank(x, n)` | 窗口内最后值的百分位排名 `[0,1]` |
| `ts_zscore(x, n)` | `(x - ts_mean) / ts_std`；`std` 为 0 或 NaN → NaN |
| `ts_corr(x, y, n)` | 窗口内 Pearson 相关 |
| `ts_pct_change(x, n)` | `x / lag(x, n) - 1` |

**截面（按行跨 symbols）**

| 算子 | 语义 |
| --- | --- |
| `cs_rank(x)` | 行内 pct 排名 `[0,1]` |
| `cs_zscore(x)` | 行内 `(x - mean) / std(ddof=0)`；`std = 0` → 0（对齐 `factors/research.py:winsorize_zscore`） |
| `cs_demean(x)` | `x - 行均值` |

**常量**：`const`（arity 0，无窗口，参数 `value` 为有限数）—— 表达式内的数值字面量一律写成 `const` 节点（如 `where(cond, x, const(0))`）；裸数字仍是非法节点。

**算术与安全**：`add / sub / mul / div`（二元）、`neg / abs / sign / log / sqrt`（一元）、`safe_div(x, y)`（分母为 0 或 NaN → NaN）、`log`（`x <= 0` → NaN）、`sqrt`（`x < 0` → NaN）、`clip(x, lo, hi)`、`gt / lt / ge / le`（返回布尔面板）、`where(cond, a, b)`。

**未来数据防护**：算子集合内**不存在负向位移**；`lag` 强制 `n >= 1`；任何算子窗口参数不接受 0 或负数。

### 6.4 约束常量

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `MAX_DEPTH` | 6 | 表达式树深度（根为 1） |
| `MAX_NODES` | 24 | 节点总数 |
| `WINDOW_MIN` / `WINDOW_MAX` | 1 / 252 | 单算子窗口。下界为 1：1 根 bar 的收益/位移是标准写法（低波、风险调整动量模板依赖 `ts_pct_change(close, 1)`）；退化的窗口（`ts_std`/`ts_zscore`/`ts_corr` 在 `n = 1`）产出 NaN 而非错误值，由 L2 的覆盖率门槛拦截。`0` 与负数一律拒绝，前视防护不受影响 |
| `MAX_LOOKBACK` | 504 | 任一叶子到根路径上的窗口之和 |
| `DSL_VERSION` | 1 | 语义版本，随语义变更递增并计入候选指纹 |

## 7. 模板库（v1 重点：可解释、易排查）

每个模板 = 金融假设 + 参数化表达式 + 参数网格。展开后每格是一条候选，另加“平滑去噪”变体 `ts_mean(expr, k)`，`k ∈ {3, 5}`。

| # | 模板 | 假设 | 表达式骨架 | 参数网格 |
| --- | --- | --- | --- | --- |
| 1 | `momentum` | 强者恒强 | `ts_pct_change(close, N)` | `N ∈ {5,10,20,60,120}` |
| 2 | `reversal` | 短期超跌反弹 | `neg(ts_pct_change(close, N))` | `N ∈ {3,5,10,20}` |
| 3 | `low_volatility` | 低波跑赢 | `neg(ts_std(ts_pct_change(close,1), N))` | `N ∈ {10,20,60}` |
| 4 | `liquidity` | 量能水平与变化含信息 | `cs_rank(ts_mean(volume, N))`、`safe_div(ts_mean(volume,N), ts_mean(volume,M))` | `N ∈ {5,10,20}`，`M ∈ {20,60}` |
| 5 | `volume_price_confirm` | 放量时的动量含额外信息 | `mul(cs_rank(ts_pct_change(close,N)), cs_rank(safe_div(volume, ts_mean(volume,M))))` | `N ∈ {5,20,60}`，`M ∈ {5,20}` |
| 6 | `risk_adjusted_momentum` | 单位风险收益更稳 | `safe_div(ts_pct_change(close,N), ts_std(ts_pct_change(close,1),N))` | `N ∈ {20,60,120}` |
| 7 | `conditional_filter` | 条件成立才取信号 | `where(gt(ts_rank(ts_mean(volume,M),120), 0.8), ts_pct_change(close,N), 0)` | `N ∈ {5,20}`，`M ∈ {5,20}` |

模板目录与元数据（假设描述、参数、i18n 键）通过 `GET /api/factor-mining/catalog` 暴露，供界面解释与排查。

## 8. 候选生成规格（v1）

- 输入：模板子集（默认全部）、预算 `budget`（默认 400，硬上限 2000）、`seed`（默认 42）。
- 算法：模板 × 参数网格笛卡尔展开；组合数超过预算时，复用 `strategy_evolution.search.ParameterSampler` 的 reservoir 采样（保序、确定性）截断。
- 输出：`[{expression, expression_hash, origin, template_id}]`，按 `expression_hash` 去重后保持稳定顺序。
- **确定性要求**：同 `(templates, budget, seed)` → 字节级相同的候选列表与顺序。这是可复现验收的一部分。

## 9. 评估阶梯与门槛

逐级淘汰，控制算力（合法性 → 快速筛选 → 稳定性 → 组合验证 → 冻结后测试）。

| 级 | 检查 | v1 默认门槛 | 淘汰原因键 |
| --- | --- | --- | --- |
| L1 | DSL 合规、字段可用、深度/节点/回看合规、可求值 | — | `factorMining.reject.invalid` |
| L2 | 覆盖率、观测数、信号强度、非常量 | `coverage >= 0.60`；IC 观测数 `>= 30`；`abs(rankIC) >= 0.02` | `.lowCoverage` / `.insufficientObservations` / `.weakIc` |
| L3 | ICIR、滚动折一致性、前后半一致、换手 | `ICIR >= 0.30`；折 IC 同号率 `>= 0.60`；前半/后半 IC 同号；平均换手 `<= 0.70` | `.weakIcir` / `.unstableFolds` / `.unstableHalves` / `.highTurnover` |
| L4 | 与内置 61 因子相关性去重 | 与任一内置因子 `abs(corr) >= 0.85` → 淘汰；NaN/缺失条目视为「无重复证据」而忽略；**过滤后无任何有限值 → 淘汰** | `.duplicateBuiltin` / `.unverifiableRedundancy` |
| L5 | 冻结段样本外 + 选择偏差校正 | OOS `abs(rankIC) >= 0.01` 且与 IS 同号；`deflated_sharpe(observed_sharpe, trials=该 run 全部 trial 数).probability >= 0.95` | `.weakOutOfSample` / `.selectionBias` |

门槛全部来自 run 配置，可覆盖但覆盖后在 run 记录留痕。L4 的候选间数值去重与因子池增量评估属 v2。

## 10. 时间口径与防偏差约定（必须落代码）

1. **按当时可知的数据计算**：资产池用 `UniverseService.resolve_members(as_of=...)` 的时点成员；`FactorResearchEngine` 已有 `membership` 面板与 `pointInTimeUniverseApplied` 标记。
2. **信号时间与成交时间一致**：信号用 `t` 收盘，入场 `t+1` 开盘，出场 `t+holding+1` 开盘（沿用 `FactorResearchEngine` 的 `executionAssumptions`：`signal=close_point_in_time`、`entry=next_bar_open`）。
3. **前视防护机器化**：算子层“前缀不变性”测试 —— 改动未来行不得改变历史因子值。
4. **测试集隔离**：搜索与门槛只看 IS + validation；冻结段只在 L5 读一次。机器化检验：把冻结段数据替换为噪声后，候选集与 IS 指标必须不变。
5. **记录全部试验**：参数变体、失败候选全部写 `qd_factor_trials`；`trials_count` 即选择偏差校正的分母。
6. **成本与成交约束按市场配置**：手续费、滑点走 `parse_rate` + 默认值；加密资金费、期货换月成本列为 v2 的市场适配项（v1 只做量价 + 手续费/滑点）。

## 11. 数据模型（PostgreSQL，3 张表）

- `qd_factor_mining_runs`：一次作业（配置、数据指纹、状态、计数）。
- `qd_factor_candidates`：去重后的候选（表达式、指纹、来源、状态、指标、样本外指标），`UNIQUE (user_id, expression_hash)`。
- `qd_factor_trials`：每次评估一行（stage、verdict、reason、指标），随 run 级联删除。

索引：`(user_id, id DESC)`、`(run_id, stage)`、`(user_id, expression_hash)`、`(user_id, status, id DESC)`。
迁移同时写入 `migrations/20261013_factor_mining.sql` 与 `migrations/init.sql`（全新安装权威路径），保持幂等（`IF NOT EXISTS`）。

## 12. HTTP 契约（Human API）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/factor-mining/run` | 提交挖掘作业 → `202 {jobId, status}` |
| POST | `/api/factor-mining/estimate` | 预算预估（**候选数与模板数**——实现只承诺这两项，落库/耗时预估不在 v1 契约内） |
| GET | `/api/factor-mining/jobs` | 作业列表（`limit` <= 50） |
| GET | `/api/factor-mining/jobs/<job_id>` | 作业详情（含 `progress`） |
| POST | `/api/factor-mining/jobs/<job_id>/cancel` | 取消（协作式） |
| GET | `/api/factor-mining/candidates` | 候选库（按 `runId` / `status` / `market` 过滤） |
| GET | `/api/factor-mining/candidates/<candidate_id>` | 候选详情（含表达式与全部指标） |
| GET | `/api/factor-mining/catalog` | 字段库 + 算子库 + 模板库元数据 |

- 全部 `@login_required`；越权访问返回 404（不泄露存在性）。
- 封套 `{"code": 1, "msg": "success", "data": ...}`；错误 `code: 0` + i18n 键。
- 错误键：`factorMining.runFailed` / `.jobNotFound` / `.candidateNotFound` / `.sourceRequired` / `.dateRangeRequired` / `.budgetExceeded` / `.concurrentLimit` / `.universeTooSmall` / `.noMarketData` / `.invalidMarket`。
- 新 tag `FactorMining`；改完必须重新导出 OpenAPI 并跑 `tests/test_openapi.py`。

## 13. 分期

| 版本 | 内容 | 验收重点 |
| --- | --- | --- |
| **v1（本计划）** | 单市场单周期跑通；模板库 + 参数扩展 + 预算内采样；L1–L5 门槛；试验留痕；Human API；可复现 | 每个结果能复现，时间口径正确 |
| **v2** | 表达式树进化/交叉的受约束组合搜索；候选间数值去重；因子池增量评估（协同价值）；跨市场字段（资金费率、持仓量、期限结构、换月成本）；加密交割合约的期限结构/换月成本已落地，传统期货仍待独立数据面 | 新候选是否带来增量信息 |
| **v3** | AI 提案（读论文/研报/失败记录 → 输出假设 + 表达式，受同一字段与算子白名单约束）；失败反馈循环；模拟运行监控 | 固定预算下提高研究产出 |

v1 优先投入顺序：**数据时间口径 > 评估器 > 试验记录**。这三块可靠之后，换成遗传规划、强化学习或 LLM 生成候选才有可信的比较依据。

## 14. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 计算量爆炸（数百候选 × 数百标的 × 数年日线） | 向量化面板算子 + 逐级淘汰 + 候选预算上限 + `pytest.ini` 的 `timeout=120` 约束单测规模 |
| 耦合 `FactorResearchEngine` 时回归既有单因子研究 | 只新增表达式入口，不改既有分支；参数化测试锁住现有行为 |
| 数据字段与点时口径不满足假设 | v1 只用量价字段；基本面留 v1.1，启用前先验证点时可用性 |
| 过拟合与选择偏差 | 冻结段隔离 + DSR 校正 + 全量试验留痕三件套 |
| 覆盖无关工作区改动 | 文档任务只追加；开工前复核 `git status`，不假定与规格写作时一致 |

## 15. 未决事项

1. 通过因子如何对外消费：是否需要只读的“挖掘因子研究入口”（复用 `POST /api/backtest/factor-research`，接受 `expressionHash` 替代 `factorId`）—— 本计划**不做**，v1 只提供候选库与指标。
2. 是否给挖掘作业加计费/配额：v1 只做每用户并发上限与预算硬上限。
3. Agent Gateway / MCP 暴露：v2 评估，需同步 `docs/agent/agent-openapi.json` 与契约对齐测试。
