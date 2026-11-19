---
title: Runbook：PG 连接池耗尽
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-07-11
---

# Runbook：PG 连接池耗尽

## 症状

- API 返回 `500` 错误，日志包含 `QueuePool limit of size X overflow Y reached`
- Grafana 连接池指标（`pg_pool_checked_out`）持续满载
- `/api/v1/health` 响应延迟突增

## 快速诊断

```bash
# 1. 查看当前 PG 连接数
docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"

# 2. 查找长时间 idle-in-transaction 连接
docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation -c \
  "SELECT pid, now() - query_start AS duration, state, query
   FROM pg_stat_activity
   WHERE state != 'idle' AND query_start < now() - interval '5 minutes'
   ORDER BY duration DESC;"

# 3. 查看 API 连接池配置
grep -E 'POOL_SIZE|MAX_OVERFLOW|POOL_TIMEOUT' .env
```

## 处理步骤

### 紧急：终止泄漏的连接

```bash
# 终止指定 pid（替换 <pid>）
docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation -c \
  "SELECT pg_terminate_backend(<pid>);"

# 批量终止 idle-in-transaction 超过 10 分钟的连接
docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation -c \
  "SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE state = 'idle in transaction'
     AND now() - query_start > interval '10 minutes';"
```

### 根因：连接泄漏

开发态 API 跑在宿主机，重启 `pnpm dev:api` 的进程即可释放所有池中连接。生产叠加 compose 时可以重启 API 容器：

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml restart api
```

> 此操作会短暂中断服务（约 5–10 秒），适合低流量时段。

### 根因：池大小配置不足

在 `.env` 中调整：

```dotenv
# 默认值（SQLAlchemy AsyncEngine）
DB_POOL_SIZE=10        # 池基本大小
DB_MAX_OVERFLOW=20     # 允许超出池大小的最大连接数
DB_POOL_TIMEOUT=30     # 等待连接的超时秒数
```

调整后重启开发态 API 进程，或按上面的生产 compose 命令重启 API 容器使配置生效。

### 根因：PG 自身连接数限制（`max_connections`）

```bash
# 查看当前限制
docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation -c \
  "SHOW max_connections;"

# 若使用 managed PG，在云控制台调整
# 若自托管，修改 postgresql.conf 后重启 PG
```

建议值：`max_connections` ≥ (API 池大小 + overflow) × 实例数 + 20（预留 psql 手动连接）

## 预防措施

- 生产环境建议使用 **PgBouncer** 做连接复用，API 连接数可设更小
- 监控 `pg_stat_activity` 中 `idle in transaction` 的连接数（告警阈值建议 > 5）
- 定期检查慢查询日志：`log_min_duration_statement = 1000`

## 相关文档

- [监控与告警](/ops/observability/)
- [后端基础设施](/dev/concepts/backend-infrastructure)
