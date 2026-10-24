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

| Compose 服务名                | 订阅队列（`-Q`）              | 核心消费职责                                                  |
| ----------------------------- | ----------------------------- | ------------------------------------------------------------- |
| `celery-worker`               | `default,media,cleanup,audit` | 通用异步通知、视频抽帧/分片、过期资产清理、审计写入           |
| `celery-worker-maintenance`   | `maintenance`                 | 审计/预测月度分区维护、冷分区归档、物化视图刷新（独立高权限） |
| `celery-worker-gpu`           | `gpu,ml`                      | GPU 批量预标注、SAM2/SAM3 视频追踪作业                        |
| `celery-worker-cpu`           | `ml.cpu`                      | CPU 推理任务、点云 3D 质量检查与跨帧传播                      |
| `celery-worker-export`        | `export`                      | 数据导出、训练数据版本构建与打包                              |
| `celery-worker-image-pyramid` | `image-pyramid`               | 大图金字塔（DZI）切片生成                                     |
| `celery-worker-gpu-control`   | `gpu.control`                 | GPU 生命周期控制与仲裁维护                                    |

Celery 不会自动热重载挂载的 Python 源码。修改 Worker 或其调用的 Service 后，需要刷新实际消费队列的进程。训练数据构建走 `export`，自动训练与 Study 协调走 `default`；原生训练由独立执行器拉取，不存在 `training` 队列消费者。以 `apps/api/app/workers/celery_app.py` 的 `task_routes` 和 Compose 的 `-Q` 为准。

## 本地单进程调试启动

若希望在宿主机终端直接启动某个队列的 Worker 以便挂载断点或观察实时日志：

```bash
docker compose up -d redis postgres minio
cd apps/api
uv run celery -A app.workers.celery_app worker -l info -Q default,media,export
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
docker compose exec redis redis-cli LLEN export
docker compose exec redis redis-cli LLEN celery
```

若发现 `LLEN celery` 大于 0 而业务队列为 0，说明新增的任务未在 `app/workers/celery_app.py` 的 `task_routes` 中声明专用队列，或调用方未继承 `task_default_queue="default"`。

## 编写 Celery 任务的常见陷阱

- **协程调用与数据库连接池**：Celery Worker 采用同步 prefork/solo 进程模型。在任务内调用异步 Service 时，应使用 `asyncio.run(...)` 搭配任务级独立 `create_async_engine`（并在 `finally` 中 `await engine.dispose()`），切勿在多进程 Worker 中跨事件循环复用模块级全局 `async_session`。
- **参数必须可 JSON 序列化**：不要向 `.delay()` / `.apply_async()` 直接传递 SQLAlchemy ORM 实例，一律传递主键 UUID 并在任务内部重新查询。
- **幂等与围栏校验**：任务可能因 Worker 重启或超时被重试。写入结果前应校验目标业务行（如 `AsyncJob`、`TrainingDatasetVersion`、`VideoTrackerJob`）的当前状态与版本号。

## 目录观测扫描未完成

先查看 `/api/v1/dataset-scans/{run_id}` 的 error_code、phase、complete 和当前 async_job_id；长期清单通过 `/entries` 分页读取。确认来源对应的完整 Celery hostname 已注册 `app.workers.dataset_scan.run_dataset_scan`，且使用该节点专属队列；不能用其他节点的成功探测代替。

`dispatch_unavailable` 可在 broker 恢复后调用 `/resume`。pending 发布前中断或 running 租约过期也可显式恢复；有效租约内返回409。每次恢复创建新任务ID，旧检查点保留；不要改数据库代次、令牌或手工把 Job 标 completed。取消走现有 AsyncJob 入口，不需强杀进程。

`configuration_changed` 需核对位置修订后启动新运行；`directory_changed` 或 `directory_identity_changed` 需先确认目录是否被移动、替换或继续写入。受控根身份变化必须核查部署映射；不以重试静默改绑。`directory_too_large` / `scan_budget_exceeded` 应缩小目录范围。失败、取消和超限都不代表文件不存在，不能据此删除历史样本。

代表性文件树的资源、取消与恢复测量使用[目录扫描资源基线](../reference/dataset-scan-benchmark.md)。它运行于一次性环境，保留现有预算，区分元信息扫描与图片正文读取。
