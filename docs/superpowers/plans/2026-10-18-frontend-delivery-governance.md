# 前端交付治理实现计划

> 执行方式：当前会话协调文档、只读 explorer 和独立 worker；用户已批准以下五项顺序，无需重复确认。未经请求不提交、不发布、不删除旧实现。

**目标：** 让用户可见功能按页面、API、契约共同交付，并以实际前端命令和浏览器证据报告完成状态。

**架构：** 主产品 UI 由独立 `QuantDinger-Vue` 仓库负责，本仓库通过显式源码目录调用其已有脚本。仓库内独立因子页保留；新增公共交付文档、验证入口与 CI，不把本机私有源码复制到公共仓库。

**技术栈：** Python 标准库（交付检查与单测）、已有 Vue 2.7 / Vue 3 工程、Node.js、Playwright、GitHub Actions。

**规格：** 本次用户批准的五项治理要求，以及会话 AGENTS.md 的「前端开发门禁」与 §8.1。

## Baseline read set

- `AGENTS.md`、`CLAUDE.md`、`DEVELOPMENT.md`、`.github/PULL_REQUEST_TEMPLATE.md`。
- `docker-compose.yml`、`docker-compose.build.yml`、`.github/workflows/basic-ci.yml`、`.github/workflows/docker-publish.yml`。
- `frontend-factor-mining/package.json`、`vite.config.js`、`src/App.vue`、`src/api/factorMining.js`、提交/作业/候选组件。
- 外部主前端的 `package.json`、已跟踪 `pnpm-lock.yaml`、Dockerfile、router/config、统一请求、工作区样式、因子页与现有单测（只按相对位置登记，不复制私有源码）。
- `backend_api_python/app/routes/factor_mining.py`、`app/services/factor_mining/engine.py`、`tests/test_factor_mining_api.py`。
- `scripts/check_docs.py`；已有用户改动必须保留。

## 边界与影响面

- 主前端源码的本机检出名不是部署标识。默认镜像、源码目录、构建 revision 必须分别记录。
- 主前端包管理器由已跟踪 pnpm lock、`packageManager` 和 Dockerfile 确认；不采用被该仓库忽略的 npm lock。
- 主产品路由为 hash router 的 `/#/factor-mining`。独立页入口为 `/`；两者的浏览器证据不可互相替代。
- 不改交易 runtime、API 授权、持久化或实盘保护；不实施因子挖掘 v3，也不在本轮补全 v2 搜索控件。
- 不把没有运行、缺少脚本、仅 mock、未发布或只验证独立页的结果报告为主产品联调完成。
- 不覆盖 `.gitignore`、`scripts/check_docs.py` 和其他任务已有修改；`DEVELOPMENT.md` 只原子修改本轮前端段落。

## 原子任务与 owner

### 1. 登记真实前端交付归属（协调者）

- [x] 修改 `AGENTS.md` 的 repository map，区分外部主产品源码与 tracked 独立页。
- [x] 建立 `doc/web_doc/00-index.md`，登记模块、源码、开发入口、包管理器、构建与部署目标。
- [x] 修订 `DEVELOPMENT.md` 前端段落；`CLAUDE.md` 保持转引 AGENTS，补文档入口。
- [x] 给独立页添加当前定位和退役条件，不删除实现。

### 2. 三面清单变成需求覆盖表（协调者）

- [x] 创建 `doc/web_doc/03-state-and-api.md` 与 `06-factor-mining.md`。
- [x] 每条流程分别登记 UI / API / contract / 状态 / 验证 / 当前缺口。
- [x] 进化模式控件缺失、主产品未完成浏览器验收等保持明确未完成。

### 3. 按用户流程建立任务模板（协调者）

- [x] 创建 `doc/web_doc/07-feature-delivery-template.md`，附交付流程与规则加载核查入口。
- [x] 每项任务包含前端 owner、后端 owner、共享契约、浏览器 verifier 和退出条件。
- [x] 更新 PR 模板，分别报告后端、前端、联调、部署和 UI 证据。

### 4. 可执行的前端基线（协调者）

- [x] 创建 `01-routes.md`、`02-components.md`、`04-test-map.md`、`05-acceptance-checklist.md`。
- [x] 固化现有 UI 库、认证、请求、布局/token、i18n，明确五态、响应式和键盘/焦点/语义/对比度底线。
- [x] 只为已核实的因子模块写具体页面文档；其他模块登记为 TODO(待核实)，不伪造全产品覆盖。

### 5. 验证入口、CI 与报告（worker + 浏览器 verifier）

- [x] 先给交付检查写标准库单测：缺源码、缺脚本、锁文件识别、命令失败、脱敏报告、仅部分验证不可算全量完成。
- [x] 实现 `scripts/check_frontend_delivery.py` 和 `scripts/tests/test_frontend_delivery.py`，外部源码通过参数传入。
- [x] 创建 `tools/frontend-verification/`：可重复的 Playwright mock 流程与独立的只读测试后端检查入口；截图、console/network、axe 证据单独保存。完整写入/持久化联调保持未验证。
- [x] 创建 `.github/workflows/frontend-delivery.yml`：公共工具测试 + tracked UI 范围检查、静态与实际 mock 浏览器 job；主产品通过显式外部 checkout/ref 验证。不静默跳过后宣称已验证，远端 workflow 尚未执行。
- [x] 命令依赖缺失、typecheck 尚未配置、真实测试后端未就绪时输出 blocked/unverified 与下一步命令，保持非成功状态。

### 验证入口的收尾范围

- 增加独立页非修复 lint、完整 JS/Vue 模板 typecheck、7 项 API 契约单测和可跟踪 npm lock；运行时依赖与页面行为不变，API 只补 JSDoc。
- 检查器对 target/package identity、实际包管理器版本、缺命令、失败、超时和证据路径/字段做检查；错来源和 mock/只读结果不能提升为完整联调或部署。
- Windows npm 采用同一 Node 安装的 npm CLI，避免 PowerShell 与 PATH shim 解析到不同版本；不改全局 npm、PATH 或用户配置。
- Mock 工具在实际页面操作表单、断言 HTTP 202、轮询完成、reload 和详情配置；fixture 不证明数据库持久化。
- 类型、错误文案和权限失败作为当前产品缺口保留，不放宽门禁来获得通过。当前 scope 守卫覆盖因子 Web API 与 tracked 独立页；其他模块需按影响分析扩展映射。

## 验证目标

- `python -m unittest discover -s scripts/tests -p test_frontend_delivery.py -v`：验证交付入口的失败/报告语义。
- `python scripts/check_docs.py`：验证编号文档与本地链接，不改用户已有 docs checker。
- `python scripts/check_frontend_delivery.py --help` 与实际运行：参数与当前项目状态一致。
- 读取 source 后运行现有 lint、unit、build；缺少 typecheck 不能改名为语法检查蒙混通过。
- Playwright mock 测试使用隔离浏览器，覆盖可复现的 UI 状态；连接后端测试要求明确测试 URL/账户，禁止默认连本机生产服务。
- 1440×900、390×844 截图；console、network、axe 报告；仅报告实际执行且通过的范围。
- `git diff --check` 与末次 status：检查变更边界，不 stage 独立前端仓库内容。

## 审查重点

1. 缺少主前端源码或某检查命令时，结果必须是 blocked，而不是因跳过而通过。
2. 外部源码目录允许显式设置，但禁止把私有源码、token、完整环境写进公共产物。
3. Mock 流程、测试后端联调、真实部署必须分别计数，不能互相替代。
4. CI 不得因私有源码缺失而伪造主产品通过，外部 checkout 只在显式输入和权限下执行。
5. 既有未提交修改和独立前端仓库保持原状；不把退役条件变成删除指令。

## Checkpoint / resume hint

- 当前：五项治理交付物已落盘，工具回归、文档和 CI YAML 已验证。独立页仍有 3 处类型错误与两视口错误/权限状态失败；主产品缺类型脚本、unit 入口在当前 Node 失败，不能报告 UI 完成。
- 角色适配：专用 explorer 绑定模型不可用；已由可用通用子代理完成相同只读职责，未重复调用失败配置。
- 新鲜证据：`outputs/frontend-delivery/main-static.json`；独立浏览器证据由 `tools/frontend-verification/artifacts/` 的 target 子目录隔离保存。真实后端联调与部署未执行。
- 主产品在基线验证后出现范围外的未提交 AI 模块变更，已保留。此前 lint/build 和手动 glob 的 388 项单测只对应验证时基线，不代表后来工作区已通过；当前业务状态仍需其所属任务验证。
- 本轮未刷新 graphify：变更集中于文档、验证工具和类型说明，无业务运行时调用链变更；未提交状态不生成额外图谱产物，导航结果仍需回到源码验证。
- 恢复时先读本计划、`doc/web_doc/00-index.md`、`git diff --stat` 与测试输出；以代码和 fresh verification 为准。
- drift check：若需要修改业务 UI、token、认证、API 或部署拓扑，先更新范围，不能混入本轮治理。
