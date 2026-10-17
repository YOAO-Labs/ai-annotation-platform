---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 审计月报

`GET /api/v1/audit-logs/monthly-summary?month=2026-10&business_only=true` 只允许超级管理员。`month` 为 UTC 自然月，默认排除 `http.*`；非法或超出在线保留期的月份返回 422。

成功响应保留 `month`、`timezone=UTC`、可空的 `materialized_through`、事件／错误／操作种类总数、完整自然日序列、前十操作、目标类型和角色分布。公开计数仍为整数；空维度使用空字符串，事件数相同按 key 升序，4xx／5xx 计入错误。

Python 负责授权及物化视图／实时分区查询；独立 Go 服务仅归并分组事实，不访问数据库、存储或用户凭据。内部 wire 契约由 `apps/audit-summary/openapi.json` 持有，与公开 REST 分开。

计算服务超时、不可达或协议响应无效时返回 503：

```json
{ "detail": { "reason": "audit_summary_unavailable" } }
```

其他 reason 为 `audit_summary_invalid_response` 和 `audit_summary_capacity_exceeded`。内部请求／响应限制为 64 MiB，超限明确失败而不截断；故障不会返回伪造的零事件。服务恢复后可安全重新 GET，不创建后台作业或业务写入。SDK `client.audit_logs.monthly_summary()` 与 CLI `aap audit-logs monthly-summary` 保留原入口。

部署和边界见 [ADR 0090](../../dev/adr/0090-module-languages-and-audit-summary-process)。
