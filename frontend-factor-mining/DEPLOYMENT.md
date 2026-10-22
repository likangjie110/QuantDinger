# 因子挖掘功能完整部署指南

> 范围：本指南仅描述独立兼容页面。默认 Compose 发布的是独立仓库 QuantDinger-Vue 的镜像，见 [当前交付归属](../doc/web_doc/00-index.md)。以下清单是既有实现说明；当前运行质量以 [前端验收](../doc/web_doc/05-acceptance-checklist.md) 和 fresh verification 为准。

## 一、架构概览

```
┌─────────────────────┐
│  前端 Vue 3 应用     │
│  localhost:3000     │
│  (因子挖掘 UI)       │
└──────────┬──────────┘
           │ HTTP /api/factor-mining/*
           ↓
┌─────────────────────┐
│  后端 Flask API     │
│  localhost:5000     │
│  (8 个 REST 端点)   │
└──────────┬──────────┘
           │
           ├─→ PostgreSQL (数据库)
           ├─→ Redis (缓存)
           ├─→ Redis Jobs (Celery)
           └─→ Celery Worker (异步任务)
```

## 二、前端实现完成清单

### ✅ 已实现的功能

#### 1. 核心组件 (5 个)
- ✅ `SubmitJob.vue` - 提交作业表单
- ✅ `JobList.vue` - 作业列表与监控
- ✅ `JobDetail.vue` - 作业详情
- ✅ `CandidateList.vue` - 候选库浏览
- ✅ `CandidateDetail.vue` - 候选详情

#### 2. API 集成 (8 个端点)
- ✅ `GET /catalog` - DSL 词表
- ✅ `POST /estimate` - 静态估价
- ✅ `POST /run` - 提交作业
- ✅ `GET /jobs` - 作业列表
- ✅ `GET /jobs/<id>` - 作业详情
- ✅ `POST /jobs/<id>/cancel` - 取消作业
- ✅ `GET /candidates` - 候选库列表
- ✅ `GET /candidates/<id>` - 候选详情

#### 3. i18n 国际化 (49 个键)
- ✅ 24 个接口层错误
- ✅ 12 个淘汰原因
- ✅ 8 个模板假设
- ✅ 5 个其他文案

#### 4. 交互功能
- ✅ 表单验证
- ✅ 实时进度监控（每 5 秒轮询）
- ✅ 状态标签着色
- ✅ 分页加载
- ✅ 筛选条件
- ✅ 展开/收起详情
- ✅ 对话框弹窗

## 三、本地开发部署

### 步骤 1: 启动后端服务

```bash
# 进入后端目录
cd backend_api_python

# 激活虚拟环境
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Linux/Mac

# 启动 Flask 开发服务器
python run.py
```

后端将运行在 `http://localhost:5000`

### 步骤 2: 启动前端服务

```bash
# 进入前端目录
cd frontend-factor-mining

# 安装依赖（首次运行）
npm install

# 启动开发服务器
npm run dev
```

前端将运行在 `http://localhost:3000`

### 步骤 3: 访问应用

打开浏览器访问: **http://localhost:3000**

## 四、功能验证清单

### 测试场景 1: 提交作业

1. 访问「提交作业」标签页
2. 填写表单:
   - 策略源 ID: 1
   - 市场: 美股
   - 时间周期: 1D
   - 开始日期: 2023-01-01
   - 结束日期: 2023-12-31
   - 预算: 50
3. 点击「估价」查看估价结果
4. 点击「提交作业」提交挖掘任务
5. 应该看到成功提示和作业 ID

### 测试场景 2: 监控作业

1. 切换到「作业列表」标签页
2. 应该看到刚提交的作业
3. 状态应为「运行中」
4. 进度条实时更新
5. 点击「查看详情」查看作业完整信息
6. 可以点击「取消」终止作业

### 测试场景 3: 浏览候选

1. 切换到「候选库」标签页
2. 查看所有因子候选
3. 使用筛选条件:
   - 按状态筛选（搜索通过/冻结通过/已淘汰）
   - 按市场筛选
   - 按运行 ID 筛选
4. 展开行查看表达式 JSON
5. 点击「查看详情」查看完整指标

## 五、前端技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| Vue 3 | ^3.4.0 | 渐进式 JavaScript 框架 |
| Element Plus | ^2.5.0 | Vue 3 UI 组件库 |
| Vite | ^5.0.0 | 下一代前端构建工具 |
| Axios | ^1.6.0 | HTTP 客户端 |
| Day.js | ^1.11.10 | 日期时间处理 |
| Vue Router | ^4.2.5 | 路由管理（待集成） |
| Pinia | ^2.1.7 | 状态管理（待集成） |
| ECharts | ^5.4.3 | 数据可视化（待集成） |

## 六、目录结构

```
frontend-factor-mining/
├── src/
│   ├── api/
│   │   └── factorMining.js         # API 接口封装
│   ├── components/
│   │   ├── SubmitJob.vue           # 提交作业表单
│   │   ├── JobList.vue             # 作业列表
│   │   ├── JobDetail.vue           # 作业详情
│   │   ├── CandidateList.vue       # 候选库列表
│   │   └── CandidateDetail.vue     # 候选详情
│   ├── hooks/
│   │   └── useI18n.js              # 国际化 Hook
│   ├── i18n/
│   │   └── zh-CN.js                # 中文语言包
│   ├── App.vue                      # 根组件
│   └── main.js                      # 入口文件
├── index.html                       # HTML 模板
├── package.json                     # 项目配置
├── vite.config.js                   # Vite 配置
├── .gitignore                       # Git 忽略
└── README.md                        # 项目文档
```

## 七、API 代理配置

前端通过 Vite 代理访问后端 API:

```javascript
// vite.config.js
export default defineConfig({
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true
      }
    }
  }
})
```

## 八、构建生产版本

### 1. 构建前端

```bash
cd frontend-factor-mining
npm run build
```

构建产物在 `dist/` 目录。

### 2. 集成到 Docker

将构建产物集成到前端 Docker 镜像:

```dockerfile
FROM nginx:alpine
COPY dist/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
```

### 3. Nginx 配置

```nginx
server {
    listen 80;
    server_name localhost;
    
    root /usr/share/nginx/html;
    index index.html;
    
    # SPA 路由支持
    location / {
        try_files $uri $uri/ /index.html;
    }
    
    # API 代理
    location /api/ {
        proxy_pass http://backend:5000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 九、待优化功能

### 短期 (1-2 周)
- [ ] DSL 词表展示页面
- [ ] 候选库分页优化
- [ ] 错误处理增强
- [ ] 加载状态优化

### 中期 (1 个月)
- [ ] IC 序列可视化图表
- [ ] 因子相关度热力图
- [ ] 表达式树可视化
- [ ] 暗色主题支持

### 长期 (2-3 个月)
- [ ] 表达式可视化编辑器
- [ ] 候选对比功能
- [ ] 导出报告
- [ ] 英文国际化
- [ ] 单元测试覆盖
- [ ] E2E 测试

## 十、开发注意事项

### 1. 认证
目前前端从 `localStorage.getItem('token')` 读取 JWT token。
生产环境需要实现完整的登录流程。

### 2. 错误处理
所有后端错误通过 i18n 键返回，前端需要在 `zh-CN.js` 中定义对应文案。

### 3. 轮询策略
`JobList.vue` 每 5 秒轮询一次作业状态。
生产环境建议改用 WebSocket 或 Server-Sent Events。

### 4. 性能优化
- 候选列表使用虚拟滚动（候选数 > 1000 时）
- 表达式 JSON 使用懒加载
- 图表使用按需加载

## 十一、故障排查

### 前端无法访问后端

**问题**: CORS 错误或连接被拒绝

**解决**:
1. 确认后端运行在 `http://localhost:5000`
2. 检查 Vite 代理配置
3. 查看浏览器控制台错误信息

### 作业列表不刷新

**问题**: 轮询未工作

**解决**:
1. 检查浏览器控制台是否有请求错误
2. 确认有正在运行的作业
3. 检查 `pollTimer` 是否被正确设置

### 候选库为空

**问题**: 没有数据或筛选条件过严

**解决**:
1. 确认至少有一个作业成功完成
2. 点击「重置」清除筛选条件
3. 检查数据库 `qd_factor_candidates` 表

## 十二、联系与支持

- **后端仓库**: `backend_api_python/`
- **前端仓库**: `frontend-factor-mining/`
- **API 文档**: `docs/api/openapi.yaml`
- **产品文档**: `docs/product/FACTOR_MINING_CN.md`

---

**前端状态**: ✅ 开发完成，可投入使用  
**构建时间**: 2026-01-XX  
**开发者**: QuantDinger 全栈团队
