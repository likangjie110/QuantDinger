# 01 路由、入口与权限

所属目标与仓库见 [00](00-index.md)。主产品路由是独立仓库的当前代码事实；本仓库不复制主产品 router 源码。

## 已核实入口

| 目标 | 浏览器入口 | 源码位置 | 权限与交互 |
| --- | --- | --- | --- |
| main | `/#/factor-mining` | `QuantDinger-Vue:src/config/router.config.js`、`src/views/factor-mining/index.vue` | dashboard permission；提交/作业/候选三个 tab，提交回执后切换 jobs |
| main | `/#/user/login` | `QuantDinger-Vue:src/router/index.js`、`src/permission.js`、统一 request | hash router；认证失效走统一登录流程 |
| standalone | `/` | [App.vue](../../frontend-factor-mining/src/App.vue) | 单独页面内三个 tab；不是主产品的 factor-mining 路由 |

主产品 dev 默认 8000、preview 默认 8001；独立页 dev 默认 3000。只有使用对应开发命令时这些端口才有效；不得从端口推断部署版本。

## 页面信息与视觉映射

信息层级、主次任务与设计理由按 [09 信息架构与设计决策](09-information-architecture.md) 填写在对应页面/模块文档；线框图、实际截图及基准遵循 [10 布局与视觉基准](10-layout-and-visual-baseline.md)。本表关联具体模块记录与资产，不重复维护设计正文。

| 真实路由/页面 | 用户主任务与关键动作 | 09 信息层级/设计决策记录 | 10 线框图/实际截图/基准记录 | 来源与核实状态 |
|---|---|---|---|---|
| main `/#/factor-mining` | 配置/估价/提交 → 作业状态 → 候选与 AI 提案 | [06](06-factor-mining.md)、[09](09-information-architecture.md) | 未配置；截图未采集 | 代码已见；视觉证据待核实 |
| standalone `/` | 同上，独立兼容页 | [06](06-factor-mining.md) | 未配置；截图未采集 | 代码已见；视觉证据待核实 |
| 其他主产品页面 | TODO(待核实) | 未配置 | 未配置 | `TODO(待核实)` |

图示骨架或示例须明确标注“模板示例”；缺少真实页面证据时保留 `TODO(待核实)`。当前实现、目标设计与已审阅基准按 [10](10-layout-and-visual-baseline.md) 分别引用，不能将当前截图直接当作已认可设计。

## 响应式变化记录

| 页面/区域 | 实测宽度与断点来源 | 重排/换行/收起等变化过程与触发条件 | 信息/动作可达性与滚动行为 | 10 截图与审阅记录 |
|---|---|---|---|---|
| main 因子挖掘 | 768 / 480；`QuantDinger-Vue:src/mobile-responsive.less` | TODO(待核实) | TODO(待核实) | 未采集 |
| standalone 因子页 | TODO(待核实) | TODO(待核实) | TODO(待核实) | 未采集 |

除桌面和移动视口外，记录断点附近及内容开始拥挤的中间宽度，核实切换前后的阅读顺序和关键动作。隐藏信息的取舍关联 [09](09-information-architecture.md)，交互与滚动规则关联 [03](03-state-and-api.md)，验证映射关联 [04](04-test-map.md)；仅两端截图不能证明中间过程正确。

## 路由变更检查

- 新页面同批登记路由、菜单/导航、权限、重定向、深链接和刷新行为。
- main 使用既有 hash router；验收地址必须包含实际 hash 路径，不能只截图首页。
- 没权限的界面及按钮行为、认证失效回跳、跨租户 404 都需要独立场景。
- explorer 应同时读取实际前后端源码根目录，而不是机械寻找不存在的 `server/` / `web/`。
- 其他主产品页面、移动 H5 路由与完整导航清单是 `TODO(待核实)`，首次触及时追加对应模块文档。
