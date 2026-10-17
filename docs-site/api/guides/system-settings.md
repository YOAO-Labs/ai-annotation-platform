---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-17
---

# 系统设置

以下端点只允许超级管理员调用。设置写入数据库覆盖，环境变量仍是部署默认值；连接器主机白名单继续使用[专属端点](./storage-connections.md)。

SDK 使用 `client.system_settings`，CLI 使用 `aap system-settings`；SMTP 测试会真实发送邮件，须明确确认。版本、密码与重置示例见 [账户、管理与运维](../../dev/sdk/account-and-operations)。

| 方法与路径                               | 行为                                       |
| ---------------------------------------- | ------------------------------------------ |
| `GET /api/v1/settings/system`            | 读取有效值、来源元数据和不透明版本         |
| `PATCH /api/v1/settings/system`          | 更新显式给出的白名单字段                   |
| `POST /api/v1/settings/system/reset`     | 原子删除指定键的覆盖并记录审计             |
| `POST /api/v1/settings/system/test-smtp` | 使用已保存 SMTP 配置向当前用户发送测试邮件 |

GET 保留已有字段及 `smtp` 对象，新增六个运营配置字段、`version` 和 `metadata`。每项元数据描述部署默认值、来源、修改人/时间、类型、单位、作用、允许范围和当前值是否在范围内；密码默认值与有效值均不会回显。

```json
{
  "expected_version": "<version-from-get>",
  "task_create_sync_threshold": 0,
  "video_chunk_warmup_lookahead": 0
}
```

PATCH 保留合法的 `false`、`0` 和空字符串。`null` 继续表示忽略字段，不能用它重置覆盖。SMTP 密码传空字符串表示明确清除，省略表示保留；重置密码则重新使用部署默认。

```json
{
  "expected_version": "<version-from-get>",
  "keys": ["dataset_import_max_files", "dataset_import_max_total_bytes"]
}
```

写入与版本核对在同一串行化事务内进行。版本冲突返回 `409`，`detail.settings` 包含最新的非敏感读回；客户端保留草稿并要求用户核对后重试。旧 PATCH 调用可以不带版本，但仍受权限、白名单、严格类型和范围校验约束。未知字段及非法值返回 `422`。

六项参数的范围、缓存传播及真实消费者见[运行时覆盖契约](../../dev/concepts/system-settings.md)。连接器导入的两项预算在 API 受理时写入该次作业快照，后续修改及重试均不能改变已接受任务的预算。
