# 因子挖掘 P2 T3 队列隔离验收记录

日期：2026-10-19。对应 [P2 设计](../superpowers/specs/2026-10-19-factor-mining-p2-design.md)
与 [P2 计划](../superpowers/plans/2026-10-19-factor-mining-p2.md) 的 T3。
本记录只描述 T3，不代表其他 P2 任务或生产部署已验收。共享工作区修改，未提交。

## 范围、接口与所有权

本次仅后端与部署：没有改 HTTP 契约、页面或 UI payload。调用方继续使用
`submit_job(kind='factor_mining', ...)`；Celery 消息沿用
`quantdinger.tasks.agent_job(job_id)`，结果和进度继续读取既有 PostgreSQL 作业记录。

只增加 mining 投递分支及 `celery-mining-worker` 部署服务；有限作业 owner 仍为
`QD_PROCESS_ROLE=celery`。通用 Worker 保留 `jobs,ai,maintenance`，显式关闭 Celery
时的本地 executor fallback 保持原行为。未新增 adapter、交易 owner 或业务状态副本。
readiness CLI 增加可选 `--queues`；旧命令及 HTTP readiness 契约保持原状。

跨任务接口已在实现前通过会话说明；当前宿主没有子智能体或会话消息工具，无法向其他
owner 单独投递消息。未编辑 engine、storage、service、根模块文档或其他 owner 的文件。

Baseline read set：指定设计/计划、`AGENTS.md`、`.agents/README.md`、
`docs/architecture/README.md`、`PROCESS_ROLES_AND_TASKS.md`、Celery/作业/readiness
源码及其现有测试、适用 Compose/env、部署中英文文档与依赖锁。

## 修改路径

- `backend_api_python/app/celery_app.py`
- `backend_api_python/app/utils/agent_jobs.py`
- `backend_api_python/app/commands/worker_health.py`
- `backend_api_python/tests/test_factor_mining_queue.py`
- `backend_api_python/tests/test_celery_worker_readiness.py`
- `docker-compose.yml`
- `docker-compose.ghcr.yml`
- `docker-compose.production.yml`
- `.env.example`
- `backend_api_python/env.example`
- `docs/deployment/DISTRIBUTED_RUNTIME_SCALING.md`
- `docs/deployment/DISTRIBUTED_RUNTIME_SCALING_CN.md`
- `docs/deployment/CLOUD_DEPLOYMENT_EN.md`
- `docs/deployment/CLOUD_DEPLOYMENT_CN.md`
- 本验收记录。

`docker-compose.build.yml` 和 observability 覆盖文件无需修改，继承基础定义并纳入静态验证。

## 原子任务与 checkpoint

1. 确认任务入口、消费者、队列与有限作业 owner：已核实。
2. 增加投递、durable binding、部署与自身队列 readiness 测试并执行 red：已记录。
3. 最小实现队列分支、专用 Worker、容量与 readiness：已实施。
4. 同步环境示例、部署中英文说明与升级/回滚排空边界：已实施。
5. 聚焦回归、lint、编译、Compose 与文档静态验证：结果如下。

Resume hint：T3 实现与聚焦静态验收已具备，可交主代理联调；不重复执行其他 P2 owner
的任务。Drift check：没有迁移、业务数据库写入、真实模型调用、生产部署、P1 Worker 接管
或取消提交一致性改动。修复轨为专用队列与容量配置；退役轨仅替换新挖掘作业的通用队列
投递，保留旧 `jobs` 消息排空、本地 fallback 与公共任务入口。

## Red / green

后端命令的工作目录均为 `backend_api_python/`，使用其 `.venv/Scripts/python.exe`。

先写测试、未实施时运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_factor_mining_queue.py tests/test_celery_worker_readiness.py -q --timeout=90
```

Red：exit 1，`15 failed, 4 passed in 2.49s`。预期失败为 mining 仍走 `.delay()`、未声明
队列、Compose 缺少专用 Worker、readiness 尚不支持 `--queues`。已兼容的其他任务、关闭
Celery 的本地执行与 dispatch 失败语义通过。

最小实现后运行同一命令：exit 0，`19 passed in 1.60s`。

聚焦回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_factor_mining_queue.py tests/test_celery_worker_readiness.py tests/test_celery_boundaries.py tests/test_agent_jobs_progress.py tests/test_agent_jobs_failure.py tests/test_agent_backtest_billing.py -q --timeout=90
```

Green：exit 0，`45 passed in 20.77s`。测试覆盖持久提交后投递、任务参数保持、其他队列路由、
dispatch 失败、本地 fallback、自身节点与预期队列集合精确匹配、缺失/错误/混合订阅拒绝、
broker 检查失败，以及现有进度、失败信息与计费终态边界。数据库与 broker 使用测试 doubles。

## 其他新鲜验证

```powershell
.\.venv\Scripts\python.exe -m ruff check app/celery_app.py app/utils/agent_jobs.py app/commands/worker_health.py tests/test_factor_mining_queue.py tests/test_celery_worker_readiness.py
.\.venv\Scripts\python.exe -m compileall -q app/celery_app.py app/utils/agent_jobs.py app/commands/worker_health.py tests/test_factor_mining_queue.py tests/test_celery_worker_readiness.py
.\.venv\Scripts\python.exe -m celery -A app.celery_app:celery_app worker --help
```

三条命令 exit 0；ruff 输出 `All checks passed!`。本地 Celery 与锁文件均为 `5.6.3`，
CLI 验证 `--hostname`、`--concurrency`、`--prefetch-multiplier`、`-Q` 存在。
官方版本路径正文为 Celery 5.6 系列文档，页标题显示 5.6.2；没有据此声称依赖已经升级。
参考：[路由](https://docs.celeryq.dev/en/v5.6.3/userguide/routing.html)、
[Worker 检查](https://docs.celeryq.dev/en/v5.6.3/userguide/workers.html#queues-list-of-active-queues)、
[Compose 资源配置](https://docs.docker.com/reference/compose-file/services/)。

以下在仓库根目录执行；Docker Compose 为 `v5.2.0`。八条全部 exit 0，无错误输出：

```powershell
docker compose --env-file .env.example -f docker-compose.yml config --quiet
docker compose --env-file .env.example -f docker-compose.ghcr.yml config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.production.yml config --quiet
docker compose --env-file .env.example -f docker-compose.ghcr.yml -f docker-compose.production.yml config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.build.yml config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.build.yml -f docker-compose.production.yml config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.observability.yml config --quiet
docker compose --env-file .env.example -f docker-compose.ghcr.yml -f docker-compose.observability.yml config --quiet
```

另外，对 `config --format json` 的合并结果作内存断言：源码/build/production 和
GHCR/production 的 mining 队列、通用队列、镜像一致性、资源预算、只读/非 root、DB pool
和 readiness 命令符合定义，均 exit 0。仅使用示例环境，没有打印实际运行环境或凭据。

仓库根目录执行：

```powershell
.\backend_api_python\.venv\Scripts\python.exe scripts/check_docs.py
git diff --check -- backend_api_python/app/celery_app.py backend_api_python/app/utils/agent_jobs.py backend_api_python/app/commands/worker_health.py docker-compose.yml docker-compose.ghcr.yml docker-compose.production.yml .env.example backend_api_python/env.example docs/deployment/DISTRIBUTED_RUNTIME_SCALING.md docs/deployment/DISTRIBUTED_RUNTIME_SCALING_CN.md docs/deployment/CLOUD_DEPLOYMENT_EN.md docs/deployment/CLOUD_DEPLOYMENT_CN.md
```

均 exit 0；文档门禁输出 `Documentation structure, links, code fences, and assets are valid.`。

Final review: self-review (no subagent tool)。已依据现有审查清单核对 T3 diff 与新增测试，
未发现本范围内必须修复的问题。作者自审弱于独立审查，不代表独立审查已执行。

图谱只读影响核查：末检确认已有 `graphify-out/graph.json`（被 Git 忽略），按 graphify
查询约定从实际词汇选择 `[agent, celery, health, job, jobs, queue, submit, worker]`。
`graphify --help` 被 pyenv 的未设置版本提示阻断，改用项目 Python 在内存中读取一跳关系。
图谱列出 backtest、strategy evolution、mining service 三处 `submit_job()` 调用者，已用
当前源码 `rg -n -C 2 submit_job` 逐处复核。图谱位置落后于当前源码，不能作为行号权威。
未刷新/写入共享图谱：多个 owner 正在共享工作区修改，留给总控在整批联调后统一刷新；
不把混合中间态写成 T3 的索引验收，也不提交自动生成图谱。

## 未覆盖与残余风险

- 未启动实际 Redis/Celery 容器，未验证真实消息消费、健康检查时延、重启恢复或容量压测。
- `mining` 与其他队列仍共享宿主机、PostgreSQL、任务 Redis 和存储；资源隔离配置不是吞吐保证。
- 旧 `jobs` 中已排队的 mining 作业仍由通用 Worker 处理，迁移窗口内不能声称完全隔离。
- HTTP readiness 仅检查数据库和 broker，心跳汇总也不能证明挖掘消费者可用。
- 未执行 frontend/browser、真实 provider、业务数据库写入、生产部署或 P2 全套联调。
- 丢失 Worker 接管及取消提交一致性沿用既有边界，属于规格明确排除的 P1 风险。

实际环境手动验收：按中英文部署指南先启动 mining 消费者，逐副本运行
`python -m app.commands.worker_health celery --queues mining`；提交受控测试挖掘作业，
核对 mining 接收、job 状态/进度/结果与刷新读取，并发提交普通任务确认通用队列继续处理。
对旧队列升级排空、回滚 drain 与共享资源压力单独实测，禁止用本记录的静态检查替代。
