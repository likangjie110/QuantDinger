# 02 组件与界面基线

优先复用当前交付目标的 UI 系统，新增组件先检查已有实现。目标选择见 [00](00-index.md)，五态见 [03](03-state-and-api.md)。本文件记录既有规范与验收底线，不代表旧页面已全部通过。

## 主产品 main

| 范围 | 已核实基线 | 维护要求 |
| --- | --- | --- |
| 组件库 | Vue 2.7、Ant Design Vue 1.7.8 | 使用现有 `a-*` 控件与其交互模式；不把 Element Plus 放进主产品 |
| 页面容器 | `qd-workspace-page`、`qd-page-frame` | 复用现有布局、主题与页面密度 |
| Design tokens | `QuantDinger-Vue:src/styles/workspace-system.less` 的 `--qd-space-*`、`--qd-radius`、`--qd-border`、`--qd-surface`、`--qd-text*`；`src/styles/antd-vars.less` | 新增 UI 优先使用已有 token；变更 token 先做设计评审并检查受影响页面 |
| 请求与认证 | `src/utils/request.js` | 复用 cookie/token/语言 headers 与 401/403 处理；组件不自行保存新 token |
| i18n | `src/locales/index.js`、`src/locales/factor-mining-overrides.js` | 使用既有平铺 dotted keys；中英文同批修改与验证，不硬编码新增文案 |
| 响应式 | `src/mobile-responsive.less`，既有 768 / 480 断点 | 不复制另一套断点体系；桌面和移动的可用性分别验收 |

因子模块复用 `SubmitJob`、`JobList`、`JobDetail`、`CandidateList`、`CandidateDetail`。新增组件写明复用范围、输入/事件、状态与所属模块，避免为同一流程再建一个页面工程。

## 设计资料与组件样例

组件在页面中的主次关系与取舍引用 [09 信息架构与设计决策](09-information-architecture.md)，尺寸、排版、状态外观、响应式变化及截图引用 [10 布局与视觉基准](10-layout-and-visual-baseline.md)，不另建重复基准。

| 真实组件/变体 | 现有样例或页面入口 | 复现步骤 | 必需 props/上下文与状态 | 实际结果与证据 |
|---|---|---|---|---|
| `SubmitJob` / `JobList` / `JobDetail` / `CandidateList` / `CandidateDetail` | main `/#/factor-mining`；standalone `/` | 按 [04](04-test-map.md) 启动对应应用 | 见 [03](03-state-and-api.md) 的请求/状态契约 | 未执行；样例通过不等于业务闭环通过 |
| 其他组件 | `TODO(待核实)` | — | — | — |

组件样例优先复用实际页面入口，不因模板强制新增展示依赖。新增样例须标注“组件示例”，其通过不代表业务闭环通过。

## 组件展示与交互约束

| 组件/变体/状态 | 内容边界与 03 数据记录 | 焦点进入/顺序/返回与 Esc 行为 | 提交/恢复/未保存与滚动规则 | 04 用例及 10 视觉记录 |
|---|---|---|---|---|
| `SubmitJob` | 三种搜索模式与字段裁剪见 [03](03-state-and-api.md) | `TODO(待核实)` | 提交/估价防重复；失败保留输入 | [04](04-test-map.md) FM-01/02；未执行 |
| `JobList` / `JobDetail` | 作业状态与 `summary.ai`、提案列表 | 详情 modal 焦点与关闭返回 `TODO(待核实)` | 活动任务轮询、终态停止、错误重试 | FM-03/06/07/09；未执行 |
| `CandidateList` / `CandidateDetail` | `items`、limit/offset 与详情独立获取 | `TODO(待核实)` | 筛选后页码重置、滚动位置规则 `TODO(待核实)` | FM-05/08；未执行 |

代表性数据与交互定义引用 [03](03-state-and-api.md)，用例关联 [04](04-test-map.md)；状态外观与截图引用 [10](10-layout-and-visual-baseline.md)。

## 独立兼容页 standalone

其 [main.js](../../frontend-factor-mining/src/main.js) 使用 Vue 3 + Element Plus，[App.vue](../../frontend-factor-mining/src/App.vue) 定义独立布局，[API wrapper](../../frontend-factor-mining/src/api/factorMining.js) 读取本页自己的 token。它们是兼容目标的局部基线，不能用来替代主产品认证或主题规范。

本轮只增加验证工具，不进行视觉重设计、token 合并或组件迁移。后续涉及独立页时仍遵循五态、响应式和 a11y 底线。

## 交互与可访问性底线

- 表单有可访问名称与对应 label，错误关联到输入；必填提示不能只靠颜色。
- 所有核心操作可用键盘完成，有可见焦点；控件不制造键盘陷阱。
- 打开对话框后焦点进入其中，关闭后回到触发操作；取消与确认语义不同。
- 按钮、标题、表格、列表使用恰当语义；异步加载与错误可被辅助技术感知。
- 不以颜色单独区分 queued/running/失败等状态；检查文本与背景对比度。
- 在 1440×900 与 390×844 记录截图；无意外横向溢出、遮挡操作或无法关闭的弹层。
- axe 检查不得有 critical 违规；键盘、焦点和所有人工视觉项仍须实际检查，不能以自动扫描替代。

新页面、公共 token 改动先交设计评审。若当前环境没有 `ui-design-agent`，使用实际可用的审查角色并注明检查范围，不能声称调用了不存在的角色。
