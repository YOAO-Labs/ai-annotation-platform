---
title: 系统监控
audience: [super_admin]
type: reference
since: v0.8.7
status: stable
last_reviewed: 2026-10-26
---

# 系统监控

平台用 Prometheus + 结构化日志做可观测，超管可从前端「平台概览」看核心指标。侧边栏「管理 → 系统健康」提供 DB / Redis / MinIO / Celery 的实时健康面板；深度排查继续走 Grafana / 直接 PromQL。

## 入口

- 前端：`/overview`（仅超管）
- 系统健康面板：`/admin/health`（仅超管，12 秒自动刷新）
- Prometheus 端点：
  - `apps/api`：`http://api:8000/metrics`
  - `grounded-sam2-backend`：`http://gpu-host:8001/metrics`

## 关键指标

### API 层

| 指标                                  | 含义         | 告警阈值（参考） |
| ------------------------------------- | ------------ | ---------------- |
| `http_requests_total{status=~"5.."}`  | 5xx 速率     | > 1% / 5min      |
| `http_request_duration_seconds` (P95) | API 延迟     | > 1s / 5min      |
| `db_pool_used` / `db_pool_size`       | 连接池水位   | 持续 > 80%       |
| `redis_pool_used`                     | Redis 连接池 | —                |

### Worker 层

| 指标                                                       | 含义                                                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `celery_queue_length{queue}`                               | 各队列待处理 + 在执行任务数（Gauge）                                                   |
| `celery_worker_heartbeat_seconds{worker}`                  | worker 上次心跳距今秒数（Gauge，越小越新鲜）                                           |
| `ml_backend_request_duration_seconds{backend_id, outcome}` | ML backend predict / interactive 单次调用耗时（Histogram，outcome="success"\|"error"） |

> 上述三个指标均在 `apps/api/app/observability/metrics.py` 注册。平台不暴露 `celery_tasks_total` 或 `celery_task_duration_seconds`；预标 job 状态分布通过 `async_jobs` 表查询，不以 Prometheus Counter 形式暴露。

### ML Backend（grounded-sam2-backend 容器）

ML backend 容器的自定义指标由 backend 自身暴露，不经过平台 API。常见可观测信号（视 backend 实现而定）：

- GPU 使用率 / 显存 / 温度（通过 `/health.gpu_info` 字段聚合到平台健康面板）
- 模型加载缓存命中率（通过 `/health.cache` 字段聚合）
- 容器 CPU / 内存（通过 `/health.host` 字段聚合）

详细 PromQL 见 [可观测性 / 监控](../../ops/observability/)。

## 日志

所有服务输出结构化 JSON 日志（`structlog`）。常用 grep：

```bash
# 看某 job 的全链路
docker logs ai-annotation-platform-celery-worker-1 2>&1 | grep <job_id>

# 看 5xx
docker logs ai-annotation-platform-api-1 2>&1 | jq 'select(.status>=500)'
```

生产环境建议接 ELK / Loki 集中。

## 健康检查端点

| 服务                  | 路径                          | 含义                                                        |
| --------------------- | ----------------------------- | ----------------------------------------------------------- |
| api                   | `/health`                     | DB + Redis + MinIO 联通性                                   |
| api                   | `/api/v1/admin/system-health` | 超管聚合视图，返回组件状态、延迟、Celery 队列和 worker 心跳 |
| api                   | `/ready`                      | lifespan 完成                                               |
| grounded-sam2-backend | `/health`                     | 模型加载完成                                                |

## 系统健康面板

![系统健康面板](../images/superadmin/system-monitoring/health-panel.png)

系统健康面板基于 `/api/v1/admin/system-health`：

- 组件状态：PostgreSQL、Redis、MinIO、Celery，展示 `ok` / `degraded` / `down` 与 latency。
- Celery 队列：显示各队列积压数量，`length ≥ 25` 标为降级（`degraded`），`length ≥ 100` 标为不可用（`down`）（`apps/api/app/api/v1/admin_system_health.py:60-65`）。
  ![Workers 表（名称/Heartbeat/Pool/状态）](../images/superadmin/system-monitoring/workers-table.png)
- Worker 心跳：显示 worker 名称、最近心跳距现在的秒数和 pool 并发上限；心跳 `≥ 120s` 标为降级，`≥ 300s` 标为不可用（`apps/api/app/api/v1/admin_system_health.py:68-75`）。

⚠️ FastAPI lifespan 阻塞会让 `/health` 30s 内不可用——曾在 CI 引发卡死，详见 [CI 服务依赖踩坑](../../dev/troubleshooting/ci-flaky-services)。

## 错误监控

前后端错误打到 Sentry（DSN 见 `.env`）。前端 BUG 反馈侧通过 `BugReportDrawer` 入 `bug_reports` 表；超管也可从侧边栏 **管理 → BUG反馈** 进入 `/bugs` 处理状态、查看 Markdown 描述/评论和截图附件。

存储位置的在线诊断、视频派生资产失败排查与重试、桶容量盘点及账目对账统一在「存储管理」中处理，详见[存储诊断与容量账目](../datasets/storage-operations)。

## 容量规划经验值

| 资源                                                               | 经验值 |
| ------------------------------------------------------------------ | ------ |
| 每标注员日均 200 任务 → API 流量 ~1 req/s 长尾                     | —      |
| 每 SAM 推理 P95 ~800ms（A10G）/ ~3s（CPU fallback）                | —      |
| 单 GPU 并发 4 推理稳定                                             | —      |
| Postgres 连接池建议 = (worker concurrency + api concurrency) × 1.5 | —      |

## 备份

- Postgres：`pg_dump` 每日 + WAL 归档
- 对象存储与受控存储位置：桶镜像、元数据与对象一致性备份及隔离环境恢复演练，详见[一致备份与隔离恢复](/ops/deploy/storage-recovery)
- Redis：可丢（broker），不需要备份
- SAM 缓存：可丢（性能优化）

服务架构详见 [部署拓扑](../../dev/concepts/deployment-topology)。
