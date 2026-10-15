# 全自动因子挖掘

全自动因子挖掘把「提假设 → 造表达式 → 逐层筛选 → 样本外复核 → 沉淀候选库」这条研究流程自动化。它覆盖的是**研究流程**，不是收益：挖掘出的因子**不承诺持续盈利**，冻结段表现也只是样本外的一次测量，不是未来收益的保证。

本页面向使用者与前端文案维护者，说明模块边界、表达式 DSL、模板库、评估阶梯、时间口径、HTTP 接口、作业与预算限制，以及全部 i18n 键。

## 1. 模块定位与边界

**做什么**

- 从 8 个金融假设模板展开成一批确定性的候选表达式，逐个打分并留下完整试验台账。
- 用分级门槛（L1–L5）把候选从「合法」一路筛到「在冻结段仍有样本外证据」。
- 把结果沉淀进候选库 `qd_factor_candidates`：同一表达式跨多次运行去重，`run_id` 指向最近一次判定它的运行。
- 全过程按用户隔离，可按作业取消，可事后审计（配置、数据指纹、计数、逐条淘汰原因都落库）。

**v1 明确不做**

| 非目标 | 说明 |
| --- | --- |
| 不接入策略运行时 | 挖出的候选只存在于 `qd_factor_candidates`。策略侧仍然只能引用内置因子注册表 `_FACTORS`（`app/services/factors/registry.py`），没有任何用户作用域的路径让策略读到挖掘结果。 |
| 不做 AI 提案 | v1 的候选全部来自模板库的参数展开，没有模型参与生成假设（留待 v3）。 |
| 不写回因子注册表 | 挖掘结果不会变成新的内置因子，`_FACTORS` 是代码里静态维护的。 |
| 不计费 | 本功能**完全不计费**：既不扣额度也不写账本。资源只由并发上限与预算上限约束（见第 8 节）。 |
| 不存可重放副本 | 只保存数据指纹 `snapshotId`，不保存行情快照本身，因此不能离线重放一次历史运行。 |

## 2. 复现一次挖掘

前置条件：一个 **portfolio 类型**的 Strategy V2 脚本源（`sourceId`），其 universe 在所选区间内至少能解析出 3 个可用标的。

```bash
# 1) 先估价：不读数据源、不建面板、不开作业
curl -s -X POST http://127.0.0.1:5000/api/factor-mining/estimate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sourceId": 12, "market": "USStock", "timeframe": "1D",
       "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42}'
# → {"code":1,"msg":"common.success","data":{"templateCount":8,"candidateBudget":50}}

# 2) 提交作业，拿回作业回执（HTTP 202）
curl -s -X POST http://127.0.0.1:5000/api/factor-mining/run \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sourceId": 12, "market": "USStock", "timeframe": "1D",
       "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42}'
# → {"code":1,...,"data":{"jobId":"<uuid>","status":"queued",...}}

# 3) 轮询到终态
curl -s http://127.0.0.1:5000/api/factor-mining/jobs/<jobId> -H "Authorization: Bearer $TOKEN"
# status ∈ queued | running | succeeded | failed | cancelled

# 4) 浏览候选库
curl -s "http://127.0.0.1:5000/api/factor-mining/candidates?status=frozen&limit=50" \
  -H "Authorization: Bearer $TOKEN"
```

终态 `succeeded` 后，`GET /jobs/<jobId>` 的 `result` 里能看到 `summary.snapshotId`（数据指纹）、`summary.trials` / `summary.candidates` / `summary.frozen` 三个计数，以及 `split` 给出的搜索段与冻结段区间。

## 3. 表达式 DSL

一个候选因子是一棵 JSON 表达式树：叶子是字段名，内部节点是 `{"op": ..., "args": [...], "params": {...}}`。**数字不能作为裸叶子**，字面量只能通过零元算子 `const` 进入表达式。

### 3.1 字段白名单

只有 5 个价量字段：`open`、`high`、`low`、`close`、`volume`。（`PRICE_VOLUME_FIELDS`）

### 3.2 算子（31 个）

| 类别 | 数量 | 算子 |
| --- | --- | --- |
| 时序（带窗口参数 `n`） | 11 | `lag` `delta` `ts_mean` `ts_sum` `ts_std` `ts_min` `ts_max` `ts_rank` `ts_zscore` `ts_pct_change` `ts_corr` |
| 截面 | 3 | `cs_rank` `cs_zscore` `cs_demean` |
| 常量 | 1 | `const`（参数 `value`） |
| 一元算术 | 5 | `neg` `abs` `sign` `log` `sqrt` |
| 二元算术 | 5 | `add` `sub` `mul` `div` `safe_div` |
| 比较 | 4 | `gt` `lt` `ge` `le` |
| 其它 | 2 | `where`（三元） `clip`（参数 `lo` / `hi`） |

逐算子的 arity 在这里写明，因为 DSL 编辑器需要它：时序算子全部只取一个参数，**唯独 `ts_corr` 是二元的**；截面算子全部只取一个；一元算术取一个，二元算术与比较取两个；`const` 取零个，`clip` 取一个，`where` 取三个。

### 3.3 约束常量

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `MAX_DEPTH` | 6 | 树深度上限（根算 1） |
| `MAX_NODES` | 24 | 节点总数上限（叶子与算子节点都算） |
| `WINDOW_MIN` | 1 | 窗口参数 `n` 下限 |
| `WINDOW_MAX` | 252 | 窗口参数 `n` 上限 |
| `MAX_LOOKBACK` | 504 | 任一「根到叶」路径上窗口之和的上限，也就是该表达式需要的最大历史长度 |

### 3.4 前视防护

- **没有负向位移**：`lag` / `delta` 的窗口参数 `n` 受 `WINDOW_MIN=1` 约束，不存在 `n ≤ 0` 的写法，因此无法把未来 bar 搬到现在。
- **窗口只向过去看**：rolling 族算子与 `ts_corr` 要求 `min_periods = n`，即窗口填满 `n` 个 bar 才出值，不满则缺失（NaN）；`lag`、`delta`、`ts_pct_change` 则由 `shift` / `pct_change` 天然产生同样的前 `n` 根缺失，它们根本没有 `min_periods` 这个概念。
- **缺失保持缺失**：NaN 输入传播；未定义结果（除零、零离散度、`log` 非正值）一律为 NaN，不产生 `inf`。
- **点在不变成**：改写后面的 bar 不会改变前面 bar 的输出。

### 3.5 归一化与指纹

`normalize_expression` 会把表达式重排成规范写法（键排序、空 `params` 丢弃），再算 `sha256(canonical + ":v" + DSL_VERSION)` 作为 **fingerprint**，即 `expression_hash`。所以同一个表达式的不同写法共享一个指纹，跨运行才能去重。

## 4. 模板库（8 个）

模板把「金融假设」和「带参数的表达式骨架 + 整数参数网格」配成一对。展开时按 `TEMPLATES` 声明顺序遍历每个网格点，每个网格点产出**基础表达式**加若干「去噪变体」（对基础表达式套 `ts_mean`，窗口取 3 和 5），按指纹去重后再按 `budget` 截断。

| 模板 id | 类别 | 假设 | 参数网格 | 网格点数 |
| --- | --- | --- | --- | --- |
| `momentum` | price | 近期涨幅高的标的继续走强 | `N ∈ {5,10,20,60,120}` | 5 |
| `reversal` | price | 近期涨幅高的标的短期回落 | `N ∈ {3,5,10,20}` | 4 |
| `low_volatility` | price | 波动低的标的风险调整后表现更好 | `N ∈ {10,20,60}` | 3 |
| `liquidity` | volume | 成交量相对基线放大预示关注度变化 | `N ∈ {5,10,20}`，`M ∈ {20,60}` | 6 |
| `liquidity_level` | volume | 成交量的绝对水平（截面排名）本身携带信息 | `N ∈ {5,10,20}` | 3 |
| `volume_price_confirm` | price_volume | 量价同向确认时动量更可靠 | `N ∈ {5,20,60}`，`M ∈ {5,20}` | 6 |
| `risk_adjusted_momentum` | price | 单位波动的动量优于原始动量 | `N ∈ {20,60,120}` | 3 |
| `conditional_filter` | price_volume | 动量只在成交量处于高位区间时有效 | `N ∈ {5,20}`，`M ∈ {5,20}` | 4 |

合计 34 个网格点。用默认模板集（全部 8 个）、默认 `budget=400`、默认 `seed=42` 展开，实际得到 **102** 个去重候选——模板集填不满预算时，`estimate` 返回的 `candidateBudget` 就是这个真实数字，而不是 400。

`liquidity_level` 是评审阶段裁决新增的第 8 个模板：`liquidity` 度量的是「量相对自身基线放大多少」，`liquidity_level` 度量的是「量的绝对水平在同截面里的排名」，两者假设不同，因此各占一个模板。

## 5. 评估阶梯与默认门槛

每个候选从 L1 往下走，**第一个不通过的门就是它的淘汰原因**，不再继续。

| 层级 | 检查内容 | 默认门槛 | 不通过时的原因键 |
| --- | --- | --- | --- |
| **L1 合法性** | DSL 形状、字段、算子、参数范围；以及求值本身（除零、溢出、标量传给面板算子等） | — | `factorMining.reject.invalid` |
| **L2 快速筛选** | 覆盖率 → IC 观测数 → 信号强度（`\|rankIC\|`） | coverage ≥ 0.60；观测数 ≥ 30；`\|rankIC\|` ≥ 0.02 | `factorMining.reject.lowCoverage` / `factorMining.reject.insufficientObservations` / `factorMining.reject.weakIc` |
| **L3 稳定性** | ICIR → 分折符号一致性 → 前后半段同号 → 平均换手 | ICIR ≥ 0.30；分折一致 ≥ 0.60；前后半 IC 同号；换手 ≤ 0.70 | `factorMining.reject.weakIcir` / `factorMining.reject.unstableFolds` / `factorMining.reject.unstableHalves` / `factorMining.reject.highTurnover` |
| **L4 与内置因子去重** | 与每个已打分内置因子的相关度（取绝对值） | < 0.85 | `factorMining.reject.duplicateBuiltin` / `factorMining.reject.unverifiableRedundancy` |
| **L5 冻结段样本外** | 样本内外 `rankIC` 同号且强度；通缩夏普（DSR）概率 | `\|OOS rankIC\|` ≥ 0.01 且与 IS 同号；DSR 概率 ≥ 0.95 | `factorMining.reject.weakOutOfSample` / `factorMining.reject.selectionBias` |

**几个容易踩的点：**

- **L4 的非有限相关度被忽略**：只有有限值才算证据。NaN 表示「引擎没能测到」——价量运行下基本面因子本来就没有可比数据——跳过它**不等于**放行。因此当**过滤后一个有限值都没有**时，L4 无法执行，直接以 `factorMining.reject.unverifiableRedundancy` 淘汰，而不是让候选白过唯一的冗余关卡。
- **不可测的换手不算低换手**：`factorMining.reject.highTurnover` 的判定写成 `not (turnover <= max_turnover)`，NaN 与 `inf` 都会失败（fail-closed）。
- **分折一致性缺省即跳过**：`foldIcSigns` 由搜索引擎写入（把 IC 序列等分成最多 4 折，取每折均值符号）。序列缺失、或折数不足两项时，都表示「没评估过」，该检查跳过。
- **L5 的 `trials` 用全部试验数**：DSR 的分母是**本次运行评估过的全部候选数（含被淘汰的）**，不是幸存者数。用幸存者数会给搜索本身定价过低，正好低估了它要修正的选择偏差。
- **门槛可覆盖**：`POST /run` 的 payload 可以带 `thresholds` 对象，键名是 `GateThresholds` 的字段名（snake_case）：`min_coverage`、`min_ic_observations`、`min_abs_rank_ic`、`min_icir`、`min_fold_consistency`、`max_turnover`、`max_builtin_correlation`、`min_oos_abs_rank_ic`、`min_deflated_sharpe_probability`。未识别或为 `null` 的键保留默认值。值必须能按字段自身类型读取：整数计数门槛 `min_ic_observations` 接受 `30` 与 `30.0`，但拒绝 `0.5`（`int()` 会把它静默变成 `0`，等于把 L2 的观测数下限关掉）；任何门槛都不接受布尔值和非有限数值。`thresholds` 不是对象、或某个值不满足上述规则时，返回 400 + `factorMining.thresholdsInvalid`，绝不落成一个被强制转换过的门槛。

## 6. 时间口径与防偏差

**时点资产池。** universe 通过 `resolve_candidates` 解析，并且**按整个请求区间**解析（不是按搜索段），因此搜索段面板和冻结段面板拿到的是同一批成员、同一套时点归属。

**信号与成交时点。** 信号用第 t 根 bar 的收盘数据计算；入场价取**下一根 bar 的开盘价**，出场价取再往后 `holdingPeriod` 根 bar 的开盘价，对应前瞻收益 `exit / entry - 1`。默认 `holdingPeriod=5`。

**搜索段与冻结段严格隔离。** 请求区间按**交易日**切分：末尾 `freezeRatio`（默认 0.3，必须落在 `(0, 0.5)`）比例的交易日构成冻结段，冻结段从搜索段最后一个交易日的**次日**开始，两段不共享任何一根 bar。搜索阶段的数据源调用止于搜索段末尾；**冻结段只被读取一次**——在 `review_frozen` 里，且只在有幸存者时才读。没有任何候选能接触到将要评判它的那段行情。

**全部试验（含失败）留痕。** 每个被评估的候选都往 `qd_factor_trials` 写一行，通过和淘汰一视同仁。这份台账的条数就是 L5 里 DSR 的分母。

**台账行只保留门槛会读的字段。** 试验的 `metrics` 里只有 `coverage`、`rankIc`、`icir`、`foldIcSigns`、`averageTurnover`、`stability`、`executionAssumptions`，以及 `icObservations`——IC 序列的**长度**，单独一个键，因为 L2 只问观测数有多少，从不问观测值是什么。其余内容（IC 序列本身、分组与多空曲线、逐日截面、相关度矩阵）只对走到台账通过侧的候选留在**候选**行上——幸存者，以及此后被冻结段复核过的那些；在 L1–L4 就被淘汰的候选从没有候选行，对它而言那条台账行就是仅存的记录。台账是一次评估一行，所以不裁剪的就是让运行记录随预算膨胀的那一项：200 标的时单行 0.78 MB，`budget=400` 的一次运行要落大约三分之一 GB——而且是三份，因为同一份记录同时写进 `qd_factor_trials`、`qd_factor_mining_runs.result_json` 与 `qd_agent_jobs.result`。

**数据指纹。** `summary.snapshotId` 是对面板内容算出的真实指纹：按标的排序，逐个把该标的的规范 OHLCV 字节混入 `sha256`（标的代码也参与哈希）。它是**数据指纹，不是可重放副本**——v1 只存指纹，不存行情快照。

**预热。** 抓取区间会在搜索段起点之前多取一段历史，长度由**本次实际要评估的候选**决定（同样的模板选择 + 预算 + 种子），取 `max(40, min(504, 最长 lookback + 5))` 根 bar 折算成自然日。用 DSL 的 504 上限去取会多花约 3.5 倍历史长度，在日线策略上等于把可搜索窗口从一年压到四个月。

**确定性与可复现。** 同一份面板 + 同一组 `(templateIds, budget, seed)` 产出同一批试验和同一批候选，顺序也一致：模板展开是有种子的，门槛阶梯是纯函数，代码里不迭代集合。

## 7. HTTP 接口

前缀 `/api/factor-mining`，OpenAPI tag 为 **`FactorMining`**。全部路由都要求登录（`Authorization: Bearer <jwt>`）。统一信封 `{"code": 1, "msg": "common.success", "data": ...}`；出错时 `code: 0`、`msg` 直接是上文的 i18n 键、`data: null`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/catalog` | DSL 词表：`fields`、`operators`、`templates` |
| `POST` | `/estimate` | 静态估价，**只**返回 `{templateCount, candidateBudget}` 这两项；不读数据源、不建面板、不开作业 |
| `POST` | `/run` | 提交一次挖掘，**HTTP 202**，返回作业回执 |
| `GET` | `/jobs` | 当前用户的挖掘作业列表，最新在前，**不带** `result` |
| `GET` | `/jobs/<job_id>` | 单个作业（含 `progress` 与完整 `result`） |
| `POST` | `/jobs/<job_id>/cancel` | 取消；执行体在下次进度上报时停下 |
| `GET` | `/candidates` | 候选库列表，支持过滤与分页 |
| `GET` | `/candidates/<candidate_id>` | 单个候选，含 `metrics` 与 `outOfSample` |

**`POST /run` 请求体。** 服务端只读取以下键，其余键被丢弃（不会落进作业表的 request）：

`sourceId`、`sourceName`、`market`、`timeframe`、`startDate`、`endDate`、`freezeRatio`、`budget`、`seed`、`templateIds`、`groups`、`holdingPeriod`、`commission`、`slippage`、`neutralizeIndustry`、`thresholds`

必填：`sourceId`（正整数）、`market`（合法市场 key）、`startDate` / `endDate`（`YYYY-MM-DD`）。`budget` 缺省 400。`timeframe` 缺省 `1D`。

**作业回执 / 作业对象**（同一套字段）：

```json
{"jobId": "<uuid>", "status": "queued", "progress": {},
 "config": {"sourceId": 12, "market": "USStock", "timeframe": "1D",
            "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42},
 "result": null, "hasResult": false, "error": null,
 "createdAt": "2026-10-13T09:00:00+00:00", "startedAt": null, "finishedAt": null}
```

**`config` 不随列表视图省略。** 它是本次请求里**实际出现**的那部分运行字段（第 7 节开头那份名单的子集），由作业表存下的 `request` 经白名单取出——那份 `request` 还带着作业存储自己的 `__userId` / `__billing` 通道，这些不回给调用者。之所以不能省：`result` 被省略时，`config` 就是轮询端唯一能认出「这个作业是哪个市场、哪段区间、多少预算」的东西。缺 `request` 的行读成空对象，不是错误——没有配置的作业仍然值得列出来。

**`error` 只有一行。** 作业失败时作业表存下的是 `异常信息 + 完整 traceback`，但栈里带着 worker 的绝对路径与模块结构，而作业存储已经把它写进该作业自己的日志行——所以只有**首行**离开这里。调用方拿到的是能据以行动的原因（通常就是上文某个 `factorMining.*` 键），栈留给运维。未失败的作业保持写入时的 `null` / `''`。

**`progress` 的形状**是 `{"stage": "search" | "review", "evaluated": <int>, "total": <int>}`；搜索段每 10 个候选上报一次，冻结段复核开始时会切到 `review`。作业尚未跑起来时是 `{}`。

**作业的列表视图与详情视图。** `/jobs` 每一项的 `result` 都是 `null`，`hasResult` 说明有没有可取的记录；运行记录（每个幸存候选的完整 metrics 加每条试验台账行）只由 `/jobs/<job_id>` 提供。一页二十份完整记录，对「只想找出刚跑完的那个」的轮询端来说是白拿的流量。`POST /run` 的回执用的是同一个信封，此时作业还没跑，所以 `hasResult` 是 `false`。`config` 与 `progress` 两个视图都给。

**`GET /jobs` 参数：** `limit`（1–50，缺省 20）。
**`GET /candidates` 参数：** `runId`（整数）、`status`（`search` / `frozen` / `rejected`）、`market`、`limit`（1–200，缺省 50）、`offset`（≥ 0，缺省 0）。非法整数返回 400 + `factorMining.invalidQuery`。

**`?runId=` 不是「本次运行的候选」的稳定视图。** 候选库按 `(user_id, expression_hash)` 归并，`runId` 指向**最后一次**审判该表达式的那次运行，所以重跑同一表达式会把候选行从旧运行的视图里搬走、挪进新运行的视图。这个过滤器回答的是「这些行最后一次是被哪次运行审判的」，这正是候选库的语义；它不回答「第 7 次运行评估了什么」。

**列表视图与详情视图的差别：** `/candidates` 列表**不带** `metrics` 和 `outOfSample`（一个候选的 metrics 含整条 IC 序列和曲线，一页 50 条不需要）。要这两块就取 `/candidates/<id>`。

**候选对象字段：** `id`、`userId`、`runId`、`expressionHash`、`expression`、`origin`、`templateId`、`market`、`timeframe`、`status`、`reason`、`createdAt`、`updatedAt`；详情另带 `metrics`、`outOfSample`。

**`status` 取值：** `search`（过了 L1–L4，等待冻结段复核）、`frozen`（也过了 L5）、`rejected`（被某一层淘汰，淘汰键在 `reason`）。

**`GET /catalog`：** `operators` **只给算子名字**，不含 arity 和是否带窗口——需要这些信息请看本页第 3.2 节。`templates` 每项是 `{templateId, hypothesisI18nKey, category, parameterGrid}`。

**相关度研究里的标签。** 挖掘出来的候选进入 L4 的相关度研究时，显示的因子名是 **`"expression"`**（内置因子排在它后面）。所以 `metrics.factorCorrelation.factors[0]` 恒为 `"expression"`，`matrix[0][1:]` 就是它与各内置因子的相关度。

## 8. 作业、并发与预算

**持久执行。** 挖掘作业走共用的 `qd_agent_jobs`，`kind = "factor_mining"`，由 Celery 持久执行路径承接（`app/tasks/agent_jobs.py` 已注册该 kind）。进程内池与 Celery 两条派发路径最终调用同一个实现 `run_mining_job`，不存在两套逻辑。

**两张记录，写入时机不同。** 作业行（`qd_agent_jobs`）是 API 轮询的时间线；运行行（`qd_factor_mining_runs`）是审计口径，它在**搜索开始之前**就写入——崩在半路也留下「当时在搜什么配置」的记录。

**并发上限。** 每用户同时只能有 **1 个活跃挖掘作业**。触发时 `/run` 返回 400 + `factorMining.concurrentLimit`。这个闸门统计的是该用户 `kind = "factor_mining"` 的 `queued` / `running` 行——Agent 网关侧的租户总闸仍然统计全部 kind，但本功能自己的上限只算自己的作业：正在跑的 backtest 不占挖掘名额，别的功能留下的、本功能清扫不到（只清自己的 kind）的幽灵作业也挡不住挖掘提交。

**预算上限。**

| 项 | 值 |
| --- | --- |
| `DEFAULT_BUDGET` | 400 |
| `MAX_BUDGET` | 2000（超出返回 `factorMining.budgetExceeded`） |
| 最小可用预算 | 1（`< 1` 或不可读返回 `factorMining.budgetInvalid`） |

`budget` 是**上限**不是承诺：模板集能展开出的去重候选不足 400 时，实际评估数就是展开数（默认模板集是 102）。

**取消。** `POST /jobs/<id>/cancel` 只把作业标记为已取消，执行体在**下一次进度上报**时抛 `JobCancelledError` 停下。此时运行行也会被收尾成 `cancelled`——而不是 `failed`，因为把用户自己的取消显示成引擎故障是错的。

**幽灵作业回收。** worker 进程崩掉后作业会僵在 `running`。`GET /jobs` 在列列表时会顺手清扫：对状态是 `running` 且 `startedAt` 早于 `max(120, CELERY_TASK_TIME_LIMIT or 3600) + 300` 秒的作业，写入失败原因 `factorMining.workerInterrupted`。这不是装饰性清理——并发闸门统计活跃作业，一个扫不掉的幽灵会永久占住该租户的提交名额。

**未决：** 停在 `queued` 的幽灵作业**不在清扫范围内**（延后处理）。

## 9. 数据表

| 表 | 粒度 | 关键约束 |
| --- | --- | --- |
| `qd_factor_mining_runs` | 一次运行一行 | 配置、数据指纹、三个计数、状态；`user_id` 外键到 `qd_users` |
| `qd_factor_candidates` | 一个表达式一行 | `UNIQUE (user_id, expression_hash)`，跨运行去重；`run_id` 是 `ON DELETE SET NULL`——候选是跨运行的资产，随运行消失的只能是它的指针 |
| `qd_factor_trials` | 一次评估一行 | `run_id` 是 `ON DELETE CASCADE`——台账脱离它定价的那次运行就没有意义 |

所有写入都按租户作用域执行，并且**可听地失败**：写入不属于调用者的运行会抛错（`factorMining.runNotFound`），而不是「写了 0 行但返回行数」——那会让一次本该记下 250 条试验的调用静默丢失台账。

## 10. i18n 键清单

前端文案在另一个仓库。以下是后端会产出的**全部** `factorMining.*` 键，后端只发键，不渲染文案。

**接口与服务层（24 个）**

| 键 | 触发场景 |
| --- | --- |
| `factorMining.runFailed` | 提交作业时发生未预期的服务端异常（500） |
| `factorMining.estimateFailed` | 估价时发生未预期的服务端异常（500） |
| `factorMining.jobNotFound` | 作业不存在、不属于该用户、或不是挖掘作业 |
| `factorMining.candidateNotFound` | 候选不存在，或不属于该用户；id 无法解析为整数时同样 |
| `factorMining.invalidQuery` | 查询参数不是合法整数 |
| `factorMining.invalidParameter` | 运行字段无法按其计价类型读取：整数字段（`seed` / `groups` / `holdingPeriod`）不是整数、`commission` / `slippage` 大到无法转换，或 `timeframe` 不在支持的形状内（`<数字><m\|h\|d\|w>`） |
| `factorMining.thresholdsInvalid` | `thresholds` 不是对象，或某个门槛无法按字段自身类型读取（`min_ic_observations` 给了小数、给了布尔值、给了非有限数值） |
| `factorMining.sourceRequired` | 缺少 `sourceId` 或不是正整数 |
| `factorMining.sourceNotFound` | `sourceId` 指向的脚本源不存在或不属于该用户 |
| `factorMining.sourceCodeRequired` | 脚本源存在但代码为空 |
| `factorMining.dateRangeRequired` | 缺少或无法解析 `startDate` / `endDate` |
| `factorMining.dateRangeTooShort` | 区间内交易日不足 2 天，切不出搜索段与冻结段 |
| `factorMining.invalidMarket` | `market` 不是已知市场 key |
| `factorMining.budgetInvalid` | `budget` 不可读或小于 1 |
| `factorMining.budgetExceeded` | `budget` 超过 2000 |
| `factorMining.concurrentLimit` | 该用户已有活跃作业 |
| `factorMining.workerInterrupted` | 僵尸作业被清扫时写入的失败原因 |
| `factorMining.freezeRatioInvalid` | `freezeRatio` 不可读或不在 `(0, 0.5)` |
| `factorMining.searchFreezeOverlap` | 切分后冻结段与搜索段重叠（结构性保护，正常不会触发） |
| `factorMining.universeTooSmall` | universe 解析出的成员少于 3 个 |
| `factorMining.usableUniverseTooSmall` | 抓取后剩余可用标的少于 3 个 |
| `factorMining.noMarketData` | 抓取没有返回任何行情 |
| `factorMining.missingIcSeries` | 研究引擎没有返回 IC 序列（上游异常） |
| `factorMining.runNotFound` | 写入指向了一个不存在或不属于调用者的运行 |

**参数化形式（3 类，消费端不要做裸键精确匹配）**

| 形式 | 来源 |
| --- | --- |
| `factorMining.templateNotFound:<template_id>` | `templateIds` 里出现了未知模板 id |
| `factorMining.fingerprintInvalid:<index>` | 第 `<index>` 个候选的指纹不是 64 位 sha256 十六进制 |
| `factorMining.reject.invalid:<reason>` | DSL 校验失败时 `ExpressionError.code` 携带的具体原因（`unknownField` / `tooDeep` / `tooManyNodes` / `unknownOperator` / `badArity` / `badNode` / `badWindow` / `lookbackExceeded`） |

**L1–L5 淘汰原因（12 个）**

| 键 | 层级 |
| --- | --- |
| `factorMining.reject.invalid` | L1（试验与候选的 `reason` 列存的是这个**裸键**；带 `:<reason>` 的形式只出现在 DSL 抛出的 `ExpressionError.code` 上） |
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

**模板假设（8 个）**

- `factorMining.template.momentum`
- `factorMining.template.reversal`
- `factorMining.template.low_volatility`
- `factorMining.template.liquidity`
- `factorMining.template.liquidity_level`
- `factorMining.template.volume_price_confirm`
- `factorMining.template.risk_adjusted_momentum`
- `factorMining.template.conditional_filter`

`GET /catalog` 的 `templates[].hypothesisI18nKey` 直接给出这些键，前端不必硬编码模板清单。

## 11. 已知非目标与未决

- **不计费**：本功能不扣额度、不写账本，资源只受并发上限与预算上限约束。
- **`queued` 幽灵不清扫**：只清扫 `running` 的僵尸作业，卡在 `queued` 的不在处理范围内（延后）。
- **`/catalog` 的 `operators` 只有名字**：不含 arity，也不标注是否带窗口参数；本页第 3.2 节的表是权威来源。
- **不接入策略运行时**：挖掘结果无法被策略引用，也没有用户作用域的读取路径。
- **不做 AI 提案**：候选全部来自模板参数展开（v3 再考虑）。
- **只存数据指纹**：没有可重放的面板副本，因此历史运行无法离线重跑。

相关：[策略工作流](STRATEGY_WORKFLOW_CN.md)、[回测中心](BACKTEST_CENTER_CN.md)、[Strategy API V2 开发指南](../trading/STRATEGY_DEV_GUIDE_CN.md)、[实盘安全](../trading/LIVE_TRADING_SAFETY_CN.md)。
