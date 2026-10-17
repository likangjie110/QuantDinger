# 因子挖掘：加密交割合约的期限结构与换月成本 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 `Crypto` 市场的因子候选可以使用 `term_structure` 与 `roll_cost` 两个字段；数据来自 Deribit 的 BTC/ETH 月度交割合约，按实测的可知时点装配，不引入前视。

**架构：** 新增只读数据源 `app/data_sources/futures_curve.py`：按到期日历枚举 Deribit 月度合约，用 `get_instrument` 读取上市/到期时点、用 `get_tradingview_chart_data` 读取日线，并把日线戳记校正为「收盘可知」时点（tick + 1 天）。字段接入沿用 v2 工作流 C 的既有路径：`dsl.FIELD_GROUPS` 增加字段 → `market_fields._SOURCE_FIELDS` 增加字段 → `enrich_frames` 逐标的填充；不新增端点、表、job kind，不递增 `DSL_VERSION`。

**技术栈：** Python 3.11、pandas、requests、pytest。

**规格：** [`docs/superpowers/specs/2026-10-13-factor-mining.md`](../specs/2026-10-13-factor-mining.md) §13（v2 跨市场字段）；字段语义见 [`docs/product/FACTOR_MINING_CN.md`](../../product/FACTOR_MINING_CN.md) §3.1 与 §11。

## 探针证据（2026-10-17，本机实测）

选 Deribit 不是偏好，是唯一同时满足「已到期合约可回溯」与「元数据完整」的可用源：

| 源 | 实测 | 结论 |
| --- | --- | --- |
| Gate 交割 | `GET /api/v4/delivery/usdt/contracts` 返回 30 个活跃合约（周/双周/季/双季）；`candlesticks?contract=BTC_USDT_20260925` 返回 `CONTRACT_NOT_FOUND`（HTTP 400） | 已到期合约不可回溯，不能做历史期限结构 |
| Binance | `fapi.binance.com` / `dapi.binance.com` 均 HTTP 451（当前出口地区受限） | 不可用 |
| OKX | TLS handshake 失败（本机 schannel） | 不可用 |
| Deribit | `get_instrument?instrument_name=BTC-25SEP20` 返回 `state=archivized`、`creation_timestamp=2020-03-02T08:06:44Z`、`expiration_timestamp=2020-09-25T08:00:00Z`；`get_tradingview_chart_data` 对已到期合约返回日线 | 唯一可用源；BTC/ETH 两个标的 |

注意：`get_instruments?expired=true` 只返回当日已交割的 1 个合约，历史合约必须按名字逐个 `get_instrument`——所以合约池由「到期日历枚举 + 元数据过滤」重建，不能依赖列表接口。

时间语义实测（`BTC-25DEC26`，2026-10-17）：

- 日线戳记是 **venue day 的开盘时点**（08:00 UTC），不是收盘时点。
- `1D` 戳 `2026-08-29T08:00Z` 的 close = `79382.5`，等于 `1H` 在 `2026-08-30T07:00Z` 的 close；`1D` 戳 `2026-08-31T08:00Z` 的 close = `79825.0`，等于 `1H` 在 `2026-09-01T07:00Z` 的 close。
- 因此一根日线的 close **在 tick + 1 天 08:00 UTC 才可知**；直接按 tick 对齐就是前视。

## 全局约束

- Python 3.11（仓库 `.venv`）；`ruff` line-length 120，规则集 `E9,F63,F7,F82`。
- 测试命令统一：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/<file>.py -q`。
- 单测不得触网：`tests/conftest.py` 的 `_block_network` 已把 `requests.get` 变成 `NetworkReached`（`BaseException`），不得用 `except Exception` 把它吞掉。
- 数据源失败一律返回空 `pd.Series`，不抛异常；只有「未知标的/未知市场」与「源请求失败」两种沉默方式，不允许用替代数据填。
- 只对 `Crypto` 市场、只对基础币为 `BTC` / `ETH` 的标的返回值；其他标的返回空序列，由 L2 覆盖率门槛决定缺口含义。
- 不新增 HTTP 端点、不新增数据表、不新增 job kind；`/catalog` 的字段来自 `fields_for`，不硬编码。
- 不递增 `DSL_VERSION`：字段名变化不改变既有表达式的 `expression_hash`。
- 源码注释用英文（`AGENTS.md`）；面向用户文案只返回 i18n 键，本计划不新增 i18n 键。
- 保留工作区已有未提交改动：只追加、不回滚、不覆盖无关文件（开工前先看一次 `git status`）。
- 每个任务结束前：`python -m py_compile <改动文件>` 通过 + 该任务的测试通过。
- 确定性：同一 `(标的, 窗口)` 的曲线值逐字节相同；合约枚举与排序一律按到期时间与名称排序后再迭代。

## 审查重点（Review Focus）

1. **日线戳记前视**：把 08:00 的日线 close 当成 08:00 可知 → 每个 bar 都提前一天读到收盘价。钉住：任务 1 `test_daily_close_is_indexed_when_it_became_known`、任务 2 `test_the_curve_index_is_the_close_known_instant`。
2. **合约池里出现当时尚未上市的合约**：用 `get_instrument` 的 `creation_timestamp` 过滤；只用到期时间排序而不看上市时间，会把未来合约的价格读进过去。钉住：任务 2 `test_the_universe_excludes_a_contract_created_after_the_bar`。
3. **换月规则不连续**：主力合约必须在到期前 7 天整切换，早一天或晚一天都会让 `roll_cost` 在错误的位置跳变。钉住：任务 2 `test_the_main_contract_rolls_seven_days_before_expiry`。
4. **跨标的/跨市场误填**：Deribit 只有 BTC/ETH；SOL/USDT 成员拿到 BTC 的曲线就是「看起来合理的错数」。钉住：任务 1 `test_an_underlying_outside_btc_eth_returns_an_empty_series`。
5. **单个合约取数失败被拼成一条假曲线**：缺一条腿时返回 NaN 或整段空，不得用单合约价格代替价差。钉住：任务 2 `test_no_far_contract_gives_nan_not_a_one_contract_curve`、任务 1 `test_a_failed_request_returns_an_empty_series`。

## 文件结构

- 创建 `backend_api_python/app/data_sources/futures_curve.py`：Deribit 月度合约的元数据、日线、点时曲线与两个字段入口。
- 创建 `backend_api_python/tests/test_factor_mining_futures_curve.py`：录制响应驱动的数据源单测。
- 创建 `backend_api_python/tests/fixtures/deribit/instruments.json`、`chart_1d.json`：真实响应录制。
- 修改 `backend_api_python/app/services/factor_mining/dsl.py`：字段组增加两项。
- 修改 `backend_api_python/app/services/factor_mining/market_fields.py`：字段源集合、导入与 `_series` 分支。
- 修改 `backend_api_python/tests/test_factor_mining_dsl.py`、`tests/test_factor_mining_market_fields.py`、`tests/test_factor_mining_api.py`：字段集与装配断言。
- 修改 `docs/product/FACTOR_MINING.md`、`docs/product/FACTOR_MINING_CN.md`、`docs/superpowers/specs/2026-10-13-factor-mining.md`：字段表、计数、非目标口径。

---

### 任务 1：Deribit 合约元数据、到期日历与可知时点日线

**文件：**
- 创建：`backend_api_python/app/data_sources/futures_curve.py`
- 创建：`backend_api_python/tests/test_factor_mining_futures_curve.py`
- 创建：`backend_api_python/tests/fixtures/deribit/instruments.json`
- 创建：`backend_api_python/tests/fixtures/deribit/chart_1d.json`

- [ ] **步骤 1：录制真实响应为夹具**

运行下面的脚本（只跑一次，不入库），把响应写进 `tests/fixtures/deribit/`。录制窗口固定为 2026-08-01 00:00 UTC → 2026-10-02 00:00 UTC，覆盖 `BTC-25SEP26` 的到期与换月边界。

```python
import json, pathlib, requests
from datetime import datetime, timezone

OUT = pathlib.Path("backend_api_python/tests/fixtures/deribit")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://www.deribit.com/api/v2/public"
def ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)

instruments = []
for name in ["BTC-25SEP26", "BTC-25DEC26", "BTC-26MAR27", "ETH-25DEC26", "BTC-25JAN27"]:
    url = f"{BASE}/get_instrument"
    params = {"instrument_name": name}
    body = requests.get(url, params=params, timeout=20).json()
    instruments.append({"url": url, "params": params, "body": body})

charts = []
for name in ["BTC-25SEP26", "BTC-25DEC26", "BTC-26MAR27"]:
    url = f"{BASE}/get_tradingview_chart_data"
    params = {"instrument_name": name, "start_timestamp": ms(2026, 8, 1),
              "end_timestamp": ms(2026, 10, 2), "resolution": "1D"}
    body = requests.get(url, params=params, timeout=20).json()
    charts.append({"url": url, "params": params, "body": body})

(OUT / "instruments.json").write_text(json.dumps(
    {"recorded_at": "2026-10-17", "source": "deribit", "responses": instruments},
    ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "chart_1d.json").write_text(json.dumps(
    {"recorded_at": "2026-10-17", "source": "deribit", "responses": charts},
    ensure_ascii=False, indent=2), encoding="utf-8")
```

预期：两个文件都存在；`instruments.json` 含 4 个 `result` 与 1 个 `error`（`BTC-25JAN27` 尚未上市）；`chart_1d.json` 的每个 `result.status == "ok"` 且 `ticks` 非空。

- [ ] **步骤 2：编写失败的测试**

```python
# tests/test_factor_mining_futures_curve.py
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from app.data_sources import futures_curve

_FIXTURES = Path(__file__).parent / "fixtures" / "deribit"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Unrecorded(BaseException):
    """A request no fixture answers; not an Exception, so the source cannot swallow it."""


def _canonical(params):
    return tuple(sorted((str(key), str(value)) for key, value in (params or {}).items()))


def _load_bodies():
    bodies = {}
    for path in sorted(_FIXTURES.glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for item in fixture["responses"]:
            bodies[(item["url"], _canonical(item["params"]))] = item["body"]
    return bodies


@pytest.fixture(autouse=True)
def _recorded(monkeypatch):
    bodies = _load_bodies()
    def get(url, *, params=None, **_kwargs):
        key = (url, _canonical(params))
        if key not in bodies:
            raise _Unrecorded(f"unrecorded request: {url} {params}")
        return _Response(bodies[key])
    monkeypatch.setattr(futures_curve.requests, "get", get)


def test_monthly_expiry_is_the_last_friday_at_0800_utc():
    rows = futures_curve._monthly_expiries("BTC", datetime(2026, 8, 1), datetime(2026, 10, 31))
    assert ("BTC-25SEP26", pd.Timestamp("2026-09-25 08:00")) in rows
    assert ("BTC-30OCT26", pd.Timestamp("2026-10-30 08:00")) in rows
    assert all(expiry.weekday() == 4 and expiry.hour == 8 for _name, expiry in rows)

def test_instrument_name_does_not_zero_pad_the_day():
    assert futures_curve._instrument_name("BTC", datetime(2026, 11, 6)) == "BTC-6NOV26"

def test_archived_contract_metadata_is_read_from_the_recorded_body():
    meta = futures_curve._instrument("BTC-25SEP26")
    assert meta["creation"] == pd.Timestamp("2025-09-26 08:00:21")
    assert meta["expiration"] == pd.Timestamp("2026-09-25 08:00")

def test_an_unknown_instrument_is_skipped_not_raised():
    assert futures_curve._instrument("BTC-25JAN27") is None

def test_daily_close_is_indexed_when_it_became_known():
    series = futures_curve._daily_closes(
        "BTC-25DEC26", datetime(2026, 8, 1), datetime(2026, 10, 2))
    # The source answers with the candle containing the requested start
    # (raw tick 2026-07-31 08:00, close 64035.0); it is only known a day later.
    assert series.index[0] == pd.Timestamp("2026-08-01 08:00")
    assert series.iloc[0] == pytest.approx(64035.0)

def test_an_underlying_outside_btc_eth_returns_an_empty_series():
    empty = futures_curve.fetch_term_structure(
        "Crypto", "Crypto:SOL/USDT@swap", start=datetime(2026, 8, 1), end=datetime(2026, 10, 1))
    assert empty.empty

def test_a_failed_request_returns_an_empty_series(monkeypatch):
    import requests
    def fail(*_args, **_kwargs):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(futures_curve.requests, "get", fail)
    empty = futures_curve._daily_closes("BTC-25DEC26", datetime(2026, 8, 1), datetime(2026, 10, 2))
    assert empty.empty
```

夹具的查找键是 `(url, 参数映射)`：实现发送的参数必须与录制时逐字一致，测试也必须使用录制时的窗口（2026-08-01 → 2026-10-02），否则 `_Unrecorded` 会让测试失败而不是悄悄放过。

- [ ] **步骤 3：运行测试验证失败**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_futures_curve.py -q`
预期：FAIL，`ModuleNotFoundError: No module named 'app.data_sources.futures_curve'`。

- [ ] **步骤 4：实现 `app/data_sources/futures_curve.py`**

模块级常量与签名（函数体由实现者写）：

```python
_DERIBIT = "https://www.deribit.com/api/v2/public"
_MARKETS = frozenset({"Crypto"})
_UNDERLYINGS = frozenset({"BTC", "ETH"})
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_TIMEOUT = 20

def _underlying(symbol: str) -> str | None: ...
def _instrument_name(currency: str, day: datetime) -> str: ...
def _monthly_expiries(currency: str, start: datetime, end: datetime) -> list[tuple[str, pd.Timestamp]]: ...
def _instrument(name: str) -> dict | None: ...
def _daily_closes(name: str, start: datetime, end: datetime) -> pd.Series: ...
def fetch_term_structure(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series: ...
def fetch_roll_cost(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series: ...
```

实现要点（逐条决定，不留选择）：

- `_underlying`：去掉 `Crypto:` 前缀，取 `@` 之前的部分，再取 `/` 之前的 token；大小写转大写；不在 `_UNDERLYINGS` 返回 `None`。
- `_instrument_name`：`f"{currency}-{day.day}{_MONTHS[day.month - 1]}{day.year % 100:02d}"`，日不补零。
- `_monthly_expiries`：从 `start` 所在月到 `end` 所在月再加 2 个月，逐月取最后一个周五，时间固定 08:00 UTC。
- `_instrument`：GET `get_instrument`，参数 `{"instrument_name": name}`；`result` 存在则返回 `{"creation": pd.Timestamp(...).tz_localize(None), "expiration": ...}`（毫秒转 UTC 后去时区）；任何异常或 `error` 返回 `None`。
- `_daily_closes`：GET `get_tradingview_chart_data`，参数 `instrument_name`、`start_timestamp`/`end_timestamp`（毫秒，naive datetime 一律按 UTC 解释）、`resolution="1D"`；把 `result.ticks` 转 UTC 后 **加 1 天** 作为索引，`result.close` 转 float；按索引排序、重复索引保留最后一行；截到 `[start, end]`；异常或 `result.status != "ok"` 返回空 `pd.Series(dtype="float64", index=pd.DatetimeIndex([]))`。
- `fetch_term_structure` / `fetch_roll_cost`：本任务先返回空序列（任务 2 实现曲线计算），保证导入与失败路径可用。

- [ ] **步骤 5：运行测试验证通过**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_futures_curve.py -q`
预期：PASS。

- [ ] **步骤 6：Commit**

```bash
git add backend_api_python/app/data_sources/futures_curve.py backend_api_python/tests/test_factor_mining_futures_curve.py backend_api_python/tests/fixtures/deribit/
git commit -m "feat: 新增 Deribit 交割合约元数据与可知时点日线数据源"
```

---

### 任务 2：期限结构与换月成本的点时曲线

**文件：**
- 修改：`backend_api_python/app/data_sources/futures_curve.py`
- 修改：`backend_api_python/tests/test_factor_mining_futures_curve.py`

- [ ] **步骤 1：编写失败的测试**

```python
from datetime import datetime
import pandas as pd
import pytest

from app.data_sources import futures_curve

_SEP = pd.Timestamp("2026-09-25 08:00")
_OCT = pd.Timestamp("2026-10-30 08:00")
_NOV = pd.Timestamp("2026-11-27 08:00")
_DEC = pd.Timestamp("2026-12-25 08:00")

def _meta(created="2026-06-01 08:00", expires=_SEP):
    return {"creation": pd.Timestamp(created), "expiration": pd.Timestamp(expires)}

def test_the_universe_excludes_a_contract_created_after_the_bar():
    meta = {"BTC-25SEP26": _meta(created="2026-09-02 08:00", expires=_SEP)}
    closes = {"BTC-25SEP26": pd.Series([100.0], index=pd.DatetimeIndex(["2026-09-01 08:00"]))}
    curve = futures_curve._curve(
        "BTC", datetime(2026, 9, 1), datetime(2026, 9, 3),
        instrument=lambda name: meta.get(name),
        daily_closes=lambda name, start, end: closes.get(name, pd.Series(dtype="float64")))
    assert curve.empty or curve["roll_cost"].isna().all()

def test_term_structure_annualizes_between_the_two_main_contracts():
    meta = {"BTC-25SEP26": _meta(expires=_SEP), "BTC-25DEC26": _meta(expires=_DEC)}
    index = pd.DatetimeIndex(["2026-08-03 08:00"])
    closes = {"BTC-25SEP26": pd.Series([100.0], index=index),
              "BTC-25DEC26": pd.Series([103.0], index=index)}
    curve = futures_curve._curve(
        "BTC", datetime(2026, 8, 1), datetime(2026, 10, 31),
        instrument=lambda name: meta.get(name),
        daily_closes=lambda name, start, end: closes.get(name, pd.Series(dtype="float64")))
    row = curve.loc[pd.Timestamp("2026-08-03 08:00")]
    assert row["term_structure"] == pytest.approx((103.0 / 100.0 - 1.0) * 365 / 91)
    assert row["roll_cost"] == pytest.approx(0.03)

def test_the_main_contract_rolls_seven_days_before_expiry():
    meta = {"BTC-25SEP26": _meta(expires=_SEP), "BTC-30OCT26": _meta(expires=_OCT),
            "BTC-27NOV26": _meta(expires=_NOV)}
    index = pd.DatetimeIndex(["2026-09-17 08:00", "2026-09-18 08:00", "2026-09-19 08:00"])
    closes = {
        "BTC-25SEP26": pd.Series([100.0, 100.0, 100.0], index=index),
        "BTC-30OCT26": pd.Series([102.0, 102.0, 102.0], index=index),
        "BTC-27NOV26": pd.Series([104.0, 104.0, 104.0], index=index),
    }
    curve = futures_curve._curve(
        "BTC", datetime(2026, 9, 1), datetime(2026, 9, 30),
        instrument=lambda name: meta.get(name),
        daily_closes=lambda name, start, end: closes.get(name, pd.Series(dtype="float64")))
    assert curve.loc[pd.Timestamp("2026-09-17 08:00"), "roll_cost"] == pytest.approx(0.02)
    assert curve.loc[pd.Timestamp("2026-09-18 08:00"), "roll_cost"] == pytest.approx(0.02)
    # 6 days to expiry: the main is OCT, the far is NOV
    assert curve.loc[pd.Timestamp("2026-09-19 08:00"), "roll_cost"] == pytest.approx(104.0 / 102.0 - 1.0)

def test_no_far_contract_gives_nan_not_a_one_contract_curve():
    meta = {"BTC-25SEP26": _meta(expires=_SEP)}
    closes = {"BTC-25SEP26": pd.Series([100.0], index=pd.DatetimeIndex(["2026-08-03 08:00"]))}
    curve = futures_curve._curve(
        "BTC", datetime(2026, 8, 1), datetime(2026, 8, 5),
        instrument=lambda name: meta.get(name),
        daily_closes=lambda name, start, end: closes.get(name, pd.Series(dtype="float64")))
    assert curve["term_structure"].isna().all()
    assert curve["roll_cost"].isna().all()

def test_the_curve_index_is_the_close_known_instant():
    series = futures_curve._daily_closes("BTC-25DEC26", datetime(2026, 8, 1), datetime(2026, 10, 2))
    assert all(stamp.hour == 8 for stamp in series.index)
    # The first raw tick is 2026-07-31 08:00; its close is known at 2026-08-01 08:00.
    assert series.index[0] == pd.Timestamp("2026-08-01 08:00")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_futures_curve.py -q`
预期：FAIL，`AttributeError: module 'app.data_sources.futures_curve' has no attribute '_curve'`（或 `fetch_term_structure` 返回空导致断言失败）。

- [ ] **步骤 3：实现 `_curve` 与两个公开入口**

```python
_ROLL_DAYS = 7   # the main contract is the nearest monthly with at least this many days to expiry
_CURVE_FIELDS = ("term_structure", "roll_cost")

@lru_cache(maxsize=32)
def _curve(underlying: str, start: datetime, end: datetime, *,
           instrument=_instrument, daily_closes=_daily_closes) -> pd.DataFrame: ...

def fetch_term_structure(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series: ...
def fetch_roll_cost(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series: ...
```

实现要点：

- `_curve`：枚举 `_monthly_expiries` → 对每个名字调用 `instrument`，丢弃 `None` → 对保留的合约各调用一次 `daily_closes` → 以所有序列索引的并集（截到 `[start, end]`）为网格；每根合约序列先 `reindex(grid, method="ffill")`。
- 每个网格点 `t`：eligible = `creation <= t < expiration`，按 `expiration` 排序；main = 第一个 `expiration - t >= 7 天` 的合约，若没有则跳过该点；far = main 之后的第一个 eligible，若没有则该点 NaN。
- `term_structure = (P_far / P_main - 1) * 365 / (expiration_far - expiration_main).days`；`roll_cost = P_far / P_main - 1`；任一价格 NaN 或分母 ≤ 0 记 NaN。
- 返回 `pd.DataFrame(index=grid, columns=["term_structure", "roll_cost"], dtype="float64")`；`grid` 为空时返回空 DataFrame（列齐全）。
- `fetch_term_structure` / `fetch_roll_cost`：`market not in _MARKETS` 或 `_underlying(symbol) is None` 返回空 Series；否则 `curve = _curve(...)`，返回 `curve[field]`（保留 NaN 行），空表返回空 Series。
- `_curve` 上的 `lru_cache` 是刻意的：`market_fields` 会为两个字段各调用一次，缓存让同一次运行只取一遍网络；`maxsize=32` 防止长驻进程无限增长。测试直接传注入函数时，缓存键包含不同的函数对象，不会串味。

- [ ] **步骤 4：运行测试验证通过**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_futures_curve.py -q`
预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/data_sources/futures_curve.py backend_api_python/tests/test_factor_mining_futures_curve.py
git commit -m "feat: 因子挖掘新增加密交割合约期限结构与换月成本字段"
```

---

### 任务 3：字段白名单与面板装配贯通

**文件：**
- 修改：`backend_api_python/app/services/factor_mining/dsl.py:37-45`
- 修改：`backend_api_python/app/services/factor_mining/market_fields.py:37-45, 115-145`
- 测试：`backend_api_python/tests/test_factor_mining_dsl.py`
- 测试：`backend_api_python/tests/test_factor_mining_market_fields.py`
- 测试：`backend_api_python/tests/test_factor_mining_api.py:346-360`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_factor_mining_dsl.py` 加：

```python
def test_crypto_field_set_includes_the_delivery_curve_fields():
    fields = fields_for("Crypto")
    assert "term_structure" in fields
    assert "roll_cost" in fields
    assert fields_for("USStock") == PRICE_VOLUME_FIELDS
```

在 `tests/test_factor_mining_market_fields.py` 加：

```python
def test_the_curve_fields_are_filled_from_their_own_source(monkeypatch):
    _patch_histories(monkeypatch, _FakeHistories())
    curve = pd.Series(
        [0.05], index=pd.DatetimeIndex([datetime(2024, 1, 2)]))
    monkeypatch.setattr(market_fields, "fetch_term_structure", lambda *a, **k: curve)
    monkeypatch.setattr(market_fields, "fetch_roll_cost", lambda *a, **k: curve)
    index = pd.date_range("2024-01-01", periods=4, freq="D")
    frame = market_fields.enrich_frames(
        {_BTC: _bars(index, 0.0)}, market="Crypto",
        start=datetime(2024, 1, 1), end=datetime(2024, 1, 4))[_BTC]
    assert pd.isna(frame["term_structure"].iloc[0])  # nothing is known before the first publication
    assert frame["term_structure"].iloc[1:].tolist() == [0.05, 0.05, 0.05]
    assert frame["roll_cost"].iloc[1:].tolist() == [0.05, 0.05, 0.05]


def test_a_member_the_curve_cannot_answer_keeps_a_nan_column(monkeypatch):
    _patch_histories(monkeypatch, _FakeHistories())
    monkeypatch.setattr(market_fields, "fetch_term_structure",
                        lambda *a, **k: pd.Series(dtype="float64", index=pd.DatetimeIndex([])))
    monkeypatch.setattr(market_fields, "fetch_roll_cost",
                        lambda *a, **k: pd.Series(dtype="float64", index=pd.DatetimeIndex([])))
    index = pd.date_range("2024-01-01", periods=3, freq="D")
    frame = market_fields.enrich_frames(
        {_SOL: _bars(index, 0.0)}, market="Crypto",
        start=datetime(2024, 1, 1), end=datetime(2024, 1, 3))[_SOL]
    assert frame["term_structure"].isna().all()
    assert frame["roll_cost"].isna().all()
```

在 `tests/test_factor_mining_api.py` 的 catalog 用例加：

```python
assert "term_structure" in crypto.get_json()["data"]["fields"]
assert "roll_cost" in crypto.get_json()["data"]["fields"]
assert "term_structure" not in plain.get_json()["data"]["fields"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py tests/test_factor_mining_market_fields.py tests/test_factor_mining_api.py -q`
预期：FAIL，字段不在 `fields_for` 集合 / `enrich_frames` 没有新列。

- [ ] **步骤 3：实现**

- `dsl.py`：`FIELD_GROUPS["derivatives"] = ("funding_rate", "open_interest", "term_structure", "roll_cost")`；把「term structure and roll cost are in no group yet」这条注释改成「Crypto 的衍生品组由 Deribit BTC/ETH 月度合约填充」。
- `market_fields.py`：模块顶部 `from app.data_sources.futures_curve import fetch_roll_cost, fetch_term_structure`；`_SOURCE_FIELDS` 增加两项；`_series` 增加两个分支，未知字段仍 `raise KeyError(field)`；模块 docstring 补一句「期限结构来自 Deribit 的跨场所曲线，日线 close 以 tick + 1 天为可知时点」。

- [ ] **步骤 4：运行测试验证通过**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_factor_mining_dsl.py tests/test_factor_mining_market_fields.py tests/test_factor_mining_api.py -q`
预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add backend_api_python/app/services/factor_mining/dsl.py backend_api_python/app/services/factor_mining/market_fields.py backend_api_python/tests/test_factor_mining_dsl.py backend_api_python/tests/test_factor_mining_market_fields.py backend_api_python/tests/test_factor_mining_api.py
git commit -m "feat: 期限结构与换月成本进入 Crypto 字段白名单并接入面板"
```

---

### 任务 4：文档口径与全量回归

**文件：**
- 修改：`docs/product/FACTOR_MINING.md`、`docs/product/FACTOR_MINING_CN.md`
- 修改：`docs/superpowers/specs/2026-10-13-factor-mining.md:216`

- [ ] **步骤 1：同步两版产品文档**

- §3.1 字段表：`derivatives` 组加 `term_structure`、`roll_cost`；把「`term_structure` 与 `roll_cost` 不在任何组里」那段改成实现口径：数据来自 Deribit BTC/ETH 月度交割合约，日线以 tick + 1 天为可知时点，非 BTC/ETH 标的是 NaN 列。
- §3.1 的 `/catalog` 计数：`?market=Crypto` 从「两个组共七个字段」改为「两个组共九个字段」。
- §11 已知非目标：把「不在本次范围内」的条目改成「**传统期货**的期限结构与换月成本仍不在范围内（需要期货多合约数据面）；加密交割合约已由 Deribit 月度合约提供」。
- 规格 §13 的 v2 行追加一句：加密交割合约的期限结构/换月成本已落地，传统期货仍待独立数据面。

- [ ] **步骤 2：检查文档门禁**

运行：在 worktree 根执行 `python scripts/check_docs.py`
预期：退出码 0。若脚本要求文档内链接与标题一致，按报错逐条修。

- [ ] **步骤 3：跑因子挖掘全量单测**

运行：

```bash
cd backend_api_python
.venv/Scripts/python.exe -m pytest tests/test_factor_mining_api.py tests/test_factor_mining_data.py tests/test_factor_mining_dsl.py tests/test_factor_mining_engine.py tests/test_factor_mining_evaluator.py tests/test_factor_mining_evolution.py tests/test_factor_mining_field_set_guard.py tests/test_factor_mining_freeze.py tests/test_factor_mining_gates.py tests/test_factor_mining_incremental.py tests/test_factor_mining_market_fields.py tests/test_factor_mining_operators.py tests/test_factor_mining_pool.py tests/test_factor_mining_service.py tests/test_factor_mining_storage.py tests/test_factor_mining_templates.py tests/test_derivatives_source.py tests/test_factor_mining_futures_curve.py -q
```

预期：全部 PASS（基线 469 + 新增用例）。若 `test_openapi.py` 因 catalog 描述变化失败，运行 `python scripts/export_openapi.py` 后重跑；本计划不新增路径，预期不需要重新导出。

- [ ] **步骤 4：真实数据验收（手工，不写测试）**

运行：

```bash
cd backend_api_python
.venv/Scripts/python.exe - <<'PY'
from datetime import datetime
from app.data_sources.futures_curve import fetch_term_structure, fetch_roll_cost
s = fetch_term_structure("Crypto", "Crypto:BTC/USDT@binance:swap",
                         start=datetime(2026, 8, 1), end=datetime(2026, 10, 1))
r = fetch_roll_cost("Crypto", "Crypto:BTC/USDT@binance:swap",
                    start=datetime(2026, 8, 1), end=datetime(2026, 10, 1))
print("term_structure", s.tail(3).to_dict())
print("roll_cost", r.tail(3).to_dict())
assert s.notna().any() and r.notna().any()
assert s.index.equals(r.index)
PY
```

预期：两条序列非空、索引一致；值在到期边界附近跳变（说明换月规则生效）；无 NaN 以外的异常。

- [ ] **步骤 5：Commit**

```bash
git add docs/product/FACTOR_MINING.md docs/product/FACTOR_MINING_CN.md docs/superpowers/specs/2026-10-13-factor-mining.md
git commit -m "docs: 登记加密交割合约期限结构与换月成本的口径"
```

---

## 手工验收（需要真实网络，不在单测范围）

1. `fetch_term_structure` 在 `2026-09-19`（`BTC-25SEP26` 距到期 6 天）之后的值改用 `BTC-25DEC26`/`BTC-26MAR27` 计算，`roll_cost` 在同一天跳变。
2. 同一窗口重复调用，两条序列逐字节相同（`_curve` 的缓存不得引入非确定性）。
3. `GET /catalog?market=Crypto` 返回 9 个字段；`?market=USStock` 仍是 5 个。
4. 一个命名 `term_structure` 的候选在 Crypto 市场通过 L1（不再 `unknownField`），在 USStock 市场仍被 `unknownField` 拒绝。

## 未决与已知限制

1. **只有 BTC/ETH**：Deribit 不提供其他标的的交割合约；SOL 等成员是 NaN 列，由 L2 覆盖率决定是否可挖掘。后续要扩展标的，先找有同样「到期合约可回溯」能力的数据源。
2. **日线粒度**：曲线每天只在 08:00 UTC 更新一次；小时级因子看到的是阶梯函数。需要更细粒度时把 `resolution` 改成 `60`，代价是请求量成倍增长——本计划不做。
3. **跨场所基差**：Deribit 是 USD 计价的反向合约，成员可能是 USDT 永续；`term_structure` 是两条 Deribit 合约的比值，货币差异大部分抵消，但不是严格同场所口径。文档必须写明这一点。
4. **请求量**：一个 1 年窗口约枚举 14 个月度合约，每个 2 次请求（元数据 + 日线），首次运行约 28 次网络往返；`lru_cache` 让两个字段共享同一次抓取。若不可接受，退路是缩小窗口或加持久缓存，而不是放宽点时口径。
