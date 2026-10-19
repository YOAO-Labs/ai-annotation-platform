---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# Docker rebuild vs restart：Celery 不会热重载

## 症状

改了 `apps/api/app/workers/tasks.py` 里的 task 代码（比如新增/重命名参数），重启浏览器、改 dispatcher 入参，结果运行时仍报 `TypeError: batch_predict() got an unexpected keyword argument 'xxx'`，磁盘上的源代码看起来明明已经更新。

这是 Celery 进程保留已导入代码的典型症状；开发态 bind mount 让文件更新可见，但不会重载进程内模块。

## 复现

```bash
# 修改 worker task 签名
vi apps/api/app/workers/tasks.py

# API 进程自动 reload（uvicorn --reload），看似一切正常
curl http://localhost:8000/health  # OK

# 触发预标注 → worker 执行 batch_predict 时报 TypeError
```

## 根因

| 服务                            | 是否热重载                  |
| ------------------------------- | --------------------------- |
| `apps/api`（FastAPI / uvicorn） | ✅ `--reload` 监听挂载卷    |
| 前端 vite                       | ✅ HMR                      |
| **Celery worker**               | ❌ **没有任何自动重载机制** |

dev `docker-compose.yml` 把整个 `apps/api` 挂卷到所有 worker 容器里，所以源文件**看起来**是新的，但 Celery 进程已经把旧版 task 加载进解释器，挂载只影响下次进程启动后的导入。

## 修复 / 规避

**业务代码改动 → 仅重启实际消费队列的 worker：**

```bash
# 视频 tracker 或 GPU 预标（ml / gpu）
docker restart ai-annotation-platform-celery-worker-gpu-1

# 默认 / media / cleanup / audit 任务
docker restart ai-annotation-platform-celery-worker-1
```

**依赖 / Dockerfile / 镜像层改动 → rebuild：**

```bash
docker compose build celery-worker celery-worker-gpu celery-worker-cpu celery-worker-export
docker compose up -d celery-worker celery-worker-gpu celery-worker-cpu celery-worker-export
```

**rebuild 触发条件清单**：

- `pyproject.toml` / `uv.lock` / `requirements.txt`
- `package.json` / `pnpm-lock.yaml`
- `Dockerfile` / `.dockerignore`
- 基础镜像版本（`FROM python:3.x`）
- `docker-compose.yml` 的 `build:` 块、build args、`COPY` 路径

**验证 worker 是否拿到新代码：**

```bash
docker exec ai-annotation-platform-celery-worker-1 \
  python -c "import inspect, app.workers.tasks as t; print(inspect.signature(t.batch_predict))"
```

如果签名仍是旧的，再重启对应容器。

## 长效防御

worker 异常会通过任务状态、通知与前端查询兜底暴露。修改 worker task 签名或路由时，先在真实消费队列上验证，并确认没有把任务投到无人订阅的 `celery` 队列。

## 相关

- 文档：[后端基础设施](../concepts/backend-infrastructure#队列与订阅模型)
- How-to：[调试 Celery](../how-to/debug-celery)
