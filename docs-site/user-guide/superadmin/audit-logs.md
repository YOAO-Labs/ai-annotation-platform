---
audience: [super_admin]
type: reference
status: stable
last_reviewed: 2026-10-24
---

# 审计日志

`audit_logs` 表是平台关键操作的不可改追踪流水。超管可在前端审计页查询，开发者可直接 SQL。

## 入口

事件范围常驻列表上方；动作、对象类型、操作人、对象 ID 与详情键值在漏斗 **筛选** 面板中编辑，修改后即时生效。操作人选项可搜索，追溯条件以可编辑、可单独移除的摘要显示；空详情键值明确显示为「空字符串」。**清除追溯** 保留事件范围与独立概览月份，详见 [使用筛选](../reference/filtering.md)。

`/audit`（仅 super_admin 有前端入口；API 列表端点 project_admin 可访问，导出仅 super_admin）

## 表结构要点

![详情 Modal detail_json + 时间线追溯按钮](../images/superadmin/audit-logs/detail-modal.png)

| 字段                        | 含义                                                |
| --------------------------- | --------------------------------------------------- |
| `actor_id`                  | 触发动作的用户；`ON DELETE SET NULL` 保留历史       |
| `action`                    | 命名空间动作，如 `project.create` / `task.approve`  |
| `target_type` / `target_id` | 操作对象类型与 ID                                   |
| `detail_json`               | JSONB，存上下文（旧值、IP、UA、filter_criteria 等） |
| `created_at`                | timestamptz                                         |

`audit_logs` 受 trigger 守护——**任何 UPDATE/DELETE 默认被拒**（"audit_logs rows are immutable"）。例外：受三重门禁保护的 E2E fixture reset/cleanup 流程通过 `SET LOCAL "app.allow_audit_update" = 'true'` 临时豁免（详见 [E2E 数据隔离](../../dev/troubleshooting/dev-data-preservation)）。

## 已覆盖动作

按命名空间组织（命名规则：`命名空间.动词`，下面列出主要动作，完整列表见 `apps/api/app/services/audit.py` 的 `AuditAction` 枚举）。前端 `auditLabels` 提供翻译。

### 用户与权限

- `auth.login` / `auth.logout` / `auth.logout_all`
- `user.invite` / `user.register` / `user.role_change` / `user.deactivate` / `user.delete`
- `user.profile_update` / `user.password_change` / `user.password_admin_reset`

### 项目

- `project.create` / `project.update` / `project.delete` / `project.transfer`
- `project.member_add` / `project.member_remove`
- `project.export`

### 数据

- `dataset.create` / `dataset.delete` / `dataset.import`
- `dataset.link` / `dataset.unlink`
- `storage_connection.create` / `storage_connection.update` / `storage_connection.delete`
- `batch.created` / `batch.status_changed` / `batch.deleted`
- `batch.distribute_even` / `batch.bulk_archive` / `batch.bulk_delete`
- `batch.export`

### AI / ML

- `predictions.import` / `predictions.purge`
- `failed_prediction.dismissed` / `failed_prediction.restored`
- `video_tracker_job.create` / `video_tracker_job.cancel` / `video_tracker_job.accept` / `video_tracker_job.discard` / `video_tracker_job.decision`
- `video_correction_job.create` / `video_correction_job.cancel`（人工 Mask 纠错传播创建 / 取消）
- `video_mask.keyframe_correct` / `video_mask.keyframe_operate`（保存纠错帧，以及删除、消失或恢复关键帧状态）
- `annotation.mask_mutation`（Mask 拆分、复制、合并或严格非重叠的聚合提交）
- `annotation.convert`（图片 / 视频标注转换的聚合提交）
- `ml_registry.created` / `ml_registry.updated` / `ml_registry.deleted`（全局注册 CRUD）
- `ml_service_pool.created` / `ml_service_pool.updated` / `ml_service_pool.deleted` / `ml_service_pool.member_upserted` / `ml_service_pool.member_removed` / `ml_service_pool.member_drained` / `ml_service_pool.member_resumed`
- `ml_backend.created` / `ml_backend.updated` / `ml_backend.deleted` / `ml_backend.enablement` / `ml_backend.reloaded` / `ml_backend.unloaded` / `ml_backend.warmup` / `ml_backend.smoke_tested`（项目兼容与实例生命周期；详见 [ML Backend 注册](./ml-backend-registry)）

> 上述 ML 相关动作由后端以**原始字符串**写入，目前未纳入 `AuditAction` 枚举。可分别按 `action LIKE 'ml_registry.%'`、`'ml_service_pool.%'` 或 `'ml_backend.%'` 查询。

原生 Mask、实例原子操作、标注转换与视频纠错审计只保存有界摘要和 lineage：对象 / 版本、窗口、backend / pool / model、候选数量、fallback、digest 与人工覆盖前后摘要。审计和普通日志都不保存 RLE counts、完整 geometry、笔迹坐标、logits、receipt 或原始 prompt 正文。

### 标注

- `annotation.create` / `annotation.update` / `annotation.delete`
- `annotation.import` / `annotation.group` / `annotation.bulk_update`
- `annotation.comment_add` / `annotation.comment_delete`

### 审核

- `task.submit` / `task.withdraw`
- `task.review_claim` / `task.approve` / `task.reject`
- `task.reopen` / `task.accept_rejection` / `task.skip`

### 系统

- `system.settings_update` / `system.bootstrap_admin`
- `audit.export` / `audit.archive`

## 查询界面

![审计日志筛选栏](../images/superadmin/audit-logs/filter-bar.png)

前端提供以下过滤维度（`GET /api/v1/audit-logs`）：

- 顶部时间范围选择器（`from` / `to` 参数）
- `action` 精确匹配
- `target_type` / `target_id` 按对象查
- `actor_id` 按操作人查
- `business_only=true` 排除 `http.*` 中间件元数据行
- `detail_key` + `detail_value`：`detail_json` JSONB 字段级过滤（走 GIN 索引），例如 `?detail_key=role&detail_value=super_admin`
- 行内点击展开 `detail_json`

条件和页码保存在地址中，刷新与浏览器前进后退可以恢复。文本停止输入 250ms 后应用；列表和导出使用同一组已应用条件。默认仅显示业务事件，选择“全部”后保留该范围。

填写 `detail_key` 后，空白的 `detail_value` 是明确的空字符串条件。例如 `detail_key=role&detail_value=` 会查找该字段为空字符串的记录，不会省略此条件。

### 月度概览

审计页在明细列表上方提供 UTC 月度概览。选择月份后可查看事件总数、错误事件数、动作类型数、每日趋势、
Top action、目标类型和操作人角色分布。“仅业务事件”会排除 `http.*` 中间件元数据；切换到“全部”时，
概览和下方明细使用同一口径。

点击 Top action 的条形或动作名称，会把同一个 action 精确过滤应用到明细列表并写入 URL，便于从异常
峰值继续追查原始操作、请求 ID 和 detail。

历史完整日从日聚合物化视图读取，物化覆盖日期之后的数据由在线审计分区实时补齐。概览会显示最后物化
日期；刷新任务延迟时，查询自动扩大实时补齐范围，因此不会静默漏数。概览只覆盖在线保留期，超期月份
仍通过审计归档接口回源。

<details>
<summary>🛠️ 开发者与自动化脚本接口参考</summary>

月报接口仅允许 super admin 访问：

```text
GET /api/v1/audit-logs/monthly-summary?month=2026-08&business_only=true
```

支持导出 CSV 或 JSON（同步 `StreamingResponse`，最大 50,000 行；超限时需缩小过滤范围）：

```text
GET /api/v1/audit-logs/export?format=csv&from=2026-01-01&action=task.reject
```

</details>

## 直接 SQL 查询示例

```bash
docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation -c \
  "SELECT created_at, actor_id, action, target_type, target_id, detail_json
   FROM audit_logs
   WHERE created_at > NOW() - INTERVAL '1 day'
     AND action LIKE 'task.%'
   ORDER BY created_at DESC LIMIT 50;"
```

## 分区策略

`audit_logs` 按月分区（[ADR 0007](../../dev/adr/archive/0007-audit-log-partitioning)）。运维上每月初由 cron 创建下月分区，旧分区可按合规策略归档/删除（默认保留 12 个月）。

### 读取历史归档

归档列表和内容接口仅限当前超级管理员使用。`GET /api/v1/audit-logs/archives` 只列出已完成发布的归档；未确认的写入保留待核对，不会作为可读归档显示。

使用 `GET /api/v1/audit-logs/archives/{year}/{month}?limit=100&offset=0` 分页读取，单页最多 1000 行。每次请求都会完整核验归档后再返回指定页，末尾 `_partition_meta` 也是一行，因此 `total` 比审计事件数多一。

历史归档按创建时保存的 bucket 和内容摘要读取，调整新归档的写入 bucket 不影响已有归档。缺少独立归属的旧归档会返回冲突，平台不会从当前 bucket 猜测读取。中途撤权、凭据到期、维护状态变化或内容损坏都会阻止返回，详见[审计归档 API](../../api/guides/audit-archives)。

## 写入侧约定

新功能要写审计：

1. 在 `apps/api/app/services/audit.py` 的 `AuditAction` 枚举中添加新动作
2. 在业务代码中调用 `AuditService.log(db, actor=..., action=AuditAction.XXX, target_type=..., target_id=..., detail={...})`
3. 前端 `auditLabels` 加翻译
4. 新动作进 changelog

不要绕过 service 直接 INSERT——trigger 不区分来源，但 service 层负责字段统一。
