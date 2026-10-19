# 因子挖掘：Agent Gateway / MCP 暴露 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把现有 `/api/factor-mining/*` 的 9 个能力以租户隔离、scope 受控的方式暴露到 `/api/agent/v1/factor-mining/*`，新增 9 个 MCP 工具，并让 `docs/agent/agent-openapi.json` 与契约对齐测试同步。

**架构：** Agent 路由 1:1 镜像人工 API，直接复用 `FactorMiningService` 和同一份 run 字段白名单；不新增 scope、数据表、job kind 或计费。读接口用 `R`，提交/取消用 `B`。MCP 仍是 `/api/agent/v1` 的薄包装。

**技术栈：** Python 3.11、Flask、pytest、FastMCP、httpx。

**规格：** [`docs/superpowers/specs/2026-10-13-factor-mining.md`](../specs/2026-10-13-factor-mining.md) §15.3；[`v2 计划`](2026-10-16-factor-mining-v2.md)「未决与前置」第 3 条；[`v3 计划`](2026-10-18-factor-mining-v3.md)（其中「Agent Gateway/MCP 暴露……仍未开始」）；[`v3 设计`](../specs/2026-10-18-factor-mining-v3-design.md) 的请求字段与提案端点；人工面字段口径见 [`docs/product/FACTOR_MINING_CN.md`](../../product/FACTOR_MINING_CN.md) §7、§8。

## 评估结论（2026-10-18）

### 现状与证据

| 事项 | 事实 | 证据 |
| --- | --- | --- |
| 人工面 | 因子挖掘已有 9 个接口：`catalog`、`estimate`、`run`、`jobs`、`jobs/<id>`、`jobs/<id>/cancel`、`jobs/<id>/proposals`、`candidates`、`candidates/<id>` | `backend_api_python/app/routes/factor_mining.py:78-235` |
| 服务层 | 全部按 `user_id` 隔离；提交走 `qd_agent_jobs`，`kind="factor_mining"`；已有按用户的单活跃作业门与预算硬上限 | `app/services/factor_mining/service.py:61,131-196` |
| Agent 面 | `/api/agent/v1` 共 68 个路径，没有任何 factor-mining 路径 | `docs/agent/agent-openapi.json` |
| 通用作业面 | 通用 `/api/agent/v1/jobs*` 只按 `user_id` 查 `qd_agent_jobs`，因此已能读到/取消 mining 作业，但没有 kind 守卫、没有 `_public_job` 形状，也不触发 mining 的僵尸作业清扫 | `app/routes/agent_v1/jobs.py`、`app/utils/agent_jobs.py:465` |
| 僵尸清扫 | 只有 `FactorMiningService.list_jobs` 会把超时 `running` 作业置为失败；只靠通用 `/jobs` 的 Agent 用户永远不触发清扫，会被 `factorMining.concurrentLimit` 永久挡住 | `app/services/factor_mining/service.py:175-190` |
| Token 归属 | `submit` 把 `agent_token_id` 硬编码为 `None`；回测提交则会写入 token id | `app/services/factor_mining/service.py:152-157`、`app/routes/agent_v1/backtests.py:218-224` |
| 幂等 | `agent_required` 对 `W/B/N/T` 的非 GET 请求强制 `Idempotency-Key`，并持久化响应供重放；路由层不需要重复实现 | `app/utils/agent_auth.py:662-712` |
| Scope 先例 | 只读 POST（`indicators/validate`、`strategy-sources/compile`）用 `R`；回测提交与通用取消用 `B` | `docs/agent/agent-openapi.json` |
| MCP | `MCP_TOOL_NAMES` 共 58 个；工具数有硬断言；MCP 工具元数据由 `ai_tool_registry` 的扩展表生成 | `mcp_server/src/quantdinger_mcp/server.py:32`、`mcp_server/tests/test_security_and_tools.py:33`、`app/services/ai_tool_registry.py:625-700` |
| 对齐测试 | 注册路由必须与 `agent-openapi.json` 的操作集合完全相等；MCP 工具名必须与注册表元数据相等，且每个 tool 的 route 必须已在 Agent 或人类 OpenAPI 中登记 | `backend_api_python/tests/test_ai_agent_contract_alignment.py:88-115` |

### 基线修订（2026-10-19）

执行前基线从 `853de7b` 前进到 `cbe4271`（因子挖掘 v3 已合并）。v3 把人工面从 8 个接口扩到 9 个（新增 `GET /jobs/<job_id>/proposals`），并把 `_RUN_FIELDS` 扩到 24 个键（`aiRounds`、`aiProposalsPerRound`、`replayJobId`、`researchNotes`）。本计划按当前态执行：**镜像 9 个接口、9 个 MCP 工具、工具总数 67**；`searchMode` 枚举加入 `ai`；v3 字段进入 `FactorMiningRunRequest`。这是对原计划「8 个」口径的显式修订，不改变 scope、存储与计费决策。

### 决策

1. **完整镜像 9 个接口，不只暴露提交。** Agent 需要发现词表（`catalog`）、估价（`estimate`）、轮询/取消（`jobs`）、读取 AI 提案（`jobs/<id>/proposals`）与候选（`candidates`）；只给 submit 会让 Agent 无法在不猜字段的情况下提交，也无法取回候选或审计 AI 运行。
2. **使用专用 `/factor-mining/jobs*`，不复用通用 `/jobs*`。** 专用路由才有 kind 守卫、`_public_job` 形状和僵尸清扫；通用 `/jobs*` 保持原样（既有契约，不在本次改动范围内）。两者重叠是既有行为，文档中说明通用作业面仍可读取任意 kind。
3. **不新增 scope。** 读用 `R`，提交/取消用 `B`。`B` 在本计划中按「仿真计算」理解，文档把 B 的说明从「回测」扩为「回测/仿真计算」。新增 scope 会改变 Token 授权模型与所有客户端，收益不足。
4. **共享 run 字段白名单。** 把人工路由的 `_RUN_FIELDS` 改名为公共 `RUN_FIELDS`，Agent 路由导入同一常量；`test_factor_mining_api.py` 已有的「白名单 == `MiningConfig` 字段」断言继续守住漂移。**不**把白名单搬进服务层，避免改动人工路由的 OpenAPI 描述。
5. **记录提交 token。** `FactorMiningService.submit` 增加可选 `agent_token_id`，Agent 路由传入 `g.agent_token["id"]`；与回测作业的归属口径一致。HTTP 层幂等已经覆盖重放，不在服务层重复。
6. **MCP 新增 9 个工具，与 9 个路由一一对应。** `submit_factor_mining_run` 与 `cancel_factor_mining_job` 要求调用方传 `idempotency_key`；两者都标记为写工具，取消为 destructive，且与既有 `cancel_job` 一样要求 `confirm_cancel=true`。`list_factor_mining_proposals` 是只读工具。
7. **文档中英同步，不发布 PyPI。** 仓库头新增 67 个工具；PyPI `0.6.2` 仍是 58 个。MCP_SETUP 明确区分「PyPI 0.6.2」与「仓库 `pip install -e ./mcp_server`」，版本号由单独的 release commit 决定。

### 明确的非目标

- 不新增 scope `F`、数据表、job kind、计费或配额；资源仍受单活跃作业与 `budget` 硬上限约束。
- 不修改通用 `/api/agent/v1/jobs*` 的语义或返回形状。
- 不实现规格 §15.1 的「按 `expressionHash` 进入因子研究」。
- 不发布 `quantdinger-mcp` 到 PyPI；不修改 `mcp_server/pyproject.toml` 版本号。
- 不修改工作区中与本计划无关的未提交文件（`.gitignore`、`DEVELOPMENT.md`、`docs/architecture/README_CN.md`、`scripts/check_docs.py`、`.spec-workflow/`、`SYSTEM_ARCHITECTURE_CN.md`、`frontend-vue/`）。

### 残余风险

- **计算开销：** 拿到 `B` 的 Token 可以提交挖掘。现有单活跃作业门 + `budget` 硬上限 + 速率限制是唯一护栏；本功能不计费。
- **通用作业面重叠：** 有 `R` 的 Token 仍可通过 `/jobs/{id}` 读到 mining 作业的原始行（含 `request.__userId`），这是既有行为；本计划不扩大也不收紧它。
- **大结果：** `GET /factor-mining/jobs/{id}` 会返回完整 `result`（全部试验台账）。Agent 应优先用候选列表/详情；MCP 没有额外的响应体积上限。
- **PyPI 滞后：** 发布前，PyPI 用户看不到新工具。

## 全局约束

- 后端测试命令：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/<file>.py -q`。
- MCP 测试命令：`python -m pip install -e './mcp_server[dev]' && python -m pytest mcp_server/tests -q`（当前 `.venv` 未安装 `mcp`）。
- 单测不得触网；不得用 `except Exception` 吞掉 `tests/conftest.py` 的 `NetworkReached`。
- 源码注释用英文；面向用户文案只返回现有 `factorMining.*` i18n 键，不新增键。
- 保留工作区已有未提交改动：只追加、不回滚、不覆盖无关文件。
- 每个 Commit 使用中文描述 + 英文 Conventional Commits type。
- 新增路由必须同步 `docs/agent/agent-openapi.json`；新增 MCP 工具必须同步 `MCP_TOOL_NAMES`、`tool_contract.WRITE_TOOLS` 与 `ai_tool_registry` 扩展表，否则对齐测试失败。

## 审查重点（Review Focus）

1. **越权提交/取消：** 只有 `R` 的 Token 调用 `POST /factor-mining/jobs` 或 `/cancel` 必须得到 403，且不得创建/取消作业。钉住：任务 1 `test_submit_requires_b_scope`、`test_cancel_requires_b_scope`。
2. **跨租户读取：** A 用户不能读到 B 用户的作业或候选，必须 404 而不是空列表或 403。钉住：服务层既有 `test_cancel_job_cancels_only_the_callers_own_job`（`agent_jobs` 按 `user_id` 过滤），任务 1 `test_get_job_returns_404_when_the_service_has_no_row`、`test_candidate_with_a_non_integer_id_is_404`（路由把 `None` 映射为 404）。
3. **幂等键缺失/重放/冲突：** 提交缺少 `Idempotency-Key` 必须 400；同键同体重放必须返回同一作业；同键异体必须 409。钉住：任务 1 `test_submit_requires_an_idempotency_key`，重放/冲突沿用 `test_agent_v1_full_surface.py` 的既有中间件测试。
4. **僵尸作业导致永久阻塞：** Agent 只靠通用 `/jobs` 不会触发清扫。专用 `GET /factor-mining/jobs` 必须调用 `service.list_jobs`（清扫在其中）。钉住：任务 1 `test_list_jobs_delegates_to_the_service_sweep_path`。
5. **字段白名单漂移：** Agent 路由必须与人工路由使用同一份 `RUN_FIELDS`；新增 v2 字段不能只进一边。钉住：任务 1 `test_run_payload_keeps_the_shared_whitelist_and_drops_unknown_keys` + 既有 `test_run_field_whitelist_matches_the_config_it_feeds`。
6. **作业 kind 串用：** 把回测作业 id 传给 mining 的 get/cancel 必须 404，不能返回或取消别的功能作业。钉住：服务层既有 `test_get_job_hides_another_features_job`，任务 1 的 404 路由测试。

## 文件结构

- 修改 `backend_api_python/app/routes/factor_mining.py`：`_RUN_FIELDS` 改名 `RUN_FIELDS`，`_run_payload` 引用更新。
- 创建 `backend_api_python/app/routes/agent_v1/factor_mining.py`：9 个 Agent 路由。
- 修改 `backend_api_python/app/routes/agent_v1/__init__.py`：注册新模块。
- 修改 `backend_api_python/app/services/factor_mining/service.py`：`submit` 增加 `agent_token_id`。
- 修改 `backend_api_python/tests/test_factor_mining_api.py`、`test_factor_mining_service.py`：引用与 token 归属测试。
- 创建 `backend_api_python/tests/test_agent_factor_mining.py`：Agent 路由契约测试。
- 修改 `docs/agent/agent-openapi.json`：9 个路径 + `FactorMiningRunRequest` schema。
- 修改 `mcp_server/src/quantdinger_mcp/server.py`：9 个工具 + `MCP_TOOL_NAMES`。
- 修改 `mcp_server/src/quantdinger_mcp/tool_contract.py`：写工具注解。
- 修改 `backend_api_python/app/services/ai_tool_registry.py`：MCP 扩展路由、写标记、research 分类。
- 修改 `mcp_server/tests/test_security_and_tools.py`：工具数 58 → 67。
- 修改文档：`docs/agent/README(_CN).md`、`AGENT_QUICKSTART(_CN).md`、`MCP_SETUP(_CN).md`、`mcp_server/README.md`、`docs/product/FACTOR_MINING(_CN).md`。

---

### 任务 1：Agent Gateway 因子挖掘路由与 OpenAPI

**文件：**
- 创建：`backend_api_python/app/routes/agent_v1/factor_mining.py`
- 修改：`backend_api_python/app/routes/agent_v1/__init__.py`
- 修改：`backend_api_python/app/routes/factor_mining.py:31-42`
- 修改：`backend_api_python/app/services/factor_mining/service.py:131-166`
- 修改：`backend_api_python/tests/test_factor_mining_api.py:193-194`
- 修改：`backend_api_python/tests/test_factor_mining_service.py`（新增一个测试）
- 创建：`backend_api_python/tests/test_agent_factor_mining.py`
- 修改：`docs/agent/agent-openapi.json`

- [ ] **步骤 1：编写失败的 Agent 路由测试**

创建 `backend_api_python/tests/test_agent_factor_mining.py`，使用下面的夹具。`_reserve_idempotency` / `_complete_idempotency` 必须显式打桩，否则 `B` 路由会访问 PostgreSQL 的幂等表。

```python
import pytest

from app.routes.agent_v1 import factor_mining as route
from app.utils import agent_auth


def _token(scopes: str = "R,B") -> dict:
    return {
        "id": 77, "user_id": 31, "name": "factor-agent", "scopes": scopes,
        "markets": "*", "instruments": "*", "paper_only": True,
        "rate_limit_per_min": 100, "status": "active", "expires_at": None,
    }


def _headers(key: str | None = None) -> dict:
    headers = {"Authorization": "Bearer qd_agent_FACTORTEST12345"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


class _StubService:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = {}

    def _reply(self, name, **kwargs):
        self.calls[name] = kwargs
        value = self.responses.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def estimate(self, payload):
        return self._reply("estimate", payload=payload)

    def submit(self, *, user_id, payload, agent_token_id=None):
        return self._reply(
            "submit", user_id=user_id, payload=payload, agent_token_id=agent_token_id,
        )

    def get_job(self, job_id, *, user_id):
        return self._reply("get_job", job_id=job_id, user_id=user_id)

    def get_job_proposals(self, job_id, *, user_id, limit=50, offset=0):
        return self._reply(
            "get_job_proposals", job_id=job_id, user_id=user_id, limit=limit, offset=offset,
        )

    def list_jobs(self, *, user_id, limit):
        return self._reply("list_jobs", user_id=user_id, limit=limit)

    def cancel_job(self, job_id, *, user_id):
        return self._reply("cancel_job", job_id=job_id, user_id=user_id)

    def list_candidates(self, *, user_id, run_id=None, status="", market="", limit=50, offset=0):
        return self._reply(
            "list_candidates", user_id=user_id, run_id=run_id, status=status,
            market=market, limit=limit, offset=offset,
        )

    def get_candidate(self, candidate_id, *, user_id):
        return self._reply("get_candidate", candidate_id=candidate_id, user_id=user_id)


@pytest.fixture(autouse=True)
def _authorized(monkeypatch):
    token = _token()
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _raw: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(agent_auth, "_reserve_idempotency", lambda *_: ("reserved", None))
    monkeypatch.setattr(agent_auth, "_complete_idempotency", lambda *_: None)
    yield token
    agent_auth._rate_state.clear()


@pytest.fixture
def service(monkeypatch):
    def install(**responses):
        stub = _StubService(**responses)
        monkeypatch.setattr(route, "get_factor_mining_service", lambda: stub)
        return stub
    return install
```

权限测试通过修改 autouse 夹具产出的 token 来切换 scope：

```python
def test_submit_requires_b_scope(client, service, _authorized):
    stub = service(submit={"jobId": "job-1", "status": "queued"})
    _authorized["scopes"] = "R"
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-1"),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31"},
    )
    assert response.status_code == 403
    assert "submit" not in stub.calls
```

写入这些测试（断言按下面写）：

```python
def test_catalog_is_read_scope_and_returns_the_dsl_vocabulary(client):
    body = client.get("/api/agent/v1/factor-mining/catalog", headers=_headers()).get_json()
    assert body["code"] == 0
    assert set(body["data"]) == {"fields", "operators", "templates"}


def test_estimate_is_read_scope_and_needs_no_idempotency_key(client, service):
    stub = service(estimate={"templateCount": 8, "candidateBudget": 40})
    response = client.post(
        "/api/agent/v1/factor-mining/estimate",
        headers=_headers(),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31", "unknown": True},
    )
    assert response.status_code == 200
    assert stub.calls["estimate"]["payload"]["sourceId"] == 1
    assert "unknown" not in stub.calls["estimate"]["payload"]


def test_submit_requires_an_idempotency_key(client, service):
    service(submit={"jobId": "job-1", "status": "queued"})
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers(),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31"},
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.get_json()["message"]


def test_submit_returns_202_and_records_the_agent_token(client, service):
    stub = service(submit={"jobId": "job-1", "status": "queued"})
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-2"),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31", "unknown": True},
    )
    assert response.status_code == 202
    assert response.get_json()["data"]["jobId"] == "job-1"
    assert stub.calls["submit"]["user_id"] == 31
    assert stub.calls["submit"]["agent_token_id"] == 77
    assert "unknown" not in stub.calls["submit"]["payload"]


def test_submit_surfaces_the_service_key_as_400(client, service):
    service(submit=ValueError("factorMining.sourceRequired"))
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-3"),
        json={"market": "USStock"},
    )
    assert response.status_code == 400
    assert response.get_json()["message"] == "factorMining.sourceRequired"


def test_list_jobs_delegates_to_the_service_sweep_path(client, service):
    stub = service(list_jobs=[{"jobId": "job-1", "status": "succeeded"}])
    response = client.get(
        "/api/agent/v1/factor-mining/jobs?limit=5", headers=_headers(),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["items"][0]["jobId"] == "job-1"
    assert stub.calls["list_jobs"] == {"user_id": 31, "limit": 5}


def test_get_job_returns_404_when_the_service_has_no_row(client, service):
    service(get_job=None)
    response = client.get(
        "/api/agent/v1/factor-mining/jobs/job-404", headers=_headers(),
    )
    assert response.status_code == 404
    assert response.get_json()["message"] == "factorMining.jobNotFound"


def test_proposals_are_tenant_scoped_and_paged(client, service):
    stub = service(get_job_proposals=[{"id": 1, "round": 0, "ordinal": 0}])
    response = client.get(
        "/api/agent/v1/factor-mining/jobs/job-1/proposals?limit=10&offset=2",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["items"][0]["id"] == 1
    assert stub.calls["get_job_proposals"] == {
        "job_id": "job-1", "user_id": 31, "limit": 10, "offset": 2,
    }


def test_proposals_return_404_for_an_unknown_job(client, service):
    service(get_job_proposals=None)
    response = client.get(
        "/api/agent/v1/factor-mining/jobs/job-404/proposals", headers=_headers(),
    )
    assert response.status_code == 404
    assert response.get_json()["message"] == "factorMining.jobNotFound"


def test_cancel_requires_b_scope(client, service, _authorized):
    stub = service(cancel_job={"jobId": "job-1", "status": "cancelled"})
    _authorized["scopes"] = "R"
    response = client.post(
        "/api/agent/v1/factor-mining/jobs/job-1/cancel", headers=_headers("cancel-1"),
    )
    assert response.status_code == 403
    assert "cancel_job" not in stub.calls


def test_candidates_forward_filters_and_reject_a_non_integer_run_id(client, service):
    stub = service(list_candidates=[{"id": 5}])
    response = client.get(
        "/api/agent/v1/factor-mining/candidates?runId=7&status=frozen&market=USStock&limit=10",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert stub.calls["list_candidates"] == {
        "user_id": 31, "run_id": 7, "status": "frozen",
        "market": "USStock", "limit": 10, "offset": 0,
    }
    bad = client.get(
        "/api/agent/v1/factor-mining/candidates?runId=abc", headers=_headers(),
    )
    assert bad.status_code == 400
    assert bad.get_json()["message"] == "factorMining.invalidQuery"


def test_candidate_with_a_non_integer_id_is_404(client, service):
    service(get_candidate={"id": 5})
    response = client.get(
        "/api/agent/v1/factor-mining/candidates/not-an-int", headers=_headers(),
    )
    assert response.status_code == 404
    assert response.get_json()["message"] == "factorMining.candidateNotFound"


def test_run_payload_keeps_the_shared_whitelist_and_drops_unknown_keys(client, service):
    stub = service(submit={"jobId": "job-1", "status": "queued"})
    client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-4"),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31", "searchMode": "evolution",
              "population": 40, "generations": 5, "eliteRatio": 0.2,
              "aiRounds": 2, "aiProposalsPerRound": 10,
              "researchNotes": "note", "replayJobId": "job-0",
              "unknown": True, "__userId": 999},
    )
    assert stub.calls["submit"]["payload"] == {
        "sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
        "endDate": "2023-12-31", "searchMode": "evolution",
        "population": 40, "generations": 5, "eliteRatio": 0.2,
        "aiRounds": 2, "aiProposalsPerRound": 10,
        "researchNotes": "note", "replayJobId": "job-0",
    }
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_agent_factor_mining.py -q`
预期：FAIL，`ModuleNotFoundError` 或 404（路由未注册）。

- [ ] **步骤 3：把人工路由的白名单改为公共 `RUN_FIELDS`**

在 `backend_api_python/app/routes/factor_mining.py` 中把 `_RUN_FIELDS` 改名为 `RUN_FIELDS`，`_run_payload` 引用同步；更新 `tests/test_factor_mining_api.py` 的两处 `mining_routes._RUN_FIELDS` 引用，以及 `tests/test_factor_mining_service.py:272` 的 docstring 措辞。

- [ ] **步骤 4：实现 `FactorMiningService.submit` 的 token 归属**

`app/services/factor_mining/service.py`：

```python
def submit(
    self, *, user_id: int, payload: Mapping[str, Any],
    agent_token_id: int | None = None,
) -> dict[str, Any]:
    ...
    receipt = agent_jobs.submit_job(
        user_id=int(user_id),
        agent_token_id=agent_token_id,
        kind=JOB_KIND,
        request_payload=stored,
        runner=self._run_mining_job,
    )
```

在 `tests/test_factor_mining_service.py` 增加：

```python
def test_submit_records_the_agent_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mining_service.agent_jobs, "submit_job",
        lambda **kwargs: captured.update(kwargs) or {"job_id": "job-1", "status": "queued"},
    )
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 0)
    FactorMiningService().submit(user_id=4, payload=_payload(), agent_token_id=9)
    assert captured["agent_token_id"] == 9
```

- [ ] **步骤 5：创建 Agent 路由模块**

`backend_api_python/app/routes/agent_v1/factor_mining.py` 的内容要点：

```python
"""Agent Gateway factor-mining surface: the same service the human API uses."""
from __future__ import annotations

from flask import g, request

from app.routes.factor_mining import RUN_FIELDS
from app.services.factor_mining import dsl, templates
from app.services.factor_mining.service import get_factor_mining_service
from app.utils.agent_auth import SCOPE_B, SCOPE_R, agent_required, current_user_id

from . import agent_v1_bp
from ._helpers import clip_int, envelope, error, get_json_or_400


def _run_payload() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {}
    return {key: value for key, value in body.items() if key in RUN_FIELDS}
```

9 个路由（装饰器、scope、返回形状必须按此写）：

| 路由 | scope | 行为 |
| --- | --- | --- |
| `GET /factor-mining/catalog` | `R` | 返回 `{fields, operators, templates}`；`fields` 用 `dsl.fields_for(request.args.get("market") or "")` |
| `POST /factor-mining/estimate` | `R` | `get_json_or_400()` 后切白名单，调用 `service.estimate(payload)`；`ValueError` → `error(400, str(exc))` |
| `POST /factor-mining/jobs` | `B` | 切白名单，调用 `service.submit(user_id=current_user_id(), payload=..., agent_token_id=int(g.agent_token["id"]))`；成功 `envelope(data, status=202)`；`ValueError` → 400 |
| `GET /factor-mining/jobs` | `R` | `limit = clip_int(..., default=20, lo=1, hi=50)`；`envelope({"items": service.list_jobs(user_id=..., limit=limit)})` |
| `GET /factor-mining/jobs/<job_id>` | `R` | `service.get_job(...)`；`None` → `error(404, "factorMining.jobNotFound", http=404)` |
| `POST /factor-mining/jobs/<job_id>/cancel` | `B` | `service.cancel_job(...)`；`None` → 404；成功 `envelope(data, message="cancelled")` |
| `GET /factor-mining/jobs/<job_id>/proposals` | `R` | `service.get_job_proposals(job_id, user_id=..., limit=clip(1-200), offset>=0)`；`None` → 404 `factorMining.jobNotFound`；成功 `envelope({"items": [...]})` |
| `GET /factor-mining/candidates` | `R` | 解析 `runId`（非法 → 400 `factorMining.invalidQuery`）、`status`、`market`、`limit`(1-200)、`offset`(≥0)；`envelope({"items": ...})` |
| `GET /factor-mining/candidates/<candidate_id>` | `R` | 非整数或查无 → 404 `factorMining.candidateNotFound`；成功 `envelope(data)` |

`catalog` 路由不经过服务层，直接组合 `dsl.fields_for`、`dsl.OPERATOR_SPECS`、`templates.template_catalog()`，与人工路由保持同一份词表来源。

- [ ] **步骤 6：注册蓝图**

在 `app/routes/agent_v1/__init__.py` 的 `register` 中加入 `from . import factor_mining  # noqa: F401`（放在 `research` 之后、`trading_data` 之前均可）。

- [ ] **步骤 7：运行路由测试验证通过**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_agent_factor_mining.py tests/test_factor_mining_api.py tests/test_factor_mining_service.py -q`
预期：PASS。

- [ ] **步骤 8：同步 Agent OpenAPI**

在 `docs/agent/agent-openapi.json` 的 `paths` 增加上表 9 个路径；每个操作写 `summary`、`x-scope-class`、必要的 `parameters`、`requestBody`、`responses`。`POST jobs` 与 `POST jobs/{job_id}/cancel` 必须声明必填 `Idempotency-Key` 头，`POST jobs` 返回 202。

在 `components.schemas` 增加：

```json
"FactorMiningRunRequest": {
  "type": "object",
  "additionalProperties": false,
  "description": "Normal runs require sourceId, market, startDate, and endDate. AI replay requires searchMode=\"ai\" and replayJobId instead.",
  "properties": {
    "sourceId": {"type": "integer", "minimum": 1},
    "sourceName": {"type": "string"},
    "market": {"type": "string"},
    "timeframe": {"type": "string", "default": "1D"},
    "startDate": {"type": "string", "format": "date"},
    "endDate": {"type": "string", "format": "date"},
    "freezeRatio": {"type": "number"},
    "budget": {"type": "integer", "minimum": 1},
    "seed": {"type": "integer"},
    "templateIds": {"type": "array", "items": {"type": "string"}},
    "groups": {"type": "integer"},
    "holdingPeriod": {"type": "integer"},
    "commission": {"type": "number"},
    "slippage": {"type": "number"},
    "neutralizeIndustry": {"type": "boolean"},
    "thresholds": {"type": "object"},
    "searchMode": {"type": "string", "enum": ["enumeration", "evolution", "ai"]},
    "population": {"type": "integer", "minimum": 1},
    "generations": {"type": "integer", "minimum": 1},
    "eliteRatio": {"type": "number", "minimum": 0, "exclusiveMaximum": 1},
    "aiRounds": {"type": "integer", "minimum": 1, "maximum": 3},
    "aiProposalsPerRound": {"type": "integer", "minimum": 1, "maximum": 20},
    "researchNotes": {"type": "string", "maxLength": 8000},
    "replayJobId": {"type": "string"}
  }
}
```

- [ ] **步骤 9：运行契约对齐测试**

运行：`cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_ai_agent_contract_alignment.py -q`
预期：PASS（注册路由集合与 OpenAPI 路径集合相等）。

- [ ] **步骤 10：Commit**

```bash
git add backend_api_python/app/routes/agent_v1/factor_mining.py \
        backend_api_python/app/routes/agent_v1/__init__.py \
        backend_api_python/app/routes/factor_mining.py \
        backend_api_python/app/services/factor_mining/service.py \
        backend_api_python/tests/test_agent_factor_mining.py \
        backend_api_python/tests/test_factor_mining_api.py \
        backend_api_python/tests/test_factor_mining_service.py \
        docs/agent/agent-openapi.json
git commit -m "feat: 为 Agent Gateway 暴露因子挖掘接口"
```

---

### 任务 2：MCP 工具与注册表

**文件：**
- 修改：`mcp_server/src/quantdinger_mcp/server.py`
- 修改：`mcp_server/src/quantdinger_mcp/tool_contract.py`
- 修改：`backend_api_python/app/services/ai_tool_registry.py`
- 修改：`mcp_server/tests/test_security_and_tools.py:33`

- [ ] **步骤 1：编写失败的 MCP 测试**

先安装 MCP 依赖（当前 `.venv` 未安装 `mcp`）：`python -m pip install -e './mcp_server[dev]'`。
把 `mcp_server/tests/test_security_and_tools.py` 的 `assert len(fresh_module.MCP_TOOL_NAMES) == 58` 改为 `== 67`。运行：`python -m pytest mcp_server/tests/test_security_and_tools.py -q`
预期：FAIL，断言 `58 == 67`。

- [ ] **步骤 2：在 `server.py` 注册 9 个工具**

在 `MCP_TOOL_NAMES` 加入：

```python
"get_factor_mining_catalog",
"estimate_factor_mining",
"submit_factor_mining_run",
"list_factor_mining_jobs",
"get_factor_mining_job",
"list_factor_mining_proposals",
"cancel_factor_mining_job",
"list_factor_mining_candidates",
"get_factor_mining_candidate",
```

在工具实现区加入（保留现有 `_get` / `_post` / `assert_json_dict` 风格）：

```python
@_tool
def get_factor_mining_catalog(market: str = "") -> Any:
    """List factor-mining fields, operators, and hypothesis templates."""
    params = {"market": market} if market else {}
    return _get("/api/agent/v1/factor-mining/catalog", params=params)


@_tool
def estimate_factor_mining(payload: dict) -> Any:
    """Price a factor-mining run without reading data or opening a job."""
    return _post(
        "/api/agent/v1/factor-mining/estimate",
        json=assert_json_dict("payload", payload),
    )


@_tool
def submit_factor_mining_run(payload: dict, idempotency_key: str) -> Any:
    """Queue a factor-mining run (scope B; one active run per tenant)."""
    return _post(
        "/api/agent/v1/factor-mining/jobs",
        json=assert_json_dict("payload", payload),
        headers=_idempotency_headers(idempotency_key),
    )


@_tool
def list_factor_mining_jobs(limit: int = 20) -> Any:
    """List the tenant's factor-mining jobs, newest first."""
    return _get(
        "/api/agent/v1/factor-mining/jobs",
        params={"limit": max(1, min(50, int(limit)))},
    )


@_tool
def get_factor_mining_job(job_id: str) -> Any:
    """Fetch one factor-mining job, including its full result when finished."""
    return _get(f"/api/agent/v1/factor-mining/jobs/{job_id}")


@_tool
def list_factor_mining_proposals(job_id: str, limit: int = 50, offset: int = 0) -> Any:
    """List the AI proposals one factor-mining run recorded, in generation order."""
    return _get(
        f"/api/agent/v1/factor-mining/jobs/{job_id}/proposals",
        params={
            "limit": max(1, min(200, int(limit))),
            "offset": max(0, int(offset)),
        },
    )


@_tool
def cancel_factor_mining_job(
    job_id: str,
    idempotency_key: str = "",
    confirm_cancel: bool = False,
) -> Any:
    """Cancel one of the tenant's factor-mining jobs (requires explicit confirmation)."""
    if not confirm_cancel:
        return {
            "error": True,
            "status": 400,
            "body": {"message": "Re-call with confirm_cancel=true after explicit user approval."},
        }
    return _post(
        f"/api/agent/v1/factor-mining/jobs/{job_id}/cancel",
        headers=_idempotency_headers(idempotency_key),
    )


@_tool
def list_factor_mining_candidates(
    run_id: int | None = None,
    status: str = "",
    market: str = "",
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """List the tenant's mined factor candidates with filters and paging."""
    params: dict[str, Any] = {
        "limit": max(1, min(200, int(limit))),
        "offset": max(0, int(offset)),
    }
    if run_id is not None:
        params["runId"] = int(run_id)
    if status:
        params["status"] = status
    if market:
        params["market"] = market
    return _get("/api/agent/v1/factor-mining/candidates", params=params)


@_tool
def get_factor_mining_candidate(candidate_id: int) -> Any:
    """Fetch one mined candidate with metrics and out-of-sample review."""
    return _get(f"/api/agent/v1/factor-mining/candidates/{int(candidate_id)}")
```

- [ ] **步骤 3：更新 `tool_contract.WRITE_TOOLS`**

在 `WRITE_TOOLS` 增加：

```python
"submit_factor_mining_run": False,
"cancel_factor_mining_job": True,
```

`submit` 是写但不破坏；`cancel` 与既有 `cancel_job` 一样标记为 destructive。

- [ ] **步骤 4：更新 `ai_tool_registry` 扩展表**

在 `_MCP_EXTENSION_ROUTES` 增加 9 个名字到路由的映射；在 `_MCP_EXTENSION_WRITES` 增加 `submit_factor_mining_run` 与 `cancel_factor_mining_job`。把分类表达式改为：

```python
"research" if name in {
    "list_universes", "get_universe", "list_universe_members",
    "list_factors", "get_factor", "list_watchlist", "add_watchlist",
    "remove_watchlist",
} or "factor_mining" in name
```

不要新增 category 字符串，复用 `research`。

- [ ] **步骤 5：运行 MCP 与对齐测试**

运行：

```bash
python -m pip install -e './mcp_server[dev]'
python -m pytest mcp_server/tests -q
cd backend_api_python && .venv/Scripts/python.exe -m pytest tests/test_ai_agent_contract_alignment.py -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add mcp_server/src/quantdinger_mcp/server.py \
        mcp_server/src/quantdinger_mcp/tool_contract.py \
        mcp_server/tests/test_security_and_tools.py \
        backend_api_python/app/services/ai_tool_registry.py
git commit -m "feat: 通过 MCP 暴露因子挖掘工具"
```

---

### 任务 3：文档（中英同步）

**文件：**
- 修改：`docs/agent/README.md`、`docs/agent/README_CN.md`
- 修改：`docs/agent/AGENT_QUICKSTART.md`、`docs/agent/AGENT_QUICKSTART_CN.md`
- 修改：`docs/agent/MCP_SETUP.md`、`docs/agent/MCP_SETUP_CN.md`
- 修改：`mcp_server/README.md`
- 修改：`docs/product/FACTOR_MINING.md`、`docs/product/FACTOR_MINING_CN.md`

- [ ] **步骤 1：更新 scope 说明**

`docs/agent/README.md` / `README_CN.md` 的 scope 表把 `B` 的说明从「Backtest」/「回测」扩为「Backtest / simulation compute」/「回测与仿真计算」，并在能力列表加一行：因子挖掘提交/取消属于 `B`，词表、估价、作业与候选读取属于 `R`。

- [ ] **步骤 2：Quickstart 增加因子挖掘章节**

`AGENT_QUICKSTART.md` / `_CN.md` 增加「Factor mining / 因子挖掘」一节，列出 9 个 `/api/agent/v1/factor-mining/*` 路径、`R`/`B` 映射、提交必须带 `Idempotency-Key`、取消 MCP 工具需要 `confirm_cancel=true`、同一用户同时只能有一个活跃挖掘作业、`budget` 有硬上限。英文与中文内容对应。

- [ ] **步骤 3：MCP_SETUP 区分 PyPI 与仓库头**

把 `docs/agent/MCP_SETUP.md` / `_CN.md` 的「PyPI 0.6.2 = 58 tools」改为事实性表述：PyPI `0.6.2` 仍是 58 个工具；仓库头新增 9 个因子挖掘工具，共 67 个，发布前用 `pip install -e ./mcp_server` 获取。不要改 `pyproject.toml` 版本号。

- [ ] **步骤 4：MCP README 能力表**

在 `mcp_server/README.md` 的 Tool surface 表增加一行：`Factor mining tools`，scope `R/B`，用途 `mining catalog, estimates, runs, and candidate library`。

- [ ] **步骤 5：产品文档补充 Agent 访问口径**

在 `docs/product/FACTOR_MINING.md` / `_CN.md` §7「HTTP 接口」末尾增加一小节：Agent Gateway 镜像同一套 9 个操作（8 个唯一路径）到 `/api/agent/v1/factor-mining/*`，scope 映射为 R/B，响应使用 Agent 信封（`code/message/data`），作业对象形状与人工面 `_public_job` 一致。说明通用 `/api/agent/v1/jobs*` 仍可读取任意 kind，但 mining 的推荐路径是专用接口。AI 运行结束后，提案用 `GET /api/agent/v1/factor-mining/jobs/<id>/proposals` 读取。

- [ ] **步骤 6：运行文档检查**

运行：`python scripts/check_docs.py`
预期：`Documentation structure, links, code fences, and assets are valid.`

- [ ] **步骤 7：Commit**

```bash
git add docs/agent/README.md docs/agent/README_CN.md \
        docs/agent/AGENT_QUICKSTART.md docs/agent/AGENT_QUICKSTART_CN.md \
        docs/agent/MCP_SETUP.md docs/agent/MCP_SETUP_CN.md \
        mcp_server/README.md \
        docs/product/FACTOR_MINING.md docs/product/FACTOR_MINING_CN.md
git commit -m "docs: 补充因子挖掘的 Agent 与 MCP 访问口径"
```

---

### 任务 4：全量验证

- [ ] **步骤 1：后端聚焦回归**

```bash
cd backend_api_python
.venv/Scripts/python.exe -m pytest tests/test_agent_factor_mining.py \
  tests/test_factor_mining_api.py tests/test_factor_mining_service.py \
  tests/test_ai_agent_contract_alignment.py tests/test_agent_v1_full_surface.py \
  tests/test_agent_security.py -q
```

预期：全部 PASS。

- [ ] **步骤 2：MCP 回归**

```bash
python -m pytest mcp_server/tests -q
```

预期：全部 PASS。

- [ ] **步骤 3：文档检查**

```bash
python scripts/check_docs.py
```

预期：通过。

- [ ] **步骤 4：记录未覆盖项**

在最终报告中写明：未做真实数据库端到端提交（需要运行中的 PostgreSQL 与数据源）；未发布 PyPI；未验证 Agent 在真实 MCP 客户端中的完整对话流程。残余风险按「评估结论」中的四条陈述。
