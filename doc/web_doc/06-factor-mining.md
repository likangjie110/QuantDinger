# 06 因子挖掘页面

- 目标用户与主任务：研究用户估价并提交挖掘任务，观察运行状态、查看结果和候选。
- 主产品入口：`/#/factor-mining`，dashboard 权限；模块位于 `QuantDinger-Vue:src/views/factor-mining/`。
- 独立兼容页入口：`/`，位于 [frontend-factor-mining/src/App.vue](../../frontend-factor-mining/src/App.vue)。
- 契约：[03 状态与 API](03-state-and-api.md)；测试：[04 测试映射](04-test-map.md)。

## 信息架构、布局与视觉记录

- 用户角色、主任务、信息优先级与设计决策：[09 信息架构与设计决策](09-information-architecture.md)。
- 布局图、真实截图、视觉 token、资产元信息与基准维护：[10 布局与视觉基准](10-layout-and-visual-baseline.md)。
- 当前状态：主产品本机真实联调已采集并审阅桌面/移动截图，已知暗色对比度和窄屏裁切问题见 [本机运行验证](../../docs/superpowers/plans/2026-10-20-factor-mining-local-validation.md)；目标设计与共享视觉基准未配置。下方状态矩阵与组件约束仍是验收义务，不能由局部运行证据推断全覆盖。
- 待补：standalone `/` 的本轮截图、两个目标的完整布局图/共享资产及合格基准；准备方式与阻塞按 [10](10-layout-and-visual-baseline.md) 登记。

## 三面需求覆盖表

“代码已见”仅说明静态实现存在，不能替代命令或浏览器验证。实现状态和验收状态分别记录；本表不是逐次运行日志。

| ID / 用户操作 | UI 面：页面、组件与交互 | API / 契约面 | 代码现状与缺口 | 验收证据要求 |
| --- | --- | --- | --- | --- |
| FM-01 配置、估价并提交 | SubmitJob 表单、必填校验、估价结果、提交防重复、成功转作业列表；AI 模式显示轮次、每轮条数、研究笔记 | POST estimate/run；202 回执；jobId/config；AI 字段 `searchMode/aiRounds/aiProposalsPerRound/researchNotes` | 主产品代码已见；枚举模式不发送 AI 字段；独立页是单独实现；真实联调未由本轮静态检查证明 | 浏览器实际填写、payload、202、相同 jobId/config 回显；测试后端记录 |
| FM-02 选择搜索模式 | enumeration/ai/evolution；进化参数 population/generations/eliteRatio，模式切换裁剪字段；holdoutRatio 初值 0.10 | 三模式；AI 轮次 1–3、每轮 1–20、笔记 ≤8000；holdoutRatio 0–0.30 | 主产品新增进化与保留段控件；实际验收证据单列于 P2 计划 | 实际选择进化、提交、检查字段与详情；其他模式不得携带进化参数 |
| FM-03 查看任务状态和详情 | JobList、JobDetail、刷新、活动任务轮询、终态停止、详情对话框；AI 摘要与提案表 | GET jobs / jobs/<jobId>；queued/running/succeeded/failed/cancelled；`summary.ai`；`GET jobs/<jobId>/proposals` | 主产品已有 5 秒活动轮询与销毁清理；详情按 AI 模式加载提案，modal 内 loading/空/失败/404 可重试 | 验证 loading→运行→终态、同一任务、退出后停止轮询、失败与空态区分；提案空态/失败/404 单独验证 |
| FM-04 取消任务 | 确认/返回、取消中状态、重读任务状态 | POST jobs/<jobId>/cancel；租户、终态语义 | 组件中已有取消路径；不能从按钮存在推断取消已成功 | 测试账户在隔离后端提交；确认前不发请求；取消后检查服务端终态 |
| FM-05 筛选并浏览候选 | CandidateList / CandidateDetail、筛选重置、上一页/下一页、空态 | GET candidates；runId/status/market/limit/offset；详情独立获取 | 主产品 limit=50，以满页推断下一页，没有可依赖的 total | 0/不足一页/满页，筛选后页码重置，详情与列表 schema 分开验证 |
| FM-06 错误与权限 | 提示、保留输入、可重试、统一登录失效处理；详情错误在 modal 内展示并带重试 | 400 key；401/403；越权 404；网络/500；AI 错误键 `aiProposalFailed`、`aiNoValidProposals`、`aiReplayNotFound`、`aiReplayIncompatible`、`reject.duplicateProposal` | 错误多为消息反馈；详情与提案错误已改为区块内提示；独立 error/empty 模型与五态截图尚需验证 | 每个预期错误单独场景；检查无泄露、无无限 loading，预期网络错误清单 |
| FM-07 AI 显式重放 | 可重放 AI 终态任务发起重放；仅发送 searchMode/replayJobId | 服务端复制源配置和提案；错误保持原契约 | 主产品新增重放入口；无伪造估价 | 核对两字段请求、新 job 回执、刷新后仍可读取新任务 |
| FM-08 任务直达候选 | 任务行跳转候选，URL query 保存 tab/runId | candidates?runId；当前行按 `(user_id, market, timeframe, expression_hash)` 唯一，同 context 重评更新 `runId`，跨 context 保留 | 主产品新增导航与刷新恢复 | 同 run 筛选只表示最后由该 run 判定的当前候选；旧 run 的历史评估从 run result/trials 读取，不把当前候选筛选当历史清单 |
| FM-09 研究审计 | 详情显示研究组、累计 trial、冻结版本与保留段边界 | summary.research；holdout.evaluated=false | 新研究组表持久化；旧 run 不回填 | 新旧结果兼容，保留段仅预留；指纹不能冒充行情快照 |

## 作业与候选持久化边界

每租户最多一个活跃挖掘 job；提交在数据库事务内串行检查并插入。Celery 与本地 fallback 共用持久 execution token / renewable lease，接管会替换 token，旧 owner 的进度和结果写入被拒绝。默认租约 120 秒、最多 3 次 attempt；超过 60 秒未派发的 queued 作业和 lease 过期的 running 作业由现有 maintenance 与用户作用域查询入口有界恢复。一次接管保留旧 failed run 审计并用同一 jobId 新开 run；AI 从最近含已保存提案的失败 attempt 恢复，不重新请求模型。

搜索幸存候选只在冻结复核完成后发布。候选、成功 run 与成功 job 在一个事务内写入；取消与发布竞争同一 job 行锁，取消先提交则不新增候选，发布先提交则返回成功状态。候选当前行按 `(user_id, market, timeframe, expression_hash)` 唯一，同一评估 context 更新当前行，跨 context 保留独立指标。详见 [P1 实施与验证记录](../../docs/superpowers/plans/2026-10-19-factor-mining-p1.md)。

独立页已识别的现存缺口：类型检查在 tab model、input-number prop 和候选列表信封长度使用处失败；403 场景仅显示原始 Axios 错误文本，不构成清晰的权限反馈。代码与浏览器工具会保留这些失败；本轮不通过排除文件或接受原始错误文案改变验收定义。

## 状态矩阵

| 页面区域 | loading | empty | error | success | 无权限 |
| --- | --- | --- | --- | --- | --- |
| 提交 | 估价 / 提交按钮状态 | 尚无估价时的说明 | 参数 / 网络 / provider 错误 | 估价或作业回执 | 统一认证，不发未授权提交 |
| 作业 | 初载 / 刷新 | 成功且 items=[] | 失败不能变成“无作业” | 列表、状态、详情 | 401/403 与不存在/越权 404 分开 |
| AI 摘要 / 提案 | 详情与提案分区 loading | 无 AI 摘要；提案 items=[] 显示空态 | 详情失败在 modal 内 alert + 重试；提案失败为区块提示，不阻塞详情 | 摘要计数与提案表（宽屏）/ 卡片（窄屏）（limit=200，覆盖单次 60 条上限） | 404 统一按 `jobNotFound` 处理，不暴露存在性 |
| 候选 | 加载 / 翻页 | 筛选无结果 | 提示与重试 | 列表 / 表达式 / 指标详情 | 仅当前用户数据 |

矩阵中的条目是验收义务。现有错误处理是否完全满足矩阵，必须用 [浏览器证据](05-acceptance-checklist.md) 验证，未执行时保持未验证。

## 组件、布局、响应式与 a11y

- 主产品复用 Ant Design Vue、`qd-workspace-page` / `qd-page-frame`、现有 `--qd-*` tokens；不得引入 Element Plus 控件。
- 主产品使用 768 / 480 的既有移动断点。检查 1440×900、390×844，同时确认筛选和对话框不遮挡关键操作。
- 表单标签关联、键盘顺序、可见焦点、对话框焦点归还、异步错误可感知；不以颜色单独表达状态。
- 进化/保留段数值控件提供关联标签与可访问名称；暗色高级设置面板、研究提示和重放配置使用对应主题文字色，重放提交按钮保证可读对比。全页 a11y 的检查结果与未覆盖项单独记录于 P2 验收报告，不能从局部样式推断整页通过。
- 主产品 `factorMining.*` 文案使用现有 locales override；独立页使用其现有 hooks/i18n，不能据此声称主产品中英文已覆盖。

## 保留与替代

独立页当前继续保留。主产品覆盖 FM-01～FM-06 的必要功能、通过对应浏览器与测试后端验收并有部署证据后，才具备讨论旧实现退役的依据；删除必须属于明确清理任务。

AI 提案等 v3 范围跟随其独立规格和计划。主产品已有枚举/AI 模式、提案 API 与作业列表实现和此前 mock/单元证据；本机真实联调已验证枚举、进化、取消、刷新和候选读取。当前本机未配置模型 key，AI 提案、反馈与重放的真实闭环仍未覆盖；部署未验证，不能将局部运行或旧基线检查视为整体通过。详情见 [本机运行验证](../../docs/superpowers/plans/2026-10-20-factor-mining-local-validation.md)。
