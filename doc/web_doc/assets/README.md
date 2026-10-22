# 图示与视觉资产

`templates/` 中的 SVG 是可编辑的模板示意，所有区域、数值与颜色仅用于说明记录形式，不代表项目页面、真实截图或设计规范。

| 图示 | 用途 |
|---|---|
| [信息层级骨架](templates/information-hierarchy.svg) | 区分用户任务、主要信息、辅助信息与操作反馈 |
| [桌面布局骨架](templates/desktop-wireframe.svg) | 标注区域、尺寸约束、滚动边界和操作位置 |
| [移动布局骨架](templates/mobile-wireframe.svg) | 标注重排、主操作、滚动与安全区域 |

## 在项目中采用

1. 按 [09 信息架构](../09-information-architecture.md) 识别真实用户任务与页面区域。使用对应源码、设计和实际浏览器结果，重画项目图示并标注来源。
2. 复制所需骨架到 `assets/<模块>/`；图源、当前实现截图、目标设计与已审阅基准分别命名和登记。目录布局是建议，已有资产约定优先。
3. 按 [10 视觉基准](../10-layout-and-visual-baseline.md) 登记元信息、类型、审阅状态和已知问题，在对应模块 Markdown 内嵌图片并提供文字摘要。
4. 当前实现截图必须由实际应用采集。实际应用使用 fixture/mock 数据时注明来源与覆盖范围，可记录该条件下的界面外观；不能据此声明真实后端/API/持久化集成已通过。静态图示、生成效果图和独立 fixture 演示页面分别标明用途，不能冒充实际业务页面。
5. 用代表性且可共享的数据采集图片。随模板复制本目录所需图示，不复制 `.verification/`、本机验证日志、依赖和私有内容。

以下仅为资产命名示例，模板未提供这些业务文件：

```text
assets/<模块>/
  information-hierarchy.svg
  desktop-layout.svg
  mobile-layout.svg
  current-desktop-success.png
  target-desktop.svg
  reviewed-desktop-success.png
  reviewed-desktop-success.meta.md
```

截图存在不代表已经审阅或通过验收。改页面同批核对图示、元信息与基准；无法采集或读图时记录阻塞及未覆盖项。
