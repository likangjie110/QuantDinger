# 因子挖掘 P2 实施计划

规格：[P2 设计](../specs/2026-10-19-factor-mining-p2-design.md)。采用有文件所有权的并行实现，主代理负责契约、持久化与联调。用户已要求全部实施，按现有授权执行。

## 任务与所有权

| ID | 任务 | 文件所有权 | 验证目标 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 同 run L4 数值去重 | engine.py、相关 engine/pool 测试 | 两个不同指纹数值孪生仅留一个，trial 不丢 | implemented; regression verified |
| T2 | 分阶段评估 | engine.py、strategy_v2/factor_research.py、研究/引擎测试 | L2/L3 淘汰不执行后续阶段；完整模式与旧指标一致 | implemented; regression verified |
| T3 | mining 队列与容量隔离 | celery_app.py、utils/agent_jobs.py、Compose、队列测试、部署文档/env | mining 投递、其他队列兼容、Compose config | static verified; live consumer unverified |
| T4 | AI 整轮 deadline | proposals.py、llm.py、对应测试 | 重试/fallback 共用预算，耗尽停止，不触网 | unit and independent review verified |
| T5 | AI 上下文与终段失败反馈 | storage.py、service.py、反馈/服务测试 | 上下文隔离、L5/L6 计数、历史结果不随候选更新丢失 | unit, isolated PG and independent review verified |
| T6 | 研究组、累计试验、冻结版本、保留段 | 新 research.py、storage.py、service.py、迁移/init.sql；engine 由 T1/T2 owner 配合 | 同组累计且每run幂等，跨租户隔离，保留段不被读取 | unit and isolated PG verified |
| T7 | 主产品 evolution/replay/候选跳转和研究展示 | frontend-vue 因子模块、i18n、unit | payload 裁剪、重放最小请求、URL刷新一致 | scoped checks and business mock verified; full-page gates failed |
| T8 | 契约、模块文档与验收 | routes/factor_mining.py、docs/api/openapi.yaml、doc/web_doc、browser harness | API 对齐、真实启动截图/console/network/a11y、独立报告各层 | contract/docs verified; integration and deployment unverified |

## 已锁定接口

- engine owner 增加 `MiningConfig.holdout_ratio: float = 0.0`；`from_payload()` 读取 `holdoutRatio`，范围 0–0.30；`PeriodSplit` 可选 `holdout_start/end`；`split_periods()` 先留 holdout 再用 freezeRatio 分剩余区间。
- engine owner 增加 `FactorMiningEngine.freeze_evidence() -> dict`，返回 `freezeSnapshotId`、`freezeVersion`、`holdout`；review 使用 prepare_freeze 的真实指纹，holdout 仅提供边界且 evaluated=false。旧 config 与现有公开完整研究响应兼容。
- `AISearchOptions.history_context: Mapping[str, Any] | None = None`；service 提供上下文；engine 的 failure_summary 调用仅在 context 非空时传 `context=...`，兼容旧 fake。
- 主代理实现 `research_context(config) -> dict` 和 `FactorMiningRepository.register_experiment_trials(run_id, *, user_id, context, trials_count) -> dict`，返回 experimentId/contextHash/runTrials/cumulativeTrials；同一run重复登记不重复计数。
- service 在 save_trials 后、review_frozen 前登记累计次数，将累计值传 trials_count，复核后合并 `engine.freeze_evidence()` 到 `summary.research`。测试 doubles 必须实现新协作者契约，不以 hasattr 静默省略治理。
- 前端 request 新增可选 `holdoutRatio`；summary.research 如规格定义。重放只发送 AI 模式和 replayJobId。
- AI deadline owner 保持 fake proposer 兼容，真实 LLMProposer 接受单轮剩余时间，call_llm_api 的 fallback 使用同一 deadline。

## 原子执行与证据

每个实现任务：读取当前源码/影响调用链 → 写针对行为的测试 → 运行并记录预期失败 → 最小实现 → 聚焦验证 → 交给主代理检查 diff。不同 owner 不编辑同一个文件，跨接口变更先发消息。

后端统一使用 `backend_api_python/.venv/Scripts/python.exe -m pytest ... -q --timeout=90`。先运行相关文件，再跑因子挖掘全套、受影响研究/LLM/任务契约测试；无需重复已通过检查，除非后续改动影响它。

前端按实际锁文件使用 pnpm，运行 `lint:nofix`、`test:unit`、`build`。浏览器验收负责人独立于 UI 实现负责人；不以 mock 或现有截图代替新业务结果。

## Checkpoint / resume hint

- 当前：六项 P2 均已实现。T4 的两次代理均因服务容量错误无代码产出，已由主代理完成。包含传输修补和反馈分段反例的最终后端统一回归为 684 passed / 157 warnings；独立审查复核无新增阻断问题。用户已明确允许隔离测试 schema，真实 PG 16 项检查通过，测试 schema 清理为 0。
- Checkpoint：本轮六项代码实现与范围内验证已结束。原独立浏览器审查完成后，最后的提示/按钮修补与复验由主代理完成；不能称为第二次独立审查。最新真实SPA mock为12/12业务终态断言通过，扩展P2 selector违规0；全页axe70条规则记录仍failed。全仓lint/unit、缺失typecheck及真实后端联调均不升级为通过。后续联调、部署或全页a11y需使用对应隔离环境与新证据，不能引用本轮mock冒充。
- Drift check：六项都要有代码和验证；不扩展到 P1 或策略运行时；不改用户无关工作树。
- Evidence references：每次完成任务在下表登记实际命令、输出与未覆盖范围。

| 任务 | Red 证据 | Green / 联调证据 | 未覆盖项 |
| --- | --- | --- | --- |
| T5/T6 | research 3 failed；storage 4 failed/25 passed；service 2 failed；均为实现前行为缺口 | 聚焦181 passed；独立审查补测/LLM复核117 passed；OpenAPI独立Blueprint 1 passed；真实PG脚本16项通过 | 未在部署环境执行迁移或重启检查 |
| T3 | 15 failed/4 passed | 19 passed；扩展45 passed；8个Compose组合通过，详见队列验收记录 | 真实消费者、容量、部署未执行 |
| T7 | 5 passed/16 failed；真实页面axe复现新控件对比度失败 | 最终4模块34 passed；递归模块lint、build通过；业务mock桌面/移动12/12；扩展P2 selector违规0 | 全仓lint23 errors/1261 warnings；unit519 passed/1路径失败；typecheck缺失；全页axe70条规则记录仍failed |
| T1/T2 | P2 行为测试 23 failed | 聚焦31 passed；相关187 passed；5组公共完整响应对照一致 | 未用真实行情源做性能压测 |
| T4 | deadline新增4 failed；审查补测重定向/SDK4 failed、初始化耗尽1 failed | 最终 proposals/LLM 两文件84 passed；HTTP直连/显式代理均禁自动重定向，SDK不重复重试 | 无真实provider验证；不保证强制抢占单个阻塞I/O |
| 统一后端 | 各任务的red见上 | 最终684 passed，157条NumPy相关度警告；范围内Ruff通过 | 无真实消费者、provider、性能压测或部署 |

统一回归命令（`backend_api_python/`）：

```powershell
$factorTests = @(rg --files tests -g 'test_factor_mining*.py')
.\.venv\Scripts\python.exe -m pytest @factorTests tests/test_factor_research.py tests/test_llm_litellm_provider.py tests/test_celery_worker_readiness.py tests/test_celery_boundaries.py tests/test_agent_jobs_progress.py tests/test_agent_jobs_failure.py tests/test_agent_backtest_billing.py -q --timeout=90 --tb=short
.\.venv\Scripts\python.exe -m pytest tests/test_llm_litellm_provider.py tests/test_factor_mining_proposals.py -q --timeout=60 --tb=short
```

独立审查确认 T5/T6 未发现其他重要实现缺陷，已补齐 startDate/endDate/freezeRatio/holdoutRatio 不匹配反例；单元测试核对绑定参数，真实 PG 验证实际行排除。T4 原请求可在虚拟时间0/40/80秒沿重定向发出三次请求，修补后的离线adapter断言只发送一次；独立复核确认该缺口已消除。HTTP发送前和响应后均校验绝对deadline，重定向显式失败；未提供预算的旧调用维持兼容。

架构漂移检查：仅新增研究组持久化职责与 `mining` worker 容量，不增加交易 owner；沿用现有模型/provider fallback，传入同一deadline。公共完整研究响应、旧请求两段默认值和独立兼容UI均保留。

验证边界事件：新增 OpenAPI 测试首次复用旧 app fixture，create_app 连接本机数据库并记录升级1个内置指标样例；已向用户说明。新增测试改用独立 Blueprint，后续 spec 导出显式 mock `_bootstrap_database` 和 `run_startup_hooks`，避免启动维护；未回滚数据库内容。后续隔离 schema 写验证已获授权并执行。事后只读查询显示测试 schema 剩余0、public中两张研究表存在；该查询不能证明业务 schema 在本轮期间从未变更，也没有证明这些表的创建来源。先前「业务 schema 未被写入研究表」的表述缺少证据，已向用户更正；不将测试 schema 清理结果升级为业务 schema 零变更保证。

## 隔离 PostgreSQL 验证

用户明确答复「允许隔离测试 schema」后，在 `backend_api_python/` 执行：

```powershell
.\.venv\Scripts\python.exe -c 'import os, runpy; from dotenv import dotenv_values; values = dotenv_values(".env"); task_dsn = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL"); assert task_dsn, "DATABASE_URL is required"; os.environ["DATABASE_URL"] = task_dsn; runpy.run_path("tests/integration/check_factor_mining_postgres.py", run_name="__main__")'
```

exit 0，16 项检查通过。脚本随机创建 `qd_factor_mining_test_<uuid>`，所有测试连接明确设置该 schema 为搜索路径，在 finally 删除。覆盖旧表兼容、迁移重复应用、同 run 8 线程只登记一次、不同 run 累计、租户隔离、研究组不可变、贡献和计数一起回滚、run结果反馈不受候选覆盖影响，以及持仓/来源/资产池/日期/分段上下文排除。结束后只读查询 `pg_namespace` 确认 `qd_factor_mining_test_%` 数量为 0。DSN 仅在进程内加载，未输出或写入报告。

## 主产品前端验证

`frontend-vue/` 最终变更包括新控件标签、暗色高级面板、提示与重放摘要对比度，以及准确的重放错误文案。重放按钮由既有primary-color token生成较深色，并覆盖全局主题的important规则，保持主题色相且增加白字对比。第一次补充 aria-label 后曾漏报2项 Vue 属性格式错误，使用递归 glob lint 复现并格式修正；最终模块命令通过，全仓 lint 恢复23 errors/1261 warnings。没有放宽 lint 或 axe 规则。

```powershell
pnpm exec eslint "src/views/factor-mining/**/*.{js,vue}" src/locales/factor-mining-overrides.js
node --test tests/unit/factorMiningP2.test.mjs tests/unit/factorMiningAiPayload.test.mjs tests/unit/factorMiningLocaleRuntime.test.mjs tests/unit/factorMiningLocaleContract.test.mjs
pnpm run build
```

分别 exit 0、34 passed、exit 0（构建仍有既有大 chunk 警告）。标准 `pnpm run test:unit` 为519 passed/1 failed，失败为 factorLibraryLocalization 读取缺少的后端 checkout 路径；`pnpm run typecheck` 返回 Missing script，保持 blocked。

浏览器证据由 [P2 browser acceptance](../../../tools/frontend-verification/P2-ACCEPTANCE.md) 维护。原始 `p2-mock-final` 为独立审查红证据，48 checks中38 passed/10 axe failed。主代理补修 `.fm-sub` 对比度，并加强检测覆盖研究摘要/指纹与重放按钮；检测单测先1 failed，补齐共享helper后21 passed。扩展检测又复现按钮被全局重要样式覆盖，修补后最新 `p2-mock-accepted-scope-final` 业务、响应式、console/network通过，16张截图、源码hash稳定（`e338396259c08ce448402e4bf7f1ece8a7602fae6d5f3f7bec9118372a96b668`）。12/12业务终态断言通过、扩展P2 selector违规0，全页axe仍有70条规则记录，命令exit1；不能把局部通过当作整页无缺陷。没有干净基线对照，不把其余问题一概认定为既有。Mock只证明真实SPA消费与刷新重读，持久性由上述PG测试另证；缺少真实隔离HTTP后端，integration blocked。没有生产部署或真实模型验收。

## 图谱与工作区

已使用实际解释器运行 `python -m graphify update . --no-cluster`，最终exit0，24445 nodes/62176 edges；不调用LLM。SQL提取缺少 tree_sitter_sql、两份范围外前端文件解析不全，图谱仅导航；文档语义索引未刷新。生成图谱未提交。根仓库和独立前端均保留未提交改动，不覆盖原有无关工作。
