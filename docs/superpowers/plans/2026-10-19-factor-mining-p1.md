# 因子挖掘 P1 与验证基础设施实施计划

规格：[P1 设计](../specs/2026-10-19-factor-mining-p1-design.md)。用户已明确要求实施，无需重复确认开始；采用主代理与有文件所有权的通用子代理并行。

## 原子任务与所有权

| ID | 任务 | Owner / 文件边界 | 验证目标 | 状态 |
| --- | --- | --- | --- | --- |
| F1/F2 | 可移植单测与真实 typecheck | Hubble；frontend-vue 配置、scripts、测试、types 与必要最小类型修正 | 原路径失败复现；全unit/真实typecheck/build；不隐藏错页 | 路径测试修复；全unit536 passed；真实typecheck入口实现但全量类型门禁仍failed |
| P1-A | 日期成员掩码 | Halley；engine/evaluator/pool/incremental及对应测试 | 非当期成员不影响横截面/L6，入池前warmup仍可用 | implemented；新增17项、相关11文件320 passed |
| P1-B1 | claim/lease、接管与原子提交槽 | Socrates；factor_mining_jobs新模块、agent_jobs/Celery任务及专属测试 | 双提交/双执行、续期/过期、旧token拒写，其他kind兼容 | implemented；生命周期46 passed，其他kind相关18 passed，隔离PG验证通过 |
| P1-B2 | 运行编排与原子发布 | 主代理；storage/service、迁移及其测试 | 取消与发布同锁、job/run/候选一起提交，失败回滚 | implemented；service/storage/publish/recovery反例208 passed；PG竞争通过 |
| P1-C | 候选上下文身份 | 主代理；storage/迁移/真实PG脚本 | 不同市场/周期不覆盖；原id保持；重复迁移安全 | implemented；真实PG核验独立context与id保留 |
| V1 | 独立审查与证据同步 | Sartre只读审查；Herschel限产品/03/06文档；主代理维护计划与集成 | 重点租户/lease/fencing/取消/时点/迁移；更新契约与前端映射 | reviewed; 3 recovery/state gaps fixed and regression-tested |

## 执行步骤

1. 核实实际调用链、当前未提交基线和已有租户/lease模式，使用图谱导航后用源码复核。
2. 各owner为其行为写最小失败反例，记录red；跨owner接口先明确后实现。主代理统一migration/init，其他owner不得修改同文件。
3. 最小实现并运行聚焦回归；不移动交易owner或修改无关模块。前端typecheck若发现大量原有问题先整理具体诊断再决定最小修正路径，不能降低检查覆盖。
4. 真实PG随机schema验证并发、锁、回滚与升级；不调用create_app或业务启动维护，不触真实模型/订单。
5. 汇总后端相关全回归、前端unit/typecheck/lint/build；有页面变更则追加实际浏览器验收。独立审查后的修改再次跑受影响检查。
6. 同步编号文档/中英文产品与部署说明，刷新代码图谱；记录范围、实际证据及未验证项。不自动提交或部署。

## Checkpoint / resume hint

- 当前：三类P1实现已联通。最终统一后端回归764 passed/157 NumPy warnings；新增范围Ruff和diff通过。隔离PG 24项通过、测试schema清理为0。独立审查的连续中断提案恢复、空合法提案失败与bound run状态问题均已修复。
- 前端路径失败已解决；完整unit 536 passed、0 skipped。真实全源 typecheck 命令与配置已建立，覆盖238个应用JS/Vue文件，实跑2660诊断/89文件、exit2；配置与全源覆盖已补齐，但类型门禁仍failed。完整lint保留23 errors/1261 warnings；build通过。仅request.js增加会擦除的返回类型JSDoc，interceptor与运行行为未变。
- 2026-10-20最终核对：`uv run python scripts/check_docs.py`通过；根仓与`frontend-vue`的`git diff --check`均exit 0；typecheck基础设施单测3 passed。全量typecheck仍为2660诊断/89文件、exit 2，门禁保持failed，不能标记为通过。继续清零需要另行限定全源类型治理范围；不以排除页面或过滤诊断代替。
- 后端统一回归764 passed、隔离PG 24项通过及测试schema清理0均已有本轮记录；若后续不改行为代码，无需重复全套回归或数据库写检查。Celery broker真实进程kill/网络中断/生产部署仍是环境验证，不由PG/单测替代。
- Drift check：全部P1和两项前端门禁均须处理，不扩展为全仓UI重构，不回滚P2或用户无关改动。
- Evidence：各项red/green、真实PG与前端实际命令随后追加；历史P2通过数只作基线，不当作本轮验证。

## 实际接口与恢复边界

- `factor_mining_jobs.claim_job(job_id)` 返回权威 job/user/request/token/attempts；终态返回None，活lease返回JobLeaseBusy。`assert_execution(cur, job_id, user_id, token)`在调用者事务内锁job并检查租户/token/状态/lease，不自行commit。失去所有权抛JobExecutionLost，用户取消抛JobCancelledError。
- `run_job(job_id, runner)`供Celery与本地共用，独立于引擎进度续lease并注入`__executionToken`；外层不再单独写成功状态。过期接管关闭旧running run并保留审计；新attempt开新run，AI复用旧已记录提案。默认lease120秒、attempt最多3，耗尽则job与running runs一起失败。
- `repository.for_execution(job_id,user_id,token)`返回绑定副本，不污染service单例；写入逐事务检查同一job行和run的token归属。`publish_run`一次事务写最终候选、run成功与job成功；异常和取消也由同锁约束。已移除搜索survivor的提前候选upsert。
- 提交在job store同TX获取user/kind advisory lock再检查active并insert；GET/list/submit和现有maintenance仅重投queued超过60秒或lease过期的作业，且用户入口按当前user过滤。撤掉started_at+time_limit推断死亡路径。
- 迁移`20261019_factor_mining_p1.sql`更换候选唯一约束，增加job lease/token/attempts及run token；旧候选内容和id不重写，API shape/候选id查询保持兼容。

## Red / green 与真实数据库证据

| 范围 | Red | 当前Green | 边界 |
| --- | --- | --- | --- |
| P1-A | 13失败；L6非成员干扰导致residual IC约0.8296；另2时区反例 | 新增17项、相关11文件320 passed | 无真实行情源压测 |
| 仓储发布 | 缺for_execution/publish_run的4失败；跨租户绑定反例失败 | 存储/服务/发布及状态/recovery反例208 passed；Ruff通过 | broker真实进程终止/生产恢复未实测 |
| 服务取消/恢复 | review后cancel仍发布及顺序2失败；started_at误杀/缺tenant恢复2失败；连续中断提案恢复/非法bound状态7失败 | service/storage/publish/recovery 208 passed；最终统一后端764 passed | 不提供阻塞I/O硬杀；真实broker kill drill未执行 |
| 候选迁移 | 旧ON CONFLICT反例1失败、新迁移缺失1失败 | migration contract及隔离PG 24项通过，核实不同context独立id | 仅随机schema验证，无生产迁移 |
| 生命周期 | 先构建46个行为/兼容场景 | 46 passed；其他kind/SSE/脱敏18 passed | 真实broker worker kill/restart未执行 |
| 统一后端 | 上述red | 764 passed，157 NumPy warnings，133.77s | 真实broker进程中断/部署/provider未执行 |

统一命令（`backend_api_python/`）：

```powershell
$miningTests = @(rg --files tests -g 'test_factor_mining*.py')
$jobTests = @(rg --files tests -g 'test_agent_jobs*.py')
.\.venv\Scripts\python.exe -m pytest @miningTests @jobTests tests/test_factor_research.py tests/test_llm_litellm_provider.py tests/test_celery_worker_readiness.py tests/test_celery_boundaries.py tests/test_agent_backtest_billing.py -q --timeout=90 --tb=short
```

真实PG仍按此前用户授权，脚本仅创建随机`qd_factor_mining_test_<uuid>`，显式search_path，finally删除。最新执行共24项通过，结束后测试schema数量为0。覆盖候选迁移保留id/跨context身份、过期接管拒旧写、cancel先无候选、publish先cancel不能覆盖、candidate失败全回滚、8线程submit恰好1个、活lease拒竞争/旧token拒renew、连续两次崩溃后恢复较早AI提案。接管反例最初因fixture时区和生产连接UTC约定不同失败，统一fixture时区后通过；未更改生产lease判断迎合测试，也未调用create_app或启动业务维护。

前端原路径单测red→green，版本化61因子catalog支持独立检出与显式`BACKEND_SRC_PATH`一致性校验；没有跳过断言或接受空列表。完整unit为536 passed/0 failed/0 skipped。真实typecheck锁定vue-tsc2.2.12/TypeScript5.8.3，`allowJs/checkJs`覆盖全部238个应用文件，JS和Vue语义探针均拒绝错误代码；全源实测2660诊断/89文件、exit2，类型门禁仍failed。

前端最终门禁（10月20日新鲜运行）：`pnpm run test:unit` 536 passed/0 failed/0 skipped；`pnpm run build` exit0（存在大chunk警告）；自有基础设施lint通过；全仓`pnpm run lint:nofix`保持23 errors/1261 warnings。完整`pnpm run typecheck --pretty false`覆盖238个JS/Vue应用文件，实测2660诊断/89文件、exit2。关闭`checkJs`的试验会丢失应用JS语义检查而未采用；保留`noImplicitThis`比默认多16个TS2683；会漏报模板错误的`Vue.extend`包装也拒绝。缺失的typecheck脚本及全源配置已经补齐；现存类型诊断仍未修复，门禁仍failed。

文档/图谱收尾：`scripts/check_docs.py`原先把根目录`.pnpm-store`下的Webpack模板当作文档扫描，已在保留既有用户对`frontend-vue`/`graphify-out`忽略规则的基础上增加缓存目录排除；最终文档门禁通过，代码图谱仅本地AST刷新且未提交。新增缓存由本轮冻结安装产生，已加至gitignore；缓存内容保留、未删除。

独立审查：修复连续中断时跳过较旧已记录AI提案、replay全部提案L1拒绝时成功发布、bound repository接受非法run状态；对应前端以外测试包含在764统一回归，并由审查者确认无原阻断残留。另有合法bound状态正向场景无单独行为测试，但PG成功/失败发布与取消竞争覆盖。

全仓文档检查曾因 pnpm 自身store中 `public/index.html` 的Webpack模板占位符误当链接失败。新增`.pnpm-store`忽略规则后复跑，输出`Documentation structure, links, code fences, and assets are valid.`；根docs与检查器其它用户修改均保留。
