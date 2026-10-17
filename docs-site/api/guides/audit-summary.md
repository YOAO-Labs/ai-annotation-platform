---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 审计月报

`GET /api/v1/audit-logs/monthly-summary?month=2026-10&business_only=true` 只允许超级管理员。`month` 为 UTC 自然月，默认排除 `http.*`；非法或超出在线保留期的月份返回 422。

成功响应保留 `month`、`timezone=UTC`、可空的 `materialized_through`、事件／错误／操作种类总数、完整自然日序列、前十操作、目标类型和角色分布。公开计数仍为整数；空维度使用空字符串，事件数相同按 key 升序，4xx／5xx 计入错误。

Python 负责授权、物化视图／实时分区查询及本地归并，不依赖外部计算进程。SDK `client.audit_logs.monthly_summary()` 与 CLI `aap audit-logs monthly-summary` 保留原入口。

Go 归并仅作为独立实验保留，没有接入此公开端点。实验 wire 契约与公开 REST 分开，见 [ADR 0090](../../dev/adr/0090-module-languages-and-audit-summary-process)。
