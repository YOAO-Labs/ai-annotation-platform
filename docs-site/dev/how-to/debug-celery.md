---
title: 调试 Celery 后台任务
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# How-to：调试 Celery

本指南介绍平台的多队列 Celery 拓扑、本地启动与热刷新方式、测试 Eager 模式以及常见异步任务排障技巧。

## 队列与 Worker 服务映射

平台按任务负载特性将后台任务划分为 7 个独立 Worker 服务与 1 个定时调度器（`celery-beat`）：

| Compose 服务名              | 订阅队列（`-Q`）              | 核心消费职责                                                   |
| --------------------------- | ----------------------------- | -------------------------------------------------------------- |
| `celery-worker`             | `default,media,cleanup,audit` | 通用异步通知、视频抽帧/分片、过期资产清理、审计写入            |
| `celery-worker-maintenance` | `maintenance`                 | 审计/预测月度分区维护、冷分区归档、物化视图刷新（独立高权限）  |
| `celery-worker-gpu`         | `gpu,ml`                      | GPU 批量预标注、SAM2/SAM3 视频追踪作业                         |
| `celery-worker-cpu`         | `ml.cpu`                      | CPU 推理任务、点云 3D 质量检查与跨帧传播                       |
| `celery-worker-export`      | `export,image-pyramid`        | 异步数据集导出打包、大图金字塔（DZI）切片生成                  |
| `celery-worker-import`      | `import`                      | 外部数据集与预标注包异步导入                                   |
| `celery-worker-training`    | `training`                    | 在线学习训练数据集版本快照冻结、训练包构建、回流发布与超参搜索 |

> **关键提示**：Celery **不会**自动热重载挂载的 Python 源码。每次修改 `apps/api/app/workers/` 或其调用的 Service 代码后，必须重启实际消费该队列的 Worker（例如 `docker compose restart celery-worker-training`）。

## 本地单进程调试启动

若希望在宿主机终端直接启动某个队列的 Worker 以便挂载断点或观察实时日志：

```bash
docker compose up -d redis postgres minio
cd apps/api
uv run celery -A app.workers.celery_app worker -l info -Q default,media,training
```

## 单元测试中的 Eager 模式

在 `pytest` 单元测试中不应依赖外部真实 Worker，可使用 Eager 模式在当前进程内同步执行任务体：

```python
@pytest.fixture(autouse=True)
def celery_eager(monkeypatch):
    from app.workers.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
```

## 检查 Redis 队列积压

当任务长时间停留在 `pending` / `queued` 状态时，可直接在 Redis 中检查各队列长度：

```bash
# 检查各逻辑队列的待消费消息数
docker compose exec redis redis-cli LLEN default
docker compose exec redis redis-cli LLEN gpu
docker compose exec redis redis-cli LLEN training
docker compose exec redis redis-cli LLEN celery
```

若发现 `LLEN celery` 大于 0 而业务队列为 0，说明新增的任务未在 `app/workers/celery_app.py` 的 `task_routes` 中声明专用队列，或调用方未继承 `task_default_queue="default"`。

## 编写 Celery 任务的常见陷阱

- **协程调用与数据库连接池**：Celery Worker 采用同步 prefork/solo 进程模型。在任务内调用异步 Service 时，应使用 `asyncio.run(...)` 搭配任务级独立 `create_async_engine`（并在 `finally` 中 `await engine.dispose()`），切勿在多进程 Worker 中跨事件循环复用模块级全局 `async_session`。
- **参数必须可 JSON 序列化**：不要向 `.delay()` / `.apply_async()` 直接传递 SQLAlchemy ORM 实例，一律传递主键 UUID 并在任务内部重新查询。
- **幂等与围栏校验**：任务可能因 Worker 重启或超时被重试。写入结果前应校验目标业务行（如 `AsyncJob`、`TrainingDatasetVersion`、`VideoTrackerJob`）的当前状态与版本号。
