# 03 状态、API 与契约

先明确交付目标；主产品和独立因子页可以消费相同 API，但它们的认证、组件与页面证据不可互换。完整使用方式见 [模块地图](00-index.md)。

## 五态定义

| 状态 | 用户可见行为与退出条件 | 验证方式 |
| --- | --- | --- |
| `loading` | 说明正在加载；提交操作防重复；刷新不误删上次有效数据 | 延迟目标请求，断言 loading 和按钮行为；验证请求完成后的状态 |
| `empty` | 成功返回零条数据后给出空态与下一步；不能把失败显示成空结果 | 成功信封 + 空 items；断言空态而非报错 |
| `error` | 展示可理解且本地化的错误；重试可达；保留已填表单或旧数据 | 参数错误、网络失败、500 分别验证；不得无限 loading |
| `success` | 展示结果或任务回执；异步任务继续展示 queued/running/终态 | 检查请求 payload、202 回执、列表与详情的同一 jobId/config |
| `unauthorized`（无权限） | 未登录按统一流程跳登录；权限不足可见提示，不能展示他人数据或假空态 | 独立检查 401、403 和按契约隐藏存在性的 404；不把三者合并 |

这是交付要求，不表示每个现有组件已通过五态验收。因子模块的当前缺口见 [06](06-factor-mining.md)。预期错误的 HTTP 状态也要记录，只有与场景匹配的错误才属于预期网络记录。

## 因子挖掘 API 映射

权威实现：[factor_mining.py](../../backend_api_python/app/routes/factor_mining.py)。共同信封为 `{code, msg, data}`；成功 `code: 1`，`msg` 是 i18n key；所有端点要求登录。

| 操作 | 端点 | 关键字段 / 语义 | UI 消费 |
| --- | --- | --- | --- |
| 目录 | `GET /api/factor-mining/catalog` | fields/operators/templates；可选 market | 字段与模板目录；不能代替作业结果 |
| 估价 | `POST /api/factor-mining/estimate` | 与 run 配置共享字段；不创建 job | 提交前估价、参数错误反馈 |
| 提交 | `POST /api/factor-mining/run` | 202 + job receipt；`sourceId, market, timeframe, startDate, endDate, budget, seed` 等；AI 模式另收 `searchMode=ai`、`aiRounds`（1–3）、`aiProposalsPerRound`（1–20）、`researchNotes`（≤8000）、`replayJobId` | 禁止将 queued 回执当计算成功；非 AI 模式携带 AI 字段会被 `invalidParameter` 拒绝 |
| 作业列表 | `GET /api/factor-mining/jobs` | limit；`data.items`；结果通过详情读取 | 列表、活动任务轮询 |
| 作业详情 | `GET /api/factor-mining/jobs/<jobId>` | config/progress/result/error/timestamps；AI 运行的 `result.summary.ai` 含 `rounds/proposals/l1Rejected/duplicates/evaluated/model/provider/replayOf/snapshotChanged` | 详情、请求配置回显、终态；AI 摘要 |
| AI 提案列表 | `GET /api/factor-mining/jobs/<jobId>/proposals` | `data.items`，取同一 jobId 的最新 run，按 `(round, ordinal)` 排序；行含 `round/ordinal/hypothesis/status/reason/expressionHash/model/provider`；状态 `proposed/evaluated/l1_rejected/duplicate`；limit 上限 200 | 详情提案表（宽屏）/ 卡片（窄屏）；运行中可读取已写入的提案；未知/越权任务返回 404 |
| 取消 | `POST /api/factor-mining/jobs/<jobId>/cancel` | 当前用户作业；取消先提交则 cancelled 且不发布候选，发布先提交则返回 succeeded | 确认后提交，按返回状态与刷新结果展示 |
| 候选列表 | `GET /api/factor-mining/candidates` | runId/status/market/limit/offset；items；没有保证 total；search 保留兼容，新任务不发布 search 候选 | 筛选、翻页、空态 |
| 候选详情 | `GET /api/factor-mining/candidates/<id>` | 表达式、指标、样本外数据 | 详情展开，不从精简列表臆造指标 |

参数拒绝返回 400；未登录按认证契约返回 401；job/candidate 不属于当前用户或不存在时返回 404。全局 403 行为由目标 UI 的统一请求处理。请求字段以路由白名单和 [MiningConfig](../../backend_api_python/app/services/factor_mining/engine.py) 为准，不将前端多余字段当已持久化配置。

P1 持久化语义：同用户同 market/timeframe 最多一个 queued/running mining job，数据库事务锁防止并发双提交。job claim 以可续租 execution token fencing；过期接管开同 jobId 的新 attempt/run，旧 token 的写入均拒绝。取消与发布串行化；只有冻结复核完成才会原子写候选、成功 run 与成功 job。候选 identity 为 `(user_id, market, timeframe, expression_hash)`；同 context 重评更新当前行，跨 context 不互相覆盖。维护任务和当前用户的 submit/get/list 入口有界重投过期 lease 或长时间 queued job；它们不等同于生产 broker kill/restart 验收。

AI 相关错误键：`factorMining.aiProposalFailed`（模型调用失败或回答不可用）、`factorMining.aiNoValidProposals`（没有有效提案）、`factorMining.aiReplayNotFound`（重放源不存在或不属于当前用户）、`factorMining.aiReplayIncompatible`（非 AI、running/cancelled、无已记录提案、DSL 版本或重述配置不兼容；有提案且兼容的 success/failed run 可重放）、`factorMining.reject.duplicateProposal`（提案重复）。`reject.invalid:<detail>` 是带参数的族，UI 按 `:` 前缀翻译。

作业持久化边界见 [service](../../backend_api_python/app/services/factor_mining/service.py)、[storage](../../backend_api_python/app/services/factor_mining/storage.py) 与 [execution ownership](../../backend_api_python/app/utils/factor_mining_jobs.py)：

- 搜索 survivors 只留内存，review 后由 `publish_run` 在一次事务内发布 candidate + run `success` + job `succeeded`。取消与发布锁定同一 job 行；取消先赢不新增或更新候选，发布先赢保留成功结果。
- 租户并发提交以事务 advisory lock + active count + insert 保证只有一个 queued/running 挖掘作业。每个 attempt 使用 `execution_token` 与定期 renew 的 lease（默认 120 秒）；接管更换 token，旧 owner 的全部持久写入被拒绝，attempt 默认总上限 3。
- 现有 maintenance，以及当前租户的 submit/get/list，会恢复超过 60 秒的 queued 和 lease 过期或缺失的 running；本地 fallback 同样恢复。中断旧 running run 标为 failed 并保留审计，新 attempt 新建 run、保留 jobId；提案按 latest run 读取，AI 复用已记录 proposals。不承诺逐指令 checkpoint、硬杀阻塞 I/O 或 provider 精确一次调用。
- DSL 横截面算子、L4 比较与 L6 候选/池成员/收益使用一致的时点 membership mask；缺失 mask 日期或标的按非成员处理，原始字段与 time-series 子树保留入池前 warmup 历史。

## 代表性数据与显示边界

字段含义与值域以本页契约为准；内容场景与数据集选择关联 [09 信息架构](09-information-architecture.md)，具体显示规则由模块文档维护，布局与视觉证据关联 [10 布局与视觉基准](10-layout-and-visual-baseline.md)，业务与刷新断言关联 [04 测试映射](04-test-map.md)。

| 数据边界 | 适用页面/字段与契约依据 | 代表性输入与来源 | 格式/占位/截断/排序等 UI 规则 | 04 用例与 10 视觉记录 |
|---|---|---|---|---|
| 长文案、长表达式与多语言 | 表达式、`researchNotes`（≤8000）、AI `hypothesis/reason` | `TODO(待核实)` | 换行/截断/完整值查看方式，`TODO(待核实)` | 未采集 |
| 零值与未知值区分：0、null、缺失、未加载 | 估价、作业结果与候选指标 | 成功空 `items` 与失败分开构造 | 空态不得显示成错误，失败不得显示成空结果 | [06](06-factor-mining.md) 状态矩阵；截图未采集 |
| 极值、精度、单位与时区 | `holdoutRatio` 0–0.30、`aiRounds` 1–3、`aiProposalsPerRound` 1–20、日期区间 | 边界值与非法值 | 校验提示与本地化错误 key | 本页五态；视觉未采集 |
| 空列表、单条、密集列表与分页 | jobs/candidates `limit`；AI 提案 `limit=200`（覆盖单次 60 条上限） | 0 条、不足一页、满页 | 翻页、滚动与溢出规则，`TODO(待核实)` | [04](04-test-map.md) |
| 实时更新频率、突发、乱序与过期 | 活动任务 5 秒轮询；运行中提案已写入 | 延迟/失败/终态响应 | 更新提示、位置/选择保持与恢复，`TODO(待核实)` | [06](06-factor-mining.md) |

不得把合法零值、未知值、无权限与尚未加载合并成同一业务含义；按契约分别定义显示和操作可用性。没有对应能力时写“不适用”及源码/产品依据，不为填表虚构行为。

## 交互细则与恢复

逐项注明允许、禁止或不适用及依据，不默认所有弹层均可用 Esc 关闭。纯客户端交互注明“无 API”及理由，临时状态注明刷新后恢复或重置规则。设计取舍关联 [09 信息架构与设计决策](09-information-architecture.md)，组件映射见 [02 组件与界面基线](02-components.md)。

| 页面/组件与触发动作 | 预期规则与状态归属 | API/数据影响 | UI、键盘与刷新断言 | 定义/用例/证据 |
|---|---|---|---|---|
| 焦点进入、Tab 顺序、校验失败定位与关闭后返回 | 提交表单与详情对话框；`TODO(待核实)` | 无直接 API 影响 | 焦点进入弹层、关闭后回到触发点 | 未执行 |
| Esc、取消与点击外部：是否关闭、可否中断 | 作业详情 modal；取消操作需确认 | `POST jobs/<jobId>/cancel`，取消不等于请求已中断 | 取消前不提交；Esc 规则 `TODO(待核实)` | [06](06-factor-mining.md) FM-04；未执行 |
| 提交与重复提交、提交中退出或返回 | `SubmitJob`；提交/估价按钮防重复 | `POST estimate/run` | disabled/submitting 与恢复 | [06](06-factor-mining.md) FM-01；未执行 |
| 失败后输入保留、重试与状态恢复 | 提交表单、详情/提案区块 | 400/网络/500 与可重试错误 | 保留已填内容，避免无限 loading | [06](06-factor-mining.md) FM-06；未执行 |
| 未保存变更：离开、关闭、刷新与重新进入 | 表单与重放临时状态 | 无 | 重放表单刷新后重置；未提交内容规则 `TODO(待核实)` | [06](06-factor-mining.md)；未执行 |
| 页面/弹层/列表滚动、滚动锁定与位置恢复 | 候选列表、作业详情 modal | 无 | 分页/筛选后滚动位置 `TODO(待核实)` | 未执行 |

## 主产品的客户端契约

- API adapter：`QuantDinger-Vue:src/api/factor-mining.js`（含 `getJobProposals`，详情读取固定 `limit=200` 以覆盖 AI 单次最多 60 条提案）。
- 提交 payload：`src/views/factor-mining/aiPayload.js` 的 `buildRunPayload` 按 `searchMode` 裁剪；非 AI 模式不发送 `aiRounds/aiProposalsPerRound/researchNotes/replayJobId`。
- 进化模式 `evolution` 显示 `population/generations/eliteRatio`（初值 32/20/0.25），其他模式裁剪这三个字段。`holdoutRatio` 合法范围 0–0.30；旧 API 请求默认 0，主产品新任务初值 0.10。
- AI 重放仅提交 `{searchMode:'ai', replayJobId}`，配置由服务端复制，保留原 `holdoutRatio`；临时重放表单状态刷新后重置，已提交任务仍可重新读取。
- `result.summary.research` 增加 `experimentId/contextHash/runTrials/cumulativeTrials/freezeVersion/freezeSnapshotId/holdout`。研究组按用户、来源、市场、周期、请求区间和分段定义累计；不因 seed、模式、门槛、持仓期或费用变化重新计数。每个 run 贡献一次，冻结复核使用累计次数。
- `holdout` 提供 `reserved/startDate/endDate/evaluated:false`。先预留请求尾段，再按 `freezeRatio` 切剩余搜索/验证段；保留段不参与搜索、L5/L6 或 AI 反馈，本轮未提供最终测试端点。冻结版本是分段定义和实际验证数据指纹，不代表保存行情副本。
- 从任务直达候选时，`tab/runId` 保存在路由 query；刷新恢复同一 run 筛选。候选唯一键为 `(user_id, market, timeframe, expression_hash)`；同 context 重评更新当前行及 run 归属，跨 context 不覆盖；历史失败证据改读 run 结果，不由候选当前行推断。
- 请求：复用 `src/utils/request.js`，包括 token、cookie、语言 headers、认证失效和权限提示；禁止功能组件自行另建一套 token 存储。
- i18n：复用 `src/locales/factor-mining-overrides.js` 与既有 locales 合并机制。
- 异步结果：提交回执、列表摘要和详情不是同一 schema。修改任一字段时同步 adapter、读取组件、类型/契约、fixture 和测试断言。

独立页的 [factorMining.js](../../frontend-factor-mining/src/api/factorMining.js) 是独立 Axios wrapper，其 localStorage token 机制不得作为主产品认证已验证的依据。

## 契约变更同批检查

1. 列明请求 / 响应新增、删除、可空字段、枚举和默认值。
2. 同步 OpenAPI、受影响的前端类型或 JSDoc、mock、事件流和消费组件；无该项时写具体原因。
3. 钉住错误码、错误 key、401/403/404、空 items、分页边界与异步状态。
4. API 与 UI 使用同一份需求 ID；接口回归测试和浏览器验收分别关联。
5. 有 UI 消费方却只改后端时，不能以“仅后端”掩盖产品尚未接入；分阶段交付必须列出前端 TODO 与未完成状态。
