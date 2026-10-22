# 00 Web 前端模块与交付地图

本目录是 Web 前端的当前态基线。开发前读取对应编号文档，同批更新；编号只增不改。过程记录和本次命令结果保存在实现计划或验收报告。`TODO(待核实)` 不作为行为结论。

## 实际交付归属

| 目标 | 源码与所属仓库 | 开发 / 构建 | 部署目标 |
| --- | --- | --- | --- |
| `main` 主产品 | 独立 `OpenByteInc/QuantDinger-Vue`；通过 `FRONTEND_SRC_PATH` 指定 checkout。Compose build 默认 `./QuantDinger-Vue`，目录名可不同 | Vue 2.7、Ant Design Vue；按该仓库 `packageManager` 与已跟踪 pnpm lock 安装；`pnpm run dev` / `pnpm run build`，dev 默认 8000 | 该仓库的 Dockerfile / release workflow 构建 `quantdinger-frontend`；本仓库 Compose 默认拉预构建镜像，Web 默认端口 8888 |
| `standalone` 独立因子页 | 本仓库跟踪的 `frontend-factor-mining/` | Vue 3、Element Plus、npm；`npm run dev` / `npm run build`，dev 默认 3000 | 单独的 Vite `dist/`；默认 Compose 没有发布此目录 |

本机嵌套检出的主前端仍属于其独立仓库，不把源码、认证数据或本机配置复制进本仓库。默认镜像 tag、主前端 Git revision、本地开发 URL 是三种不同标识，必须分别核对。改变源码目录不会自动改变已部署镜像。

## 交付决策与退役条件

- 新增主产品功能默认交付 `main`；独立页只承担其已存在范围的兼容、契约和验证维护。
- 两个目标分别保持现有组件库、认证和布局；禁止为了绕过主产品接入而再生成一个 UI 工程。
- 保留独立页及历史说明。只有接替主产品实现已通过功能覆盖、五态、响应式、权限、浏览器联调和部署验证，并且清理任务明确授权时，才考虑删除旧实现。
- 进化搜索参数属于 API 已有、主产品已提交基线和独立页尚未完整接入的缺口，详见 [06 因子挖掘](06-factor-mining.md)。主产品工作区另有未提交的枚举/AI 模式改动，按其所属任务验收；本轮治理不把这些业务变化改成“已完成”。

## 模块 → 文档映射

| 编号 | 文档 | 维护范围 |
| --- | --- | --- |
| 01 | [路由与入口](01-routes.md) | 路由、导航、登录、权限、交付目标 |
| 02 | [组件与视觉基线](02-components.md) | 复用组件、token、布局、i18n、响应式、a11y |
| 03 | [状态与 API](03-state-and-api.md) | 五态、字段、错误、鉴权、分页、契约边界 |
| 04 | [测试映射](04-test-map.md) | 真实命令、mock、测试后端联调、证据范围 |
| 05 | [验收清单](05-acceptance-checklist.md) | DoD、阻塞、完成状态、部署证据 |
| 06 | [因子挖掘模块](06-factor-mining.md) | 提交 → 作业状态 → 详情 / 取消 → 候选流程覆盖 |
| 07 | [交付执行流程](07-delivery-workflow.md) 与 [模板附录](07-feature-delivery-template.md) | 影响评估、三面需求、用户闭环、分工与最终报告 |
| 08 | [指令加载核查](08-instruction-loading.md) | 当前指令、技能与角色可用性，区分文件存在与行为证据 |
| 09 | [信息架构与设计决策](09-information-architecture.md) | 用户任务、信息优先级、结构图、设计决策、代表性内容 |
| 10 | [布局与视觉基准](10-layout-and-visual-baseline.md) | 布局图、响应式、视觉 token、资产元信息、当前/目标/基准与已知问题 |

其他产品页面的具体清单为 `TODO(待核实)`。首次触及其他模块时，在有边界的代码核查后追加编号，不能将本次因子页面核查说成全产品覆盖。

## 设计资料入口与最小交付包

- 用户任务、信息层级与设计理由：[09 信息架构](09-information-architecture.md)。
- 布局、实际截图、代表性页面样板与基准生命周期：[10 视觉基准](10-layout-and-visual-baseline.md)。
- 可编辑的示意图与采用说明：[assets/README.md](assets/README.md)。示意不能代替业务事实或截图。
- 页面模块文档模板：[templates/page-module.md](templates/page-module.md)；Playwright 验收模板见 [templates/README.md](templates/README.md)。
- 每个受影响模块关联文字说明、信息结构图、桌面/移动布局图、真实截图、组件/契约/测试及审阅记录；未取得的材料标记待核实并记录阻塞。
- 页面信息架构与区域几何分别表达，图片内嵌到模块文档并配文字摘要。当前实现、目标设计、已审阅基准分开登记；已知问题有独立记录。

## Baseline references

- 仓库入口：[AGENTS.md](../../AGENTS.md)、[DEVELOPMENT.md](../../DEVELOPMENT.md)。
- 部署：[默认 Compose](../../docker-compose.yml)、[源码构建 override](../../docker-compose.build.yml)。
- API：[HTTP 约定](../../docs/architecture/API_CONVENTIONS.md)、[Human API](../../docs/api/README.md)、[因子路由](../../backend_api_python/app/routes/factor_mining.py)。
- 主产品源码引用以 `QuantDinger-Vue:src/...` 表示独立仓库位置，不是本仓库的必需目录。
- 独立页：[入口](../../frontend-factor-mining/src/App.vue)、[API wrapper](../../frontend-factor-mining/src/api/factorMining.js)。

## 维护规则

1. 页面、组件、状态、API 和测试变更必须同批更新对应模块文档与需求表；用户任务、信息层级、布局、文案、主题或代表性数据变化时，同批核对 09/10、图源、截图与元信息，失效资料标记待更新。
2. API、UI、contract 分别记录实现和验证。代码可见、构建通过、mock 通过、联调通过和已发布不得互换。
3. 文档只存当前态口径；进度留在 [治理计划](../../docs/superpowers/plans/2026-10-18-frontend-delivery-governance.md) 或具体功能计划。
4. 口径冲突写明两个来源和待验证项；优先实际代码、受版本管理的配置和新鲜验证。
5. 报告不公开 token、账号、cookie、私有源码、绝对本机路径或未经清理的网络载荷。截图及日志默认保存到被忽略的 `outputs/frontend-delivery/`。
