# 测试网验证 Runbook（本次五条修复）

> 本文是 **fork 本地文档**，用于把五条上游修复从"静态 + 单测已验证"推进到"真机已验证"。上游不需要它，因此只有中文版。

## 0. 覆盖范围与原则

| 修复 | 能否离线验证 | 真机需要什么 |
| --- | --- | --- |
| #279 凭据脱敏 | 否（要看真实异常文本） | 一个**故意无效**的 LLM key |
| #281 连接探测 | 否 | 一个可用的 LLM key + 非 openrouter 的 `LLM_PROVIDER` |
| #229 `PROXY_URL` | 否 | 一个能到交易所的代理（或模拟直连失败） |
| #274 平仓对账 / 59669 | 否 | **demo/testnet 的 swap 账户**（OKX 最佳） |
| #200 IBKR 会话 | 部分（`--selftest` 已离线验证） | **TWS / IB Gateway**（paper 优先） |

原则：**只用 demo / testnet / paper**；每步先只读、后写入；不碰真实资金。

**前置**：`docker compose up -d` 起全栈（或本机 backend + PG + Redis），并跑迁移：

```bash
cd backend_api_python && QD_PROCESS_ROLE=migration python -m app.commands.migrate
```

观察面：`backend` 与 `trading-worker` 的日志 + 数据库查询。记录格式见 §7。

---

## 1. #279 凭据脱敏

1. 配置一个**无效的** OpenRouter 或 Google key（系统设置 → AI/LLM）。
2. 触发一次会失败的 AI 调用：`POST /api/indicator/aiGenerate`，或 AI 聊天发一句话。
3. 同时看 **backend 日志**与 **HTTP 响应体**。

**期望证据**
- 日志中形如 `...:generateContent?key=<redacted>` 或 `Authorization: Bearer <redacted>`；
- 500 响应的 `msg` 字段中**没有** key 原文（只应有 `<redacted>` 或状态码）。

**失败长什么样**：任一处出现 `key=AIza...` / 原始 token → 回归。
（本分支还额外验证过：Gemini 走 `x-goog-api-key` 头，URL 里不再有 key；两处裸 `raise` 已改经 `_scrubbed_request_error()`。）

---

## 2. #281 连接探测

1. 设 `LLM_PROVIDER=deepseek`（或任一**非** openrouter 的值）并配置对应 key。
2. 管理员界面点 **Test OpenRouter connection**，或直接：
   `POST /api/settings/test-connection` body `{"service": "openrouter"}`

**期望证据**：`msg` 以 `openrouter:` 开头；抓包看到请求打到 `https://openrouter.ai/api/v1/auth/key`（不是公开的 `/models`）。

**失败长什么样**：`msg` 里出现 `deepseek` 字样 → provider 参数没传（这正是本分支修的缺陷）。

**附带**：让模型调用失败后调 `aiGenerate`，流式 `debug` 帧应带 `generation_error`，不再是"静默换模板 + 宣称通过校验"。

---

## 3. #229 `PROXY_URL`

1. 设 `PROXY_URL=<你的代理>`，**不要**设 `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`。
2. 让 `trading-worker` 发起一次**私有** REST（触发一次持仓同步或下一笔订单）。

**期望证据**：worker 侧请求经代理成功；worker 日志无 `ConnectionResetError(104)`；代理侧日志能看到该请求。

**失败长什么样**：worker 仍直连（代理日志无记录）→ 回归。
反向确认（无需交易所）：不设 `PROXY_URL` 时标准代理变量仍生效——本分支已在 `requests` adapter 层实测 `proxies=None` 与省略参数等价。

⚠️ 兼容性提醒：`PROXY_URL` 若指向"只服务行情、到不了交易所"的代理，实盘会从"部分可用"变成"不可用"。这是文档承诺的行为，不是 bug。

---

## 4. #274 平仓对账与 59669（demo swap）

**前置**：临时开启清腿（**默认是关闭的**）：

```
POSITION_SYNC_PURGE_FLAT_LEDGER=true
POSITION_SYNC_PURGE_GRACE_SEC=120
OKX_CANCEL_ALL_ALGO_ORDERS=false        # 本步骤要求保持 false
```

### 4A 交易所侧平仓能被对账

1. 部署一个带原生保护的策略：配置里 `protection.stop_loss_pct`（或 `take_profit_pct` / `trailing_stop_pct`）设一个小值。
2. demo 账户开一笔小仓，等交易所侧触发（或直接在交易所手动平掉）。
3. 观察日志与 DB。

**期望证据**
- 日志：`[PositionSync] Strategy N recorded external flat close trade_id=... for SYMBOL side`
- `qd_strategy_positions` 里 `(strategy_id, symbol, side)` 行**消失**
- `qd_strategy_trades` 新增 `type='close_long'|'close_short'` 且 `close_reason` 为 `exchange_native_close`（或 `exchange_flat_reconcile`）的行，`amount` 与残留量一致

**失败长什么样**：腿被删但**没有**对应 `close_*` 行 → H3 回归（这正是本分支修掉的默认行为）。

### 4B 手动算法单不被误伤

1. 在 demo 账户给某标的挂一个**手动**移动止损。
2. 让策略在同一标的下一笔开仓单（`OKX_CANCEL_ALL_ALGO_ORDERS` 保持 `false`）。
3. 查询交易所挂单。

**期望证据**：手动止损**仍在**。
若把该开关设为 `true` 再重复：手动止损会被取消——那是开关的语义，不是 bug。

### 4C 缓存快照不触发清腿

设 `POSITION_SYNC_INTERVAL_SEC=300`、`POSITION_SYNC_PURGE_GRACE_SEC=120`，制造"持仓 + 开仓已超宽限期 + 快照来自缓存"。

**期望证据**：日志出现 `[PositionSync] Strategy N skips flat-leg purge: snapshot served from cache.`，腿**保留**。

### 4D 59669 恢复

1. 在 OKX demo 给某标的挂一张 condition 单。
2. 让需要设置杠杆的策略对**同一标的**开仓。

**期望证据**：`OKX error 59669 (attempt 1/4)` → `Cancelled N algo order(s) for <instId>` → 后续成功；且**只**影响该 `instId`（`OKX_CANCEL_ALL_ALGO_ORDERS=false` 时不得出现跨合约清扫）。

---

## 5. #200 IBKR（paper 优先）

1. **离线**（已在本机验证过，可复跑）：
   `python scripts/ibkr_probe_session_ownership.py --selftest` → 期望 `"process_private": true`
2. **有网关时**：
   `python scripts/ibkr_probe_session_ownership.py --host <host> --port 4002 --client-id 99`
   记录 `verdict`（`replaces` / `rejects` / `coexists`）、`request_timeout`、`server_version`。
3. **跨 worker**：`GUNICORN_WORKERS=2` 起 backend，两个用户（凭证指向同一网关）分别 connect：
   - `GET /api/ibkr/status` 应稳定（不来回翻转）；
   - `GET /api/ibkr/account` 必须返回**各自的**账号（H1 验收点）。
4. **只读不被提权**：以 `readonly=true` 连接后让会话失效（重启 gateway），再请求 `/api/ibkr/account` —— 隐式重连必须是只读的，尝试下单应被 IB 拒绝。

**失败长什么样**：能看到对方账户 → 跨租户回归；隐式重连后能下单 → 提权回归。

---

## 6. 通用只读冒烟（可选）

```bash
cd backend_api_python
python scripts/exchange_smoke_test.py --exchange bitget --market spot --symbol BTC/USDT --demo
```

默认只读；下单需**同时**给 `--allow-orders` 与 `EXCHANGE_SMOKE_ALLOW_ORDERS=1`。

---

## 7. 结果记录模板

```
日期: 2026-__-__   代码: main@<sha>
#279 [通过/失败]  证据: <日志片段或响应片段>
#281 [通过/失败]  证据:
#229 [通过/失败]  证据:
#274-A/B/C/D [通过/失败]  证据:
#200-1/2/3/4 [通过/失败]  证据:
未通过项 → 记入记忆并回头补回归测试；必要时在上游 PR 评论里跟进。
```

## 8. 收尾

- 恢复默认：`POSITION_SYNC_PURGE_FLAT_LEDGER` 与 `OKX_CANCEL_ALL_ALGO_ORDERS` 都改回 `false`（或直接删掉这两行）。
- 本文档不随上游 PR 提交；它只服务于本 fork 的验收。
