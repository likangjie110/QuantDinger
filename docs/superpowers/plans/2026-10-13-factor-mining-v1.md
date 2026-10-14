# 全自动量化因子挖掘（v1）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在 QuantDinger 后端新增因子挖掘模块：给定资产池、周期、区间与预算，一次作业自动完成候选表达式生成 → 逐级评估淘汰 → 与内置因子去重 → 冻结段样本外复评 → 全量试验留痕，并通过 Human API 暴露。

**架构：** 候选统一表示为 JSON 表达式树（DSL），由白名单算子在 pandas 面板（index=日期，columns=标的）上向量化求值；评估复用 `FactorResearchEngine` 的既有口径与 `strategy_evolution.statistics` 的稳健性统计；作业复用 `agent_jobs.submit_job` 在 celery-worker 上跑有限任务；结果落 PostgreSQL 三张新表。不新增进程角色，不改 `_FACTORS` 代码注册表，不接入策略运行时。

**技术栈：** Python 3.11、Flask、pandas、PostgreSQL（psycopg 风格 `get_db_connection`）、Celery（经 `agent_jobs`）、pytest。

**规格：** [`docs/superpowers/specs/2026-10-13-factor-mining.md`](../specs/2026-10-13-factor-mining.md)（执行前两份都要读；本计划的论证依据全部来自该规格）

## 全局约束

- Python 3.11（仓库 `.venv`）；`ruff` line-length 120，规则集 `E9,F63,F7,F82`。
- 测试命令统一：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/<file>.py -q`。
- `pytest.ini` 设 `timeout = 120`：**任何单测不得触网、不得建真 DB 连接**，一律用合成面板与假 provider。
- 源码注释用英文（`AGENTS.md`）；面向用户文案只返回 i18n 键 `factorMining.*`，不硬编码英文句子。
- 不得修改 `_FACTORS`（`app/services/factors/registry.py:157`）的语义，不得把挖掘因子写回源码文件。
- 不得新增进程角色；挖掘作业只能通过 `agent_jobs.submit_job` 提交（`docs/architecture/README.md` 进程所有权）。
- 触碰 HTTP 契约的任务必须重新导出 OpenAPI：`python scripts/export_openapi.py`，并跑 `tests/test_openapi.py`。
- 保留工作区已有未提交改动：只追加、不回滚、不覆盖无关文件（开工前先看一次 `git status`，不要假定它和本计划写作时一致）。
- 每个任务结束前：`python -m py_compile <改动文件>` 通过 + 该任务的测试通过。

## 审查重点（Review Focus）

规格隐含、但最可能伤到使用者的输入/条件，按可能性排序。每行都已在对应任务里安排了钉住它的测试。

1. **前视偏差**：任何算子或求值路径读到未来 bar → 因子值虚高。钉住：任务 2 `test_operators_do_not_leak_future_bars`（改未来行不改变历史输出）+ 任务 7 `test_search_is_isolated_from_freeze_segment`。
2. **幸存者偏差**：用今天的成分股回测历史 → IC/分组全失真。钉住：任务 6 `test_prepare_panel_uses_point_in_time_members`。
3. **字段口径不成立却照跑**：生成了依赖不存在/口径不明数据的候选，或候选被静默丢弃无法复盘。钉住：任务 1 `test_normalize_rejects_unknown_field` + 任务 7 `test_rejected_candidates_still_record_trials`。
4. **选择偏差**：试验次数漏记 → DSR 校正失效、指标虚高。钉住：任务 5 `test_screen_out_of_sample_tightens_as_the_trial_ledger_grows`（同一收益序列，`trials=1` 通过、`trials=250` 以 `selectionBias` 淘汰——分母方向可被否证）+ 任务 9 `test_save_trials_counts_failures`（失败候选同样入账）。
5. **成本与换手失真**：手续费/滑点静默走成 0，或换手爆炸的因子被当成可用。钉住：任务 5 `test_screen_stability_rejects_high_turnover` + 任务 7 `test_metrics_carry_execution_assumptions`。

---

### 任务 1：表达式 DSL —— 校验、规范化与指纹

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/dsl.py`
- 创建：`backend_api_python/app/services/factor_mining/__init__.py`
- 测试：`backend_api_python/tests/test_factor_mining_dsl.py`

**Interfaces：**

```python
DSL_VERSION = 1
MAX_DEPTH = 6
MAX_NODES = 24
WINDOW_MIN = 2
WINDOW_MAX = 252
MAX_LOOKBACK = 504
PRICE_VOLUME_FIELDS = ("open", "high", "low", "close", "volume")
OPERATOR_SPECS: dict[str, tuple[int, bool]]   # op -> (arity, windowed)

class ExpressionError(ValueError):
    def __init__(self, code: str) -> None: ...   # code = "factorMining.reject.invalid:<reason>"

@dataclass(frozen=True)
class NormalizedExpression:
    node: dict
    canonical: str
    fingerprint: str          # sha256(f"{canonical}:v{DSL_VERSION}")
    fields: tuple[str, ...]
    windows: tuple[int, ...]
    lookback: int
    node_count: int
    depth: int

def normalize_expression(node: Any, *, allowed_fields: Collection[str] = PRICE_VOLUME_FIELDS) -> NormalizedExpression
```

`OPERATOR_SPECS` 的算子与 arity（窗口参数统一叫 `n`）：
`lag, delta, ts_mean, ts_sum, ts_std, ts_min, ts_max, ts_rank, ts_zscore, ts_pct_change` → arity 1, windowed；`ts_corr` → arity 2, windowed；`cs_rank, cs_zscore, cs_demean, neg, abs, sign, log, sqrt` → arity 1, 非窗口；`add, sub, mul, div, safe_div, gt, lt, ge, le` → arity 2；`where` → arity 3；`clip` → arity 1 + params `lo, hi`。

- [ ] **步骤 1：编写失败的测试**

```python
import pytest
from app.services.factor_mining.dsl import ExpressionError, normalize_expression

def test_normalize_produces_stable_fingerprint_regardless_of_key_order():
    first = {"op": "ts_mean", "params": {"n": 20}, "args": ["close"]}
    second = {"args": ["close"], "op": "ts_mean", "params": {"n": 20}}
    assert normalize_expression(first).fingerprint == normalize_expression(second).fingerprint

def test_normalize_rejects_unknown_field():
    with pytest.raises(ExpressionError, match="unknownField"):
        normalize_expression({"op": "ts_mean", "args": ["order_book_depth"], "params": {"n": 5}})

def test_normalize_rejects_unknown_operator():
    with pytest.raises(ExpressionError, match="unknownOperator"):
        normalize_expression({"op": "future_return", "args": ["close"], "params": {"n": 5}})

@pytest.mark.parametrize("node,reason", [
    ({"op": "lag", "args": ["close"], "params": {"n": 0}}, "badWindow"),
    ({"op": "lag", "args": ["close"], "params": {"n": -5}}, "badWindow"),
    ({"op": "ts_mean", "args": ["close"], "params": {"n": 300}}, "badWindow"),
    ({"op": "ts_mean", "args": ["close"], "params": {}}, "badWindow"),
    ({"op": "add", "args": ["close"], "params": {}}, "badArity"),
])
def test_normalize_rejects_bad_windows_and_arity(node, reason):
    with pytest.raises(ExpressionError, match=reason):
        normalize_expression(node)

def test_normalize_rejects_depth_and_node_budget():
    deep = "close"
    for _ in range(8):
        deep = {"op": "neg", "args": [deep], "params": {}}
    with pytest.raises(ExpressionError, match="tooDeep"):
        normalize_expression(deep)

def test_normalize_rejects_lookback_over_budget():
    node = {"op": "ts_mean", "args": [
        {"op": "ts_mean", "args": [
            {"op": "ts_mean", "args": ["close"], "params": {"n": 252}}], "params": {"n": 252}}],
        "params": {"n": 252}}
    with pytest.raises(ExpressionError, match="lookbackExceeded"):
        normalize_expression(node)

def test_normalize_reports_fields_windows_and_lookback():
    result = normalize_expression({"op": "add", "args": [
        {"op": "ts_mean", "args": ["close"], "params": {"n": 20}},
        {"op": "ts_mean", "args": ["volume"], "params": {"n": 60}}], "params": {}})
    assert result.fields == ("close", "volume")
    assert result.windows == (20, 60)
    assert result.lookback == 60
    assert result.node_count == 5
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py -q`
预期：collection error，`ModuleNotFoundError: No module named 'app.services.factor_mining'`

- [ ] **步骤 3：实现 `dsl.py`**

递归下行校验：叶子必须是 `allowed_fields` 内的字符串（否则 `unknownField`）；内部节点 `op` 必须在 `OPERATOR_SPECS`（否则 `unknownOperator`），`len(args) == arity`（否则 `badArity`），窗口算子的 `params["n"]` 必须是 `WINDOW_MIN..WINDOW_MAX` 的整数（否则 `badWindow`）；`clip` 的 `lo/hi` 必须是有限数。`depth > MAX_DEPTH` → `tooDeep`；`node_count > MAX_NODES` → `tooManyNodes`；路径窗口和 `> MAX_LOOKBACK` → `lookbackExceeded`。`canonical = json.dumps(node, sort_keys=True, separators=(",", ":"))`，`fingerprint = sha256(f"{canonical}:v{DSL_VERSION}")`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py -q`
预期：PASS（8 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/dsl.py backend_api_python/app/services/factor_mining/__init__.py backend_api_python/tests/test_factor_mining_dsl.py
git commit -m "feat: add factor mining expression DSL with validation and fingerprints"
```

---

### 任务 2：算子库 —— 向量化面板代数与前视防护

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/operators.py`
- 测试：`backend_api_python/tests/test_factor_mining_operators.py`

**Interfaces：**

```python
OPERATORS: dict[str, Callable[[list[Any], Mapping[str, Any]], pd.DataFrame]]

def apply_operator(name: str, args: list[Any], params: Mapping[str, Any]) -> pd.DataFrame
```

约定：所有值与返回值都是 `pd.DataFrame`（index=日期升序 `DatetimeIndex`，columns=标的名）；`gt/lt/ge/le` 返回 bool 面板；`where(cond, a, b)` 取 `b` 处允许标量 `0`（由调用方构造成常量面板）。

- [ ] **步骤 1：编写失败的测试**

```python
import numpy as np
import pandas as pd
import pytest
from app.services.factor_mining.dsl import OPERATOR_SPECS
from app.services.factor_mining.operators import OPERATORS, apply_operator

def _panel(values):
    return pd.DataFrame(values, index=pd.date_range("2025-01-01", periods=len(values), freq="B"), columns=["A", "B"])

def test_operator_registry_matches_dsl_specs():
    assert set(OPERATORS) == set(OPERATOR_SPECS)

def test_ts_mean_uses_full_window_and_emits_nan_until_satisfied():
    panel = _panel([[1.0, 2.0], [2.0, 4.0], [3.0, 6.0]])
    result = apply_operator("ts_mean", [panel], {"n": 2})
    assert np.isnan(result.iloc[0]).all()
    assert result.iloc[1].tolist() == [1.5, 3.0]

def test_lag_shifts_forward_only():
    panel = _panel([[1.0, 2.0], [2.0, 4.0]])
    result = apply_operator("lag", [panel], {"n": 1})
    assert np.isnan(result.iloc[0]).all()
    assert result.iloc[1].tolist() == [1.0, 2.0]

def test_safe_div_returns_nan_on_zero_and_nan_denominator():
    numerator = _panel([[1.0, 1.0], [1.0, 1.0]])
    denominator = _panel([[0.0, 2.0], [np.nan, 4.0]])
    result = apply_operator("safe_div", [numerator, denominator], {})
    assert np.isnan(result.iloc[0, 0]) and np.isnan(result.iloc[1, 0]) and np.isnan(result.iloc[1, 1])
    assert result.iloc[0, 1] == 0.5

def test_cs_rank_is_cross_sectional_per_row():
    panel = _panel([[1.0, 3.0], [5.0, 1.0]])
    result = apply_operator("cs_rank", [panel], {})
    assert result.iloc[0].tolist() == [0.5, 1.0]
    assert result.iloc[1].tolist() == [1.0, 0.5]

def test_operators_do_not_leak_future_bars():
    base = _panel([[1.0, 2.0], [2.0, 3.0], [3.0, 5.0], [4.0, 8.0]])
    mutated = base.copy()
    mutated.iloc[-1] = [100.0, 200.0]
    for name, args, params in [
        ("ts_mean", [base], {"n": 2}),
        ("ts_std", [base], {"n": 2}),
        ("ts_rank", [base], {"n": 3}),
        ("ts_pct_change", [base], {"n": 1}),
    ]:
        before = apply_operator(name, args, params).iloc[:-1]
        after = apply_operator(name, [mutated if arg is base else arg for arg in args], params).iloc[:-1]
        pd.testing.assert_frame_equal(before, after)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_operators.py -q`
预期：FAIL，`ModuleNotFoundError: No module named 'app.services.factor_mining.operators'`

- [ ] **步骤 3：实现 `operators.py`**

按规格 6.3 逐算子实现：时序用 `frame.rolling(n, min_periods=n)`；`ts_std` 用 `ddof=1`；`ts_rank` 用 `rolling(n).apply(lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False)`；`ts_zscore` 分母为 0 时置 NaN；截面用 `frame.rank(axis=1, pct=True)` / `frame.sub(frame.mean(axis=1), axis=0).div(frame.std(axis=1, ddof=0).replace(0, np.nan), axis=0)`（`cs_zscore` 的 std 为 0 时按规格返回 0）；`safe_div` 用 `y.where(y != 0)` 后再除并把 inf 置 NaN；`log`/`sqrt` 先用 `where` 屏蔽非法定义域；`where(cond, a, b)` 用 `a.where(cond, b)`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_operators.py -q`
预期：PASS（6 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/operators.py backend_api_python/tests/test_factor_mining_operators.py
git commit -m "feat: add whitelisted vectorized factor operators with point-in-time safety"
```

---

### 任务 3：表达式求值器与内置因子对齐

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/evaluator.py`
- 测试：`backend_api_python/tests/test_factor_mining_evaluator.py`

**Interfaces：**

```python
class ExpressionEvaluator:
    def __init__(self, fields: Mapping[str, pd.DataFrame]) -> None: ...
    def evaluate(self, node: Mapping[str, Any]) -> pd.DataFrame
    def cache_info(self) -> dict[str, int]      # {"subtrees": n, "hits": n}
```

- [ ] **步骤 1：编写失败的测试**

```python
import numpy as np
import pandas as pd
from app.services.factor_mining.evaluator import ExpressionEvaluator

def _fields():
    index = pd.date_range("2025-01-01", periods=60, freq="B")
    close = pd.DataFrame({symbol: [100 + offset * 5 + day for day in range(60)]
                          for offset, symbol in enumerate(["A", "B", "C"])}, index=index)
    volume = pd.DataFrame({symbol: [1000 + day for day in range(60)] for symbol in ["A", "B", "C"]}, index=index)
    return {"close": close, "volume": volume}

def test_evaluator_matches_pandas_reference_for_momentum_expression():
    fields = _fields()
    node = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}
    result = ExpressionEvaluator(fields).evaluate(node)
    expected = fields["close"].pct_change(20, fill_method=None)
    pd.testing.assert_frame_equal(result, expected)

def test_evaluator_reuses_shared_subtrees():
    fields = _fields()
    shared = {"op": "ts_mean", "args": ["volume"], "params": {"n": 5}}
    node = {"op": "div", "args": [
        {"op": "cs_rank", "args": [shared]},
        {"op": "cs_rank", "args": [ {"op": "add", "args": [shared, shared], "params": {}} ]}], "params": {}}
    evaluator = ExpressionEvaluator(fields)
    result = evaluator.evaluate(node)
    assert result.shape == fields["close"].shape
    assert evaluator.cache_info()["hits"] >= 1

def test_evaluator_returns_nan_frame_for_missing_forward_data():
    fields = _fields()
    node = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}
    result = ExpressionEvaluator(fields).evaluate(node)
    assert result.iloc[:20].isna().all().all()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_evaluator.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：实现 `evaluator.py`**

按子树的 canonical JSON 做记忆化（同一子树只算一次，命中计数进 `cache_info`）；叶子取 `self.fields[name]`，字段缺失或缺列时返回全 NaN 面板并保持 index/columns 对齐到 `close` 面板；调用 `apply_operator` 求值。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_evaluator.py -q`
预期：PASS（3 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/evaluator.py backend_api_python/tests/test_factor_mining_evaluator.py
git commit -m "feat: add memoized expression evaluator over price-volume panels"
```

---

### 任务 4：模板库与预算内确定性候选展开

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/templates.py`
- 测试：`backend_api_python/tests/test_factor_mining_templates.py`

**Interfaces：**

```python
MAX_BUDGET = 2000
DEFAULT_BUDGET = 400

@dataclass(frozen=True)
class FactorTemplate:
    template_id: str
    hypothesis_i18n_key: str
    category: str
    parameter_grid: Mapping[str, tuple[int, ...]]
    build: Callable[[Mapping[str, int]], dict]

TEMPLATES: dict[str, FactorTemplate]

@dataclass(frozen=True)
class CandidateSeed:
    expression: dict
    fingerprint: str
    template_id: str
    origin: str          # "template"
    params: dict

def expand_candidates(*, template_ids: Sequence[str] | None = None, budget: int = DEFAULT_BUDGET,
                      seed: int = 42, smoothing: bool = True) -> list[CandidateSeed]
def template_catalog() -> list[dict]
```

- [ ] **步骤 1：编写失败的测试**

```python
import pytest
from app.services.factor_mining.dsl import normalize_expression
from app.services.factor_mining.templates import MAX_BUDGET, TEMPLATES, expand_candidates, template_catalog

def test_catalog_exposes_hypothesis_keys_and_grids():
    catalog = {item["templateId"]: item for item in template_catalog()}
    assert set(catalog) >= {"momentum", "reversal", "low_volatility", "liquidity",
                            "volume_price_confirm", "risk_adjusted_momentum", "conditional_filter"}
    assert catalog["momentum"]["hypothesisI18nKey"] == "factorMining.template.momentum"

def test_every_expanded_candidate_passes_dsl_validation():
    for candidate in expand_candidates(budget=MAX_BUDGET):
        normalized = normalize_expression(candidate.expression)
        assert normalized.fingerprint == candidate.fingerprint
        assert normalized.lookback <= 504

def test_expansion_is_deterministic_for_same_seed():
    first = [item.fingerprint for item in expand_candidates(budget=120, seed=7)]
    second = [item.fingerprint for item in expand_candidates(budget=120, seed=7)]
    assert first == second

def test_budget_limits_candidate_count_and_dedupes():
    candidates = expand_candidates(budget=25)
    assert len(candidates) <= 25
    assert len({item.fingerprint for item in candidates}) == len(candidates)

def test_expansion_can_be_restricted_to_one_template():
    candidates = expand_candidates(template_ids=["reversal"], budget=MAX_BUDGET)
    assert {item.template_id for item in candidates} == {"reversal"}

def test_budget_above_hard_limit_is_rejected():
    with pytest.raises(ValueError, match="factorMining.budgetExceeded"):
        expand_candidates(budget=MAX_BUDGET + 1)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_templates.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：实现 `templates.py`**

按规格第 7 节的七张模板与参数网格实现 `build`；`expand_candidates` 做笛卡尔展开，`smoothing=True` 时追加 `ts_mean(expr, 3)` 与 `ts_mean(expr, 5)` 变体；组合数超过 `budget` 时复用 `app/services/strategy_evolution/search.py:13` 的 `ParameterSampler` reservoir 语义截断（保序、确定性）；先按 `normalize_expression(...).fingerprint` 去重再截断；`budget` 超 `MAX_BUDGET` 抛 `ValueError("factorMining.budgetExceeded")`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_templates.py -q`
预期：PASS（6 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/templates.py backend_api_python/tests/test_factor_mining_templates.py
git commit -m "feat: add hypothesis template library with budgeted deterministic expansion"
```

---

### 任务 5：评估阶梯门槛（L2–L5）

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/gates.py`
- 测试：`backend_api_python/tests/test_factor_mining_gates.py`

**Interfaces：**

```python
@dataclass(frozen=True)
class GateThresholds:
    min_coverage: float = 0.60
    min_ic_observations: int = 30
    min_abs_rank_ic: float = 0.02
    min_icir: float = 0.30
    min_fold_consistency: float = 0.60
    max_turnover: float = 0.70
    max_builtin_correlation: float = 0.85
    min_oos_abs_rank_ic: float = 0.01
    min_deflated_sharpe_probability: float = 0.95
    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "GateThresholds": ...

@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    stage: str        # "L2" | "L3" | "L4" | "L5"
    reason: str       # "" when passed, else "factorMining.reject.<name>"

def screen_metrics(metrics: Mapping[str, Any], thresholds: GateThresholds) -> GateVerdict
def screen_stability(metrics: Mapping[str, Any], thresholds: GateThresholds) -> GateVerdict
def screen_redundancy(correlations: Mapping[str, float], thresholds: GateThresholds) -> GateVerdict
def net_return_series(long_short_curve: Sequence[Mapping[str, Any]]) -> list[float]
def screen_out_of_sample(*, is_metrics: Mapping[str, Any], oos_metrics: Mapping[str, Any],
                         returns: Sequence[float], trials: int,
                         thresholds: GateThresholds) -> GateVerdict
```

- [ ] **步骤 1：编写失败的测试**

```python
from app.services.factor_mining.gates import (GateThresholds, net_return_series, screen_metrics,
                                              screen_out_of_sample, screen_redundancy, screen_stability)

def _metrics(**overrides):
    base = {"coverage": 0.9, "rankIc": 0.05, "icir": 1.2, "averageTurnover": 0.3,
            "icSeries": [{"time": f"2025-01-{day:02d}", "value": 0.05} for day in range(1, 31)],
            "stability": {"firstHalfIc": 0.04, "secondHalfIc": 0.06}}
    base.update(overrides)
    return base

def test_screen_metrics_rejects_low_coverage():
    verdict = screen_metrics(_metrics(coverage=0.4), GateThresholds())
    assert verdict.passed is False and verdict.reason == "factorMining.reject.lowCoverage"

def test_screen_metrics_rejects_too_few_observations():
    verdict = screen_metrics(_metrics(icSeries=[{"time": "2025-01-01", "value": 0.05}]), GateThresholds())
    assert verdict.reason == "factorMining.reject.insufficientObservations"

def test_screen_metrics_rejects_weak_ic():
    assert screen_metrics(_metrics(rankIc=0.005), GateThresholds()).reason == "factorMining.reject.weakIc"

def test_screen_metrics_passes_strong_candidate():
    assert screen_metrics(_metrics(), GateThresholds()).passed is True

def test_screen_stability_rejects_high_turnover():
    verdict = screen_stability(_metrics(averageTurnover=0.95), GateThresholds())
    assert verdict.reason == "factorMining.reject.highTurnover"

def test_screen_stability_rejects_inconsistent_halves():
    verdict = screen_stability(_metrics(stability={"firstHalfIc": 0.06, "secondHalfIc": -0.04}), GateThresholds())
    assert verdict.reason == "factorMining.reject.unstableHalves"

def test_screen_redundancy_rejects_builtin_duplicate():
    verdict = screen_redundancy({"momentum_20": 0.92, "reversal_5": 0.1}, GateThresholds())
    assert verdict.reason == "factorMining.reject.duplicateBuiltin"

def test_screen_out_of_sample_uses_total_trial_count():
    curve = [{"time": "2025-01-01", "gross": 1.0, "net": 1.0}, {"time": "2025-01-02", "gross": 1.1, "net": 1.09}]
    returns = net_return_series(curve)
    assert returns == [0.09]
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05),
                                   oos_metrics=_metrics(rankIc=0.04),
                                   returns=[0.01, -0.002, 0.004, 0.006], trials=250,
                                   thresholds=GateThresholds())
    assert verdict.stage == "L5"
    assert verdict.reason in {"", "factorMining.reject.selectionBias"}

def test_screen_out_of_sample_rejects_sign_flip():
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=-0.04),
                                   returns=[0.01, -0.002, 0.004, 0.006], trials=10, thresholds=GateThresholds())
    assert verdict.reason == "factorMining.reject.weakOutOfSample"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_gates.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：实现 `gates.py`**

纯函数，不读 DB、不读时钟。`screen_metrics` 按 coverage → 观测数 → `abs(rankIc)` 顺序短路，返回首个未过项。`screen_stability` 按 ICIR → 折一致性 → 前后半同号 → 换手顺序短路；折一致性读 `metrics["foldIcSigns"]`（序列，任务 7 写入），**该键缺失或长度 < 2 时跳过这一项（视为未评估）而不是判负** —— 搜索路径由任务 7 保证总是写入，跳过只是为了让门槛函数在缺数据的输入上不产生假淘汰。`screen_redundancy` 取 `abs` 最大值与阈值比较。`screen_out_of_sample` 先比较 IS/OOS 符号与 `abs(rankIc)`，再用 `app/services/strategy_evolution/statistics.py` 的 `sample_sharpe` + `deflated_sharpe(returns, observed_sharpe=..., trials=trials)`，`probability < 阈值` 或 `available is False` → `selectionBias`。`net_return_series` 由 `net` 列逐点相除减 1。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_gates.py -q`
预期：PASS（9 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/gates.py backend_api_python/tests/test_factor_mining_gates.py
git commit -m "feat: add graded factor screening gates with deflated-sharpe selection-bias control"
```

---

### 任务 6：数据准备与 IS/冻结切分（时点口径）

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/engine.py`
- 测试：`backend_api_python/tests/test_factor_mining_data.py`

**Interfaces：**

```python
@dataclass(frozen=True)
class MiningConfig:
    market: str
    timeframe: str
    start_date: datetime
    end_date: datetime
    source_id: int
    source_name: str = ""
    freeze_ratio: float = 0.3
    budget: int = 400
    seed: int = 42
    template_ids: tuple[str, ...] = ()
    groups: int = 5
    holding_period: int = 5
    commission: float = 0.0005
    slippage: float = 0.0005
    neutralize_industry: bool = False
    thresholds: GateThresholds = GateThresholds()
    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, default_commission: float, default_slippage: float) -> "MiningConfig": ...

@dataclass(frozen=True)
class PeriodSplit:
    search_start: datetime
    search_end: datetime
    freeze_start: datetime
    freeze_end: datetime

def split_periods(config: MiningConfig) -> PeriodSplit

@dataclass(frozen=True)
class PreparedPanel:
    frames: Mapping[str, pd.DataFrame]
    members: list[dict]
    fields: Mapping[str, pd.DataFrame]
    snapshot_id: str
    frequency: str
    warnings: list[str]

class MiningDataProvider:
    def __init__(self, *, backtest_service: StrategyV2BacktestService | None = None,
                 snapshot_store: MarketDataSnapshotStore | None = None) -> None: ...
    def prepare(self, *, user_id: int, code: str, config: MiningConfig) -> PreparedPanel
```

- [ ] **步骤 1：编写失败的测试**

```python
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.factor_mining.engine import MiningConfig, MiningDataProvider, split_periods


class _FakeBacktestService:
    def __init__(self):
        self.resolve_calls = []
        self.fetch_calls = []

    def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
        self.resolve_calls.append((user_id, start_date, end_date))
        return ([{"market": "USStock", "symbol": "AAPL", "effectiveFrom": None},
                 {"market": "USStock", "symbol": "MSFT", "effectiveFrom": None},
                 {"market": "USStock", "symbol": "NVDA", "effectiveFrom": None}], 42)

    def fetch_frames(self, candidates, frequency, start_date, end_date):
        self.fetch_calls.append((frequency, start_date, end_date))
        index = pd.date_range(start_date, end_date, freq="B")
        frames = {f"USStock:{item['symbol']}": pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0}, index=index)
            for item in candidates}
        return frames, []


class _FakeSnapshotStore:
    def save(self, frame):
        return {"snapshotId": "a" * 64, "snapshotFormat": "test"}


def _config(**overrides):
    base = dict(market="USStock", timeframe="1d",
                start_date=datetime(2024, 1, 1), end_date=datetime(2024, 12, 31),
                source_id=7)
    base.update(overrides)
    return MiningConfig(**base)


def test_split_periods_keeps_freeze_tail_disjoint_from_search():
    split = split_periods(_config(freeze_ratio=0.25))
    assert split.search_start == datetime(2024, 1, 1)
    assert split.search_end < split.freeze_start
    assert split.freeze_end == datetime(2024, 12, 31)


def test_prepare_panel_uses_point_in_time_members():
    service = _FakeBacktestService()
    provider = MiningDataProvider(backtest_service=service, snapshot_store=_FakeSnapshotStore())
    panel = provider.prepare(user_id=5, code="PORTFOLIO = {}", config=_config())
    assert service.resolve_calls[0][0] == 5
    assert [item["symbol"] for item in panel.members] == ["AAPL", "MSFT", "NVDA"]
    assert set(panel.fields) >= {"open", "high", "low", "close", "volume"}
    assert panel.snapshot_id == "a" * 64


def test_prepare_panel_fetches_only_the_search_window():
    service = _FakeBacktestService()
    provider = MiningDataProvider(backtest_service=service, snapshot_store=_FakeSnapshotStore())
    config = _config(freeze_ratio=0.25)
    provider.prepare(user_id=1, code="PORTFOLIO = {}", config=config)
    split = split_periods(config)
    fetch_frequency, fetch_start, fetch_end = service.fetch_calls[0]
    assert fetch_start <= split.search_start
    assert fetch_end < split.freeze_end


def test_prepare_panel_rejects_universe_smaller_than_three():
    class _TinyService(_FakeBacktestService):
        def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
            return ([{"market": "USStock", "symbol": "AAPL", "effectiveFrom": None}], None)

    provider = MiningDataProvider(backtest_service=_TinyService(), snapshot_store=_FakeSnapshotStore())
    with pytest.raises(ValueError, match="factorMining.universeTooSmall"):
        provider.prepare(user_id=1, code="PORTFOLIO = {}", config=_config())
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_data.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：实现 `engine.py` 的数据准备部分**

`MiningConfig.from_payload` 用 `app/services/backtest_limits` 之外的既有做法：调用方传入 `default_commission_if_missing(None)` / `default_slippage_if_missing(None)` 的解析结果（与 `app/routes/backtest_center.py:238` 一致）；`timeframe` 归一化用 `app/data_sources` 的 `normalize_chart_timeframe`；预算超 `MAX_BUDGET` 抛 `factorMining.budgetExceeded`；`freeze_ratio` 必须 `0 < r < 0.5`。

`split_periods` 用交易日比例切分：`search_end` = 区间内第 `(1 - freeze_ratio)` 分位日期，`freeze_start` = `search_end + 1 天`。

`MiningDataProvider.prepare`：编译传入的 Strategy V2 源码取 manifest（`compile_strategy_v2`），调 `backtest_service.resolve_candidates(user_id, manifest, config.start_date, config.end_date)`，成员数 `< 3` 抛 `factorMining.universeTooSmall`；调 `fetch_frames(candidates, config.timeframe, fetch_start, split.search_end)`（**只取搜索段**，带 warmup 回看）；把每个 symbol 的 frame 按字段拼成面板字典；对合并后的面板调 `snapshot_store.save(...)` 取 `snapshotId` 作为数据版本；`fetch_frames` 返回的 skipped 记进 `warnings`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_data.py -q`
预期：PASS（4 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/engine.py backend_api_python/tests/test_factor_mining_data.py
git commit -m "feat: add factor mining data preparation with point-in-time universe and search/freeze split"
```

---

### 任务 7：搜索循环、逐级淘汰与试验留痕

**文件：**
- 修改：`backend_api_python/app/services/strategy_v2/factor_research.py`（`run(...)` 增加可选 `factor_panel` 入参，约 `:36-60`）
- 修改：`backend_api_python/app/services/factor_mining/engine.py`
- 测试：`backend_api_python/tests/test_factor_mining_engine.py`

**Interfaces：**

```python
class FactorMiningEngine:
    def __init__(self, *, data_provider: MiningDataProvider, on_progress: Callable[[dict], None] | None = None) -> None: ...
    def run(self, *, user_id: int, code: str, config: MiningConfig) -> dict
    # 返回 {"config": {...}, "split": {...}, "trials": [...], "candidates": [...], "summary": {...}}
```

`FactorResearchEngine.run(...)` 新增关键字参数 `factor_panel: pd.DataFrame | None = None`：非空时跳过 `self._factor_panel(...)` 直接使用它，其余流程与返回结构不变。

- [ ] **步骤 1：编写失败的测试**

```python
import numpy as np
import pandas as pd

from app.services.factor_mining.engine import FactorMiningEngine, MiningConfig
from app.services.factor_mining.gates import GateThresholds


class _FakeProvider:
    def __init__(self, frames, members):
        self.panel = type("Panel", (), {
            "frames": frames, "members": members, "fields": {},
            "snapshot_id": "b" * 64, "frequency": "1d", "warnings": [],
        })()

    def prepare(self, *, user_id, code, config):
        return self.panel


def _frames_and_members(periods=200):
    index = pd.date_range("2024-01-01", periods=periods, freq="B")
    frames, members = {}, []
    for offset, symbol in enumerate(["A", "B", "C", "D", "E", "F"]):
        drift = 0.002 * (offset + 1)
        close = [100 * (1 + drift) ** day for day in range(periods)]
        frames[f"USStock:{symbol}"] = pd.DataFrame(
            {"open": close, "high": [value * 1.001 for value in close],
             "low": [value * 0.999 for value in close], "close": close,
             "volume": [1000.0 + day * (offset + 1) for day in range(periods)]}, index=index)
        members.append({"market": "USStock", "symbol": symbol, "effectiveFrom": None})
    return frames, members


def _config(**overrides):
    from datetime import datetime
    base = dict(market="USStock", timeframe="1d",
                start_date=datetime(2024, 1, 1), end_date=datetime(2024, 10, 31), source_id=1,
                budget=12, seed=3,
                thresholds=GateThresholds(min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
                                          min_fold_consistency=0.0, max_turnover=1.0,
                                          min_coverage=0.0, min_oos_abs_rank_ic=0.0,
                                          min_deflated_sharpe_probability=0.0))
    base.update(overrides)
    return MiningConfig(**base)


def test_search_records_one_trial_per_evaluated_candidate():
    frames, members = _frames_and_members()
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members))
    result = engine.run(user_id=1, code="PORTFOLIO = {}", config=_config())
    assert len(result["trials"]) == len({item["fingerprint"] for item in result["trials"]})
    assert len(result["trials"]) >= 1
    assert all(item["stage"] in {"L1", "L2", "L3", "L4", "L5"} for item in result["trials"])


def test_rejected_candidates_still_record_trials():
    frames, members = _frames_and_members()
    strict = _config(thresholds=GateThresholds(min_abs_rank_ic=0.99))
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members))
    result = engine.run(user_id=1, code="PORTFOLIO = {}", config=strict)
    assert result["candidates"] == []
    assert result["trials"] and all(item["verdict"] == "reject" for item in result["trials"])
    assert result["trials"][0]["reason"].startswith("factorMining.reject.")


def test_search_is_isolated_from_freeze_segment():
    frames, members = _frames_and_members()
    mutated = {name: frame.copy() for name, frame in frames.items()}
    for frame in mutated.values():
        frame.iloc[-40:] = frame.iloc[-40:] * 3.0
    first = FactorMiningEngine(data_provider=_FakeProvider(frames, members)).run(
        user_id=1, code="PORTFOLIO = {}", config=_config())
    second = FactorMiningEngine(data_provider=_FakeProvider(mutated, members)).run(
        user_id=1, code="PORTFOLIO = {}", config=_config())
    assert [item["fingerprint"] for item in first["candidates"]] == \
           [item["fingerprint"] for item in second["candidates"]]
    assert [item["metrics"]["rankIc"] for item in first["candidates"]] == \
           [item["metrics"]["rankIc"] for item in second["candidates"]]


def test_metrics_carry_execution_assumptions():
    frames, members = _frames_and_members()
    result = FactorMiningEngine(data_provider=_FakeProvider(frames, members)).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(commission=0.001, slippage=0.002))
    assert result["summary"]["executionAssumptions"]["commission"] == 0.001
    assert result["summary"]["executionAssumptions"]["slippage"] == 0.002


def test_run_is_reproducible_with_same_seed():
    frames, members = _frames_and_members()
    first = FactorMiningEngine(data_provider=_FakeProvider(frames, members)).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(seed=11))
    second = FactorMiningEngine(data_provider=_FakeProvider(frames, members)).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(seed=11))
    assert [item["fingerprint"] for item in first["trials"]] == [item["fingerprint"] for item in second["trials"]]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_engine.py -q`
预期：FAIL，`FactorMiningEngine` 不存在

- [ ] **步骤 3：给 `FactorResearchEngine.run` 增加 `factor_panel` 入参**

在 `factor_research.py` 的 `run(...)` 签名末尾加关键字参数 `factor_panel: pd.DataFrame | None = None`，把 `factor = self._factor_panel(...)` 改为 `factor = factor_panel if factor_panel is not None else self._factor_panel(prepared, factor_name, close)`；`factor_name` 允许为空串（表达式候选没有内置 id）。不改其他分支。

- [ ] **步骤 4：运行既有因子研究测试确认无回归**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_research.py tests/test_strategy_v2_factor_api.py -q`
预期：PASS（原有 17 项全绿）

- [ ] **步骤 5：实现 `FactorMiningEngine.run` 的搜索循环**

流程：`data_provider.prepare` → 构 `ExpressionEvaluator(panel.fields)` → `expand_candidates(template_ids, budget, seed)` → 逐候选：L1 `normalize_expression`（异常 → trial `reject/factorMining.reject.invalid`，继续下一个）→ 用求值器算出候选面板 → 调 `FactorResearchEngine().run(frames=panel.frames, factor_panel=candidate_panel, factor_id="", start_date=split.search_start, end_date=split.search_end, groups, holding_period, commission, slippage, neutralize_industry, members=panel.members)` → L2/L3 门槛（写入 `foldIcSigns`：把 `icSeries` 等分 4 折，各折 IC 均值符号）→ L4 用 `result["factorCorrelation"]["correlations"]` 过 `screen_redundancy` → 通过者进候选列表；每次评估都追加一条 trial。`on_progress` 每 10 个候选发一次 `{"stage": "search", "evaluated": n, "total": total}`。汇总写 `executionAssumptions`（含传入的 commission/slippage）。

- [ ] **步骤 6：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_engine.py tests/test_factor_research.py -q`
预期：PASS（5 + 8 项）

- [ ] **步骤 7：Commit**

```bash
git add backend_api_python/app/services/strategy_v2/factor_research.py backend_api_python/app/services/factor_mining/engine.py backend_api_python/tests/test_factor_mining_engine.py
git commit -m "feat: add factor mining search loop with graded screening and trial ledger"
```

---

### 任务 8：冻结段样本外复评（L5）

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/engine.py`
- 测试：`backend_api_python/tests/test_factor_mining_freeze.py`

**Interfaces：**

```python
class FactorMiningEngine:
    def review_frozen(self, *, user_id: int, code: str, config: MiningConfig,
                      survivors: Sequence[Mapping[str, Any]], trials_count: int) -> list[dict]
    # 返回 [{...,"status": "frozen"|"rejected", "outOfSample": {...}, "reason": "..."}]
```

冻结段取数走同一个 `MiningDataProvider.prepare` 之外的独立方法 `prepare_freeze(*, user_id, code, config) -> PreparedPanel`（同一 provider，区间为冻结段 + warmup）。

- [ ] **步骤 1：编写失败的测试**

```python
import pandas as pd

from app.services.factor_mining.engine import FactorMiningEngine, MiningConfig
from app.services.factor_mining.gates import GateThresholds
from tests.test_factor_mining_engine import _FakeProvider, _config, _frames_and_members


def test_freeze_review_marks_survivors_frozen_or_rejected():
    frames, members = _frames_and_members()
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members))
    config = _config(thresholds=GateThresholds(min_ic_observations=3, min_abs_rank_ic=0.0, min_icir=0.0,
                                               min_fold_consistency=0.0, max_turnover=1.0, min_coverage=0.0,
                                               min_oos_abs_rank_ic=0.0, min_deflated_sharpe_probability=0.0))
    search = engine.run(user_id=1, code="PORTFOLIO = {}", config=config)
    reviewed = engine.review_frozen(user_id=1, code="PORTFOLIO = {}",
                                    config=config, survivors=search["candidates"], trials_count=len(search["trials"]))
    assert reviewed
    assert all(item["status"] in {"frozen", "rejected"} for item in reviewed)
    assert all("outOfSample" in item for item in reviewed)


def test_freeze_review_rejects_when_out_of_sample_sign_flips():
    frames, members = _frames_and_members()
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members))
    strict_oos = _config(thresholds=GateThresholds(min_abs_rank_ic=0.0, min_icir=0.0, min_fold_consistency=0.0,
                                                   max_turnover=1.0, min_coverage=0.0, min_ic_observations=3,
                                                   min_oos_abs_rank_ic=0.99,
                                                   min_deflated_sharpe_probability=0.0))
    search = engine.run(user_id=1, code="PORTFOLIO = {}", config=strict_oos)
    reviewed = engine.review_frozen(user_id=1, code="PORTFOLIO = {}", config=strict_oos,
                                    survivors=search["candidates"], trials_count=len(search["trials"]))
    assert all(item["status"] == "rejected" for item in reviewed)
    assert all(item["reason"] == "factorMining.reject.weakOutOfSample" for item in reviewed)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_freeze.py -q`
预期：FAIL，`review_frozen` 不存在

- [ ] **步骤 3：实现 `prepare_freeze` 与 `review_frozen`**

`prepare_freeze` 与 `prepare` 同路径，只把取数区间换成 `[freeze_start - warmup, freeze_end]`，并断言冻结段与搜索段无重叠（重叠则 `ValueError("factorMining.searchFreezeOverlap")`）。`review_frozen` 对每个 survivor 用 `factor_panel` 在冻结段跑 `FactorResearchEngine`，再用 `screen_out_of_sample(is_metrics=搜索段指标, oos_metrics=冻结段指标, returns=net_return_series(冻结段 longShortCurve), trials=trials_count, thresholds=config.thresholds)` 判决；`trials_count` 必须是该 run 的**全部** trial 数。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_freeze.py -q`
预期：PASS（2 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/engine.py backend_api_python/tests/test_factor_mining_freeze.py
git commit -m "feat: add frozen-segment out-of-sample review with selection-bias correction"
```

---

### 任务 9：PostgreSQL 持久化（runs / candidates / trials）

**文件：**
- 创建：`backend_api_python/migrations/20261013_factor_mining.sql`
- 修改：`backend_api_python/migrations/init.sql`（追加同样三段 DDL + 索引）
- 创建：`backend_api_python/app/services/factor_mining/storage.py`
- 测试：`backend_api_python/tests/test_factor_mining_storage.py`

**Interfaces：**

```python
class FactorMiningRepository:
    def persist_run(self, *, user_id: int, config: Mapping[str, Any], source_id: int, source_name: str,
                    snapshot_id: str, manifest: Mapping[str, Any], status: str = "running") -> int: ...
    def finish_run(self, run_id: int, *, status: str, result: Mapping[str, Any],
                   error_message: str = "") -> None: ...
    def save_trials(self, run_id: int, *, user_id: int, trials: Sequence[Mapping[str, Any]]) -> int: ...
    def upsert_candidates(self, *, user_id: int, run_id: int, candidates: Sequence[Mapping[str, Any]]) -> int: ...
    def count_trials(self, run_id: int) -> int: ...
    def list_runs(self, *, user_id: int, limit: int = 20) -> list[dict]: ...
    def get_run(self, run_id: int, *, user_id: int) -> dict | None: ...
    def list_candidates(self, *, user_id: int, run_id: int | None = None, status: str = "",
                        market: str = "", limit: int = 50, offset: int = 0) -> list[dict]: ...
    def get_candidate(self, candidate_id: int, *, user_id: int) -> dict | None: ...
    @staticmethod
    def _hydrate_run(row: Mapping[str, Any], *, include_result: bool = False) -> dict: ...
    @staticmethod
    def _hydrate_candidate(row: Mapping[str, Any], *, include_metrics: bool = True) -> dict: ...
```

表结构照规格第 11 节，字段名与 `qd_factor_research_runs` 风格一致（`*_json TEXT`、`created_at TIMESTAMP DEFAULT NOW()`）。

- [ ] **步骤 1：编写失败的测试**

```python
import json

from app.services.factor_mining.storage import FactorMiningRepository


def test_hydrate_candidate_parses_expression_and_metrics():
    row = {
        "id": 3,
        "expression_json": json.dumps({"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}),
        "expression_hash": "c" * 64,
        "origin": "template",
        "template_id": "momentum",
        "status": "frozen",
        "reject_reason": "",
        "market": "USStock",
        "timeframe": "1d",
        "metrics_json": json.dumps({"rankIc": 0.05, "icir": 1.1}),
        "out_of_sample_json": json.dumps({"rankIc": 0.03}),
    }

    summary = FactorMiningRepository._hydrate_candidate(dict(row), include_metrics=False)
    detail = FactorMiningRepository._hydrate_candidate(dict(row), include_metrics=True)

    assert summary["expressionHash"] == "c" * 64
    assert "metrics" not in summary
    assert detail["expression"]["op"] == "ts_pct_change"
    assert detail["metrics"]["rankIc"] == 0.05
    assert detail["outOfSample"]["rankIc"] == 0.03


def test_hydrate_candidate_tolerates_broken_json():
    row = {"id": 4, "expression_json": "{not json", "expression_hash": "d" * 64,
           "origin": "template", "template_id": "", "status": "rejected",
           "reject_reason": "factorMining.reject.weakIc", "metrics_json": "[]",
           "out_of_sample_json": "null", "market": "", "timeframe": ""}

    detail = FactorMiningRepository._hydrate_candidate(dict(row))

    assert detail["expression"] is None
    assert detail["metrics"] == {}
    assert detail["outOfSample"] == {}


def test_hydrate_run_summary_omits_result_payload():
    row = {"id": 9, "config_json": json.dumps({"market": "USStock"}), "status": "success",
           "trials_count": 250, "candidates_count": 12, "frozen_count": 3,
           "manifest_json": json.dumps({"snapshotId": "e" * 64}),
           "result_json": json.dumps({"summary": {"trials": 250}})}

    summary = FactorMiningRepository._hydrate_run(dict(row), include_result=False)
    detail = FactorMiningRepository._hydrate_run(dict(row), include_result=True)

    assert summary["trialsCount"] == 250
    assert "result" not in summary
    assert detail["result"]["summary"]["trials"] == 250
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_storage.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：写迁移并实现 `storage.py`**

迁移三张表 + 索引，全部 `IF NOT EXISTS`；`qd_factor_trials.run_id` 对 runs 做 `ON DELETE CASCADE`；`qd_factor_candidates` 加 `UNIQUE (user_id, expression_hash)`。仓储按 `app/services/strategy_v2/storage.py:640-760` 的既有写法（`get_db_connection`、`cur.execute`、显式 commit、`_hydrate*` 静态方法）；`upsert_candidates` 用 `ON CONFLICT (user_id, expression_hash) DO UPDATE` 更新状态与指标；`save_trials` 用 `executemany` 并返回写入行数。

- [ ] **步骤 4：跑存储测试与 DB 引导测试**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_storage.py tests/test_db_bootstrap.py -q`
预期：PASS（3 + 原有项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/migrations/20261013_factor_mining.sql backend_api_python/migrations/init.sql backend_api_python/app/services/factor_mining/storage.py backend_api_python/tests/test_factor_mining_storage.py
git commit -m "feat: persist factor mining runs, candidates, and trials"
```

---

### 任务 10：应用服务与作业编排

**文件：**
- 创建：`backend_api_python/app/services/factor_mining/service.py`
- 测试：`backend_api_python/tests/test_factor_mining_service.py`

**Interfaces：**

```python
class FactorMiningService:
    def __init__(self, *, engine_factory: Callable[..., FactorMiningEngine] | None = None,
                 repository: FactorMiningRepository | None = None,
                 backtest_service: StrategyV2BacktestService | None = None) -> None: ...
    def estimate(self, payload: Mapping[str, Any]) -> dict
    def submit(self, *, user_id: int, payload: Mapping[str, Any]) -> dict
    def get_job(self, job_id: str, *, user_id: int) -> dict | None
    def list_jobs(self, *, user_id: int, limit: int = 20) -> list[dict]
    def cancel_job(self, job_id: str, *, user_id: int) -> dict | None
```

`submit` 走 `app/utils/agent_jobs.py:81` 的 `submit_job(user_id=..., agent_token_id=None, kind="factor_mining", request_payload={**payload, "__userId": user_id}, runner=_run_mining_job)`。

- [ ] **步骤 1：编写失败的测试**

```python
from datetime import datetime

import pytest

from app.services.factor_mining import service as mining_service
from app.services.factor_mining.service import FactorMiningService


def _payload(**overrides):
    base = {"sourceId": 3, "market": "USStock", "timeframe": "1d",
            "startDate": "2024-01-01", "endDate": "2024-10-31", "budget": 20}
    base.update(overrides)
    return base


def test_submit_uses_factor_mining_job_kind(monkeypatch):
    captured = {}

    def _fake_submit_job(**kwargs):
        captured.update(kwargs)
        return {"job_id": "job-1", "status": "queued"}

    monkeypatch.setattr(mining_service.agent_jobs, "submit_job", _fake_submit_job)
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 0)

    receipt = FactorMiningService().submit(user_id=4, payload=_payload())

    assert captured["kind"] == "factor_mining"
    assert captured["user_id"] == 4
    assert captured["request_payload"]["__userId"] == 4
    assert receipt["jobId"] == "job-1"


def test_submit_rejects_second_concurrent_job(monkeypatch):
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 1)
    with pytest.raises(ValueError, match="factorMining.concurrentLimit"):
        FactorMiningService().submit(user_id=4, payload=_payload())


def test_submit_requires_source_and_dates():
    service = FactorMiningService()
    with pytest.raises(ValueError, match="factorMining.sourceRequired"):
        service.estimate(_payload(sourceId=0))
    with pytest.raises(ValueError, match="factorMining.dateRangeRequired"):
        service.estimate(_payload(endDate=""))


def test_estimate_reports_candidate_count_within_budget():
    estimate = FactorMiningService().estimate(_payload(budget=20))
    assert estimate["candidateBudget"] == 20
    assert estimate["templateCount"] >= 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_service.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：实现 `service.py`**

`_run_mining_job(payload, on_progress)`：弹出 `__userId`，构建 `MiningConfig`，`MiningDataProvider.prepare(user_id, code, config)` 取源码（`get_script_source_service().get_source(source_id, user_id=...)`，与 `strategy_evolution/service.py:29` 相同做法），`FactorMiningRepository.persist_run` → `engine.run` → `save_trials` → `upsert_candidates` → `review_frozen` → 回写候选状态与 `finish_run(status="success")`；异常时 `finish_run(status="failed", error_message=...)` 后再抛，让 `agent_jobs` 记失败。`estimate` 只做参数校验与静态估算（模板数 × 网格规模，截断到 budget），不取数。

`agent_jobs.submit_job` 返回的是快照键（`job_id` / `status` / `kind` / `created_at`，见 `app/utils/agent_jobs.py:215`），服务层统一转成对外驼峰形状（`jobId` / `status` / `progress` / `result` / `error` / `createdAt` / `startedAt` / `finishedAt`），与 `app/routes/strategy_evolution.py:135` 的 `_public_job` 一致；`get_job` / `list_jobs` 复用同一转换。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_service.py -q`
预期：PASS（4 项）

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/service.py backend_api_python/tests/test_factor_mining_service.py
git commit -m "feat: orchestrate factor mining as a durable agent job"
```

---

### 任务 11：Human HTTP 接口与 OpenAPI 注册

**文件：**
- 创建：`backend_api_python/app/routes/factor_mining.py`
- 修改：`backend_api_python/app/openapi/tags.py`（新增 `FACTOR_MINING = "FactorMining"`）
- 修改：`backend_api_python/app/openapi/register.py`（`_PREFIX_TAGS` 与 `registrations` 各加一条）
- 修改：`docs/api/openapi.yaml`（`python scripts/export_openapi.py` 生成）
- 测试：`backend_api_python/tests/test_factor_mining_api.py`

**Interfaces：**

```python
factor_mining_blp = Blueprint("factor_mining", __name__)
# POST /run, POST /estimate, GET /jobs, GET /jobs/<job_id>, POST /jobs/<job_id>/cancel,
# GET /candidates, GET /candidates/<candidate_id>, GET /catalog
```

- [ ] **步骤 1：编写失败的测试**

按仓库既有 route 测试约定（见 `tests/test_strategy_evolution.py:835-860`）：`Flask.test_request_context` + `g.user_id` + `inspect.unwrap(view)` 绕过 `login_required`，协作者用 `monkeypatch.setattr` 打在导入模块上。

```python
import inspect

import pytest
from flask import Flask, g

from app.routes import factor_mining as factor_mining_routes


class _FakeService:
    def submit(self, *, user_id, payload):
        assert user_id == 23
        return {"jobId": "job-9", "status": "queued"}

    def estimate(self, payload):
        return {"candidateBudget": 20, "templateCount": 7}

    def get_candidate(self, candidate_id, *, user_id):
        return None


@pytest.fixture
def app():
    return Flask(__name__)


def test_run_returns_202_with_job_receipt(app, monkeypatch):
    monkeypatch.setattr(factor_mining_routes, "get_factor_mining_service", lambda: _FakeService())
    with app.test_request_context("/api/factor-mining/run", json={"sourceId": 1}):
        g.user_id = 23
        response, status = inspect.unwrap(factor_mining_routes.run_factor_mining)()
    assert status == 202
    assert response.get_json()["data"]["jobId"] == "job-9"


def test_run_surfaces_validation_error_as_400(app, monkeypatch):
    class _Rejecting(_FakeService):
        def submit(self, *, user_id, payload):
            raise ValueError("factorMining.sourceRequired")

    monkeypatch.setattr(factor_mining_routes, "get_factor_mining_service", lambda: _Rejecting())
    with app.test_request_context("/api/factor-mining/run", json={}):
        g.user_id = 23
        response, status = inspect.unwrap(factor_mining_routes.run_factor_mining)()
    assert status == 400
    assert response.get_json()["msg"] == "factorMining.sourceRequired"


def test_candidate_detail_returns_404_for_unknown_id(app, monkeypatch):
    monkeypatch.setattr(factor_mining_routes, "get_factor_mining_service", lambda: _FakeService())
    with app.test_request_context("/api/factor-mining/candidates/999"):
        g.user_id = 23
        response, status = inspect.unwrap(factor_mining_routes.get_candidate)("999")
    assert status == 404
    assert response.get_json()["msg"] == "factorMining.candidateNotFound"


def test_catalog_lists_fields_operators_and_templates(app):
    with app.test_request_context("/api/factor-mining/catalog"):
        response = inspect.unwrap(factor_mining_routes.factor_mining_catalog)()
    payload = response.get_json()["data"]
    assert "close" in payload["fields"]
    assert "ts_mean" in payload["operators"]
    assert payload["templates"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_api.py -q`
预期：FAIL，模块不存在

- [ ] **步骤 3：实现 `routes/factor_mining.py` 并注册 OpenAPI tag**

路由只做解析、鉴权、封套，视图函数命名与测试一致：`run_factor_mining` / `estimate_factor_mining` / `list_factor_mining_jobs` / `get_factor_mining_job` / `cancel_factor_mining_job` / `list_factor_mining_candidates` / `get_candidate` / `factor_mining_catalog`；除 `factor_mining_catalog` 外都返回 `(jsonify(...), status)` 二元组。`/run` 返回 202；越权与不存在统一返回 404 + `factorMining.jobNotFound` / `.candidateNotFound`；`/catalog` 由 `PRICE_VOLUME_FIELDS` + `OPERATOR_SPECS` + `template_catalog()` 组装（不含 `build`）。`tags.py` 加常量，`register.py` 加 `("/api/factor-mining", "FactorMining")` 与 blueprint 注册。

- [ ] **步骤 4：重新导出 OpenAPI 并跑契约测试**

运行：`python scripts/export_openapi.py && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_api.py tests/test_openapi.py -q`
预期：PASS，且 `docs/api/openapi.yaml` diff 中出现 `/api/factor-mining/*` 八条路径与 `FactorMining` tag

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/routes/factor_mining.py backend_api_python/app/openapi/tags.py backend_api_python/app/openapi/register.py docs/api/openapi.yaml backend_api_python/tests/test_factor_mining_api.py
git commit -m "feat: expose factor mining run, job, candidate, and catalog endpoints"
```

---

### 任务 12：文档、集成检查与手工验收

**文件：**
- 创建：`docs/product/FACTOR_MINING.md`
- 创建：`docs/product/FACTOR_MINING_CN.md`
- 创建：`backend_api_python/tests/integration/check_factor_mining_postgres.py`
- 修改：`docs/README.md`（追加索引行；该文件已有未提交改动，只追加）
- 修改：`README_CN.md`（`services/` 目录树追加 `factor_mining/`，与 `factors/`、`strategy_evolution/` 同级）

**Interfaces：**

```python
# tests/integration/check_factor_mining_postgres.py
def main() -> int: ...   # 0 = pass；对齐 tests/integration/check_agent_billing_postgres.py 的输出风格
```

- [ ] **步骤 1：编写集成检查脚本**

按 `tests/integration/check_agent_billing_postgres.py` 的既有约定：读 `DATABASE_URL`（缺失则打印阻塞点并以非 0 退出码结束）、用 `psycopg2` 建一个唯一命名的临时 schema，在其中先建一张 `qd_users` 桩表（迁移 DDL 硬引用 `qd_users(id)`，外键需要它存在），把 `search_path` 指向该 schema 后应用 `migrations/20261013_factor_mining.sql` 的建表语句——**表名保持 `qd_` 前缀原样**（要改的只是它们落在哪个 schema，不是名字），**不触碰业务表**，`finally` 里 DROP SCHEMA。断言：三张表存在；插入一条 run + 两条 trial 后 `count_trials(run_id, user_id=...)` 返回 **2**（该方法签名已改为关键字 `user_id`，计划正文里的旧调用是过期的）；同一 `(user_id, expression_hash)` 二次写入走**更新**而非报错，且 `run_id` 指向最近一次、`created_at` 不被覆盖；**同一份迁移连续执行两次仍成功**（幂等）；删除该 run 后其 trials **级联消失**；用另一个 `user_id` 调 `count_trials` 得到 **0**；跨 `user_id` 读取返回空。

- [ ] **步骤 2：运行集成检查**

运行：`.venv/Scripts/python.exe tests/integration/check_factor_mining_postgres.py`
预期：各断言输出 `OK`、退出码 0、临时 schema 已清理（需本机 PG 与 `DATABASE_URL`；无 PG 时如实报告阻塞点，不得伪造通过）

- [ ] **步骤 3：写双语文档**

`FACTOR_MINING.md` / `FACTOR_MINING_CN.md` 覆盖：模块定位、DSL 与字段/算子/模板白名单、评估阶梯与默认门槛、时间口径与防偏差约定、API 一览、作业与预算限制、已知非目标（不接入策略运行时、无 AI 提案、未计费）。两边内容对等。

- [ ] **步骤 4：跑全量相关测试**

运行：`.venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py tests/test_factor_mining_operators.py tests/test_factor_mining_evaluator.py tests/test_factor_mining_templates.py tests/test_factor_mining_gates.py tests/test_factor_mining_data.py tests/test_factor_mining_engine.py tests/test_factor_mining_freeze.py tests/test_factor_mining_storage.py tests/test_factor_mining_service.py tests/test_factor_mining_api.py tests/test_factor_research.py tests/test_strategy_v2_factor_api.py tests/test_openapi.py -q`
预期：全部 PASS

- [ ] **步骤 5：Commit**

```bash
git add docs/product/FACTOR_MINING.md docs/product/FACTOR_MINING_CN.md docs/README.md README_CN.md backend_api_python/tests/integration/check_factor_mining_postgres.py
git commit -m "docs: document factor mining module and add postgres integration check"
```

---

## 手工验收（需要真实数据源与 DB，不在单测范围）

1. 起后端与 worker（本机 PG17/Redis7），用真实 USStock 资产池提交一次小预算作业（`budget=50`，区间 2 年）：
   `POST /api/factor-mining/run` → `GET /api/factor-mining/jobs/<job_id>` 直到终态。
2. 断言：`trialsCount` = `qd_factor_trials` 行数；`frozenCount` ≤ `candidatesCount`；`snapshotId` 非空。
3. 同 seed 重跑一次，比对 `qd_factor_candidates.expression_hash` 集合完全一致。
4. 检查 `GET /api/factor-mining/candidates?runId=<id>&status=rejected` 能列出被淘汰候选及原因键。
5. 把结果记入验收记录（含命令、输出摘要、残余风险），不通过则以证据为准。
