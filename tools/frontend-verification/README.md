# 前端浏览器验证

本目录提供实际应用的隔离 Playwright 验证入口。`main` 固定验证主产品的 `/#/factor-mining`，`standalone` 固定验证仓库内独立因子页 `/`。独立页的证据不能用于主产品验收。

## 安装与运行

要求 Node.js 20 或以上。只在本目录安装固定依赖；不修改业务前端依赖或全局配置：

```powershell
npm --prefix tools/frontend-verification ci
npm --prefix tools/frontend-verification test
npm --prefix tools/frontend-verification run install:browser
```

已安装 Edge 的 Windows 可以使用 `--browser-channel msedge`，无需下载 Chromium。CLI 不会自行安装依赖或浏览器。

验证 tracked 独立页，并自动启动其现有 Vite 开发服务器（业务前端需已按锁文件安装依赖）：

```powershell
$revision = (git rev-parse HEAD).Trim()
node tools/frontend-verification/verify.mjs --target standalone --source-revision $revision --mode mock --browser-channel msedge --output-dir tools/frontend-verification/artifacts/standalone
```

验证主前端显式 checkout，或验证自己已经启动的同一源码：

```powershell
$source = '../QuantDinger-Vue'
$revision = (git -C $source rev-parse HEAD).Trim()
node tools/frontend-verification/verify.mjs --target main --source $source --source-revision $revision --mode mock --output-dir tools/frontend-verification/artifacts/main
node tools/frontend-verification/verify.mjs --target main --source $source --source-revision $revision --base-url http://127.0.0.1:8000 --mode mock --output-dir tools/frontend-verification/artifacts/main
```

自动启动仅调用显式 checkout 中已安装的 Vite，不执行 npm scripts、不安装包、不构建、不改业务源码，进程在验证后关闭。参数 `--base-url` 仅接受 HTTP(S) origin；路径、查询参数、用户信息和 hash 均不接受。路由由 target 决定。`--source-revision` 必须是完整 Git SHA；提供 source 时会核对包名和 HEAD。只有本工具从核对后的源码自行启动 Vite 并访问该 origin，才记录 `source.bundleVerified: true`。显式 URL 即使本地 HEAD 匹配也不能证明实际 bundle 来源，始终记录 bundleVerified=false，来源检查 blocked；保留页面观察供人工复核，不能晋级为该 revision 的完整浏览器验收。

## Mock 与真实联调边界

Mock 使用实际业务应用，仅通过 Playwright route 拦截 API。每个视口分别验证 loading、empty、error、success、unauthorized。成功场景操作真实表单：输入测试参数 → mock 估价 → mock 提交 → HTTP 202 与请求字段核对 → 列表刷新 → 自动轮询显示 running → 自动轮询显示 succeeded → 同一会话刷新页面 → 详情显示刚输入的配置与结果。列表与详情使用同一模拟生命周期，刷新期间保留内存中的 mock 作业和配置，不重置 fixture；此检查证明 mock 刷新一致性，不能证明数据库持久化。不会向后端提交作业、取消作业或进行交易。

loading 将 jobs 请求挂起并检查实际 loading 组件；empty 返回空列表；error 注入 500 并检查错误提示；unauthorized 注入 403 并要求明确权限提示。错误字段使用既有 `factorMining.runFailed` 与认证工具中 `Permission denied: <permission>` 的格式；jobs 路由没有专用的稳定 500/403 信封，这两项是边界故障注入，不声明其后端契约。提交 mock 严格保留后端 `/run` 的 HTTP 202 信封；候选列表只保证 `data.items`，不凭空补 total。返回了 403、只显示通用失败/原始翻译键，均不能算权限状态通过。error 提示暴露原始 `factorMining.errors.*` 键也会失败。当前页面缺少状态时保留失败与截图，不用替代 UI、样式注入或假通过掩盖。

仅 mock 失败场景的特定 jobs 500/403 是预期网络/console 错误，其余错误仍然失败。未知 API 返回 501 并失败；不访问外部域名；浏览器禁用 Service Worker，防止请求绕过拦截。

真实后端联调需要显式测试 URL 和环境名称：

```powershell
$source = '../QuantDinger-Vue'
$revision = (git -C $source rev-parse HEAD).Trim()
node tools/frontend-verification/verify.mjs --target main --source $source --source-revision $revision --mode integration --base-url http://127.0.0.1:8000 --test-environment test --output-dir tools/frontend-verification/artifacts/integration
```

`--test-environment test|staging` 是调用方对目标的明确声明，不能自动识别部署是否生产；操作者必须先核实。联调使用全新、无个人登录态的浏览器，不接受真实账号、密码、token 或个人 storage state。仅允许 factor-mining catalog/jobs/candidates 与 auth/info 的 GET/HEAD，拦截其余 API 和所有写操作。页面需要登录时会报告 blocked；需在隔离测试环境准备匿名/受控测试访问后重跑。此入口没有默认登录或交易行为。

联调检查真实页面、jobs HTTP 200、JSON `code=1` / `data.items`、UI 的对应行或空结果，以及页面刷新后的只读结果一致性；不能验证提交或五态。报告将观察结果记录在 `integration.readOnly.status`，`integration.scope` 固定 `read-only`，完整 `integration.status` 保持 unverified。显式 URL 的来源检查仍 blocked。Mock 永远不能令 integration 通过，deployment 永远为 unverified。

## 证据格式

输出目录中固定生成 `browser-report.json`、`console.json`、`network.json`、`axe.json`；截图包含 `1440×900` 与 `390×844`，每个状态独立命名。文件路径相对报告目录，证据目录由本目录 `.gitignore` 排除。

报告 `schemaVersion: 1`：

```json
{
  "schemaVersion": 1,
  "target": "standalone",
  "sourceRevision": "<full-source-git-sha>",
  "mode": "mock",
  "route": "/",
  "status": "failed",
  "screenshots": [
    { "state": "success", "viewport": { "width": 1440, "height": 900 }, "path": "success-1440x900.png" }
  ],
  "console": { "status": "passed", "path": "console.json", "errorCount": 0 },
  "network": { "status": "passed", "path": "network.json", "unexpectedErrorCount": 0 },
  "axe": { "status": "passed", "path": "axe.json", "criticalCount": 0 },
  "states": { "unauthorized": { "status": "failed", "checks": ["unauthorized-1440"] } },
  "checks": [],
  "integration": { "status": "unverified" },
  "deployment": { "status": "unverified" },
  "limitations": []
}
```

退出码：`0` 表示已执行范围通过，`1` 表示观察到失败，`2` 表示依赖、来源、应用或参数阻塞。依赖缺失、浏览器未安装、axe 无法运行都会保留 blocked，不能用缺少 critical 计数推断通过。`axe.json` 保存所有级别违规摘要和节点数，critical>0 会失败；不保存页面 HTML。键盘/焦点、完整 a11y 和截图布局质量仍需审阅。

Console 只保存类型、路径分类、消息 SHA-256 与预期标识，不保存原文或参数，避免应用意外打印账号/凭据。Network 不保存 host、query、headers、cookies、请求体或响应体，作业/候选 ID 归一化为 `:id`。截图仍可能包含目标测试数据，只在明确测试环境采集并按团队策略保管。不得把截图视为自动视觉通过。

交付检查器可用 `--browser-report <目录>/browser-report.json` 读取结果，必须核对相同 target/sourceRevision，并区分 mock、真实联调及部署。

## 官方 API 依据

- Playwright network mocking：<https://playwright.dev/docs/mock>
- Playwright accessibility / AxeBuilder：<https://playwright.dev/docs/accessibility-testing>
- Playwright API：<https://playwright.dev/docs/api/class-page>、<https://playwright.dev/docs/api/class-browsercontext>

实现使用官方 `BrowserContext.route` / `Route.fulfill`、事件采集、截图和 `AxeBuilder.analyze()`；不连接个人浏览器，也不使用关闭浏览器安全隔离的参数。
