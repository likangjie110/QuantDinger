# 全自动因子挖掘

全自动因子挖掘把「提假设 → 造表达式 → 逐层筛选 → 样本外复核 → 沉淀候选库」这条研究流程自动化。它覆盖的是**研究流程**，不是收益：挖掘出的因子**不承诺持续盈利**，冻结段表现也只是样本外的一次测量，不是未来收益的保证。

本页面向使用者与前端文案维护者，说明模块边界、表达式 DSL、模板库、评估阶梯、时间口径、HTTP 接口、作业与预算限制，以及全部 i18n 键。

## 1. 模块定位与边界

**做什么**

- 从 8 个金融假设模板展开成一批确定性的候选表达式，逐个打分并留下完整试验台账。
- 用分级门槛（L1–L6）把候选从「合法」一路筛到「在冻结段仍有样本外证据」，最后再过一道「相对调用者自己的候选库是否还有增量」。
- 把结果沉淀进候选库 `qd_factor_candidates`：同一表达式跨多次运行去重，`run_id` 指向最近一次判定它的运行。
- 全过程按用户隔离，可按作业取消，可事后审计（配置、数据指纹、计数、逐条淘汰原因都落库）。

**本模块明确不做**

| 非目标 | 说明 |
| --- | --- |
| 不接入策略运行时 | 挖出的候选只存在于 `qd_factor_candidates`。策略侧仍然只能引用内置因子注册表 `_FACTORS`（`app/services/factors/registry.py`），没有任何用户作用域的路径让策略读到挖掘结果。 |
| 不写回因子注册表 | 挖掘结果不会变成新的内置因子，`_FACTORS` 是代码里静态维护的。 |
| 不计费 | 本功能**完全不计费**：既不扣额度也不写账本。资源只由并发上限与预算上限约束（见第 9 节）。 |
| 不存可重放行情副本 | 只保存数据指纹 `snapshotId`，不保存行情快照本身，因此不能离线重放一次历史运行、拿回完全相同的数据。v3 的 AI 运行可以重放**提案**（`replayJobId`，见第 8 节），重放不调用模型，但仍会重新抓取数据；指纹变了就由 `snapshotChanged` 标出。 |

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

字段按组划分，一个市场的可用集就是它拥有的那些组的并集：

| 组 | 字段 | 市场 |
| --- | --- | --- |
| `price_volume` | `open`、`high`、`low`、`close`、`volume` | 全部市场 |
| `derivatives` | `funding_rate`、`open_interest`、`term_structure`、`roll_cost` | 仅 `Crypto` |

`fields_for(market)` 按组顺序返回这个集合。市场名**按运行时原样精确匹配**（`Crypto`、`USStock`）：不做大小写折叠、不做别名解析，因此 `crypto`——以及空或未知的市场——只会回退到 `price_volume` 组。这个回退是刻意的，方向是「拒绝」而非「放行」：市场名写错只会少字段，不会多字段；而且落在 `fields_for(market)` 之外的字段本来就会在 L1 以 `unknownField` 被拒。

某组存在只说明**数据源能回答**这个字段，不说明每个标的都有值：衍生品序列返回空的标的仍保留它的 frame，该字段是一整列 NaN，由 L2 的覆盖率门槛决定这个缺口算不算问题。`term_structure` 与 `roll_cost` 来自 Deribit 的 BTC、ETH 月度交割合约：交易所把每根日线的戳记放在当日 08:00 UTC 开盘，因此一根日线的 close 要到 tick + 1 天 08:00 UTC 才可知，两条序列都按这个更晚的时点索引。主力腿是距到期至少 7 天的最近月度合约，远月腿是它的下一个；`term_structure` 是按两个到期日之间的日历天数年化的远月/主力价差，`roll_cost` 是同一价差未年化的值。只有 BTC 与 ETH 有这类合约，其余 `Crypto` 标的保留整列 NaN。两条腿都是 Deribit 合约，币种差异大部分抵消，但成员可能在其他场所报价（例如 USDT 永续），因此这是跨场所基差而非严格的同场所价差。写成公式：`term_structure = (far / main - 1) * 365 / d`，其中 `d` 是两个到期日之间的日历天数；`roll_cost = far / main - 1`，不做年化；合约只在上市到到期之间进入池子。由于只有 BTC 与 ETH 成员带这两个字段，依赖其中任一字段的候选，只有在 BTC/ETH 成员占活跃宇宙的比例达到 L2 覆盖率门槛（默认 0.60）时才能通过；`min_coverage` 可由运行参数覆盖。

`GET /catalog` 按市场提供这份清单：`?market=Crypto` 返回两个组共九个字段；不带 `market`（或带一个本版本不认识的市场名）返回价量五个字段。

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
| `MAX_DEPTH` | 8 | 树深度上限（根算 1） |
| `MAX_NODES` | 24 | 节点总数上限（叶子与算子节点都算） |
| `WINDOW_MIN` | 1 | 窗口参数 `n` 下限 |
| `WINDOW_MAX` | 252 | 窗口参数 `n` 上限 |
| `MAX_LOOKBACK` | 504 | 任一「根到叶」路径上窗口之和的上限，也就是该表达式需要的最大历史长度 |

**为什么深度上限定为 8，以及它没有改变什么。** 原值是 6。交叉会把一个表达式的子树嫁接到另一个表达式的路径上，子代深度是两个半边的深度相加——上界如果比最深的合法表达式毫无余量，最深的那批候选就根本组合不起来：最深的模板表达式（`conditional_filter` / `volume_price_confirm` 的基础式加一层平滑变体）深度正好是 6，恰好顶到旧上界，新上界给了这一族 2 层余量。深度上限是**校验策略，不是表达式语义的一部分**，因此 `DSL_VERSION` **不递增**、既有 `expression_hash` 全部不变：同一个表达式升级前后指纹相同，没有任何已存候选被重新建档。会变的是**哪些表达式合法**：原先会以 `factorMining.reject.invalid:tooDeep` 被拒的深度 7、8 的树现在被接受，余量本来就紧的种子也可能产出数量不同的被拒子代。

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
| **L4 冗余去重** | 与每个已打分的比较因子的相关度（取绝对值）：内置注册表的因子用内置门槛，调用者自己的冻结候选池（名字带 `pool:` 命名空间）用池门槛 | 内置 < 0.85；池 < 0.85 | `factorMining.reject.duplicateBuiltin` / `factorMining.reject.duplicateInPool` / `factorMining.reject.unverifiableRedundancy` |
| **L5 冻结段样本外** | 样本内外 `rankIC` 同号且强度；通缩夏普（DSR）概率 | `\|OOS rankIC\|` ≥ 0.01 且与 IS 同号；DSR 概率 ≥ 0.95 | `factorMining.reject.weakOutOfSample` / `factorMining.reject.selectionBias` |
| **L6 池增量** | 在每个截面上把调用者的整个冻结候选池回归掉之后，残差与前瞻收益是否仍有相关，且与原始 IC 同侧 | `\|残差 IC\|` ≥ `min_abs_rank_ic`（0.02）**且**残差 IC 与原始 IC 同号 | `factorMining.reject.noIncrementalValue` |

**几个容易踩的点：**

- **L4 忽略非有限相关度，且按命名空间分组**：只有有限值才算证据。NaN 表示「引擎没能测到」——价量运行下基本面因子本来就没有可比数据，或者池成员的存储表达式已无法重建——跳过它**不等于**放行。因此规则是分组的：**某个命名空间**（内置、池）被问过、却**一个有限值都没有**时，L4 无法执行，候选直接以 `factorMining.reject.unverifiableRedundancy` 淘汰，而不是白过唯一的冗余关卡。从未被问过的命名空间不作要求，否则所有没有池的运行都会 fail-closed；映射整体为空则同样属于「什么都没问过」，也判无法验证。内置段先判，因此两段都超阈的候选报的是内置重复键。
- **L4 的两道阈值彼此独立**：`max_builtin_correlation` 与 `max_pool_correlation` 默认都是 0.85，但一次运行可以用不同尺度看待自己的库与注册表因子，所以两者分开读、淘汰键也分开——「与内置因子重复」和「与你自己挖出来的东西重复」该把运维引向不同地方。
- **L6 只在有读数时才淘汰**：池增量有三种状态，其中只有一种会淘汰候选——`true`（有增量信息）、`false`（测出了，且不含池子里没有的信息，**唯一**可据以淘汰的状态）、以及「没答案」。池为空、或池里什么都测不出时，候选保持 `frozen`，原因写在增量自己的块里：`factorMining.poolIncrement.noPool`、`factorMining.poolIncrement.unmeasured`。这两个都不是淘汰键，也不会出现在候选的 `reason` 里。这与 L4 的 fail-closed 刻意相反：在 L4 放行的候选会进入此后每次运行都要比较的池，重复会复利；在这里放行的候选已经过了 L2–L5，运维可以事后按证据摘掉它。
- **不可测的换手不算低换手**：`factorMining.reject.highTurnover` 的判定写成 `not (turnover <= max_turnover)`，NaN 与 `inf` 都会失败（fail-closed）。
- **分折一致性缺省即跳过**：`foldIcSigns` 由搜索引擎写入（把 IC 序列等分成最多 4 折，取每折均值符号）。序列缺失、或折数不足两项时，都表示「没评估过」，该检查跳过。
- **L5 的 `trials` 用全部试验数**：DSR 的分母是**本次运行评估过的全部候选数（含被淘汰的）**，不是幸存者数。用幸存者数会给搜索本身定价过低，正好低估了它要修正的选择偏差。
- **门槛可覆盖**：`POST /run` 的 payload 可以带 `thresholds` 对象，键名是 `GateThresholds` 的字段名（snake_case）：`min_coverage`、`min_ic_observations`、`min_abs_rank_ic`、`min_icir`、`min_fold_consistency`、`max_turnover`、`max_builtin_correlation`、`max_pool_correlation`、`min_oos_abs_rank_ic`、`min_deflated_sharpe_probability`。`min_abs_rank_ic` 是本次运行唯一的信号强度门槛，L6 也用同一个值读残差。未识别或为 `null` 的键保留默认值。值必须能按字段自身类型读取：整数计数门槛 `min_ic_observations` 接受 `30` 与 `30.0`，但拒绝 `0.5`（`int()` 会把它静默变成 `0`，等于把 L2 的观测数下限关掉）；任何门槛都不接受布尔值和非有限数值。`thresholds` 不是对象、或某个值不满足上述规则时，返回 400 + `factorMining.thresholdsInvalid`，绝不落成一个被强制转换过的门槛。

## 6. 时间口径与防偏差

**时点资产池。** universe 通过 `resolve_candidates` 解析，并且**按整个请求区间**解析（不是按搜索段），因此搜索段面板和冻结段面板拿到的是同一批成员、同一套时点归属。

**信号与成交时点。** 信号用第 t 根 bar 的收盘数据计算；入场价取**下一根 bar 的开盘价**，出场价取再往后 `holdingPeriod` 根 bar 的开盘价，对应前瞻收益 `exit / entry - 1`。默认 `holdingPeriod=5`。

**搜索段与冻结段严格隔离。** 请求区间按**交易日**切分：末尾 `freezeRatio`（默认 0.3，必须落在 `(0, 0.5)`）比例的交易日构成冻结段，冻结段从搜索段最后一个交易日的**次日**开始，两段不共享任何一根 bar。搜索阶段的数据源调用止于搜索段末尾；**冻结段只被读取一次**——在 `review_frozen` 里，且只在有幸存者时才读。没有任何候选能接触到将要评判它的那段行情。

**池增量读的是搜索段。** L6 衡量一个幸存者相对调用者冻结候选池的增量，用的是**搜索段自己**的那些截面，绝不是冻结段：增量是搜索期口径的判断，而冻结段是用来评判候选的证据，不是给候选当证据的。池成员由它存储的表达式在当前搜索评分所用的同一份字段面板上重建——这才是成员可比的前提；整个测量读的是搜索段那份已裁剪的 frame，因此数据源多返回的、越过搜索段末尾的那些 bar 到不了这个读数里。

**全部试验（含失败）留痕。** 每个被评估的候选都往 `qd_factor_trials` 写一行，通过和淘汰一视同仁。这份台账的条数就是 L5 里 DSR 的分母。

**台账行只保留门槛会读的字段。** 试验的 `metrics` 里只有 `coverage`、`rankIc`、`icir`、`foldIcSigns`、`averageTurnover`、`stability`、`executionAssumptions`，以及 `icObservations`——IC 序列的**长度**，单独一个键，因为 L2 只问观测数有多少，从不问观测值是什么。其余内容（IC 序列本身、分组与多空曲线、逐日截面、相关度矩阵）只对走到台账通过侧的候选留在**候选**行上——幸存者，以及此后被冻结段复核过的那些；在 L1–L4 就被淘汰的候选从没有候选行，对它而言那条台账行就是仅存的记录。台账是一次评估一行，所以不裁剪的就是让运行记录随预算膨胀的那一项：200 标的时单行 0.78 MB，`budget=400` 的一次运行要落大约三分之一 GB——而且是三份，因为同一份记录同时写进 `qd_factor_trials`、`qd_factor_mining_runs.result_json` 与 `qd_agent_jobs.result`。

**数据指纹。** `summary.snapshotId` 是对面板内容算出的真实指纹：按标的排序，逐个把该标的的规范 OHLCV 字节混入 `sha256`（标的代码也参与哈希）。它是**数据指纹，不是可重放副本**——v1 只存指纹，不存行情快照。

**预热。** 抓取区间会在搜索段起点之前多取一段历史，取多少取决于搜索模式，因为各模式对「将要评估什么」的已知程度不同。**枚举模式**完全已知：候选全部来自模板，运行在抓取之前就能先把模板集算出来（同样的模板选择 + 预算 + 种子），只取 `max(40, min(504, 最长 lookback + 5))` 根 bar 折算成自然日。用 DSL 的 504 上限去取会多花约 3.5 倍历史长度，在日线策略上等于把可搜索窗口从一年压到四个月。**进化模式**什么都不知道：交叉可以把任意子树（带任意合法窗口参数）嫁到任意路径上，所以它取满上界（`MAX_LOOKBACK=504` 根 bar ≈ 953 自然日）。**AI 模式**出于另一个原因处于同样位置：表达式由模型写出，调用之前没有任何东西能限定它的 lookback，因此同样取满上界、付同样的历史成本。若市场数据策略对抓取区间有上限，这个上界会让进化或 AI 运行**直接被拒**（由共用的回测区间校验在请求任何一根 bar 之前抛出，作业以 `strategyV2.backtestRangeLimit` 失败），而不是把窗口缩小后照跑。

**确定性与可复现。** 同一份面板 + 同一份运行配置（请求里的每一个字段，含 `searchMode`）产出同一批试验和同一批候选，顺序也一致。`(templateIds, budget, seed)` 只是其中一部分：模式决定哪些来源贡献候选、抓取多少历史，进化模式下 `population`、`generations`、`eliteRatio` 决定从什么里繁殖。模板展开是有种子的，进化流只从 `random.Random(seed)` 取数，门槛阶梯是纯函数，代码里不迭代集合。

## 7. HTTP 接口

前缀 `/api/factor-mining`，OpenAPI tag 为 **`FactorMining`**。全部路由都要求登录（`Authorization: Bearer <jwt>`）。统一信封 `{"code": 1, "msg": "common.success", "data": ...}`；出错时 `code: 0`、`msg` 直接是第 11 节的 i18n 键、`data: null`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/catalog` | DSL 词表：`fields`、`operators`、`templates`；可选 `?market=` 指定字段集（见 3.1） |
| `POST` | `/estimate` | 静态估价，**只**返回 `{templateCount, candidateBudget}` 这两项；不读数据源、不建面板、不开作业 |
| `POST` | `/run` | 提交一次挖掘，**HTTP 202**，返回作业回执 |
| `GET` | `/jobs` | 当前用户的挖掘作业列表，最新在前，**不带** `result` |
| `GET` | `/jobs/<job_id>` | 单个作业（含 `progress` 与完整 `result`） |
| `POST` | `/jobs/<job_id>/cancel` | 取消；执行体在下次进度上报时停下 |
| `GET` | `/jobs/<job_id>/proposals` | AI 作业的提案，按生成顺序排列（见第 8 节）；运行中的作业返回已经写入的行 |
| `GET` | `/candidates` | 候选库列表，支持过滤与分页 |
| `GET` | `/candidates/<candidate_id>` | 单个候选，含 `metrics` 与 `outOfSample` |

**`POST /run` 请求体。** 服务端只读取以下键，其余键被丢弃（不会落进作业表的 request）：

`sourceId`、`sourceName`、`market`、`timeframe`、`startDate`、`endDate`、`freezeRatio`、`budget`、`seed`、`templateIds`、`groups`、`holdingPeriod`、`commission`、`slippage`、`neutralizeIndustry`、`thresholds`、`searchMode`、`population`、`generations`、`eliteRatio`、`aiRounds`、`aiProposalsPerRound`、`researchNotes`、`replayJobId`

必填：`sourceId`（正整数）、`market`（合法市场 key）、`startDate` / `endDate`（`YYYY-MM-DD`）。`budget` 缺省 400。`timeframe` 缺省 `1D`。

**搜索模式。** `searchMode` 取 `enumeration`（缺省，即模板展开）、`evolution`（模板展开 + 由它繁殖出的流）或 `ai`（纯模型提案，见第 8 节）；其它值一律以 `factorMining.invalidParameter` 拒绝，**绝不**回退成缺省值——这些模式搜索的候选集不同、消耗的历史长度也不同。`population`（缺省 32）、`generations`（缺省 20）、`eliteRatio`（缺省 0.25）是繁殖参数，**进化运行必须三个都给出且非空**：本层臆造出来的值等于一次没被要求的搜索。三者分别按整数、整数、数值读取，上下界用同一个键拒绝：`population` 与 `generations` 必须 ≥ 1，`eliteRatio` 必须落在 `[0, 1)`（份额 ≥ 1 会让每一代的精英占满全部名额、繁殖循环没有空间产生子代，运行会搜完模板、什么都没繁殖出来，却仍被记为成功）。枚举与 AI 模式下三者可选，给了同样受上下界约束。

`aiRounds`（缺省 2，1–3）、`aiProposalsPerRound`（缺省 10，1–20）、`researchNotes`（纯文本，至多 8000 字符）与 `replayJobId`（作业 id）只属于 AI 模式：非 AI 运行携带其中任何一个**非空**值都会以 `factorMining.invalidParameter` 拒绝，不会被静默忽略。重放契约见第 8 节。

**`/estimate` 不受以上任何影响。** 它校验同一份 payload（含模式、繁殖参数与 AI 字段），返回体**仍然只有** `{templateCount, candidateBudget}` 两项：不回显模式，因为调用方本来就知道自己提交的是什么。AI 运行的 `templateCount` 是 `0`，`candidateBudget` 是 `min(aiRounds * aiProposalsPerRound, budget)`——AI 提案流就是整次搜索，运行预算仍然是它的上限。`/estimate` **不解析** `replayJobId`：两键重放请求里只有模式和作业 id，没有任何可计价的运行形状。要估价一次重放，请发送被重放运行的完整配置并去掉 `replayJobId`。

**作业回执 / 作业对象**（同一套字段）：

```json
{"jobId": "<uuid>", "status": "queued", "progress": {},
 "config": {"sourceId": 12, "market": "USStock", "timeframe": "1D",
            "startDate": "2023-01-01", "endDate": "2024-12-31", "budget": 50, "seed": 42},
 "result": null, "hasResult": false, "error": null,
 "createdAt": "2026-10-13T09:00:00+00:00", "startedAt": null, "finishedAt": null}
```

**`config` 不随列表视图省略。** 它是本次请求里**实际出现**的那部分运行字段（第 7 节开头那份名单的子集），由作业表存下的 `request` 经白名单取出——那份 `request` 还带着作业存储自己的 `__userId` / `__billing` 通道，这些不回给调用者。之所以不能省：`result` 被省略时，`config` 就是轮询端唯一能认出「这个作业是哪个市场、哪段区间、多少预算」的东西。缺 `request` 的行读成空对象，不是错误——没有配置的作业仍然值得列出来。

**`error` 只有一行。** 作业失败时作业表存下的是 `异常信息 + 完整 traceback`，但栈里带着 worker 的绝对路径与模块结构，而作业存储已经把它写进该作业自己的日志行——所以只有**首行**离开。这条保证由作业存储（`agent_jobs.get_job`）给出，对**所有作业类型**成立（回测与策略演化同样如此），不是本功能特有的。调用方拿到的是能据以行动的原因（通常就是上文某个 `factorMining.*` 键），栈留给运维。未失败的作业保持写入时的 `null` / `''`。

**`progress` 的形状**是 `{"stage": "search" | "review", "evaluated": <int>, "total": <int>}`；搜索段每 10 个候选上报一次，冻结段复核开始时会切到 `review`。作业尚未跑起来时是 `{}`。

**作业的列表视图与详情视图。** `/jobs` 每一项的 `result` 都是 `null`，`hasResult` 说明有没有可取的记录；运行记录（每个幸存候选的完整 metrics 加每条试验台账行）只由 `/jobs/<job_id>` 提供。一页二十份完整记录，对「只想找出刚跑完的那个」的轮询端来说是白拿的流量。`POST /run` 的回执用的是同一个信封，此时作业还没跑，所以 `hasResult` 是 `false`。`config` 与 `progress` 两个视图都给。

**`GET /jobs` 参数：** `limit`（1–50，缺省 20）。
**`GET /candidates` 参数：** `runId`（整数）、`status`（`search` / `frozen` / `rejected`）、`market`、`limit`（1–200，缺省 50）、`offset`（≥ 0，缺省 0）。非法整数返回 400 + `factorMining.invalidQuery`。

**`?runId=` 不是「本次运行的候选」的稳定视图。** 候选库按 `(user_id, expression_hash)` 归并，`runId` 指向**最后一次**审判该表达式的那次运行，所以重跑同一表达式会把候选行从旧运行的视图里搬走、挪进新运行的视图。这个过滤器回答的是「这些行最后一次是被哪次运行审判的」，这正是候选库的语义；它不回答「第 7 次运行评估了什么」。

**列表视图与详情视图的差别：** `/candidates` 列表**不带** `metrics` 和 `outOfSample`（一个候选的 metrics 含整条 IC 序列和曲线，一页 50 条不需要）。要这两块就取 `/candidates/<id>`。

**候选对象字段：** `id`、`userId`、`runId`、`expressionHash`、`expression`、`origin`、`templateId`、`market`、`timeframe`、`status`、`reason`、`createdAt`、`updatedAt`；详情另带 `metrics`、`outOfSample`。

**`status` 取值：** `search`（过了 L1–L4，等待冻结段复核）、`frozen`（也过了 L5，且没有在 L6 被淘汰）、`rejected`（被某一层淘汰，淘汰键在 `reason`——L6 淘汰带的是 `factorMining.reject.noIncrementalValue`，而测不出的增量不淘汰任何候选）。

**`GET /catalog`：** `fields` 是该市场自己的字段集，经由校验器所用的同一个 `fields_for` 求得，因此编辑器提供的与 L1 接受的不会漂移。可选查询参数 `market` 指定调用者即将运行的市场；不带、留空或给一个不认识的市场名，返回的是价量组（见 3.1）。`operators` **只给算子名字**，不含 arity 和是否带窗口——需要这些信息请看本页第 3.2 节。`templates` 每项是 `{templateId, hypothesisI18nKey, category, parameterGrid}`。

**相关度研究里的标签。** 挖掘出来的候选进入 L4 的相关度研究时，显示的因子名是 **`"expression"`**（内置因子排在它后面）。所以 `metrics.factorCorrelation.factors[0]` 恒为 `"expression"`，`matrix[0][1:]` 就是它与各内置因子的相关度。

## 8. AI 提案与失败反馈循环

`searchMode="ai"` 用模型提案替代模板展开：作业向服务端配置的 LLM 要假设与表达式，每条表达式都按与其它候选**同一套**字段/算子白名单校验，合法提案走**不变**的 L1–L6 阶梯。模板库仍会作为先验示例放进 prompt，但不进入候选流：AI 运行的候选一律 `origin = "ai"`、`templateId` 为空。试验台账、候选池、冻结段复核与池增量阶段与其它模式完全相同。

**请求字段。** `aiRounds`（缺省 2，1–3）、`aiProposalsPerRound`（缺省 10，1–20）、`researchNotes`（纯文本，至多 8000 字符）与 `replayJobId`（作业 id）是 `POST /run` 的 AI 字段；非 AI 运行携带任何一个**非空**值都会以 `factorMining.invalidParameter` 拒绝。模型与 provider **不是**请求字段，来自服务端的模型配置。运行的候选预算是 `min(aiRounds * aiProposalsPerRound, budget)`——AI 提案流就是整次搜索，运行预算仍然封顶；`/estimate` 对 AI 运行返回这个乘积作为 `candidateBudget`，`templateCount` 为 `0`。

**一轮至多两次模型调用（1 次重试）。** 每轮 prompt 携带市场、周期与数据区间、按组顺序排列的字段目录、算子目录（arity 与窗口）、候选预算、跨 run 失败摘要、上一轮反馈、可选研究笔记，以及至多 10 个模板示例作为先验。研究笔记与全部历史材料都以**数据**定界：其中的任何指令都不会被执行。模型必须返回严格 JSON：`{"proposals": [{"hypothesis": "...", "expression": {...}}]}`。单次调用 60 秒超时，传输或 JSON 失败重试一次（每轮至多两次尝试）；仍失败则作业失败并返回 `factorMining.aiProposalFailed`，已写入的提案保留。

**校验、去重与阶梯。** 提案先过 L1 才算候选：每条表达式都用 `allowed_fields=fields_for(market)` 归一化，模型拿不到模板搜索之外的字段或算子。校验失败存为 `status = "l1_rejected"`，`reason` 是 DSL 错误码；同一 run 内重复的指纹存为 `status = "duplicate"`，`reason` 是 `factorMining.reject.duplicateProposal`。这两类都不评估、不写试验台账。合法提案先以 `status = "proposed"` 写入并作为种子；评估结果回读后该行标为 `evaluated`（L1–L6 的判定在试验/候选台账里，不在提案的 `reason` 里）。某一轮零合法提案就提前收束，不再多花一次模型调用；所有轮都为零则作业失败并返回 `factorMining.aiNoValidProposals`。只有按 `(round, ordinal)` 顺序排在前 `min(aiRounds * aiProposalsPerRound, budget)` 条的合法提案会进入评估；模型多给的合法提案仍以 `proposed` 记录，但不会被评估。

**失败反馈。** 反馈分两层。run 内：一轮的种子评估完后，引擎内存中的 outcome 按阶段与淘汰原因聚合，附至多 10 条失败表达式（指纹、阶段、原因与表达式本身）和至多 5 条近失（按与门槛的相对差距排序）；整块至多 6000 字符，成为下一轮的 `Previous round feedback`。它必须读引擎内存，不能读 `qd_factor_trials`：试验在 `engine.run` 返回后才由服务层写库。跨 run：非重放的运行在开始前只取一次摘要，来自同一用户、同一市场与周期下最近 5 次**成功**运行的 stage/verdict/reason 计数和至多 10 条失败表达式，整块同样至多 6000 字符；之后每轮 prompt 都携带它（受空间限制）。重放不调用模型，也不注入跨 run 历史（摘要查询被整体跳过，而不是取到后不用）。失败与取消的 run 不作为历史。

**落库。** `qd_factor_proposals` 每条提案一行（`run_id`、`round`、`ordinal`、表达式、指纹、假设、model、provider、prompt hash、DSL 版本、source、status、淘汰原因）；`qd_factor_mining_runs.job_id`（新增列）把作业映射到 run，所以作业运行中与失败后都能读到已写入的提案。研究笔记不复制进 `config_json` 或提案行：它随作业请求（`qd_agent_jobs.request`，租户作用域）落库，摘要里只存 SHA-256 与长度。prompt 原文不存，只存 hash。

**AI 摘要。** AI 运行的 `GET /jobs/<job_id>` 在 `result.summary.ai` 增加：

```json
{"enabled": true, "rounds": 2, "proposals": 17, "l1Rejected": 3, "duplicates": 2,
 "evaluated": 12, "model": "...", "provider": "...", "modelFallbackUsed": false,
 "promptHashes": ["..."], "researchNotesSha256": "...", "researchNotesLength": 1234,
 "replayOf": "", "snapshotChanged": false}
```

`rounds` 是实际跑过的轮数；重放则统计「至少产生过一次评估」的记录轮次，因为整轮提案都被 L1 拒绝的记录轮在这里并没有真正跑过。`model` / `provider` 是最后一次作答的批次（重放不调用模型，因此为空）。非 AI 运行不带 `summary.ai`。

**提案端点。** `GET /api/factor-mining/jobs/<job_id>/proposals?limit=50&offset=0` 返回 `{"items": [...]}`，按 `(round, ordinal)` 排序；`limit` 上限 200。每项为 `{id, runId, round, ordinal, hypothesis, expression, expressionHash, status, reason, model, provider, promptHash, dslVersion, source, createdAt}`。运行中的作业返回已写入的行（可能为空）；未知作业、他人作业、非挖掘作业，以及尚未开出 run 的作业，都读作 404 `factorMining.jobNotFound`。

**重放。** `replayJobId` 指向调用者自己的 **AI 运行**：其它模式没有记录提案，无从重放。被重放的运行可以是 `success` 或 `failed`；`cancelled` / `running` 一律拒绝。失败运行之所以值得重放，正是因为中途失败会丢弃已评估的 trials——台账只在搜索返回后才写库——而提案行仍在；重放会对每条已记录提案按当前面板重新评估。没有记录提案的运行，无论状态如何都没有可重放的内容。重放请求只需要 `searchMode="ai"` 与 `replayJobId`；市场、区间、预算、数据源与 AI 边界由被重放的记录提供。客户端可以重述 `market`、`timeframe`、`startDate`、`endDate`、`budget`、`thresholds`，但生效值必须与记录一致，不一致返回 `factorMining.aiReplayIncompatible`。`researchNotes` 可以为新的审计记录重新提供，但不会送给模型。重放把记录的提案复制成新 run 的行（`source = "replay"`），按当前 DSL 重新执行 L1（`dslVersion` 不同返回 `factorMining.aiReplayIncompatible`），只评估合法提案；这条路径**绝不调用 proposer**。由于面板会重新抓取，`snapshotChanged` 说明新指纹是否与原 run 不同：提案及其 L1 状态可复现，指标则是一次新的测量。原 run 没有记录数据指纹时 `snapshotChanged = false`——它无法说明数据是否变化。未知或他人的 `replayJobId` 返回 `factorMining.aiReplayNotFound`。非 AI 运行、没有记录提案的运行、以及 `cancelled` / `running` 的运行，返回 `factorMining.aiReplayIncompatible`。

**限额。**

| 项 | 值 |
| --- | --- |
| 轮次 | 缺省 2，最大 3（`aiRounds`） |
| 每轮提案 | 缺省 10，最大 20（`aiProposalsPerRound`） |
| 每次运行的合法提案 | 按 `(round, ordinal)` 顺序取前 `min(aiRounds * aiProposalsPerRound, budget)` 条评估；其后的合法提案保持 `proposed` |
| 模型调用 | 60 秒超时；重试 1 次（每轮 2 次尝试） |
| 研究笔记 | 8000 字符 |
| 单轮 prompt | 24000 字符；空间不足时先截断可选材料，字段/算子目录与硬约束不截断 |
| 跨 run 历史 | 最近 5 次成功运行 |
| 反馈摘要 | 6000 字符 |

**已知限制。** 模型与 provider 是部署配置，不是单次运行的选择。本功能不抓取论文/研报，也没有文件或 PDF 解析，只读取调用者粘贴的纯文本笔记。提案质量取决于配置的 provider 与模型；AI 提案仍必须通过与其它候选完全相同的 L1–L6 阶梯——AI 模式改变的是假设从哪来，不是什么算作证据。模拟运行监控尚未实现。本功能仍然不计费；每一轮至多两次模型调用（1 次重试），整次运行受上述边界约束。

## 9. 作业、并发与预算

**持久执行。** 挖掘作业走共用的 `qd_agent_jobs`，`kind = "factor_mining"`，由 Celery 持久执行路径承接（`app/tasks/agent_jobs.py` 已注册该 kind）。进程内池与 Celery 两条派发路径最终调用同一个实现 `run_mining_job`，不存在两套逻辑。

**两张记录，写入时机不同。** 作业行（`qd_agent_jobs`）是 API 轮询的时间线；运行行（`qd_factor_mining_runs`）是审计口径，它在**搜索开始之前**就写入——崩在半路也留下「当时在搜什么配置」的记录。

**并发上限。** 每用户同时只能有 **1 个活跃挖掘作业**。触发时 `/run` 返回 400 + `factorMining.concurrentLimit`。这个闸门统计的是该用户 `kind = "factor_mining"` 的 `queued` / `running` 行——Agent 网关侧的租户总闸仍然统计全部 kind，但本功能自己的上限只算自己的作业：正在跑的 backtest 不占挖掘名额，别的功能留下的、本功能清扫不到（只清自己的 kind）的幽灵作业也挡不住挖掘提交。

**预算上限。**

| 项 | 值 |
| --- | --- |
| `DEFAULT_BUDGET` | 400 |
| `MAX_BUDGET` | 2000（超出返回 `factorMining.budgetExceeded`） |
| 最小可用预算 | 1（`< 1` 或不可读返回 `factorMining.budgetInvalid`） |

`budget` 是**上限**不是承诺：模板集能展开出的去重候选不足 400 时，实际评估数就是展开数（默认模板集是 102）。AI 运行的提案流同样封顶在 `min(aiRounds * aiProposalsPerRound, budget)`；模型产出的合法提案还可能少于这两个上限，实际评估的就是过了 L1 的那部分。

**取消。** `POST /jobs/<id>/cancel` 只把作业标记为已取消，执行体在**下一次进度上报**时抛 `JobCancelledError` 停下。此时运行行也会被收尾成 `cancelled`——而不是 `failed`，因为把用户自己的取消显示成引擎故障是错的。

**幽灵作业回收。** worker 进程崩掉后作业会僵在 `running`。`GET /jobs` 在列列表时会顺手清扫：对状态是 `running` 且 `startedAt` 早于 `max(120, CELERY_TASK_TIME_LIMIT or 3600) + 300` 秒的作业，写入失败原因 `factorMining.workerInterrupted`。这不是装饰性清理——并发闸门统计活跃作业，一个扫不掉的幽灵会永久占住该租户的提交名额。

**未决：** 停在 `queued` 的幽灵作业**不在清扫范围内**（延后处理）。

## 10. 数据表

| 表 | 粒度 | 关键约束 |
| --- | --- | --- |
| `qd_factor_mining_runs` | 一次运行一行 | 配置、数据指纹、三个计数、状态，以及 `job_id`（驱动该运行的作业）；`user_id` 外键到 `qd_users` |
| `qd_factor_candidates` | 一个表达式一行 | `UNIQUE (user_id, expression_hash)`，跨运行去重；`run_id` 是 `ON DELETE SET NULL`——候选是跨运行的资产，随运行消失的只能是它的指针 |
| `qd_factor_trials` | 一次评估一行 | `run_id` 是 `ON DELETE CASCADE`——台账脱离它定价的那次运行就没有意义 |
| `qd_factor_proposals` | 一条 AI 提案一行 | `UNIQUE (run_id, round, ordinal)`，按 `(round, ordinal)` 排序；`run_id` 是 `ON DELETE CASCADE`；每次读取都带 `user_id` 作用域，`status` / `reject_reason` 记录提案自身的审计状态（见第 8 节） |

所有写入都按租户作用域执行，并且**可听地失败**：写入不属于调用者的运行会抛错（`factorMining.runNotFound`），而不是「写了 0 行但返回行数」——那会让一次本该记下 250 条试验的调用静默丢失台账。

**池增量写在哪。** 幸存者的池增量写进它的 `outOfSample` 块——即 `qd_factor_candidates` 的 `out_of_sample_json` 列，由 `/candidates/<id>` 以 `outOfSample` 提供——键为 `poolIncrement`：

| 键 | 含义 |
| --- | --- |
| `residualIc`、`rawIc` | 参与测量的那些截面上的平均残差 IC 与平均原始 IC；没测出时是 `null`（部分平均不是一个读数） |
| `poolSize` | 本次运行的上限与自排除处理完之后，池的规模 |
| `compared` | **能**重建出面板的成员名——读数实际覆盖的就是这些 |
| `skipped` | 重建失败的成员；**跳过不等于放行** |
| `complete` | 恰好当 `skipped` 为空时为 `true`，即读数覆盖了整个池 |
| `hasIncrementalValue` | `true` / `false` / `null`（没测出）——消费端必须能分辨这三种状态 |
| `reason` | 有增量时为空；测出了但没有增量时是 `factorMining.reject.noIncrementalValue`；没有判定时是 `factorMining.poolIncrement.noPool` / `.unmeasured` |
| `usedCrossSections`、`skippedCrossSections` | 读数平均了多少个截面、又被迫丢掉了多少个 |
| `minAbsRankIc` | 残差 IC 对比的那道门槛，随读数一起存下，让一条已存读数能自解释它的判定 |

**消费端必须读 `complete` 与那两个计数，绝不能只看 `hasIncrementalValue`。** 被截断的池仍然给出判定，这是刻意的：否则只要有一个成员的存储表达式不再能重建，这一层就永久失效；而这里的 `false` 是由**已测到**的成员决定的。不能发生的是截断被隐去——只显示 `hasIncrementalValue`、不显示 `complete`、`poolSize`、`compared` 的界面，会把「这个库只比了一半」和「这个库什么都加不进去」渲染成一模一样，而后者才是读数支持的那个结论。

## 11. i18n 键清单

前端文案在另一个仓库。以下是后端会产出的**全部** `factorMining.*` 键，后端只发键，不渲染文案。

**接口与服务层（28 个）**

| 键 | 触发场景 |
| --- | --- |
| `factorMining.runFailed` | 提交作业时发生未预期的服务端异常（500） |
| `factorMining.estimateFailed` | 估价时发生未预期的服务端异常（500） |
| `factorMining.jobNotFound` | 作业不存在、不属于该用户、或不是挖掘作业 |
| `factorMining.candidateNotFound` | 候选不存在，或不属于该用户；id 无法解析为整数时同样 |
| `factorMining.invalidQuery` | 查询参数不是合法整数 |
| `factorMining.invalidParameter` | 运行字段无法按其计价类型读取：整数字段（`seed` / `groups` / `holdingPeriod` / `population` / `generations` / `aiRounds` / `aiProposalsPerRound`）不是整数、`eliteRatio` 不是数值、`commission` / `slippage` 大到无法转换、`searchMode` 是本版本不认识的值、`timeframe` 不在支持的形状内（`<数字><m\|h\|d\|w>`）、`population` / `generations` 小于 1、`eliteRatio` 不在 `[0, 1)`、`aiRounds` 不在 `1-3`、`aiProposalsPerRound` 不在 `1-20`、`researchNotes` 不是字符串或超过 8000 字符、非 AI 运行携带任何 AI 字段（`aiRounds` / `aiProposalsPerRound` / `researchNotes` / `replayJobId`），或进化运行里 `population` / `generations` / `eliteRatio` 缺失或为空串 |
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
| `factorMining.aiProposalFailed` | 模型调用在传输或 JSON 上失败且重试后仍失败；已写入的提案保留 |
| `factorMining.aiNoValidProposals` | AI 运行的所有轮次都产出零合法提案 |
| `factorMining.aiReplayNotFound` | `replayJobId` 指向的作业不存在、不属于调用者，或不是挖掘作业 |
| `factorMining.aiReplayIncompatible` | 被重放的运行不是 AI 运行、状态是 `cancelled` / `running`、没有记录提案、DSL 版本不同，或客户端重述的运行字段与记录不一致 |
| `factorMining.runNotFound` | 写入指向了一个不存在或不属于调用者的运行 |

**参数化形式（3 类，消费端不要做裸键精确匹配）**

| 形式 | 来源 |
| --- | --- |
| `factorMining.templateNotFound:<template_id>` | `templateIds` 里出现了未知模板 id |
| `factorMining.fingerprintInvalid:<index>` | 第 `<index>` 个候选的指纹不是 64 位 sha256 十六进制 |
| `factorMining.reject.invalid:<reason>` | DSL 校验失败时 `ExpressionError.code` 携带的具体原因（`unknownField` / `tooDeep` / `tooManyNodes` / `unknownOperator` / `badArity` / `badNode` / `badWindow` / `lookbackExceeded`） |

**淘汰原因（15 个）**

| 键 | 层级 |
| --- | --- |
| `factorMining.reject.duplicateProposal` | AI 提案流（run 内重复指纹；状态 `duplicate`，从不评估） |
| `factorMining.reject.invalid` | L1（试验与候选的 `reason` 列存的是这个**裸键**；带 `:<reason>` 的形式只出现在 DSL 抛出的 `ExpressionError.code` 上） |
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

**池增量读数（2 个键，刻意不是淘汰原因）**

| 键 | 出现位置 |
| --- | --- |
| `factorMining.poolIncrement.noPool` | `outOfSample.poolIncrement.reason`，调用者还没有冻结候选库——这个问题从没被问过 |
| `factorMining.poolIncrement.unmeasured` | `outOfSample.poolIncrement.reason`，比过却什么都测不出，或这次复核背后没有搜索段可读 |

这两个键都不淘汰候选，也都不出现在候选的 `reason` 或试验的 `reason` 里：它们是并列在判定旁边的两种「没答案」状态（见第 10 节），而把运维引到错的那一个，等于让他去错的地方找库。

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

## 12. 已知非目标与未决

- **不计费**：本功能不扣额度、不写账本，资源只受并发上限与预算上限约束。
- **`queued` 幽灵不清扫**：只清扫 `running` 的僵尸作业，卡在 `queued` 的不在处理范围内（延后）。
- **`/catalog` 的 `operators` 只有名字**：不含 arity，也不标注是否带窗口参数；本页第 3.2 节的表是权威来源。
- **不接入策略运行时**：挖掘结果无法被策略引用，也没有用户作用域的读取路径。
- **不抓外部研究材料**：AI 模式只读 prompt 里带的内容——字段/算子目录、运行参数、失败摘要与调用者粘贴的纯文本笔记；论文、研报、文件上传与 PDF 解析都不在范围内。
- **模型是部署配置**：单次运行不能指定模型或 provider，只能用服务端配置；provider 未配置或调用失败时 AI 作业直接失败（`factorMining.aiProposalFailed`），不会回退到模板。因此提案质量是部署的属性，不是本功能的保证。
- **模拟运行监控未实现**：把挖出的候选放到模拟前向运行里跟踪，仍在范围之外。
- **重放的是提案，不是数据**：`replayJobId` 不调用模型，能复现提案集合及其 L1 状态，但会重新抓取面板；指纹变了由 `snapshotChanged` 标出。
- **只存数据指纹**：没有可重放的面板副本，因此历史运行无法离线重跑。
- **传统期货的期限结构与换月成本仍不在范围内**，需要一条期货多合约数据面。加密交割合约已由 Deribit 月度合约提供：`term_structure` 与 `roll_cost` 已进入 `Crypto` 白名单，点时口径见第 3.1 节。

相关：[策略工作流](STRATEGY_WORKFLOW_CN.md)、[回测中心](BACKTEST_CENTER_CN.md)、[Strategy API V2 开发指南](../trading/STRATEGY_DEV_GUIDE_CN.md)、[实盘安全](../trading/LIVE_TRADING_SAFETY_CN.md)。
