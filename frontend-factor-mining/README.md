# QuantDinger 因子挖掘前端界面

> 当前定位：这是保留兼容的独立 Vue 3 页面，不是 QuantDinger-Vue 主产品 SPA，也不在默认 Compose 前端镜像中。主产品新增 UI 应在 QuantDinger-Vue 实现。源码归属、替代条件与验收见 [前端模块地图](../doc/web_doc/00-index.md)；历史完成报告不代替本次构建、浏览器或部署证据。

全自动量化因子挖掘功能的 Vue 3 前端实现。

## 功能特性

### 1. 提交作业
- 配置挖掘参数（策略源、市场、日期范围、预算等）
- 静态估价预览
- 高级选项配置（冻结比例、分组数、手续费等）

### 2. 作业列表
- 查看所有挖掘作业历史
- 实时进度监控（每 5 秒自动刷新）
- 作业状态跟踪（排队中、运行中、成功、失败、已取消）
- 取消正在运行的作业
- 查看作业详情

### 3. 候选库
- 浏览所有因子候选
- 按状态、市场、运行 ID 筛选
- 查看表达式、指标、样本外表现
- 展开查看完整表达式 JSON

## 技术栈

- **Vue 3** - 渐进式 JavaScript 框架
- **Element Plus** - Vue 3 UI 组件库
- **Vite** - 下一代前端构建工具
- **Axios** - HTTP 客户端
- **Day.js** - 日期时间处理

## 目录结构

```
frontend-factor-mining/
├── src/
│   ├── api/              # API 接口
│   │   └── factorMining.js
│   ├── components/       # Vue 组件
│   │   ├── SubmitJob.vue
│   │   ├── JobList.vue
│   │   ├── JobDetail.vue
│   │   ├── CandidateList.vue
│   │   └── CandidateDetail.vue
│   ├── hooks/            # 自定义 hooks
│   │   └── useI18n.js
│   ├── i18n/             # 国际化
│   │   └── zh-CN.js
│   ├── App.vue           # 根组件
│   └── main.js           # 入口文件
├── index.html
├── package.json
├── vite.config.js
└── README.md
```

## 快速开始

### 前置条件

- Node.js >= 16.0.0
- npm >= 8.0.0
- 后端服务运行在 `http://localhost:5000`

### 安装依赖

```bash
cd frontend-factor-mining
npm install
```

### 启动开发服务器

```bash
npm run dev
```

访问 http://localhost:3000

### 构建生产版本

```bash
npm run build
```

构建产物在 `dist/` 目录。

## API 集成

前端通过代理访问后端 API：

```
前端: http://localhost:3000/api/factor-mining/*
  ↓
代理到: http://localhost:5000/api/factor-mining/*
```

所有 API 响应遵循 QuantDinger 统一信封格式：

```json
{
  "code": 1,
  "msg": "common.success",
  "data": { ... }
}
```

## i18n 国际化

当前支持中文（zh-CN），包含：

- 49 个后端 i18n 键
  - 24 个接口层错误
  - 12 个淘汰原因
  - 8 个模板假设
  - 3 类参数化形式
  - 2 个其他

所有文案在 `src/i18n/zh-CN.js` 中定义。

## 组件说明

### SubmitJob.vue
- 作业提交表单
- 支持静态估价
- 高级选项折叠面板
- 表单验证

### JobList.vue
- 作业列表展示
- 自动轮询（每 5 秒）
- 状态标签着色
- 进度条显示

### JobDetail.vue
- 作业完整信息
- 配置参数展示
- 运行结果统计
- 错误信息显示

### CandidateList.vue
- 候选库列表
- 多条件筛选
- 分页加载
- 展开查看详情

### CandidateDetail.vue
- 候选完整信息
- 表达式 JSON 展示
- 指标详情
- 样本外表现

## 开发注意事项

1. **认证**: 需要在 localStorage 中设置 `token`
2. **代理配置**: 后端地址在 `vite.config.js` 中配置
3. **错误处理**: 所有错误信息通过 i18n 键显示
4. **轮询**: JobList 组件会自动轮询正在运行的作业

## 与后端对接

### 已实现的 API

✅ `GET /api/factor-mining/catalog` - DSL 词表  
✅ `POST /api/factor-mining/estimate` - 静态估价  
✅ `POST /api/factor-mining/run` - 提交作业  
✅ `GET /api/factor-mining/jobs` - 作业列表  
✅ `GET /api/factor-mining/jobs/<id>` - 作业详情  
✅ `POST /api/factor-mining/jobs/<id>/cancel` - 取消作业  
✅ `GET /api/factor-mining/candidates` - 候选库列表  
✅ `GET /api/factor-mining/candidates/<id>` - 候选详情  

## 待优化项

- [ ] 添加 DSL 词表展示页面
- [ ] 候选库分页优化（后端返回 total）
- [ ] IC 序列可视化图表
- [ ] 因子相关度热力图
- [ ] 表达式可视化编辑器
- [ ] 暗色主题支持
- [ ] 英文国际化
- [ ] 单元测试
- [ ] E2E 测试

## License

MIT
