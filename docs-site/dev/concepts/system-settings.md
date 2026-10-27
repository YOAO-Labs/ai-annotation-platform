---
audience: [dev, ops]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 系统设置的运行时覆盖

系统设置保留环境变量作为部署默认值，`super_admin` 可通过 `/api/v1/settings/system` 为白名单内的运营配置写入数据库覆盖。API 进程不改写 `.env`，覆盖在服务重启后仍保持；删除覆盖后回到当前部署的环境值。

## 注册表与有效值

`apps/api/app/services/system_settings_service.py` 的 `SETTING_SPECS` 同时定义键名、严格值类型、单位、作用说明和校验范围。六项高频配置如下：

| 键                               |         默认 | 范围                                            | 生效点                      |
| -------------------------------- | -----------: | ----------------------------------------------- | --------------------------- |
| `max_invitations_per_day`        |           30 | 1–1000                                          | 创建邀请时按邀请人读取      |
| `offline_threshold_minutes`      |            5 | 2–60 分钟                                       | 每轮在线状态扫描读取        |
| `dataset_import_max_files`       |        50000 | 1–`max(code default, deployment default)`       | 导入 API 受理时快照         |
| `dataset_import_max_total_bytes` | 214748364800 | 1–`max(code default, deployment default)` bytes | 导入 API 受理时快照         |
| `task_create_sync_threshold`     |         2000 | 0–`max(code default, deployment default)`       | 数据集关联请求决定同步/异步 |
| `video_chunk_warmup_lookahead`   |            1 | 0–`max(code default, deployment default)` 块    | 视频邻块预热决策            |

- **超范围部署值兼容**：部署若已使用超出新增范围的数值，读取会保留该有效值并在 GET 的 `metadata[key].in_range=false` 中标记，管理员可以显式 reset。
- **空值与严格类型校验**：历史 `system_settings.value_json IS NULL` 行继续继承环境默认；严格类型校验拒绝字符串布尔值、浮点计数和未知字段。

## 版本、并发与缓存

- **乐观版本与元数据**：
  - GET 返回不透明 `version`（包含数据库覆盖及部署基线的带密钥摘要，部署默认变化也会使旧编辑版本失效）和每项 `metadata`（`source`、`deployment_default`、`updated_at`、`updated_by`、`value_type`、`unit`、`effect`、`min_value`、`max_value`、`in_range`）。
  - PATCH / RESET 可携带读取时的 `expected_version`；服务在 PostgreSQL 事务中取得 advisory lock 后比较版本，冲突返回 `409` 并提供最新的非敏感读回。
- **事务提交与进程内缓存**：
  - 写入和审计在同一事务中完成；提交后才失效本进程缓存，回滚或未提交事务读取不会污染缓存，嵌套事务提交也不会提前发布外层修改。
  - 业务读取最多使用 30 秒进程缓存，管理 GET 在提交后绕过缓存读回。
- **SMTP 敏感字段保护**：
  - SMTP 密码只在内部用于发送，响应和冲突详情只返回 `password_set`，审计只记录是否变更。
  - 密码传空串表示保存一个明确的空值，`reset` 表示删除数据库覆盖（两者语义不同）；发送邮件使用单次查询读取已保存的主机、端口、账号、密码和发件人，避免混用不同版本的配置。

## 导入快照

连接器导入 API 用同一次读取取得文件数、总字节和设置版本，由原生接受命令同时保存到 Assets 操作与 `AsyncJob.payload.settings_snapshot`。独立执行进程按该快照保存完整清单，枚举超限在复制前失败；进程接替和同 UUID 重放继续使用首次预算。主机白名单仍在外部连接附近读取当前覆盖，显式空覆盖拒绝连接。迁移不自动采纳缺少受理证据的活动旧作业，需先排空或取消。

导入 worker 的 `payload.stage` 在枚举时为 `collecting`，进入逐项导入后为 `importing`，终态以 `AsyncJob.status` 为准。数据库无法取得预算快照时任务失败，不回退到更宽松的部署值。

## 业务降级

- **可选优化降级**：视频邻块预热（`video_chunk_warmup_lookahead`）是可选优化，读取失败时记录诊断并跳过额外队列，主视频块请求继续返回。
- **核心门禁 fail-closed**：在线状态、邀请判断和数据集关联的配置读取错误沿现有业务错误路径返回，不能静默采用更宽松的预算。
