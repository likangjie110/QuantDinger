# Playwright 用户闭环验收模板

这是接入契约，不是已经验收通过的项目用例。`acceptance` 默认 `undefined`，运行会报 `FRONTEND_ACCEPTANCE_NOT_CONFIGURED`；不能删除配置检查、跳过测试或用 `body` 可见把它改成绿灯。

## 复制和依赖

先读目标项目的 `AGENTS.md`、锁文件、`package.json` scripts、路由和现有 E2E 配置；已有 Playwright 时合并用例，保留项目原配置。没有配置时，复制 `playwright.config.ts` 到项目根、`frontend-acceptance.spec.ts` 到 `tests/`。按项目真实脚本修改启动命令、端口、鉴权状态与测试数据。

下面仅为 npm 项目的命令示例，使用项目锁文件对应的包管理器，不创建第二种锁文件：

```powershell
npm install -D @playwright/test @axe-core/playwright
$env:PLAYWRIGHT_SKIP_BROWSER_GC = '1'
npx playwright install chromium
```

模板配置默认运行 1440×900 / 390×844 两个项目。已有配置必须保留这两个验收视口或给出项目等效证据。`BASE_URL` 存在时不自动启动服务器，必须先启动目标应用；未设置时模板默认 `npm run dev`，复制后先核实此脚本确实存在。

## 页面设计资料的采用

- 信息架构与设计决策使用 [09](../09-information-architecture.md)，布局、视觉资产和审阅使用 [10](../10-layout-and-visual-baseline.md)，示意图说明见 [assets](../assets/README.md)。复制被引用的图示，保留相对目录并检查项目中的全部链接。
- 按 [页面模块模板](page-module.md) 为真实页面建立编号文档。复制到 `web_doc` 根目录后修正模板内的相对链接；填入真实任务、信息层级、布局约束、组件、数据集、状态与验收证据，替换示意图片。
- 先准备代表性内容与数据，按真实断点检查桌面、移动及中间变化过程；组件样例优先复用项目现有运行入口，不因模板强制新增展示依赖。
- 实际打开设计图和运行截图，记录资产 ID 与观察；当前实现、目标设计、已审阅基准分别登记，已知缺陷不能默认为设计约束。
- 本模板 runner 保留既有业务/状态/信号验收。文档新增的视觉审阅、组件演示和自动截图比较不会因复制文件自动执行；按 04/05 填入项目用例或人工步骤并提供实际结果。
- 仅模板图示的可解析、可渲染与链接检查不能代表任何业务页面验收；保留“模板未配置”直到有对应项目事实和新鲜证据。

## 接入真实闭环

把 `const acceptance: AcceptanceConfig | undefined = undefined` 替换为项目配置。所有字段均来自项目事实，不能沿用演示路由或猜测选择器；配置和证据映射写入 `doc/web_doc/04-test-map.md`。

| 字段 | 必须验证的内容 |
|---|---|
| `routes[].path / ready` | 真实页面路由与业务就绪条件，例如有业务含义的标题、表单或记录；`setup` 可配置登录会话 |
| `userFlow.entryPath / enter` | 从真实入口开始，通过页面操作进入目标功能 |
| `fill / submit` | 填写合法输入并点击提交；独立测试数据避免双视口/并发互相污染 |
| `expectedApi` | 有 API 时填写大写 method、完整 URL（含实际查询串）、精确成功 status；监听在提交之前建立 |
| `assertApi` | 断言真实响应 payload、字段或业务 ID，不能只检查 HTTP 200 |
| `apiNotApplicable` | 静态页面/纯客户端流程无 API 时填 `{ reason, evidence }`，与 `expectedApi / assertApi` 互斥；两者缺一均失败 |
| `assertResult` | 断言提交后页面展示的具体业务结果及输入数据 |
| `assertPersistedResult` | runner 自动 `page.reload()` 后验证约定的刷新行为：持久写操作检查同一业务 ID/值；临时客户端状态检查正确重置或恢复 |
| `stateCases[].route` | 引用 `routes[].name`，按每个受影响路由核验六态，不允许 A 页用例代替 B 页 |
| `stateCases[].setup / assertState` | 该路由状态的独立触发方法与可见 UI 断言；`path` 是场景的真实访问 URL |

`runClosedLoop` 固定执行入口 → 输入/操作 → 提交 → 页面结果 → 刷新 → 约定的刷新结果。有 API 时另外等待精确响应并断言 payload；无 API 时必须提供 `apiNotApplicable` 理由和事实证据，不为验收编造接口。只渲染页面、只检查 API、只截图均不满足该闭环。只读流程确实没有输入或写入时，先在项目验收文档说明适用闭环，再按已有读取/刷新契约实现步骤；临时状态不强制刷新后保留相同值。

`stateCases` 统一使用 `loading / empty / error / success / unauthorized / forbidden`，每个受影响路由都要覆盖；`routes[].name` 必须唯一，`stateCases[].route` 必须引用其中一个名字。`setup` 在导航前安装项目级响应控制或准备真实场景；loading 需保持响应未完成并验证占位/提示，不能等待成功后再宣称 loading 已测。401 与 403 分别验证未登录、无权限页面表现。确实不适用的状态写入 `notApplicable[routeName][state] = { reason, evidence }`，二者都必填且不得同时声称已覆盖；这是公开的验收例外，不是测试通过证据。`success` 不可标记不适用，每个路由必须有真实成功态用例。`disabled` 单列为交互状态，在项目用例里验证按钮禁用、重复提交及恢复。

## 精确限定预期失败

所有 4xx/5xx（包含 401/403）、console error、pageerror、requestfailed 默认导致失败。`expectedResponses` 只放在明确要触发错误的那个流程/页面/状态中，并使用大写 method + 完整 URL + status 精确匹配；不提供全局通配豁免。

下面的 `projectFacts` 与 `assertForbiddenUI` 代表必须由项目实现的事实与断言，不是模板自带业务：

```typescript
expectedResponses: [{
  method: 'GET',
  url: new URL(projectFacts.permissionApiPath, projectFacts.baseURL).href,
  status: 403,
  count: 1,
  reason: '本用例使用缺少该功能权限的已登录测试账号',
  assertUI: assertForbiddenUI,
}]
```

runner 要求该响应按 `count` 精确出现（默认 1），再执行 `assertUI`；缺响应、意外重复请求、UI 断言失败或其他接口/方法/状态错误都会失败。业务确需多次请求时显式设置次数，不以“至少一次”掩盖鉴权重试循环。预期 HTTP 错误可能由浏览器额外产生资源加载 console error；只有本机实测后才配置该规则的 `consoleErrors: [{ text, url }]`，按完整文本和来源 URL 限定，URL 必须等于该请求 URL，文本必须是该 status 的浏览器 `Failed to load resource` 资源诊断，规则次数及 UI 断言都必须通过。应用自身 console error、`pageerror` 和传输失败不豁免。每条响应错误都保留在信号附件中，即使被明确允许。

## 命令与证据

```powershell
npx playwright test tests/frontend-acceptance.spec.ts --list
npx playwright test tests/frontend-acceptance.spec.ts
npx playwright test tests/frontend-acceptance.spec.ts --grep '@a11y'
npx playwright show-report
```

先运行完整验收；仅 `--list` 证明用例可发现，单跑 `@a11y` 不能证明主流程可用。按项目真实脚本另外执行 lint、typecheck、unit test、build，记录命令和退出码。

- 每个真实页面、六态用例和刷新后的业务结果在各自测试输出下生成 `evidence/*.png`，默认位于 `test-results/<用例与视口>/evidence/`。使用 `testInfo.outputPath` 防止并发、重试、双视口覆盖，并附入 HTML report。
- 每个浏览器用例附 `browser-signals` JSON：console error、pageerror、requestfailed、全部 4xx/5xx、意外错误，以及每条精确例外的 method/URL/status/理由/预期次数/实际次数。失败时仍保留信号；配置中的失败截图、trace/video 用于诊断。
- 截图证明外观，API/UI/刷新断言证明业务行为。静态图片不能代替交互通过证据。首次截图仅为审阅证据，需要视觉回归时另建立经审阅的 `toHaveScreenshot` 基线。
- axe 对实际页面检查 critical 和 serious 违规；键盘、焦点、读屏语义、对比度及 i18n 仍按项目测试映射记录覆盖范围。
- 提交验收结果时提供真实路由、运行命令/退出码、报告与双视口截图路径、主流程及状态对应断言、不适用项理由和未覆盖风险。模板复制成功或 fixture 通过不能声称业务项目已验收。
