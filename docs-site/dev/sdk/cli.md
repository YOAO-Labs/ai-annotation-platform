---
title: aap CLI 参考
audience: [dev]
type: reference
status: beta
last_reviewed: 2026-10-24
---

# aap CLI 参考

`projects`、`batches`、`datasets`、`dashboard`、`predictions` 和 `annotations` 扩展工作流命令；另有 `project-performance`、`search`、`guide-assets` 和 `failed-predictions`，见 [项目、批次与数据运维](./project-workflows)。批次分派保留 preview_version，标注导入的部分错误会在完整 JSON 回执后返回非零退出码；预检和 dry-run 不会自动转为实际写入。

账户和管理命令组为 `auth`、`account`、`api-keys`、`users`、`groups`、`invitations`、`health`、`ml-capabilities`、`system-settings`、`audit-logs`、`storage-connections`、`admin-operations`、`ml-diagnostics` 和 `project-ml`。详见 [账户、管理与运维](./account-and-operations)。一次性凭据要求 `--json` 或 `--show-secret`；真实邮件、网络探测及生命周期变更使用各命令的确认选项。批量 apply 部分失败先输出完整结果再退出 1，根健康降级同样输出报告后退出 1。

`aap files` 提供任务文件分步上传、短期下载 URL 和缩略图回填；`aap scenes` 提供数据集场景与逐帧位姿读取、管理员元数据更新，见 [文件上传与数据集场景](./files-and-scenes)。

视频与运维命令组为 `videos`、`task-videos`、`video-tracker`、`video-review`、`templates` 和 `storage`。复杂请求支持 `--body file.json` 或 `--body -`；分段提交／重开／取消分配、候选决策、停用源轨迹与资产重试使用各命令的确认选项。`projects create` 支持 `--template` 或 `--source-project`，此时可以省略 `--type` 继承类型。完整流程见 [视频、模板与存储运维](./video-and-templates)。

`aap` 是 Python SDK 附带的命令行工具,需要 `[cli]` extras:

```bash
pip install 'ai-annotation-sdk[cli]'
aap --version        # aap <sdk-version>@AAP<aap-target-version>
```

CLI 与 SDK/TUI 共用同一 SemVer。`AAP...` 部分是该 release 完成测试与 OpenAPI 对账的平台 target，
不是未经验证的兼容版本区间。

新增 `comments`、`annotation-history`、`bug-reports`、`mask-formats`、`mask-qc`、`point-cloud-quality` 命令组，见 [讨论、缺陷报告与质量处理](./annotation-quality)。修复部分结果先输出回执再退出 1；结构化错误保留安全的 reason、run_id 与 job_id。

任务级读取、媒体、租约、点云与几何修改见 [任务工作流](./task-workflows)；六条 WebSocket 的离线发现、typed 事件、鉴权与有界 NDJSON 监听见 [公开事件流](./streams)。REST 已覆盖全部 558 项采用接口，十项执行器内部协议保持排除。

## 配置

CLI 凭据按以下优先级解析:

1. 环境变量 `AAP_BASE_URL` / `AAP_API_KEY`
2. 配置文件 `~/.config/ai-annotation/config.toml`(由 `aap login` 写入,权限 **0600**,含敏感 api_key,请勿提交到版本库)

两处都没有时,命令提示先 `aap login` 并以退出码 1 结束。

## 帮助系统

所有命令都支持 `-h` / `--help`(二者等价)查看用法。顶层 `aap -h` 按用途分组展示：配置与发现(`login` / `me` / `tui` / `capabilities` / `describe`)、资源与协作(`projects` / `datasets` / `batches` / `members` / `notifications` / `feedbacks` / `data-manager` / `task-views`)、标注与学习流水线(`predictions` / `pipelines` / `jobs` / `export` / `learning`)、监控与 ML 运维(`ml-backends` / `ml-registry` / `service-pools` / `stats` / `dashboard`)。

```bash
aap -h                       # 顶层: 分组命令 + 快速上手 + env 说明
aap export -h                # 子命令: 用法 + 参数 + 示例
```

## 命令一览

### aap capabilities 与 describe

```bash
aap capabilities --tag training-runs --status covered --limit 20 --offset 0 --json
aap describe training_runs.create --json
aap describe learning.runs.create --json
```

无需配置账号或运行 API。能力列表支持有界分页以及 tag / 状态筛选；`covered`、`excluded`、`uncovered` 分别表示已实现、主动排除和尚未接入。`describe` 展开一个操作的 SDK/CLI 关联、参数、schema、额外约束与示例。规范 SDK 名称和可解析的 CLI 点分路径均可使用；多步骤别名应按返回的关联或提示选择具体方法。

目录随安装包分发，真实服务器的权限、预检新鲜度、资源归属及执行器能力继续由服务端决定。完整工作流见 [Agent 接入与操作](./agents)。

### aap notifications、feedbacks 与 pipelines

| 命令组          | 子命令                                                                                                           |
| --------------- | ---------------------------------------------------------------------------------------------------------------- |
| `notifications` | `list`, `unread-count`, `mark-read`, `mark-all-read`, `delete`, `clear-read`, `preferences`, `update-preference` |
| `feedbacks`     | `thread`, `reply`, `patch`, `delete`                                                                             |
| `pipelines`     | `list`, `get`, `create`, `update`, `delete`, `apply`, `preannotate`                                              |

复杂写请求使用 `--body` 文件或 stdin `--body -`。通知清理／删除、反馈软删除以及编排复制／执行等受保护操作遵循确认规则，JSON 模式使用 `--yes`。`apply` 复制编排而不运行推理；`preannotate` 仅返回受理信息，可用 `aap jobs get <job-id> --json` 继续查询，不隐式等待。预标注可显式传 `--idempotency-key`，SDK 不自动生成或替换它。

通知针对当前账号；反馈线程的 cursor 绑定根 ID；编排库列表没有服务端分页。完整参数通过 `--help` 或离线 `describe` 读取，范围、补丁 null 与失败恢复语义见 [通知、反馈与预标注](./workflows)。

### aap data-manager 与 task-views

| 命令组         | 子命令                                                                                                                                                                        |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data-manager` | `schema`, `summary`, `matches`, `query-objects`, `object-detail`, `object-location`, `query-tracks`, `track-detail`, `assignment-preview`, `assignment-apply`, `export-tasks` |
| `task-views`   | `list`, `get`, `create`, `update`, `delete`, `copy`, `query`, `tasks`                                                                                                         |

所有命令使用 `--project`；复杂查询与写入使用 `--body 文件` 或 `--body -`。`data-manager schema` 使用 `--entity-scope`，`task-views list` 使用 `--scope`，值均为 tasks/objects/tracks。task query 与 saved-view tasks 使用 limit/offset，实体查询 body 使用不透明 cursor，不能混用分页形式。

POST 查询和 assignment-preview 是读取，不要求确认。assignment-apply、export-tasks 和视图删除使用确认规则，JSON 模式必须 `--yes`。分配应用需要原选择／目标及 preview_version；导出仅接受明确任务选择，可显式传 `--idempotency-key`，不自动扩范围或重试。stdin JSON 拒绝非有限数值。完整示例见 [Data Explorer 与保存视图](./data-explorer)。

### aap learning

学习命令统一放在 `aap learning` 下；原 `aap datasets` 管理上传素材，`aap learning datasets` 管理冻结训练版本。项目资源使用 `--project <项目 UUID>`，其他路径 ID 使用位置参数，查询条件与分页使用具名选项。每个命令可加 `--json`，完整参数见 `--help`。

| 子组              | 命令                                                                                                                                                                                                     |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `training`        | `catalog`, `projects`, `runs`                                                                                                                                                                            |
| `task-datasets`   | `list`, `unrecognized-versions`                                                                                                                                                                          |
| `datasets`        | `preflight`, `create`, `list`, `get`, `retry`, `download`, `delete`, `insights`, `classes`, `compare`, `samples`, `sample`, `download-media`                                                             |
| `runs`            | `catalog`, `parameter-schema`, `initialization-source`, `preflight`, `create`, `list`, `get`, `events`, `evaluation`, `telemetry`, `logs`, `download-artifact`, `recovery`, `cancel`, `resume`, `delete` |
| `recipes`         | `list`, `create`, `get`, `revisions`, `create-revision`, `resolve`, `clone-candidate`, `archive`                                                                                                         |
| `previews`        | `capabilities`, `create`, `get`, `download-image`, `cancel`, `delete`                                                                                                                                    |
| `models`          | `create`, `list`, `get`, `download`, `deploy`, `lineage`, `decisions`, `decide`                                                                                                                          |
| `releases`        | `list`, `create`                                                                                                                                                                                         |
| `policies`        | `get`, `save`, `check`, `disable`, `delete`, `triggers`                                                                                                                                                  |
| `evaluations`     | `qualification`, `create`, `list`, `get`, `compare`, `report`, `samples`, `source`, `download-image`, `cancel`, `retry`, `delete`                                                                        |
| `studies`         | `preview`, `create`, `list`, `get`, `cancel`                                                                                                                                                             |
| `active-learning` | `pool`, `preview`, `create`, `list`, `get`, `report`, `candidates`, `download-image`, `decisions`, `decide`, `feedback`, `create-feedback`, `cancel`, `retry`                                            |

复杂写请求使用 `--body <UTF-8 JSON 文件>` 或 `--body -` 从 stdin 读取，字段与 [SDK 请求对象](./python-client#请求对象与冻结身份)相同。无效文件、非对象 JSON 和模型校验失败会在发送前返回非零退出码；错误写到 stderr，不回显请求值。新建返回已受理资源，不自动等待训练，也不会生成或替换幂等 ID。

```bash
aap learning training catalog --json
aap learning training runs --state active --limit 20 --json
aap learning task-datasets list --project "$PROJECT_ID" --json
aap learning datasets preflight --project "$PROJECT_ID" --body selection.json --json
aap learning datasets create --project "$PROJECT_ID" --body dataset-request.json --json
aap learning datasets list --project "$PROJECT_ID" --task detect --limit 20 --offset 0 --json
aap learning runs parameter-schema --project "$PROJECT_ID" --framework ultralytics --base-model yolo11n --json
aap learning runs preflight --project "$PROJECT_ID" --body run-draft.json --json
aap learning runs create --project "$PROJECT_ID" --body run-request.json --json
aap learning runs get "$RUN_ID" --project "$PROJECT_ID" --json
aap learning runs logs "$RUN_ID" "$ATTEMPT_ID" --project "$PROJECT_ID" --after 0 --limit 120 --json
aap learning runs download-artifact "$RUN_ID" "$ATTEMPT_ID" best.pt --project "$PROJECT_ID" --out ./best.pt --json
aap learning recipes archive "$RECIPE_ID" --project "$PROJECT_ID" --expected-head-revision 2 --yes --json
aap learning policies disable --project "$PROJECT_ID" --expected-revision 2 --yes --json
```

`dataset-request.json` 必须包含与选择条件对应的 `preflight_digest`；`run-request.json` 带回训练预检的 `config_digest`，字段名为 `preflight_digest`。Study 与主动学习创建带回预览的 `preview_digest`。重放结果不确定的请求时使用同一文件；数据或参数变化后重新预检并使用新的请求 ID。

删除、取消、重试、恢复、归档、部署、发布、策略保存／检查／停用、参数搜索创建、主动学习创建和人工决策／反馈提交都会确认。交互模式默认可取消；JSON 模式必须加 `--yes`，拒绝确认不会发送变更。`--yes` 不绕过服务端权限、预算和 409 冲突保护。

训练、评估等状态以资源详情为准；`202` 只表示已受理，`cancelling` 仍可能在退出中。日志不能同时指定 `--after` 和 `--before`。下载使用 `--out`，JSON 输出 `{"path":"..."}`；删除成功输出 `{"deleted":true}`。文件校验失败保留原目标，过期预签名链接需重新调用下载命令。

项目级调用需要项目管理权限及对应 API-key scopes；全局 `training` 子组另要求平台管理角色。核心安装没有 CLI 依赖时，`aap` 会给出 extras 安装提示。执行器内部协议不提供 CLI。

### aap login

```text
aap login --url <平台地址> [--api-key <key>] [--json]
```

验证凭据并写入 `~/.config/ai-annotation/config.toml`。省略 `--api-key` 时交互式隐藏输入。写盘前会先用给定凭据调一次轻量 GET 验证连通,**验证失败不落盘**。

```bash
aap login --url http://localhost:8000 --api-key ak_...
```

### aap projects

```text
aap projects list [--json]
aap projects create --name <名称> --type <image|video|lidar> [--json]
aap projects update <project-id> [--name ...] [--status ...] [--json]
aap projects delete <project-id> [--yes] [--json]
```

```bash
aap projects list                              # rich 表格: ID/名称/类型/状态/任务进度
aap projects create --name demo --type image
```

### aap datasets

```text
aap datasets list [--state active|archived|all] [--search ...] [--data-type ...] [--limit <n>] [--offset <n>] [--json]
aap datasets archive <dataset-id> [--yes] [--json]
aap datasets restore <dataset-id> [--yes] [--json]
aap datasets create --name <名称> [--data-type <类型>] [--json]
aap datasets update <dataset-id> [--name ...] [--description ...] [--axis-convention ...|--clear-axis-convention] [--json]
aap datasets items <dataset-id> [--limit <n>] [--offset <n>] [--json]
aap datasets delete-item <dataset-id> <item-id> [--yes] [--json]
aap datasets projects <dataset-id> [--json]
aap datasets upload <dataset-id> <路径> [--zip] [--json]
aap datasets link <dataset-id> <project-id> [--json]
aap datasets preview-unlink <dataset-id> <project-id> [--json]
aap datasets unlink <dataset-id> <project-id> [--idempotency-key <UUID>] [--yes] [--json]
aap datasets delete <dataset-id> [--yes] [--json]
```

- `upload`:路径为目录时递归收集全部文件逐个上传(三步流,带进度条);为单文件时只传该文件;加 `--zip` 则按 ZIP 整包上传、后端解压入库(此时路径必须是 ZIP 文件)。
- `link`:关联数据集到项目;后端异步建任务时自动跟随 job 进度直到完成。
- `preview-unlink`:只查询将删除的 task / annotation / batch 计数。`unlink` 的交互模式会先显示同一预览再确认。
- `list` 默认只列活动数据集；用 `--state archived` 查看归档数据集，或 `--state all` 查看全部。
- `archive` / `restore` 先读取当前修订并展示影响预览，再确认提交。非交互终端和 JSON 模式必须显式传 `--yes`，否则不发请求；JSON 模式的预览写入 stderr，stdout 仅输出变更结果。预览过期或返回 409 时命令失败，须重新运行并核对，不自动刷新或重试。
- 归档保留已有任务、标注与冻结内容，停止数据集管理写入和新训练冻结；恢复不重放已撤销权限、来源委托或旧后台任务。归档数据集须先恢复才能删除。冻结版本统计仅覆盖已记录的数据集成员关系。

```bash
aap datasets create --name demo-ds
aap datasets upload 0199aa... ./imgs
aap datasets upload 0199aa... ./data.zip --zip
aap datasets link 0199aa... 0199bb...
aap datasets preview-unlink 0199aa... 0199bb...
aap datasets unlink 0199aa... 0199bb... --yes --idempotency-key <saved-uuid>
```

自动化脚本在首次调用前生成并保存 UUID；响应丢失后再次运行时通过 `--idempotency-key` 复用它。省略时每次命令生成新键，不能用于重放前一次不确定结果。

### aap batches

```text
aap batches list <project-id> [--status <批次状态>] [--json]
aap batches create <project-id> --name <名称> [--dataset-id <id>] [--priority <0-100>] [--json]
aap batches update <project-id> <batch-id> [--name ...] [--priority ...] [--json]
aap batches delete <project-id> <batch-id> [--force] [--yes] [--json]
aap batches transition <project-id> <batch-id> --status <status> [--reason ...] [--json]
aap batches reject <project-id> <batch-id> --feedback <text> [--json]
aap batches reset <project-id> <batch-id> --reason <text> [--yes] [--json]
aap batches distribute <project-id> [--annotator-id ...] [--reviewer-id ...] [--only-unassigned|--all] [--json]
aap batches bulk-activate|bulk-approve <project-id> --id <batch-id> [--id ...] [--json]
aap batches bulk-reject <project-id> --id <batch-id> [--id ...] --feedback <text> [--json]
aap batches bulk-reassign <project-id> --id <batch-id> [--id ...] [--annotator-id ...|--clear-annotator] [--reviewer-id ...|--clear-reviewer] [--json]
aap batches export <project-id> <batch-id> [--target <format>] [--body <file|->] [--json]
aap batches download-voc <project-id> <batch-id> --out <path> [--json]
```

列出和管理项目下的批次。`--status` 按批次状态过滤(如 `active` / `reviewing` / `approved`)。删除已有完成、审核或退回结果的批次必须同时显式传 `--force` 和 `--yes`;重置批次也必须交互确认或传 `--yes`。服务端仍会执行权限和 409 冲突校验。

批量命令的 JSON 结果保留 `succeeded` / `skipped` / `failed`。`failed` 非空时仍先输出完整 JSON，再以退出码 1 结束；脚本只应重试 `failed` ID。

```bash
aap batches list P-1
aap batches list P-1 --status reviewing --json
```

### aap members

```text
aap members list <project-id> [--json]
aap members add <project-id> --user-id <user-id> --role <annotator|reviewer|viewer> [--json]
aap members preview-role <project-id> <member-id> --role <annotator|reviewer|viewer> [--replacement-annotator-id <id>] [--replacement-reviewer-id <id>] [--json]
aap members change-role <project-id> <member-id> --role <r> --expected-version <n> --preview-token <t> --reason <text> [--replacement-annotator-id <id>] [--replacement-reviewer-id <id>] [--yes] [--json]
aap members remove <project-id> <member-id> [--yes] [--json]
```

列出、添加、移除项目成员并变更项目角色。`--role` 是**项目角色**(`annotator` / `reviewer` / `viewer`);`list` 会分列展示项目角色与平台角色。添加时的账号状态、平台 / 项目角色兼容性与管理权限由服务端校验。

`preview-role` 是只读预检,返回当前 / 目标角色、版本、阻塞项、受影响资源快照与一次性 `preview_token`;`change-role` 需要带上预检的 `--expected-version` 与 `--preview-token` 以及 `--reason`,属变更操作,JSON 模式必须显式 `--yes`。版本 / 快照失效或资源占用时服务端返回 409,需重新预检。变更规则见[可见性与权限](../concepts/visibility-and-permissions#成员角色变更与交接)。

```bash
aap members list P-1
aap members preview-role P-1 0199ab... --role reviewer
aap members change-role P-1 0199ab... --role reviewer --expected-version 2 --preview-token <tok> --reason "重新分配职责" --yes
```

### aap me

```text
aap me [--json]
```

显示当前认证主体(用户 / 邮箱 / 平台角色),用于自检凭据与确认权限边界;项目职责用 `aap members list` 或 `GET /projects/{id}/access` 查看。

```bash
aap me
```

### aap tasks

`aap tasks list --project <project-id>` 支持标注人、批次、退回原因、类别及 cursor 筛选。`--unbatched` 只读未分批任务，`--all-batches` 明确关闭这一限制；它不表示只读已分批任务。项目类型和数据类型可在 `aap projects list` 中重复传入，`aap datasets list --has-scenes/--no-scenes` 按场景筛选，`aap annotations list <task-id> --video-segment-id <id>` 读取指定片段，`aap jobs list --search <text>` 查询 Job。

```text
aap tasks submit|withdraw|reopen|accept-rejection <task-id> [--json]
aap tasks skip <task-id> --reason <image_corrupt|no_target|unclear|other> [--note ...] [--json]
aap tasks review-claim <task-id> [--json]
aap tasks review-approve <task-id> [--expected-qc-digest ...] [--warning-issue-id ...] [--note ...] [--json]
aap tasks review-reject <task-id> --reason-type <missing|extra|wrong_label|wrong_geometry> --reason <text> [--json]
```

任务状态和审核认领归属由服务端以 403 / 409 校验；CLI 不在本地猜测当前用户能否执行操作。

### aap annotations

```text
aap annotations bulk-update --id <annotation-id> [--id ...]
    [--class-name ...] [--attributes-json '{...}'] [--z-order <n>]
    [--locked|--unlocked] [--hidden|--visible] [--json]
```

批量修改只支持类别、属性、层级、锁定和隐藏状态，不接受 geometry。

### aap predictions

```text
aap predictions import <project-id> <文件> [--format <aap_json|coco|yolo>] [--dry-run] [--json]
```

导入外部预测结果。`--dry-run` 只校验不落库,适合正式导入前先验文件。

```bash
aap predictions import 0199bb... preds.json --format aap_json --dry-run
aap predictions import 0199bb... preds.json --format aap_json
```

### aap jobs

```text
aap jobs get <job-id> [--json]
aap jobs wait <job-id> [--json]
aap jobs cancel <job-id> [--json]
aap jobs retry-failed <job-id> [--yes] [--json]
```

- `get`：读取现有 Job 的当前状态，不等待、不取消或重建任务。
- `wait`:轮询异步任务直到终态,rich 进度条跟随 `progress_pct`。job 以 failed / cancelled 结束时输出错误并以退出码 1 结束。
- `cancel`:请求**软取消**一个 job(协作式——后端写取消标记,worker 在下一条任务边界落 `cancelled`)。仅可取消的 kind 且处于 pending/running 时有效,否则后端返回 400/409、命令以退出码 1 结束。终态由后续 `jobs wait` / `tui` 反映。
- `retry-failed`:只对服务端允许重试的 job kind 和管理角色开放；执行前必须交互确认或传 `--yes`，返回实际入队与跳过数。

```bash
aap jobs wait 0199cc...
aap jobs cancel 0199cc...
```

### aap ml-backends

```text
aap ml-backends list --project <project-id> [--json]
aap ml-backends get <backend-id> --project <project-id> [--json]
aap ml-backends available --project <project-id> [--json]
aap ml-backends enable|disable <backend-id> --project <project-id> [--json]
aap ml-backends health <backend-id> --project <project-id> [--json]
aap ml-backends pools --project <project-id> [--json]
aap ml-backends pool-enable|pool-disable <pool-id> --project <project-id> [--json]
```

项目管理员可查看本项目已经挂载的 Backend、全局可用 Backend 和可用服务池，并控制项目启用态。
`MLBackend.id` 始终是全局 registry instance ID；service pool 使用独立的逻辑 pool ID，二者不可互换。
`list` 输出 rich 表格(名称 / 状态 / model_version / GPU 利用率 / url)，`get` 输出含 `health_meta`
(GPU / cache / capabilities)的完整对象。

```bash
aap ml-backends list --project 0199bb...
aap ml-backends get 0199dd... --project 0199bb... --json
aap ml-backends available --project 0199bb...
aap ml-backends pool-enable 0199ee... --project 0199bb...
```

### aap ml-registry

```text
aap ml-registry list [--json]
aap ml-registry create --name <名称> --url <backend-url> [--prompt-auth-token|--auth-token-env <VAR>] [选项] [--json]
aap ml-registry update <registry-id> [--prompt-auth-token|--auth-token-env <VAR>] [选项] [--json]
aap ml-registry delete <registry-id> [--yes] [--json]
aap ml-registry health <registry-id> [--json]
aap ml-registry unload <registry-id> [--yes] [--json]
```

全局物理 Backend 注册表仅供 super-admin 运维。删除和卸载均需确认；服务端静默守卫未通过时返回
409/503，CLI 不自动轮询、强制卸载或绕过保护。

```bash
aap ml-registry create --name detector-a --url http://ml-a:9090
aap ml-registry health 0199dd...
aap ml-registry unload 0199dd... --yes
```

Backend token 不接受命令行明文值，避免进入 shell history 或进程参数。交互使用 `--prompt-auth-token` 隐藏输入；自动化场景先把 token 写入受保护的环境变量，再用 `--auth-token-env <变量名>` 读取。

### aap service-pools

```text
aap service-pools list|get|create|update|delete ...
aap service-pools member-add|member-remove|member-drain|member-resume <pool-id> --registry-id <registry-id> ...
aap service-pools drift-preview <pool-id> --registry-id <registry-id> [--json]
aap service-pools drift-accept <pool-id> --registry-id <registry-id>
    --expected-fingerprint <fingerprint> [--enable-pool] [--yes] [--json]
aap service-pools topology|runtime [--json]
```

service pool 是逻辑路由身份，member 指向物理 registry instance。删除池、移除或 drain 成员以及接受
能力漂移均需确认。`drift-accept` 不会自动采用服务端最新指纹：必须显式传入预览得到的
`--expected-fingerprint`；人类模式会再次打印 preview，JSON 模式则必须传 `--yes`。

```bash
aap service-pools create --name detector-pool
aap service-pools member-add 0199ee... --registry-id 0199dd... --weight 2
aap service-pools drift-preview 0199ee... --registry-id 0199dd... --json
aap service-pools topology --json
```

### aap stats

```text
aap stats [--json]
```

可见项目聚合统计 + 最近 12 周趋势(无 Textual 时用 unicode 块字符画 sparkline):数据总量 / 完成量 / AI 标注率 / 待审,各一条趋势条。

```bash
aap stats
```

### aap dashboard

```text
aap dashboard people [--project <id>] [--role <r>] [--period <p>] [--json]
aap dashboard me [--period <p>] [--json]
```

- `people`:全员绩效卡片(super_admin / project_admin;**project_admin 必须 `--project`** 指定其管理范围)。表格列 姓名 / 角色 / 产出分 / 质量分 / 退回率 / 7 日趋势。
- `me`:当前用户自助绩效(任意已认证),输出本期产出 / 质量 + 自身与团队均线 4 周趋势条 + 一次通过率。

```bash
aap dashboard people --period 7d
aap dashboard people --project 0199bb...   # project_admin 视角
aap dashboard me
```

### aap export

```text
aap export project <project-id> --target <格式> [--target ...] [--body <file|->] --out <输出路径>
    [--include-attributes/--no-include-attributes] [--video-frame-mode <m>]
    [--axis-frame <f>] [--wait/--no-wait] [--json]
```

一条命令完成「创建导出 job →(默认 `--wait`)等待完成 → 下载到 `--out`」全流程。`--target` 可重复以一次导出多个格式(如 `aap_json` / `coco` / `yolo-det` / `video_json` / `kitti`,完整清单见[导出格式参考](/user-guide/reference/export-formats))。选项与 Web / TUI 对齐:`--include-attributes`(默认含属性数据)、`--video-frame-mode`(video 项目 `keyframes` | `all_frames`)、`--axis-frame`(lidar 3D box `iso` | `source`)。`--no-wait` 只创建返回 `job_id`(配合 `aap jobs wait` 跟进),此时不需要 `--out`。

```bash
# 多格式一次导出, 等待并下载
aap export project 0199bb... --target coco --target yolo-det --out ./export.zip

# 只创建, 异步跟进
aap export project 0199bb... --target aap_json --no-wait
```

视频 `scope` 与点云 `lidar` 选项通过 `--body` 的 JSON 请求体发送；普通格式、坐标和属性选项仍使用具名参数。VOC 使用同步下载路径，必须指定 `--out`，不能混选其他格式或使用 `--no-wait`；JSON 输出仅为 `{"out":"..."}`，没有 Job ID。批次 VOC 使用 `aap batches download-voc`。完整请求体见 [范围导出与 VOC](./cookbook#_9-范围导出与同步-voc)。

### aap tui

```text
aap tui
```

启动 [TUI 监控面板](./tui),需要 `[tui]` extras;未安装时提示安装命令并以退出码 1 结束。

## --json 可脚本化契约

所有业务命令支持 `--json`。**CI / 脚本只应依赖此模式**,人类可读的 rich 输出(表格、颜色、进度条)不是稳定契约,随时可能调整。破坏性命令在 `--json` 模式下从不弹出 prompt，缺少 `--yes` 时以 usage error 退出码 `2` 失败。

`--json` 模式下:

- **stdout 只输出裸 JSON**(单行,无 rich 装饰、无进度条),可直接 `| jq` 处理;
- **错误默认走 stderr 文本**；根选项 `--error-format json` 选择结构化错误，不污染成功 stdout;
- **退出码语义**:0 = 成功;1 = SDK / HTTP / 网络失败;2 = 命令用法错误(包括破坏性 JSON 命令缺少 `--yes`)。脚本判断成败看退出码,不要解析错误文本。

各命令的 JSON 输出形状:

| 命令                              | stdout JSON                                                                                                      |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `login`                           | `{"config_path": "...", "base_url": "..."}`                                                                      |
| `projects list`                   | Project 对象数组                                                                                                 |
| `projects create`                 | Project 对象                                                                                                     |
| `projects update`                 | Project 对象                                                                                                     |
| `projects delete`                 | `{"deleted": true, "project_id": "..."}`                                                                         |
| `batches list`                    | Batch 对象数组                                                                                                   |
| `batches create/update`           | Batch 对象                                                                                                       |
| `batches delete`                  | `{"deleted": true, "project_id": "...", "batch_id": "...", "forced": bool}`                                      |
| `batches transition/reject/reset` | Batch 对象                                                                                                       |
| `batches distribute`              | BatchDistributeResult 对象                                                                                       |
| `batches bulk-*`                  | BulkBatchActionResult 对象                                                                                       |
| `batches export`                  | `{"job_id": "..."}`                                                                                              |
| `members list`                    | Member 对象数组(含 `role` / `platform_role` / `version`)                                                         |
| `members add`                     | Member 对象                                                                                                      |
| `members preview-role`            | ProjectMemberRolePreview 对象(阻塞项 / 资源快照 / `preview_token`)                                               |
| `members change-role`             | Member 对象                                                                                                      |
| `members remove`                  | `{"removed": true, "project_id": "...", "member_id": "..."}`                                                     |
| `me`                              | Me 对象(`id` / `email` / `name` / `role` …)                                                                      |
| `datasets create`                 | Dataset 对象                                                                                                     |
| `datasets update`                 | Dataset 对象                                                                                                     |
| `datasets items`                  | `Page[DatasetItem]` 对象                                                                                         |
| `datasets projects`               | Project 对象数组                                                                                                 |
| `datasets preview-unlink`         | DatasetUnlinkPreview 对象                                                                                        |
| `datasets unlink`                 | DatasetUnlinkResult 对象                                                                                         |
| `datasets delete/delete-item`     | 含 `deleted: true` 和对象 ID 的结果对象                                                                          |
| `datasets upload`(逐文件)         | UploadedItem 对象数组                                                                                            |
| `datasets upload --zip`           | ZipUploadResult 对象                                                                                             |
| `datasets link`                   | `{"link": LinkResult, "job": Job \| null}`                                                                       |
| `predictions import`              | ImportResult 对象                                                                                                |
| `jobs wait`                       | 终态 Job 对象                                                                                                    |
| `jobs cancel`                     | `{"job_id": "...", "cancel_requested": true}`                                                                    |
| `jobs retry-failed`               | JobRetryResult 对象                                                                                              |
| `tasks submit/skip/...`           | TaskActionResult 对象；`review-claim` 为 ReviewClaim                                                             |
| `annotations bulk-update`         | AnnotationBulkUpdateResult 对象                                                                                  |
| `ml-backends list`                | MLBackend 对象数组                                                                                               |
| `ml-backends get`                 | MLBackend 对象                                                                                                   |
| `ml-backends available/pools`     | ProjectMLBackend / ProjectServicePool 对象数组                                                                   |
| `ml-backends enable/disable`      | ProjectMLBackend 对象                                                                                            |
| `ml-backends pool-enable/disable` | ProjectServicePool 对象                                                                                          |
| `ml-backends health`              | MLBackendHealth 对象                                                                                             |
| `ml-registry list`                | MLBackend 对象数组                                                                                               |
| `ml-registry create/update`       | MLBackend 对象                                                                                                   |
| `ml-registry delete`              | `{"deleted": true, "registry_id": "..."}`                                                                        |
| `ml-registry health/unload`       | MLBackendHealth / MLBackendUnloadResult 对象                                                                     |
| `service-pools list`              | ServicePool 对象数组                                                                                             |
| `service-pools get/create/...`    | ServicePool 对象；`delete` 返回 `{"deleted": true, "pool_id": "..."}`                                            |
| `service-pools drift-preview`     | CapabilityDrift 对象                                                                                             |
| `service-pools topology/runtime`  | ServicePoolTopology / ServicePoolRuntimeSnapshot 对象                                                            |
| `stats`                           | ProjectStats 对象(标量 + 4 条 12 周序列)                                                                         |
| `dashboard people`                | PersonStat 对象数组                                                                                              |
| `dashboard me`                    | MyPerformance 对象                                                                                               |
| `export project`                  | `--wait`: `{"job_id": "...", "status": "...", "out": "..."}` · `--no-wait`: `{"job_id": "...", "waited": false}` |

对象字段与 [SDK 响应模型](./python-client#响应模型与前向兼容)一致(`model_dump(mode="json")` 序列化);服务端新增字段会原样透传,脚本应容忍未知字段。

示例:

```bash
# 创建项目并取回 id
pid=$(aap projects create --name ci-demo --type image --json | jq -r '.id')

# 导入预测,失败时退出码非 0 使 CI 步骤失败
aap predictions import "$pid" preds.json --format aap_json --json
```

## 错误输出约定

SDK / HTTP / 网络失败默认写入 stderr 文本并使用退出码 1；命令用法错误使用退出码 2，不向用户输出 traceback：

| 错误                        | 输出                                                                     |
| --------------------------- | ------------------------------------------------------------------------ |
| 401 未认证                  | `认证失败 (HTTP 401): <detail>; 请先运行 \`aap login\` 配置有效 API key` |
| 其他 HTTP 4xx/5xx           | `请求失败 (HTTP <code>): <detail>`                                       |
| SDK 异常(如 job 失败、超时) | 异常消息原文                                                             |
| 网络错误(连接失败、超时)    | `网络错误: <原因>`                                                       |

非 `--json` 模式下默认文本错误以红色渲染；`--json` 模式下默认仍为文本。需要机器处理时，在根命令使用 `--error-format json`：

```bash
aap --error-format json learning runs create --project "$PROJECT_ID" --body request.json --json
```

该模式把一次失败写成一个 JSON 错误对象，保留 HTTP 状态、已有服务端错误码/detail 与经过处理的字段位置，并覆盖文件、网络、确认拒绝和命令用法错误。缺少服务端错误码时保持未知；不会回显原请求输入、凭据或 traceback。成功 stdout 与既有退出码语义不变，同一进程多次调用不会沿用上一次的错误模式。

对象使用 `kind` 区分错误，按实际上下文提供 `status`、`code`、`detail`、`fields`、`request_id`、`retry_after`。Job 失败或等待超时保留 `job_id`，失败时还提供 `job_status`，便于继续查询；不附带 Job 的原始 payload 或失败消息。

结构化错误模式会关闭 stderr 上的等待和上传进度条；成功 stdout 的文本或 JSON 格式仍由各命令的 `--json` 决定。
