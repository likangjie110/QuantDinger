# 04 前端测试与证据映射

<!-- frontend-delivery: {"scope":"full-stack","target":"standalone","frontendRepository":"QuantDinger","frontendRef":"1aee2cc3dba1eddac4071b7535e6127a2dcc6a42","evidence":"unverified"} -->

运行前先确认目标、源码根目录与锁文件。[00](00-index.md) 决定归属，[05](05-acceptance-checklist.md) 决定状态。本页维护检查入口、需求映射与已知缺口；具体执行命令、源码版本和产物见对应计划，验收以该版本的实际执行为准。

## 静态与单元检查

从本仓库根目录调用跨仓库入口：

```powershell
python scripts/check_frontend_delivery.py --target main --source ./QuantDinger-Vue --run --report outputs/frontend-delivery/main.json
python scripts/check_frontend_delivery.py --target standalone --run --report outputs/frontend-delivery/standalone.json
```

主前端在其他目录时通过 `--source` 显式提供，或设置 `FRONTEND_SRC_PATH`。不要让检出目录名与已发布镜像身份混为一谈。

| 检查 | main | standalone | 结果范围 |
| --- | --- | --- | --- |
| 包管理器 | `packageManager` / 已跟踪 pnpm lock / Dockerfile 相互核对 | npm + 本目标 package-lock | 不使用主前端被忽略的 npm lock；未知双锁冲突必须阻塞 |
| lint | `pnpm run lint:nofix` | `npm run lint`，非修复配置 | 不调用带 `--fix` 的脚本掩盖门禁期间源码变化 |
| typecheck | `pnpm run typecheck --pretty false`；锁定 `vue-tsc` / TypeScript，检查238个主产品 JS/Vue 应用文件 | `npm run typecheck` | 命令已实现并全源运行；当前2660诊断/89文件、退出码2，门禁仍failed。build、语法检查不能代替类型检查 |
| unit | `pnpm run test:unit` | `npm run test:unit` | mock 的断言只说明其覆盖的消费契约 / 状态 |
| build | `pnpm run build` | `npm run build` | 可编译，不代表联调或已部署 |

主产品真实 typecheck 使用 Vue 2.7 模板目标和 `checkJs`；正向/错误 JS 与 Vue 语义探针确认编译器能区分有效代码和类型错误。入口覆盖完整应用，但现存类型诊断尚未修完；报错归零前不能报告 typecheck 通过。独立页质量脚本属于本仓库维护范围；主产品类型实现留在其独立仓库。

主产品标准 `pnpm run test:unit` 已通过 `node scripts/run-unit-tests.mjs` 启动。该入口枚举、排序 `tests/unit/*.test.mjs` 并将显式文件列表交给 `node --test`，兼容不同 Node 版本对目录与 glob 参数的处理差异。

当前 P1/P2 检查状态与实际命令、日志和边界见 [P1 实施计划](../../docs/superpowers/plans/2026-10-19-factor-mining-p1.md) 与 [P2 计划](../../docs/superpowers/plans/2026-10-19-factor-mining-p2.md)，不作为浏览器或部署通过的替代证据。

| 主产品检查 | 当前状态与缺口 |
| --- | --- |
| 因子挖掘 unit | 4 个模块、34 passed：`factorMiningP2.test.mjs`、`factorMiningAiPayload.test.mjs`、`factorMiningLocaleRuntime.test.mjs`、`factorMiningLocaleContract.test.mjs`，均位于主产品 `tests/unit/` |
| 标准全局 unit | 536 passed、0 failed、0 skipped。`factorLibraryLocalization` 不再假定固定兄弟目录；版本化61因子目录支撑独立 checkout，显式 `BACKEND_SRC_PATH` 会校验与后端 registry 一致 |
| lint | `lint:nofix` 全仓 23 errors / 1261 warnings，保持 failed |
| typecheck | 238应用文件全量检查；当前2660诊断/89文件、退出2，保持failed；没有排除页面或关闭JS检查 |
| build | 通过；只说明可编译 |

独立页当前真实 typecheck 暴露三个原有组件问题：`App.vue` 的 tab model 类型过窄；`CandidateList.vue` 向 input-number 传不支持的 clearable；同组件按 `data.length` 读取实际 `{items}` 信封。它们保持 failed，不能排除组件或放松模板检查来通过。修复属于后续明确的组件/契约任务，需对应回归与浏览器证据。

检查入口自身的回归与文档验证：

```powershell
python -m unittest discover -s scripts/tests -p test_frontend_delivery.py -v
python scripts/check_docs.py
git diff --check
```

## 浏览器测试

独立工具位于 [tools/frontend-verification](../../tools/frontend-verification/README.md)。先按其锁文件安装，再用它的实际 CLI 验证已经启动的应用。只访问明确的开发/测试 URL，不接管个人浏览器会话。

- `mock`：在实际页面上拦截 API，稳定覆盖 loading/empty/error/success/无权限、参数与消息、任务回执/列表；不使用替代 UI demo。
- `integration`：显式提供测试环境 URL；目标 API 使用真实 HTTP 后端。账户、数据和有写入行为的提交必须属于隔离测试环境，不默认连本机部署。
- 主产品路由为 `/#/factor-mining`；独立页为 `/`。默认登录页截图不证明因子页面已验证。
- 只读集成检查不证明提交/取消已通过；报告必须列出未执行的有写入行为的流程。

按 CLI `--help` 设置 `--target`、`--base-url`、`--mode`、`--source-revision`、`--output-dir`。把生成的浏览器 JSON 通过检查入口 `--browser-report` 关联；target 与 source revision 不一致时必须拒绝。Mock 结果不能提升为 integration。

验证工具从指定 checkout 自动启动应用时，才可把访问的 bundle 关联到该源码。仅同时提供本地源码目录和外部 base URL 不能证明运行版本；没有独立的 bundle 来源证据时，来源保持 blocked/unverified。只读联调单独记录范围，不升级为提交、持久化或完整用户闭环通过。

## 当前本机真实联调覆盖

主产品 `/#/factor-mining` 已在本机真实 API/PostgreSQL/Celery mining 队列上完成枚举、进化搜索、取消、任务结果刷新、候选筛选恢复及详情读取；桌面 1440×900 与移动 390×844 均已采集并实际读图。源码版本、命令、业务断言与证据边界见 [本机运行验证](../../docs/superpowers/plans/2026-10-20-factor-mining-local-validation.md)。这是测试库与开发服务的运行证据，未验证生产镜像。

成功场景的业务断言通过、console/network 错误为 0；整体浏览器报告仍 failed，axe 包含 ARIA、label、对比度等违规，稳定截图另确认候选详情暗色对比不足和窄屏裁切。补拍首次移动登录出现 public-config 401，保留失败证据；单独复跑通过，根因未确认。AI 真模型、真实越权/多租户、五态全覆盖与完整键盘验收仍未验证；静态 typecheck/lint 保留上述失败状态。

## CI 范围

[frontend-delivery.yml](../../.github/workflows/frontend-delivery.yml) 默认运行交付工具单测、浏览器 helper 单测、文档检查和变更范围守卫；涉及 tracked 独立页时运行其静态检查与实际应用 mock 浏览器，并保留失败证据。主产品源码检查通过手动 workflow 输入明确仓库/ref，在具有对应访问权限时检出；缺失主源码不表示主产品通过。

范围守卫当前覆盖本仓库独立页源码/配置和因子挖掘人类 Web API；不是所有产品模块的自动影响推断。新增模块时扩展映射与回归；其余 UI 影响仍由需求表和审查验证。实际运行结果、五态、浏览器、只读联调、完整联调和部署分别报告，CI 自身配置有效不等于远端 CI 已执行。

## 需求 → 测试映射

| 需求 | 单元 / 契约 | Mock UI | 测试后端联调 |
| --- | --- | --- | --- |
| FM-01 估价与提交 | 请求字段、202 信封、错误 key；后端 `tests/test_factor_mining_api.py` | 可控估价/回执/失败；检查可见反馈 | payload 与 job config 回显一致；提交后观察同一任务 |
| FM-02 搜索模式与保留段 | `factorMiningP2.test.mjs` / `factorMiningAiPayload.test.mjs`：进化默认 32/20/0.25、范围与必填、模式切换裁剪、holdout 默认 0.10 及 0–0.30 校验；后端允许字段与参数测试 | 验证三模式选择、条件字段与错误反馈 | 页面输入→请求→详情的模式及 holdout 保持一致；其他模式不携带进化字段 |
| FM-03 作业状态 | receipt/list/detail schema | loading/空/运行/终态/错误 | 真实轮询与停止，退出后 timer 清理 |
| FM-04 取消 | 授权/不存在/终态契约 | 确认前不提交、取消反馈 | 仅隔离环境验证实际取消状态 |
| FM-05 候选 | items/limit/offset/详情契约 | 空、满页、筛选、详情 | 真实分页与当前用户隔离 |
| FM-06 权限/失败 | 400/401/403/404、网络与 500 | 五态、i18n、重试 | 真实认证失效/拒绝；不能以 mock 代替租户验证 |
| FM-07 AI 显式重放 | `factorMiningP2.test.mjs` / `factorMiningAiPayload.test.mjs`：可重放 AI 终态、仅发送 searchMode/replayJobId、跳过普通校验与估价、失败保留来源及翻译错误 | 验证重放入口、确认源任务、新回执与失败反馈 | 服务端复制源配置与提案；核对新 jobId、无模型调用、刷新可读取新任务 |
| FM-08 候选跳转与 URL 恢复 | `factorMiningP2.test.mjs`：真实 result runId、tab/runId 刷新与前进/后退恢复、保留无关 query、非法 runId、分页重置、旧响应隔离；重放预填刷新清除 | 验证任务→候选、清除过滤、切换任务及刷新 | URL runId 与真实 candidates 请求一致；旧任务可能因候选当前行被覆盖而为空 |
| FM-09 研究摘要 | `factorMiningP2.test.mjs`：累计 trial、冻结版本/指纹、保留段日期及未评估状态，兼容旧结果缺少 research | 验证新旧任务详情、无幸存者仍显示 holdout 边界 | `summary.research` 与任务一致；holdout.evaluated=false，未抓取验证数据时 freezeSnapshotId=null；指纹不代表行情副本 |

## 视觉、数据与交互证据映射

设计取舍引用 [09 信息架构与设计决策](09-information-architecture.md)，可判定的视觉验收标准、资产元信息及基准维护统一引用 [10 布局与视觉基准](10-layout-and-visual-baseline.md)，不在此复制完整设计或将模板维度写成已验证结论。

| 覆盖项 | 页面/组件与适用条件 | 检查/断言 | 实际用例/命令/操作步骤 | 关联记录与证据 |
|---|---|---|---|---|
| 信息层级与关键动作 | main `/#/factor-mining`；standalone `/` | 对照 09 的主次顺序、入口与任务可达性 | `TODO(待核实)` | 09 决策；截图未采集 |
| 响应式变化过程 | 同上 | 1440×900 / 390×844 及 768/480 断点附近的重排、换行、收起与滚动 | `TODO(待核实)` | [01](01-routes.md) 变化记录；截图未采集 |
| 代表性数据边界 | 提交、作业、候选、AI 提案 | 对照 03 的长文案、零值/未知值、极值、列表密度及轮询更新规则 | `TODO(待核实)` | 03 数据记录；视觉未采集 |
| 交互与恢复 | 表单、详情对话框、候选筛选 | 焦点、Esc、提交/重复提交、失败恢复、未保存及滚动规则 | `TODO(待核实)` | 03 交互记录；证据未执行 |
| 组件样例可运行性 | 复用实际页面入口，不新增展示依赖 | 按 [02](02-components.md) 复现变体与状态，单列样例结果 | 未执行 | 02 入口记录；不作业务通过证据 |

## 图像审阅与基准核对

| 页面/状态/视口 | 10 实际图像与基准记录 | 实际打开/读图方式与审阅角色（人/AI） | 对照标准、具体区域与差异结论 | 10 问题/复验记录 |
|---|---|---|---|---|
| 未采集 | 未配置 | 未执行 | 未执行 | 未执行 |

AI 参与审阅时必须实际打开或读取图像，记录所读资产、方式与针对具体区域的观察；文件存在、路径可解析、元信息读取或文字复述均不算实际读图。能力不可用或尚未查看时记“未执行”并写明阻塞和人工核查步骤。视觉对照需注明当前实现、目标设计或已审阅基准身份；差异、缺陷处理、复验与基准更新沿用 [10](10-layout-and-visual-baseline.md) 的记录，不能以替换基准掩盖回归。

## UI 证据

每份报告关联 target / sourceRevision / route / mode、命令、开始/结束与结果。分别保存桌面 1440×900、移动 390×844 截图、console、network、axe JSON、五态结果和未覆盖项。日志不得保存请求认证 headers、cookie、登录凭据或未经清理的载荷。

现有页面的布局、错误状态、键盘和 a11y 缺陷应按实际失败报告；不能修改测试来接受缺陷以获得绿色状态。
