# 因子挖掘本机运行验证

## 目标与边界

用户要求在本机启动并验证使用。主产品为独立 `frontend-vue` checkout 的 `/#/factor-mining`，后端采用当前源码；本轮验证实际 HTTP、数据库与浏览器操作。沿用 P1/P2 已实现行为，不扩大为全仓类型治理或交易运行时改造。

Baseline：根 `AGENTS.md`、`DEVELOPMENT.md` 本机启动章节、`docs/architecture/README.md`、`doc/web_doc/03-state-and-api.md`、`doc/web_doc/06-factor-mining.md`、P1/P2 计划、`app/__init__.py`、`app/startup.py`、`app/utils/db.py`、实际 Vite 配置。

测试资源隔离：仅使用本机 PostgreSQL 的独立测试数据库及测试 Redis 队列。已有业务库和根 `.env` 不改动；凭据仅从既有配置在进程内读取，不写报告。API 使用 `api` 角色，后台计算仅由有限作业负责，不启动交易或调度进程。

## 原子任务和负责人

| ID | 工作 | 负责人 | 验收 |
| --- | --- | --- | --- |
| L1 | 核实本机服务与启动配置 | 主代理 | 监听端口、运行模式、数据库目标已核实 |
| L2 | 初始化独立测试库并启动真实 API、SPA | 主代理 | HTTP 健康、登录、目录与源策略可读 |
| L3 | 页面操作闭环和刷新 | 主代理；通用只读代理核对选择器与契约 | 表单→估价→提交→执行→结果→刷新；桌面/移动截图 |
| L4 | 取消与结果持久化 | 主代理 | HTTP、UI 和数据库终态一致 |
| L5 | Celery mining 消费与隔离 | 主代理 | 独立队列中的真实有限作业被执行；记录平台限制 |
| L6 | 证据与使用入口 | 主代理 | console/network/axe、实际数据源边界、遗留问题和本机 URL |

## 当前结论（2026-10-21）

本机主产品 SPA、真实 Flask API、PostgreSQL 与 Celery mining worker 已运行；枚举、进化搜索、取消和刷新读取已在真实后端完成。整体质量验收仍未通过：全页 axe 存在违规，候选详情有暗色对比度和窄屏裁切问题；既有 typecheck/lint 失败仍保留。实际模型调用、生产部署与生产进程故障恢复未覆盖。

## 启动配置与来源证据

- 页面入口：`http://127.0.0.1:8000/#/factor-mining`；后端：`http://127.0.0.1:5000/api/health`。
- 主产品前端来自独立 `frontend-vue` checkout 的 Vite 开发服务；HEAD 为 `87093cf5ca3f7276d1c56895f3bdbdf686fcf683`，含未提交的 P1/P2 与类型基础设施改动。后端 HEAD 为 `cbe4271a0fd277827f6678188a6fec6a5deb6670`，同样含未提交改动。两者均不是已发布镜像身份。
- 浏览器报告记录主产品 5 个受关注源文件的合并 SHA-256 为 `c8c9d780fa0761cd80fd87d0a8dbd7df43c782a40b012e5827ff0925ec1dd146`；该摘要不代表整个构建产物哈希。
- 使用专用 `qd_factor_local_*` 测试数据库，真实迁移与管理员初始化；测试账户沿用既有配置，报告不含凭据。原业务数据库与根 `.env` 未改动。
- 专用 Redis 监听 loopback `56479`，DB0 为 broker、DB1 为结果；已读取 `appendonly=yes`、`maxmemory-policy=noeviction`、队列 `mining` 长度为 0。未向已有业务 Redis 写入。
- API 使用 `api` 角色与 `SKIP_STARTUP_HOOKS=1`，初始化后关闭自动迁移；worker 使用独立 `mining` 队列、Windows `solo` 池、concurrency=1，没有启动交易进程或 beat。
- Playwright 使用 Edge headless，桌面 1440×900、移动 390×844；实际截图为暗色主题、English 文案，不能据此认定中文或其他主题已验收。

## 已执行的用户闭环

| 场景 / 命令模式 | 实际结果 | 持久化与证据边界 |
| --- | --- | --- |
| 枚举 `enumeration` | 真实密码登录→填写→估价 200→提交 202→queued/running→succeeded；13 项检查通过 | Celery 执行约 19.5 秒，run 4；刷新后 config/result 一致，候选筛选恢复、详情 API 200 |
| 进化 `evolution` | 移动视口真实输入进化参数，任务 succeeded；13 项检查通过 | Celery 执行约 59.3 秒，run 5；15 trials，1 条候选评审记录，冻结通过数为 0；刷新后结果一致 |
| 进程内取消 `cancel` | 关闭确认不发请求；确认后 cancelled；9 项检查通过 | 2026-10-20 的本地作业池验证，刷新和数据库终态一致 |
| 队列取消 `cancel-celery` | 同样验证关闭确认与确认取消；9 项检查通过 | 真实 mining worker 消费，约 10 秒返回；刷新和数据库终态一致，未遗留活跃任务 |
| 只读补拍 `review-desktop` / `review-mobile` | 各 7 项检查通过，读取 run 5 的详情/研究摘要/候选，滚动后关闭对话框 | 等待 Ant Design 入场动画结束后补拍；不增加挖掘任务；窄屏滚动可操作不代表字段无裁切 |

测试源为专用库中的 portfolio 策略源 1，静态成员 AAPL/MSFT/NVDA/AMZN/GOOGL/META，无下单逻辑；市场 `USStock`、周期 `1D`、区间 `2024-01-02`～`2024-12-31`。成功任务 budget=8、seed=42、holdoutRatio=0.1、freezeRatio=0.3、groups=10，进化另用 population=8、generations=2、eliteRatio=0.25。该小样本用于功能冒烟，不代表研究样本充分或策略有效。

数据沿用应用 provider/cache 路径，没有注入合成行情；本轮未证明每根行情均重新从网络下载。后端存在历史区间 delayed 提示和 NumPy 相关性 warning，作业仍完成；未据此断言数据源故障，也不将它们隐去。

## 数据库与研究结果核对

只读核查显示本次库中 4 个 succeeded、2 个 cancelled，无 queued/running 任务；候选当前表只有 1 行，最后归属 run 5，状态 `rejected`，拒绝原因为 `factorMining.reject.selectionBias`。冻结段样本诊断为 `insufficient`，包含 `groupsReduced`、`smallCrossSection`、`fewObservations`，6 个成员下有效分组数为 3、IC 观测数为 13。不能把“任务成功”或“有候选记录”表述为“挖到可用因子”。

研究摘要显示保留段未评估（`holdout.evaluated=false`）。当前候选同 context 重评会更新 runId，run 4 后续筛选可能为空；其历史结果仍从原任务/run 读取。

应用实际配置读取返回 provider=`openrouter`，所有模型 provider 的 key 均未配置。没有向真实模型发请求，AI 提案、失败反馈循环及重放闭环保持未验证。

## 浏览器、视觉和未覆盖项

- 成功场景报告的 consoleErrors/httpErrors 均为 0，但全部浏览器命令最终 exit 1：`businessStatus=passed`、`accessibilityStatus=failed`、总体 `status=failed`。axe 已执行不等于 axe 通过。
- 枚举入口 8 类 axe 违规、结果 6 类；进化入口 9 类、结果 6 类；补拍的候选详情桌面/移动各 10 类，包含 `color-contrast`、ARIA、label、`empty-table-header`、`scrollable-region-focusable` 等。计数是规则类别，不是节点总数。
- 主代理用 `view_image` 实际打开并审阅了桌面/移动结果、候选列表与稳定详情截图。桌面作业详情能展示成功、冻结数 0 和研究摘要；候选详情深色数据文字及浅底表达式文字对比不足。390×844 下研究摘要可通过内部滚动读取，长 jobId 与候选详情部分多列数据超出可见宽度；未作为视觉合格基准。
- 补拍首轮移动登录曾在 `/api/settings/public-config` 捕获 401，随后等待 Access-Token 超时；保留 `review-mobile/first-report.json`、`first-network.json`。单独重跑后 7 项操作通过、console/network 为 0；未确认首轮失败根因，不能写为登录稳定性已修复。
- 没有覆盖所有五态、真实越权/多租户、中文/亮色主题、全部键盘路径、断点附近、生产镜像、worker 强杀接管、prefork 容量或实时交易。Windows solo 只证明本机队列功能。
- 前轮静态基线为 typecheck 2660 条诊断/89 文件、exit 2，lint 23 errors；本次未修改产品源码，也未把局部运行结果升级为静态检查通过。

## 命令与 evidence references

本机验证材料位于被忽略的 `tools/frontend-verification/artifacts/local-20261020/`，不作为公开日志或发布资产；保留启动器、状态文件、截图与经过脱敏的 JSON。目录名称保留最早验证日期，报告内时间记录实际 2026-10-20/21 执行时刻。

```powershell
python tools/frontend-verification/artifacts/local-20261020/local_stack.py db-status
python tools/frontend-verification/artifacts/local-20261020/inspect_state.py
node tools/frontend-verification/artifacts/local-20261020/verify-local.mjs enumeration
node tools/frontend-verification/artifacts/local-20261020/verify-local.mjs evolution
node tools/frontend-verification/artifacts/local-20261020/verify-local.mjs cancel-celery
node tools/frontend-verification/artifacts/local-20261020/verify-local.mjs review-desktop
node tools/frontend-verification/artifacts/local-20261020/verify-local.mjs review-mobile
```

本轮实际 Python 为项目 `.venv`；上述浏览器命令会操作测试库，只有两个 review 模式为只读。报告分别位于同名子目录 `report.json`，另有 `console.json`、`network.json`、`axe.json` 与截图；`persistence-check.json` 保存只读数据库、队列和 key 配置布尔值。worker 日志另确认实际任务接收与耗时，不在公开文档复制原始日志。

优先复查的图像为 `review-desktop/candidate-detail-stable-1440x900.png`、`review-mobile/candidate-detail-stable-390x844.png`、`review-mobile/research-summary-390x844.png`；旧 `candidate-detail` 截图因过早捕获未完整显示弹窗，不用作该弹窗的视觉通过证据。

## Checkpoint / resume hint

- L1～L6 的本机启动、操作与证据采集已执行；整体产品验收仍 failed，原因与未覆盖项见上。
- 保持 SPA/API/worker 与专用 PostgreSQL、Redis 运行，供用户从上述页面使用；用既有配置的管理员账户登录，测试策略源为 1。首次密码提醒按产品流程处理，不修改密码或绕过认证。
- 未提交、推送或部署本轮改动；保留原有工作区改动与兼容独立页，没有删除源文件或创建业务 owner/fallback。
- 后续如继续验证，应先读本计划和测试状态，不重复初始化测试库，也不假设该测试源存在于业务库。需要停服时先重新核实本次 PID/监听归属，只停止本次资源。
