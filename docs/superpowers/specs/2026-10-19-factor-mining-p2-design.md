# 因子挖掘 P2 全量优化

日期：2026-10-19。范围来自用户对六项 P2 建议的实施授权。

## 目标、范围与基线

在现有 DSL、研究引擎、Celery、PostgreSQL 与主产品 Vue 2 前端上完成六项优化：同 run 数值去重、分阶段计算、队列容量隔离、AI 反馈/调用预算、跨 run 研究治理和 UI 闭环。

基线：后端 HEAD `cbe4271`；主产品独立前端 HEAD `87093cf`。开工时根仓库存在用户未提交的文档、治理工具与兼容页修改，保留这些改动；前端工作树干净。

必读：`AGENTS.md`、`.agents/README.md`、`docs/architecture/README.md`、`PROCESS_ROLES_AND_TASKS.md`、`API_CONVENTIONS.md`、因子挖掘 v1/v3 规格、`doc/web_doc/03-state-and-api.md`、`06-factor-mining.md`，以及各任务负责的源码/测试。

不扩大到上轮 P1 的 worker 接管、取消提交一致性、候选表身份重建或策略运行时接入；这些仍是独立风险。本轮不删除兼容前端，不部署生产、不调用真实模型、不写业务数据库。

## 设计决策

### P2-01 同 run 数值去重

历史池与本轮通过搜索门槛的候选共同构成 L4 比较集合。先产出的候选优先保留；只在搜索期数据上比较；不同指纹但数值相关超过 `max_pool_correlation` 的后续候选记为 L4 重复，保留 trial。不得用冻结段决定代表因子。

### P2-02 分阶段评估

按 L2 基础指标、L3 稳定性、L4 相关性、幸存者完整详情逐步计算。保留公共 `FactorResearchEngine.run()` 的既有默认完整响应；挖掘调用显式选择所需研究阶段。早期淘汰不能执行后续相关性或详情构建；数值定义和时点/成本假设保持一致。

### P2-03 队列隔离

因子挖掘投递到 `mining` 队列，由专用 Celery worker 消费；原 worker 继续消费 `jobs,ai,maintenance`。仍属于有限作业职责，不新增交易运行角色；覆盖基础 Compose 和镜像/生产/本地构建变体、worker readiness 与环境示例及中英文部署文档。

### P2-04 AI 反馈与整轮 deadline

跨 run 反馈匹配租户、市场、周期、来源/资产池语义、持仓周期与研究区间；读取 run 的不可变结果，而不是会被覆盖的候选当前行，纳入 L5/L6 阶段与样本外关键指标。保留提示长度和条数上限；不纳入独立保留测试段。

提案一轮拥有单个 monotonic deadline，覆盖解析失败重试、模型 fallback 和 provider fallback；各次调用使用剩余时间，耗尽后不再开始调用。沿用现有错误键，记录实际模型/provider；不静默回退模板。

有预算的 HTTP 调用在发送前重新计算剩余时间，使用连接/读取共用的 total timeout，拒绝自动重定向；LiteLLM 禁止内部重试，由上层统一管理重试预算。响应返回后再次检查 deadline，过期结果不接受。此设计不提供对单个阻塞 DNS、底层 I/O 或缓慢响应流的强制抢占，不能将其描述为严格的进程墙钟上限；未传预算的其他 LLM 调用保持兼容。

### P2-05 跨 run 实验、冻结版本与保留测试段

新增服务端自动研究组：相同用户、来源、市场、周期、请求区间和分段定义共享研究组，不按 seed、模式、门槛、费用或持仓周期拆分，避免重复搜索被当成第一次试验。每个 run 的已测 trial 数只登记一次，登记与累计更新处于同一事务；L5 使用累计试验数，旧 run 不重写。

新表 `qd_factor_mining_experiments` 保存用户、上下文 hash/JSON、累计试验数；`qd_factor_mining_experiment_runs` 以 run_id 唯一记录该 run 的贡献。研究组由 repository 持有，service 编排；HTTP 仅接收 `holdoutRatio`，不能传入其他租户的研究组 ID。

`MiningConfig.holdout_ratio` / HTTP `holdoutRatio` 默认 0，合法范围 0–0.30，0 表示旧两段行为；主产品新任务默认 0.10。先从请求尾部保留 holdout，再在剩余区间按既有 freezeRatio 切搜索/验证。保留段不被搜索、L5/L6、LLM 或反馈读取；本轮仅预留并展示边界，不新增自动测试段评估或策略上线动作。

结果增加 `summary.research`：`experimentId`、`contextHash`、`runTrials`、`cumulativeTrials`、`freezeVersion`、`freezeSnapshotId`、`holdout`（reserved/startDate/endDate/evaluated=false）。冻结版本标识分段定义和实际冻结数据指纹，漂移可观察；仅有指纹不宣称保存了不可变行情快照。

### P2-06 主产品 UI 闭环

在 `/#/factor-mining` 增加 evolution 模式与 population=32、generations=20、eliteRatio=0.25 控件（校验沿用后端范围），非 evolution 请求裁剪这些字段。增加 holdoutRatio 控件和研究组结果展示。

终态 AI 作业可发起显式重放，只提交 `{searchMode:'ai', replayJobId}`，沿用原参数且跳过模型；估价不伪造重放配置。任务可直达本次候选，以 URL query 保留 tab/runId，刷新继续显示同一筛选。重放预填是临时 UI 状态，刷新后按常规表单重置；实际新 job 和候选筛选可重新读取。

复用 Vue 2 / Ant Design Vue、现有 request、tokens 与 locales；覆盖 loading/empty/error/success/unauthorized、桌面/移动及键盘/标签。兼容页保留，记录其能力范围，不借本轮退役。

## 修复轨与退役轨

修复轨：以默认参数兼容旧 API/旧 run；所有阶段错误可追溯；租户检查延续。退役轨：仅替换挖掘自身的全量提前计算、通用 jobs 投递和宽泛历史反馈路径；公共研究 API 完整模式及独立前端继续存在。没有通过真实迁移/部署检查前不移除旧部署入口。

## 验收

后端：针对数值孪生、早筛短路、三段数据隔离、累计次数幂等/租户隔离、反馈上下文/不可变结果、deadline 重试/fallback 编写预期失败测试，再实现与回归。迁移须验证 init.sql 同步、可重复应用和真 PostgreSQL（有可用隔离环境时）。

队列：测试因子任务进入 mining、其他任务维持原队列；`docker compose config --quiet` 校验各适用变体，不能将静态配置通过当作容量压测。

前端：非修复 lint、unit、build；当前工程没有 typecheck 脚本须明确记录。启动实际主产品，验证进化提交/结果/刷新、重放请求、任务候选跳转及桌面/移动截图、console/network/a11y；mock、测试后端联调和部署证据分开。
