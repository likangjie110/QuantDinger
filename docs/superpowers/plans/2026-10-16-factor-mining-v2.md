# 全自动量化因子挖掘（v2）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把因子挖掘从「模板展开的确定性枚举」升级为「在既有 DSL 与门槛体系内做受约束的组合搜索」，并让搜索结果可被判定为是否带来增量信息：表达式树进化/交叉搜索、候选间数值去重（含补齐 spec 承诺的内置因子比较集合）、因子池协同价值评估、四个跨市场数据字段。

**架构：** v1 的候选统一表示（JSON 表达式树 + `dsl.py` 指纹）保持不变，进化搜索只是**同一表示的另一种生成器**——产出的每个个体照旧过 L1 合法性校验、照旧逐级淘汰、照旧写 `qd_factor_trials`。因此新增的是生成器与两个评估维度，不是新的评估管线。候选间去重与池增量共用同一条新能力（从存储的表达式重算因子面板），因此合为一个工作流。跨市场字段走 `dsl.py` 字段白名单 + `fundamental_data.enrich_panel` 这条既有的面板扩展口，白名单仍由 L1 强制。作业、存储、HTTP 契约沿用 v1，只做增量字段，**不新增端点、不新增进程角色、不新增 job kind**。

**技术栈：** Python 3.11、Flask、pandas、PostgreSQL（psycopg 风格 `get_db_connection`）、Celery（经 `agent_jobs`）、pytest。

**规格：** [`docs/superpowers/specs/2026-10-13-factor-mining.md`](../specs/2026-10-13-factor-mining.md) §13 v2 行；v1 计划 [`2026-10-13-factor-mining-v1.md`](2026-10-13-factor-mining-v1.md) 提供分层、命名与步骤惯例。执行前两份都要读。

## 全局约束

- Python 3.11（仓库 `.venv`）；`ruff` line-length 120，规则集 `E9,F63,F7,F82`。
- 测试命令统一：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/<file>.py -q`。
- `pytest.ini` 设 `timeout = 120`：**任何单测不得触网、不得建真 DB 连接**，一律用合成面板、录制响应与假 provider。
- 源码注释用英文（`AGENTS.md`）；面向用户文案只返回 i18n 键 `factorMining.*`，不硬编码英文句子。
- 不得修改 `_FACTORS`（`app/services/factors/registry.py:157`）的语义，不得把挖掘因子写回源码文件。
- 不得新增进程角色；挖掘作业只能通过 `agent_jobs.submit_job` 提交（`docs/architecture/README.md` 进程所有权）。
- 触碰 HTTP 契约的任务必须重新导出 OpenAPI：`python scripts/export_openapi.py`，并跑 `tests/test_openapi.py`。
- 保留工作区已有未提交改动：只追加、不回滚、不覆盖无关文件（开工前先看一次 `git status`，不要假定它和本计划写作时一致）。
- 每个任务结束前：`python -m py_compile <改动文件>` 通过 + 该任务的测试通过。
- **确定性是 v1 的既有验收，v2 不得放宽**（`engine.py:292-299` 模块 docstring 的契约）：同 `(templates, budget, seed, 搜索模式)` → 字节级相同的候选列表与顺序。进化搜索的随机性只能来自 `random.Random(seed)` 实例，集合遍历一律排序后再迭代。
- **`qd_factor_candidates.expression_hash` 在本次升级中必须保持稳定**：改变 `MAX_DEPTH` 是**校验策略**变更，不是表达式语义变更，因此**不递增 `DSL_VERSION`**（`dsl.py:19`）——递增会让候选库全部指纹失效、旧候选被当成新表达式重复入库。这条是本次升级里唯一容易误判的取舍，任务 3 必须用测试钉住「同一表达式升级前后指纹相同」。
- **字段白名单只进「实际拥有且时间口径可靠」的字段**（规格 §6.2）：跨市场字段在能证明点时可用性之前不得进白名单。
- **试验台账是选择偏差校正的分母**（规格 §10.5）：进化搜索**产出的每一个个体**（无论最终通过哪一级门槛）都必须写 `qd_factor_trials`——个体数量上升不是改为抽样留痕的理由。**在源头被拒的子代（非法树、裸字段）与按指纹去重跳过的子代不算产出**：它们从未被评估，不入台账。这与 v1 一致：`expand_candidates` 也是先按指纹去重才返回候选。
- **候选池的边界口径固定为 `(user_id, market, timeframe)`**：因子值是截面信号，跨市场与跨周期不可比，只有同市场同周期内的候选才构成「要不要再来一个」的选择集。

## 审查重点（Review Focus）

规格隐含、但最可能伤到使用者的输入/条件，按可能性排序。每行都已在对应任务里安排了钉住它的测试。

1. **进化搜索重新引入前视偏差**：交叉/变异合成的新表达式绕开模板的既有保证（窗口被改成 0 或负数、窗口和越过 `MAX_LOOKBACK`）→ 因子值虚高且不可复现。钉住：任务 1 `test_mutation_never_emits_a_nonpositive_window` + 任务 3 `test_every_scored_individual_cleared_l1_first`。
2. **试验台账漏记**：个体数上升后只记幸存者 → DSR 分母偏小、一切指标虚高。钉住：任务 3 `test_every_emitted_individual_gets_a_trial_row`（产出多少写多少）+ 任务 2 `test_rejected_children_are_counted_not_silently_dropped`（源头被拒的子代仍消耗预算，不会让搜索空转）。
3. **指纹漂移**：本次改了校验上界，若误递增 `DSL_VERSION`，候选库既有行的 `expression_hash` 全部失效、旧候选被重复入库。钉住：任务 3 `test_the_fingerprint_is_unchanged_by_the_raised_depth_bound`（同一表达式在改动前后指纹逐字节相同）。
4. **候选间数值去重口径错**：把本质不同的因子判为重复（误杀），或放行同一因子的仿射变换（漏杀）→ 候选库要么变窄要么被近亲灌满。钉住：任务 7 `test_numeric_dedup_rejects_an_affine_rescaled_twin` + `test_numeric_dedup_keeps_a_distinct_factor_with_low_correlation`。
5. **协同价值评估读到冻结段**：用冻结段数据评估「加进池子后有无增量」→ 冻结段隔离被击穿，样本外证据失效。钉住：任务 8 `test_pool_gain_is_measured_without_the_freeze_segment`。
6. **跨市场字段口径不可靠却照跑**：字段存在但按错误时点对齐（用了结算后才可知的费率）→ 看似有信号，实为未来数据。钉住：任务 12 `test_funding_field_is_aligned_to_the_bar_it_was_known_at`。
7. **新字段被静默拒绝**：`dsl.normalize_expression` 的 `allowed_fields` 默认值是 5 个价量字段，现有 5 个调用点全部依赖默认值——只改白名单常量而不把字段集一路传下去，新字段会在 L1 以 `unknownField` 被拒且看不出原因。钉住：任务 10 `test_a_derivatives_field_is_accepted_after_the_field_set_is_threaded_through`。
8. **非周末交易市场的日历口径**：`pd.bdate_range` 对 Crypto 把冻结段切成约 21% 的日历日而非 30% 的交易日 → 样本外窗口被系统性缩短。钉住：任务 12 `test_session_calendar_follows_the_markets_trading_days`。

---

## 文件结构

```
backend_api_python/app/services/factor_mining/
    evolution.py      新增：变异/交叉算子 + 确定性种群循环（纯函数，无 IO）
    pool.py           新增：候选池读取与面板重算（②③共用）
    incremental.py    新增：因子池增量价值评估（③）
    market_fields.py  新增：跨市场字段面板装配（④）
    engine.py         修改：候选来源抽象、warmup 上界、L4 比较集合、L6 阶段、enricher 挂点
    dsl.py            修改：深度上界、字段分组
    gates.py          修改：L4 阈值拆分
    templates.py      修改：模板 7 深度削平
    service.py        修改：配置透传、估价口径
backend_api_python/app/data_sources/derivatives.py   新增：资金费率/持仓量历史序列（④a）
backend_api_python/app/routes/factor_mining.py       修改：_RUN_FIELDS 增量字段（无新端点）
backend_api_python/app/services/strategy_v2/factor_research.py  修改：比较集合可注入
backend_api_python/tests/test_factor_mining_evolution.py        新增
backend_api_python/tests/test_factor_mining_pool.py             新增
backend_api_python/tests/test_factor_mining_incremental.py      新增
backend_api_python/tests/test_factor_mining_market_fields.py    新增
backend_api_python/tests/test_derivatives_source.py             新增
```

职责边界：`evolution.py` / `pool.py` / `incremental.py` / `market_fields.py` 都是纯计算或纯装配，不碰 DB、不碰作业；DB 访问留在 `storage.py`，编排留在 `engine.py`（沿用 `MODULE_BOUNDARIES.md` 的分层）。

---

## 工作流 A：受约束的表达式树进化搜索（①）

### 任务 1：变异与交叉算子

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/evolution.py`
- 测试：`backend_api_python/tests/test_factor_mining_evolution.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_mutation_never_emits_a_nonpositive_window():
    # 对含窗口参数的树反复变异，任何产出节点的窗口参数必须落在 [WINDOW_MIN, WINDOW_MAX]
def test_mutation_changes_exactly_one_locus():
    # 返回的树与父代恰有一个位置不同（或返回 None 表示本次变异被拒）
def test_crossover_children_are_legal_or_none():
    # 交叉产物要么通过 normalize_expression，要么是 None —— 不允许返回非法树
def test_operators_do_not_leak_future_bars_after_mutation():
    # 变异后代沿用 v1 的前缀不变性：改未来行不改变历史因子值
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_evolution.py -q`
预期：FAIL，`ModuleNotFoundError: app.services.factor_mining.evolution`

- [ ] **步骤 3：在 `evolution.py` 中实现 `mutate(node, rng)` 与 `crossover(left, right, rng)`**

签名固定为 `mutate(node: dict | str, rng: random.Random) -> dict | str | None` 与 `crossover(left, right, rng: random.Random) -> dict | str | None`。两者都只做树变换，合法性判定一律委托 `dsl.normalize_expression`：抛 `ExpressionError` 即返回 `None`。变异三种模式（同为窗口参数的 `n` 扰动、同 arity 算子替换、子树替换）由 `rng` 选择；窗口扰动后必须落在 `[dsl.WINDOW_MIN, dsl.WINDOW_MAX]` 内，越界即重新采样而非夹取（夹取会让边界值被系统性过采样）。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_evolution.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/evolution.py backend_api_python/tests/test_factor_mining_evolution.py
git commit -m "feat: 因子挖掘新增受约束的表达式树变异与交叉算子"
```

### 任务 2：确定性种群循环与适应度

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/evolution.py`
- 测试：`backend_api_python/tests/test_factor_mining_evolution.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_evolution_is_byte_identical_for_the_same_seed():
    # 同 (初代, budget, seed) 两次调用，产出的表达式序列逐字节相同
def test_evolution_respects_budget():
    # 产出的个体总数不超过 budget，且计数与实际产出条数一致
def test_rejected_children_are_counted_not_silently_dropped():
    # 被 mutate/crossover 拒绝的非法子代仍然计入产出预算，不是静默丢弃
def test_elite_survives_across_generations():
    # 上一代适应度最高的个体必然出现在下一代（精英保留不可被随机淘汰）
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_evolution.py -q`
预期：FAIL，`ImportError: cannot import name 'evolve'`

- [ ] **步骤 3：实现 `evolve(seeds, *, budget, seed, population, generations, elite_ratio, fitness) -> Iterator[CandidateSeed]`**

`seeds` 是初代（由 `templates.expand_candidates` 产出，元素是 `templates.CandidateSeed`）。**`evolve` 直接产出 `CandidateSeed`**（`origin="evolution"`、`fingerprint` 取自 `dsl.normalize_expression`、`template_id` 记最初的来源模板或空串），**不引入新类型**——这样 `engine` 消费两种来源时无需分支。适应度来自回调 `fitness(expression) -> float | None`，由 `engine` 注入已有的 L2/L3 指标（`icir`），本模块不自己算指标也不碰数据。选择用确定性锦标赛（`rng` 决定比较对象，按 `(fitness, fingerprint)` 排序打破平局）。所有集合遍历前先 `sorted()`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_evolution.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/evolution.py backend_api_python/tests/test_factor_mining_evolution.py
git commit -m "feat: 因子挖掘新增确定性种群循环与精英保留"
```

### 任务 3：引擎接入、warmup 上界与深度预算

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/engine.py:310-354`（`run` 的候选来源）、`:819-839`（`_warmup_bars`）
- 修改：`backend_api_python/app/services/factor_mining/dsl.py:20`（`MAX_DEPTH`）
- **不得修改**：`backend_api_python/app/services/factor_mining/templates.py`
- 测试：`backend_api_python/tests/test_factor_mining_engine.py`、`tests/test_factor_mining_dsl.py`、`tests/test_factor_mining_templates.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_the_fingerprint_is_unchanged_by_the_raised_depth_bound():
    # 同一表达式在 MAX_DEPTH=6 与 8 下 normalize_expression 的 fingerprint 相同
def test_max_depth_bound_accepts_eight_and_rejects_nine():
    # 深度 8 的树通过；深度 9 抛 factorMining.reject.invalid:tooDeep
def test_warmup_uses_the_dsl_lookback_budget():
    # 无论候选来自枚举还是进化，warmup 恒为 MAX_LOOKBACK，不再由预扫决定
def test_search_consumes_a_generator_source():
    # run 接受可迭代的候选来源，边产出边评估，进度总量为预算而非预先长度
def test_every_scored_individual_cleared_l1_first():
    # 任何进入评分的个体都先过 dsl.normalize_expression，非法个体只能记为无效试验
def test_every_emitted_individual_gets_a_trial_row():
    # 进化模式下 qd_factor_trials 行数 = 引擎实际收到的产出条数，且 ≤ budget
    # （源头被拒、以及按指纹去重跳过的子代都不是产出，不入台账）
def test_run_is_byte_identical_for_the_same_seed():
    # 同 seed 两次 run 的 trials 序列与顺序逐字节相同（含进化模式）
def test_smoothed_template_seven_leaves_room_for_crossover():
    # 模板 7 加平滑变体后的深度严格小于 MAX_DEPTH，留出至少 2 层余量
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_engine.py tests/test_factor_mining_dsl.py tests/test_factor_mining_templates.py -q`
预期：FAIL——深度 8 被拒、warmup 仍走预扫、`run` 不接受迭代器来源

- [ ] **步骤 3：实现三处改动**

`dsl.py:20` 的 `MAX_DEPTH` 由 6 改为 8（**不递增 `DSL_VERSION`**：这是校验策略变更，表达式语义未变，指纹必须保持稳定）。

**不要削平模板 7。** 单靠 `MAX_DEPTH` 改 8 就已经给了它余量：`conditional_filter` 的骨架深度是 5（`where`→`gt`→`ts_rank`→`ts_mean`→`volume`），加一层平滑变体后是 6，距新上界 8 恰好 **2 层**，满足 `test_smoothed_template_seven_leaves_room_for_crossover` 的「至少 2 层余量」。改模板骨架会改掉 `expand_candidates` 的枚举输出，直接违反本计划的人工验收第 1 条（枚举模式候选列表与顺序与升级前逐字节相同）——那一条是 v1 确定性契约的回归门。该测试因此退化为**回归守卫**：它守的是「模板 7 的余量存在」，不是「模板 7 被改写」。

`engine.py` 的候选来源改为接受 `Iterable[CandidateSeed]`：枚举模式下由 `expand_candidates` 提供（行为不变），进化模式下由 `evolution.evolve` 提供，进度总量在进化模式下按 `budget` 上报。`engine` 向 `evolve` 注入 `fitness` 回调（取该个体已算出的 L2/L3 指标，默认 `icir`），进化模块本身不碰数据、不算指标。`_warmup_bars` 改为恒定返回 `dsl.MAX_LOOKBACK`——进化无法预知最长 lookback，取上限是唯一保持正确性的选择，代价是每次 run 多取 504 根 bar。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_engine.py tests/test_factor_mining_dsl.py tests/test_factor_mining_templates.py tests/test_factor_mining_freeze.py -q`
预期：PASS（含 v1 既有测试无回归）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/dsl.py backend_api_python/app/services/factor_mining/templates.py backend_api_python/app/services/factor_mining/engine.py backend_api_python/tests/
git commit -m "feat: 因子挖掘搜索接受进化候选来源，深度预算放宽到 8"
```

### 任务 4：搜索模式配置与估价口径

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/engine.py:107`（`MiningConfig`）、`service.py:102`（`estimate`）、`backend_api_python/app/routes/factor_mining.py:31-35`（`_RUN_FIELDS`）
- 测试：`backend_api_python/tests/test_factor_mining_service.py`、`tests/test_factor_mining_api.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_search_mode_defaults_to_enumeration():
    # 不传搜索模式时行为与 v1 完全一致（候选列表逐字节相同）
def test_evolution_mode_requires_a_population_size():
    # searchMode="evolution" 而不给种群参数 → factorMining.invalidParameter
```

`/estimate` 的两键契约**不新增测试**：v1 既有断言已在守着它，本任务只需保持它绿——那条断言就是不把 `mode` 塞进返回体的护栏。

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_service.py tests/test_factor_mining_api.py -q`
预期：FAIL，未知字段被 `_RUN_FIELDS` 拒绝

- [ ] **步骤 3：实现配置透传**

`MiningConfig` 增加 `search_mode`（`"enumeration"` 默认 / `"evolution"`）、`population`、`generations`、`elite_ratio`；`_RUN_FIELDS` 同步放行。**`estimate` 的返回体不动**：v1 已承诺它「只返回 `{templateCount, candidateBudget}` 这两项，没有别的」（`FACTOR_MINING_CN.md` §7），而客户端本来就知道自己提交的是哪个模式——回显 `mode` 是纯粹的契约破坏，没有换来任何信息。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_service.py tests/test_factor_mining_api.py tests/test_factor_mining_engine.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/ backend_api_python/app/routes/factor_mining.py backend_api_python/tests/
git commit -m "feat: 因子挖掘作业支持选择枚举或进化搜索模式"
```

---

## 工作流 B：候选池冗余与增量价值（②③）

### 任务 5：候选池读取与面板重算

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/pool.py`
- 修改：`backend_api_python/app/services/factor_mining/storage.py:370`（`list_candidates` 支持按 `market`+`timeframe` 过滤）
- 测试：`backend_api_python/tests/test_factor_mining_pool.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_pool_is_scoped_to_one_user_market_and_timeframe():
    # 同 user 不同 market、同 market 不同 timeframe、不同 user 的候选都不进池
def test_pool_panels_are_rebuilt_from_the_stored_expression():
    # 从 expression_json 重算的面板与当初入库时的指标一致（同数据、同表达式）
def test_pool_excludes_the_candidate_under_evaluation():
    # 评估某一候选时，池里不含它自己（否则自我相关恒为 1）
def test_pool_caps_the_number_of_members():
    # 池成员数受显式上限约束，超出时按 (status, id) 稳定截断而不是随机截断
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_pool.py -q`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：实现 `load_pool(...)` 与 `pool_panels(...)`**

`load_pool(user_id, market, timeframe, *, exclude_hash, limit)` 读 `storage.list_candidates` 的 `frozen` 行（边界口径见全局约束）。`pool_panels(entries, field_panels)` 用 `evaluator` 从 `expression_json` 重算每个池成员的因子面板——**重算而不是存副本**，与 v1「只存指纹不存可重放副本」的边界一致，代价是每次评估要重跑池成员的表达式。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_pool.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/pool.py backend_api_python/app/services/factor_mining/storage.py backend_api_python/tests/test_factor_mining_pool.py
git commit -m "feat: 因子挖掘新增候选池读取与面板重算"
```

### 任务 6：相关度比较集合可注入 + 补齐内置因子

**文件：**
- 修改：`backend_api_python/app/services/strategy_v2/factor_research.py:18-25`（`_CORRELATION_FACTORS`）、`:470`（names 组装）
- 修改：`backend_api_python/app/services/factor_mining/engine.py:552`（`_correlations`）
- 测试：`backend_api_python/tests/test_factor_research.py`、`tests/test_factor_mining_engine.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_the_correlation_study_accepts_an_injected_comparison_set():
    # 传入自定义比较集合时，研究只对这些名字算相关，不再用类常量
def test_the_default_comparison_set_is_unchanged():
    # 不传参时仍是原来的 6 个名字，逐字节相同（既有调用方零回归）
def test_the_candidate_is_compared_against_registry_factors():
    # 挖掘候选的相关度研究覆盖 factors/registry 里可算的因子，而不止 6 个代理
def test_the_comparison_set_is_capped_and_reported():
    # 比较集合超过上限时按稳定顺序截断，且实际参与的名字集合可从 metrics 读出
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_research.py tests/test_factor_mining_engine.py -q`
预期：FAIL，`run()` 不接受 `comparison_factors`

- [ ] **步骤 3：实现注入与补齐**

`FactorResearchEngine.run` 增加可选 `comparison_factors: Sequence[str] | None = None`，`None` 时回落到 `_CORRELATION_FACTORS`（既有调用方零回归）。`engine._correlations` 组装两段来源：内置（`factors/registry.list_factors()` 里能算的）+ 池成员，各自带命名空间前缀以避免与内置因子重名（池成员用 `pool:<fingerprint前8位>`）。**代价必须显式**：比较集合从 6 涨到 61+池，逐截面 Spearman 的成本随之上涨，因此加显式上限（默认内置 61 + 池 20），实际参与的名字集合写进 metrics 供复盘。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_research.py tests/test_factor_mining_engine.py tests/test_factor_registry.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/strategy_v2/factor_research.py backend_api_python/app/services/factor_mining/engine.py backend_api_python/tests/
git commit -m "feat: 相关度研究接受注入的比较集合，挖掘候选补齐内置因子对照"
```

### 任务 7：L4 阈值拆分与候选间数值去重

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/gates.py:43`（阈值）、`:196`（`screen_redundancy`）
- 测试：`backend_api_python/tests/test_factor_mining_gates.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_numeric_dedup_rejects_an_affine_rescaled_twin():
    # 同一因子的仿射变换（2x+3）与原件相关度 ~1.0 → 以 duplicateInPool 淘汰
def test_numeric_dedup_keeps_a_distinct_factor_with_low_correlation():
    # 相关度 0.3 的候选不被误杀，两个阈值各自独立生效
def test_the_two_correlation_thresholds_are_independently_overridable():
    # 覆盖池阈值时内置阈值不变，反之亦然
def test_pool_redundancy_stays_fail_closed_on_all_nan():
    # 池内相关度全为 NaN 时候选不得白过这一关
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_gates.py -q`
预期：FAIL，`GateThresholds` 无 `max_pool_correlation`

- [ ] **步骤 3：实现阈值拆分**

`GateThresholds` 增加 `max_pool_correlation`（默认 0.85，与内置阈值同值——两者判的都是「这个因子没有新信息」，但可分别覆盖）。`screen_redundancy` 按名字命名空间区分两段：`pool:` 前缀的用池阈值与 `factorMining.reject.duplicateInPool`，其余用内置阈值与既有键。**fail-closed 语义不变**：过滤后没有任何有限值时仍以 `unverifiableRedundancy` 淘汰。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_gates.py tests/test_factor_mining_engine.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/gates.py backend_api_python/tests/
git commit -m "feat: L4 拆出候选池相关度阈值，新增池内重复淘汰键"
```

### 任务 8：因子池增量价值评估

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/incremental.py`
- 测试：`backend_api_python/tests/test_factor_mining_incremental.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_pool_gain_is_measured_without_the_freeze_segment():
    # 把冻结段数据替换成噪声后，增量判定与增量数值必须逐字节不变
def test_incremental_value_rejects_a_factor_the_pool_already_explains():
    # 候选若可由池内因子线性解释，残差 IC ≈ 0 → 以 noIncrementalValue 淘汰
def test_incremental_value_keeps_an_orthogonal_factor():
    # 与池内因子近正交的候选残差 IC ≈ 原 IC，予以保留
def test_residual_ic_requires_the_same_sign_as_the_raw_ic():
    # 残差 IC 与原 IC 反号时判为噪声而非增量，避免靠翻转方向骗过门槛
def test_an_empty_pool_short_circuits_to_no_measurement():
    # 池为空时不伪装成「有增量」，返回明确的未测量标记
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_incremental.py -q`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：实现 `residual_ic(candidate_panel, pool_factor_panels, returns, *, min_abs_rank_ic)`**

（第二个形参即任务 5 `pool.pool_panels()` 的返回值；此处改名以免与函数同名。）

对候选因子在每个截面上向池内因子做横截面回归，取残差再与原收益求 Spearman IC（口径同 `factor_research.py:142`：先 rank 再 pearson）。判据：`abs(residual_ic) >= min_abs_rank_ic`（默认沿用 L2 的 0.02）**且**与原始 `rankIc` 同号才判为有增量。只用 IS 与 validation 段，**冻结段不参与**——增量是搜索期判据，不是样本外证据。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_incremental.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/incremental.py backend_api_python/tests/test_factor_mining_incremental.py
git commit -m "feat: 因子挖掘新增候选池增量价值评估"
```

### 任务 9：L6 阶段接入冻结评审

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/engine.py:363`（`review_frozen`）、`:613`（`_summary`）
- 测试：`backend_api_python/tests/test_factor_mining_freeze.py`、`tests/test_factor_mining_engine.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_a_frozen_survivor_carries_its_pool_increment():
    # 通过 L5 的候选，out_of_sample 里带 poolIncrement 键（残差 IC、池规模、判定）
def test_a_candidate_without_increment_is_rejected_at_l6():
    # 残差 IC 不达门槛 → status 为 rejected、reason 为 factorMining.reject.noIncrementalValue
def test_l6_runs_after_l5_and_never_reads_the_freeze_segment():
    # 改写冻结段数据后 L5 读数变化但 L6 判定不变（阶段顺序与隔离同时钉住）
def test_an_empty_pool_leaves_candidates_frozen():
    # 池为空时 L6 不淘汰任何候选，只在 poolIncrement 里记未测量
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_freeze.py tests/test_factor_mining_engine.py -q`
预期：FAIL，`out_of_sample` 无 `poolIncrement`

- [ ] **步骤 3：实现 L6 接入**

在 `review_frozen` 的每个 survivor 完成 L5 之后追加一次增量评估，结果写入 `out_of_sample_json` 的 `poolIncrement`（残差 IC、池规模、参与比较的名字集合、判定）。**不新增端点、不新增 job kind、不新增表**——增量是「这次 run 的候选值不值得进池」的结论，天然属于一次 run；将来若要按需重估，再单独设计只读端点。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_freeze.py tests/test_factor_mining_engine.py tests/test_factor_mining_storage.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/ backend_api_python/tests/
git commit -m "feat: 冻结评审新增 L6 池增量阶段"
```

---

## 工作流 C：跨市场字段（④）

### 任务 10：字段分组与 `allowed_fields` 贯通

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/dsl.py:25`（字段常量）
- 修改：`engine.py:424`、`:465`、`:836`、`evaluator.py:56`、`templates.py:242`（5 个调用点）
- 测试：`backend_api_python/tests/test_factor_mining_dsl.py`、`tests/test_factor_mining_engine.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_normalize_rejects_an_unknown_field_by_key():
    # 白名单外的字段仍以 factorMining.reject.invalid:unknownField 拒绝
def test_a_derivatives_field_is_accepted_after_the_field_set_is_threaded_through():
    # 显式传入含 funding_rate 的字段集后，依赖它的表达式通过 L1 校验
def test_a_derivatives_field_is_rejected_when_the_market_has_none():
    # 未启用衍生品字段的市场，同一表达式仍在 L1 被拒（白名单是市场相关的）
def test_the_fingerprint_does_not_depend_on_the_field_set():
    # 同一个表达式在不同字段集下的指纹相同（字段集是策略，不是语义）
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py tests/test_factor_mining_engine.py -q`
预期：FAIL——`funding_rate` 在传入的字段集里仍被拒（调用点漏传 `allowed_fields`）

- [ ] **步骤 3：实现字段分组与贯通**

`dsl.py` 把 `PRICE_VOLUME_FIELDS` 改为分组常量 `FIELD_GROUPS = {"price_volume": (...), "derivatives": (...)}` 并新增 `fields_for(market) -> tuple[str, ...]`（按市场返回该市场允许的字段组）。**不要在白名单层判可用性**：某个标的当天没有衍生品数据不是「字段非法」，而是缺列——沿用 `_field_panels` 的既有语义保留 NaN 列，由 L2 覆盖率门槛拦截。`PRICE_VOLUME_FIELDS` 保留为 `FIELD_GROUPS["price_volume"]` 的别名以免破坏既有导入。**5 个调用点全部显式传 `allowed_fields`**：`engine.py:424`（L1）、`engine.py:465`（L5）、`engine.py:836`（warmup）、`evaluator.py:56`（缓存 key）、`templates.py:242`（指纹）。指纹只依赖表达式本身，不依赖字段集（与任务 3 同一条理由）。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py tests/test_factor_mining_engine.py tests/test_factor_mining_evaluator.py tests/test_factor_mining_templates.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/ backend_api_python/tests/
git commit -m "feat: 因子挖掘字段白名单按市场分组并贯通全部校验点"
```

### 任务 11：资金费率与持仓量历史序列

**文件：**
- 创建：`backend_api_python/app/data_sources/derivatives.py`
- 测试：`backend_api_python/tests/test_derivatives_source.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_funding_history_is_normalised_to_a_per_interval_rate():
    # 各交易所返回的百分数/小数、8h/1h 口径统一成一个按 bar 折算的费率
def test_open_interest_history_is_returned_as_a_level_series():
    # 持仓量是水平量（可派生变化率），不是已经算好的变化
def test_an_unsupported_market_returns_an_empty_series_not_an_error():
    # 不支持的市场返回空序列，由上层决定是否启用该字段
def test_a_provider_failure_does_not_poison_the_other_fields():
    # 一个字段抓取失败不影响另一个字段可用
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_derivatives_source.py -q`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：实现抓取与归一化**

按交易所实现两个函数，签名固定为 `fetch_funding_history(market, symbol, *, start, end) -> pd.Series` 与 `fetch_open_interest_history(market, symbol, *, start, end) -> pd.Series`（任务 12 按这两个签名消费，不要在这里换名字）。覆盖 Binance `fapi/v1/fundingRate` 与 `futures/data/openInterestHist`、Gate `/futures/usdt/contract_stats`、OKX 对应端点，参考实现见 `app/services/market_data_collector.py:1840`（Gate）与 `:1965`（Binance）——但那些只取 `limit=1/2` 的最近值，本任务要按历史长度取序列。单位归一化沿用 `app/professional_report/features/crypto.py:547-570` 的 `_normalise_rate` / `_normalise_amount` 口径。单测用录制响应，不触网。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_derivatives_source.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/data_sources/derivatives.py backend_api_python/tests/test_derivatives_source.py
git commit -m "feat: 新增资金费率与持仓量的历史序列数据源"
```

### 任务 12：跨市场字段装配与时点口径

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/market_fields.py`
- 修改：`backend_api_python/app/services/factor_mining/engine.py:230`（`_prepare_window`，在 `:269` 的 `fetch_frames` 之后插 enricher）、`:852`（`_field_panels` 放开字段集）
- 测试：`backend_api_python/tests/test_factor_mining_market_fields.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_funding_field_is_aligned_to_the_bar_it_was_known_at():
    # 只能用该 bar 结算前已公布的费率；用结算后的值必须让测试失败
def test_fields_are_forward_filled_not_back_filled():
    # 缺失日沿用上一可知值，绝不用未来值回填
def test_session_calendar_follows_the_markets_trading_days():
    # Crypto 用自然日、股票用交易日；冻结段比例按交易日计
def test_a_symbol_without_derivatives_keeps_a_nan_column():
    # 没有衍生品数据的标的保留 NaN 列而不是掉出截面（沿用 _field_panels 的既有语义）
def test_the_snapshot_fingerprint_covers_the_new_fields():
    # 数据指纹把新字段算进去，否则同一指纹下的两次运行数据其实不同
def test_the_shared_strategy_v2_fingerprint_is_untouched():
    # canonical_frame_bytes 对同一帧的返回值与升级前逐字节相同（跨模块零回归）
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_market_fields.py -q`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：实现装配与 enricher**

`market_fields.py` 按 `fundamental_data.enrich_panel`（`:71`）的既有模式装配：按 `(market, symbol)` 取序列、按可知时点排序去重、`reindex(dates, method="ffill")`。接入点在 `engine._prepare_window` 的 `fetch_frames` 之后。

**指纹必须覆盖新字段，但不能改共享函数。** `engine._data_digest`（`engine.py:888`）调用的是 `strategy_v2/snapshot.py:58` 的 `canonical_frame_bytes`，它只哈希 `SNAPSHOT_COLUMNS`（`snapshot.py:15`，五个价量列）——加列不会改变 `snapshot_id`，会让两次数据不同的运行共享指纹。但那个函数是 **Strategy V2 自己的数据版本原语**，改它会把回测与研究的既有数据版本一起改掉，属于全局约束禁止的跨模块行为变更。正确做法：在 `factor_mining` 内部自建一个**本地**摘要函数，对新增列按排序后的列名逐列折进摘要，`canonical_frame_bytes` 的输出与语义原样不动。

日历口径按市场取交易日，修掉 v1 遗留的 `pd.bdate_range` 假设（Crypto 冻结段比例因此从约 21% 的日历日回到 30% 的交易日）。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_market_fields.py tests/test_factor_mining_data.py tests/test_factor_mining_engine.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/ backend_api_python/tests/
git commit -m "feat: 跨市场字段按可知时点装配并纳入数据指纹"
```

### 任务 13：期限结构与换月成本 —— 前置未决，本计划只定语义

**文件：**
- 修改：`docs/product/FACTOR_MINING.md`、`docs/product/FACTOR_MINING_CN.md`（§3.1 字段白名单预留分组）

- [ ] **步骤 1：不写代码，先落字段语义与验收口径**

本任务**不实现**，原因是前置条件不成立：全仓对 `term_structure` / `roll_cost` 零代码（grep 无命中），且需要**期货多合约行情**这一条当前不存在的数据面——现有 `app/data_sources/futures.py` 只有单合约 ticker/kline。计划只固定两件事：

- **字段语义**：`term_structure` = 近月与远月年化价差；`roll_cost` = 按既定换月规则的主力合约切换成本。两者都必须能回答「哪个时点可知」。
- **验收口径**：与前四项字段相同——进白名单前必须证明点时可用性，否则 L1 拒绝依赖它的候选。

- [ ] **步骤 2：在两版文档的 §11 已知非目标里登记**

写明：期限结构与换月成本需要新建期货多合约数据面，**不在本计划范围**，需要单独的数据源计划与合约元数据（主力合约判定、换月规则、复权口径）；在数据面就绪前，字段不进白名单。

- [ ] **步骤 3：Commit**

```bash
git add docs/product/FACTOR_MINING.md docs/product/FACTOR_MINING_CN.md
git commit -m "docs: 登记期限结构与换月成本的前置条件与字段语义"
```

---

## 任务 14：文档、OpenAPI、i18n 与集成脚本收尾

**文件：**
- 修改：`docs/product/FACTOR_MINING.md`、`docs/product/FACTOR_MINING_CN.md`、`frontend-factor-mining/src/i18n/zh-CN.js:2,146-149`
- 修改：`backend_api_python/tests/integration/check_factor_mining_postgres.py`
- 重新生成：`docs/api/openapi.yaml`

- [ ] **步骤 1：同步两版产品文档**

§3.1 字段白名单（分组与市场相关性）、§3.3 约束常量（`MAX_DEPTH` 6→8 及理由）、§4 模板库（模板 7 削平）、§5 阶梯表（新增 L6 与 `duplicateInPool` / `noIncrementalValue` 两个键）、§7 HTTP 接口（`/run` 与 `/estimate` 的新字段）、§9 数据表（`poolIncrement` 落在 `out_of_sample_json`）、§10 i18n 键清单（新增键逐字补齐，表头计数同步）、§11 已知非目标（期限结构与换月成本的前置条件）。

- [ ] **步骤 2：补 i18n 键**

`frontend-factor-mining/src/i18n/zh-CN.js` 补 `factorMining.reject.duplicateInPool`、`factorMining.reject.noIncrementalValue`、`factorMining.poolIncrement.*` 与搜索模式相关文案。

- [ ] **步骤 3：重新导出 OpenAPI 并跑契约测试**

运行：`python scripts/export_openapi.py` 然后 `.venv/Scripts/python.exe -m pytest tests/test_openapi.py -q`
预期：PASS；`docs/api/openapi.yaml` 的 factor-mining 路径仍为 8 处（无新端点），`/run` 请求体新增字段已反映。

- [ ] **步骤 4：扩展真库集成脚本**

`check_factor_mining_postgres.py` 增加一段：写入一条带 `poolIncrement` 的候选并回读，断言 JSON 往返不丢键。`DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres .venv/Scripts/python.exe tests/integration/check_factor_mining_postgres.py`
预期：退出码 0

- [ ] **步骤 5：Commit**

```bash
git add docs/ frontend-factor-mining/src/i18n/zh-CN.js backend_api_python/tests/integration/check_factor_mining_postgres.py
git commit -m "docs: 同步因子挖掘 v2 的字段、阶梯、契约与键清单"
```

---

## 手工验收（需要真实数据源与 DB，不在单测范围）

1. 枚举模式跑一次 USStock 日线作业，候选列表与顺序**与升级前逐字节相同**（确定性未被破坏）。
2. 进化模式跑同一配置，`qd_factor_trials` 行数等于 `budget`，`origin` 列出现 `"evolution"`。
3. 同 seed 重跑进化作业，`trials` 序列逐字节相同。
4. 已有 `frozen` 候选的 `expression_hash` 在升级前后不变（查库比对）。
5. 池非空时新候选的 `poolIncrement` 有残差 IC 与池规模；池为空时不淘汰任何候选。
6. Crypto 配置下冻结段占比为约 30% 的**交易日**（升级前约 21% 的日历日）。

---

## 未决与前置（执行前需确认）

1. **任务 13 不实现**：期限结构与换月成本需要新建期货多合约数据面，是否单独立项由你决定。
2. **成本上升是刻意的**：比较集合从 6 涨到「内置 + 池」，`_warmup_bars` 恒取 `MAX_LOOKBACK`，两者都会让单次 run 变慢。若实测不可接受，退路是给比较集合设更小的上限（任务 6 已有上限参数），而不是放宽确定性。
3. **不进 Agent Gateway**：`docs/agent/agent-openapi.json` 里没有 factor-mining 路径，本计划不改变这一点（规格 §15.3 把 MCP 暴露列为「v2 评估」，但那是独立契约面，需要单独计划与对齐测试）。
