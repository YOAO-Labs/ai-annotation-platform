---
title: 训练数据版本 API
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-15
---

# 训练数据版本 API

基路径为 `/api/v1/projects/{project_id}/training-datasets`。所有操作要求项目负责人或超级管理员；API Key 还需 `annotations:read` 与 `datasets:read`，删除另需 `datasets:write`。

| 方法   | 路径后缀                 | 行为                                                 |
| ------ | ------------------------ | ---------------------------------------------------- |
| POST   | `:preflight`             | 校验范围、冻结候选数据，返回预检报告和 `digest`      |
| POST   | 空                       | 创建版本并排队，返回 `202` 与版本、`async_job_id`    |
| GET    | 空                       | `limit=20`、`offset=0` 分页；返回摘要列表与 `total`  |
| GET    | `/{version_id}`          | 读取版本详情与完整物化清单                           |
| POST   | `/{version_id}/retry`    | 对失败或取消版本重试同一快照，返回 `202`             |
| GET    | `/{version_id}/download` | 就绪且对象可用时，返回 10 分钟 `url` 和包的 `sha256` |
| DELETE | `/{version_id}`          | 删除无后代引用的终态版本及其存储，返回 `204`         |

预检请求示例：

```json
{
  "mode": "reviewed",
  "seed": 42,
  "validation_fraction": 0.2,
  "batch_id": null,
  "parent_id": null,
  "task_ids": [],
  "confirmed_negative_ids": []
}
```

`manager_confirmed` 模式必须显式填写 `task_ids`；`confirmed_negative_ids` 必须是其中的子集。父版本和批次必须属于当前项目。预检返回 `ready`、`digest`、`classes`、`summary`、`issues` 和 `excluded`；排除原因不阻止剩余合格数据构建，非法几何或不可用素材会阻止构建。

正式创建请求使用相同选择，增加 `name`、UUID `request_id` 和 `preflight_digest`。相同请求标识与内容重复提交返回原版本；相同标识用于不同请求返回 `409`。正式创建重新读取一致快照，摘要与预检不符时返回 `409`，要求重新预检。

版本状态为 `pending`、`running`、`ready`、`failed`、`cancelled`。通过 `POST /api/v1/async-jobs/{async_job_id}/cancel` 取消构建。取消和失败均同步数据版本与后台任务状态；旧执行退出前重试或删除返回 `409`。异步失败通过 `error_code` 和中文 `error_message` 展示，已复制的冻结素材可用于重试。

列表不返回完整素材清单。可用 `status=ready` 等状态筛选，过滤先于 `limit`／`offset` 分页执行。详情的 `manifest` 在就绪后包含 `summary`、`samples`、累计 `split_history`、`content_digest` 和 `validation_digest`，不会暴露存储凭据或源文件签名 URL。构建和发布时重新校验创建者与原 API Key 的当前权限。

## 训练总览 API

基路径 `/api/v1/training`。只允许活跃的项目管理员与超级管理员，API Key 需要 `annotations:read`、`datasets:read`。项目列表、实验列表及计数仅包含当前可管理项目；项目成员身份或历史任务创建者身份不扩大管理范围。

| 方法 | 路径        | 行为                                                                                                                      |
| ---- | ----------- | ------------------------------------------------------------------------------------------------------------------------- |
| GET  | `/catalog`  | 训练框架、基础权重、能力、共享资源、执行器和最近心跳                                                                      |
| GET  | `/projects` | 用 `search` 搜索名称或编号，`limit`／`offset` 分页；返回结构资格原因、就绪版本数与活跃实验数                              |
| GET  | `/runs`     | 用 `state=active\|finished\|failed\|all`、可选 `framework`／`project_id` 筛选及分页；返回实验、项目名称与同权限范围的计数 |

`catalog.items[].compatible` 仅在执行器在线时核对实际 GPU、镜像和框架版本；离线时为 `null`。配置已启用不代表执行器已在线或兼容。多个框架可共享一个容器，目录不会泄露执行器原始运行环境中的任意字段或凭据，也不会暴露不可管理项目的占用实验名称。

项目的 `can_prepare_dataset` 只表示媒体类型和矩形框类别满足结构条件，不代替实际样本预检。`ready_dataset_count` 只统计已有训练包校验摘要的就绪版本。实验计数在状态筛选前、框架与项目筛选后计算；`failed` 筛选包含失败和失联。队列读取沿用项目实验列表的失联对账，心跳超时标记 `lost`，继续保留资源租约。

## 训练实验 API

基路径 `/api/v1/projects/{project_id}/training-runs`，读取要求项目负责人或超级管理员及 `annotations:read`、`datasets:read`；创建、停止、续训和删除另需 `datasets:write`。

| 方法   | 路径后缀                                                     | 行为                                                   |
| ------ | ------------------------------------------------------------ | ------------------------------------------------------ |
| GET    | `/catalog`                                                   | 受控框架、基础权重、执行器在线与忙闲状态               |
| GET    | `/parameter-schema?framework=ultralytics&base_model=yolo11n` | 当前项目受控模型的完整参数元数据与定义身份             |
| POST   | `:preflight`                                                 | 检查就绪数据和有界参数，返回固定配置及 `config_digest` |
| POST   | 空                                                           | 使用相同请求及 `preflight_digest` 创建实验，返回 `202` |
| GET    | 空                                                           | 分页实验列表                                           |
| GET    | `/{run_id}`                                                  | 实验及全部执行记录                                     |
| GET    | `/{run_id}/events`                                           | 当前执行事件；`latest=true` 返回最近 100 条，按序排列  |
| POST   | `/{run_id}/cancel`                                           | 请求停止；排队实验立即取消，运行实验等待退出确认       |
| POST   | `/{run_id}/resume`                                           | 使用当前执行的已发布完整检查点创建下一次执行           |
| GET    | `/{run_id}/attempts/{attempt_id}/artifacts/{name}/download`  | 返回 10 分钟下载 URL、SHA-256 和大小                   |
| DELETE | `/{run_id}`                                                  | 删除终态实验及全部产物                                 |

创建请求包含 `name`、`dataset_id`、`framework`、受控 `base_model`、UUID `request_id` 和 `parameters`。参数范围：`epochs` 1–1000、`batch` 1–64、`imgsz` 128–1280 且为 32 的倍数、`lr0` 大于 0 且不超过 0.1、`seed` 0–2147483647、`patience` 0–1000。原六参数请求继续使用原适配器及精确幂等序列化，不接受任意框架参数。相同请求标识与内容重试返回原实验，防止网络中断后重复启动。

`framework` 支持目录中已验证的 `ultralytics` 和 `libreyolo`。目录的 `capabilities` 返回 `task_types`、`resume`、`artifact_download` 与 `model_registration`；LibreYOLO 的模型注册能力为 false。配置固定适配器修订和框架原生选项。LibreYOLO 指标文件包含真实 `summary`、`native_raw`、`native_alias_semantics`、`per_class_available=false` 和最终评估权重 SHA-256，不将其别名解释为常规精确率／召回率。

状态包括 `queued`、`preparing`、`training`、`evaluating`、`uploading`，停止时为 `cancelling`，终态为 `succeeded`、`failed`、`cancelled`。`lost` 表示心跳超时但仍持有资源，不能当作已停止。事件带当前 `attempt_id` 和连续 `sequence`，重复序号必须具有相同内容。

事件读取可指定 `attempt_id` 和 `after` 游标，每次最多 100 条，默认按序递增。客户端用最后一条 `sequence` 继续分页，并为各次执行分别维护曲线；`latest=true` 用于单独读取最近事件。`run.metrics` 与轮次事件代表最近一轮的指标，最终最佳权重评估保存在已校验的 `metrics` 产物中。

扩展请求先读取项目授权的 `parameter-schema`，然后在同一预检、创建接口提交 `configuration`，不能同时提交 `parameters`：

```json
{
  "name": "增强与优化器对照",
  "dataset_id": "<当前项目的就绪数据 UUID>",
  "framework": "ultralytics",
  "base_model": "yolo11n",
  "request_id": "<新的 UUID>",
  "configuration": {
    "schema_id": "<目录返回的 schema_id>",
    "schema_digest": "<目录返回的 schema_digest>",
    "values": { "epochs": 20, "optimizer": "AdamW", "mosaic": 0.5 }
  }
}
```

创建时仍须带预检返回的 `preflight_digest`。服务端拒绝未知、保留、不适用或尚未验证的键，拒绝隐式类型转换、非有限数、别名冲突和无效依赖。列表保持顺序与精度；目前输入尺寸只开放方形整数，冻结层选择器仍需额外资格验证。`mixup` 在 LibreYOLO 请求中是 `mixup_prob` 的别名，不能同时提交。

冻结的 `configuration` 保存 `identity`、`user_values`、`effective_values`、`origins` 与 `inactive_fields`。它与请求的 wire 结构不同，不能直接作为新请求回传。原生配置观测和核验结果通过 `native-config.json` 归档；扩展实验缺少该证据或配置不符时不能确认成功。

独立执行器使用 `/api/v1/internal/training`，通过 `X-Training-Executor-Token` 认证，调用 `claim` 以及 `attempts/{id}/heartbeat`、`uploads`、`finish`、`recover`。这些接口不使用用户 JWT。回报同时校验当前执行、执行器、启动标识和租约凭据。执行器收到短期对象 URL，没有数据库或存储管理凭据；claim 发送前持久化请求标识，丢失响应后恢复同一个分配。

## 模型注册、部署与发布

基路径 `/api/v1/projects/{project_id}`。这些接口要求项目负责人或超级管理员及 `annotations:read`、`datasets:read`；变更另需 `datasets:write`。

| 方法 | 路径后缀                                | 行为                                                            |
| ---- | --------------------------------------- | --------------------------------------------------------------- |
| POST | `/trained-models`                       | 注册同项目成功执行的权重，返回 `201`                            |
| GET  | `/trained-models`                       | 按 `limit`、`offset` 分页                                       |
| GET  | `/trained-models/{version_id}`          | 固定模型信息、部署状态与短期试用图片 URL                        |
| POST | `/trained-models/{version_id}/deploy`   | 指定 `registry_id`，校验、安装并试用；返回 ready 或 failed 部署 |
| GET  | `/trained-models/{version_id}/download` | 重新鉴权后签发权重下载 URL、大小与摘要                          |
| GET  | `/model-releases`                       | 当前选择、generation 及分页历史                                 |
| POST | `/model-releases`                       | 按预期 generation 发布、回退或清除选择，返回 `201`              |

注册包含 UUID `request_id`、`name`、`run_id`、`attempt_id` 和与原生类别顺序一致的完整 `bindings`。相同标识和请求返回原模型，内容冲突返回 `409`。只支持本平台已验证的 Ultralytics 图片检测产物。

发布包含 UUID `request_id`、`expected_generation`、`model_version_id`、`deployment_id` 和 `sample_confirmed=true`。回退增加 `rollback_from_id`，且模型与部署必须匹配该历史记录。清除时模型和部署均为 null。并发选择变动返回 `409`，应刷新；丢失响应后的原请求重试返回同一发布。发布不会自动发生。

既有 `/preannotate` 接受 `model_id="trained:<uuid>"` 或 `"project-release"`。服务端解析并冻结权重、类别绑定、部署与发布来源，客户不能提供冻结引用或权重 URL。`Idempotency-Key` 重放沿用原任务冻结身份。受控模型只接受单阶段整图图片检测，使用现有任务作用域和写入检查；不支持的范围在入队前拒绝，worker 再次校验。结果校验实际模型身份、任务身份及原生类别索引，来源保存在 Prediction 元数据中，失败重试继续使用原身份。

YOLO 的 `POST /managed-models` 和 `GET /managed-models/{version_id}` 使用 `Bearer` 凭据，仅供平台部署。平台自行生成短期内部对象 URL；后端以大小、SHA-256、原生类别及框架版本校验后原子保存。有效版本不能被其他身份覆盖，损坏副本可按固定摘要重建；缺少版本直接失败，不降级到内置模型。

## 自动训练策略

接口前缀 `/api/v1/projects/{project_id}/learning-policy`，只允许项目负责人／超级管理员，读取要求 `annotations:read` 与 `datasets:read`，变更另需 `datasets:write`。

| 方法与路径                          | 行为                                                            |
| ----------------------------------- | --------------------------------------------------------------- |
| `GET`                               | 返回当前策略；未配置时为 `null`                                 |
| `PUT`                               | 用 `expected_revision` 保存显式配置；首建为 0，版本冲突返回 409 |
| `POST /disable?expected_revision=N` | 独立关闭入口，训练目录不可用时也可关闭                          |
| `POST /check`                       | 立即检查或对账，复用周期任务的协调逻辑                          |
| `GET /triggers?limit=20&offset=0`   | 分页读取触发历史及关联数据／实验                                |
| `DELETE`                            | 仅移除已关闭且没有活跃触发或实验引用的策略与审计记录            |

保存请求明确提供父数据版本、训练器、基础模型、训练参数、变化门槛、检查间隔和冷却时间；可指定当前项目批次。平台固定实际目录的版本、镜像、基础摘要和物理资源，客户端不能提交执行脚本或产物地址。只纳入已审核数据；首次没有成功基线时统计全部合格训练内容。关闭、撤权和目录变化阻止后续新实验；成功不会自动注册或发布模型。

扩展执行器在 runtime 中声明 `executor_protocol: 2` 和精确的 `supported_configs` 身份列表。缺少握手的旧执行器只能领取原六参数实验；定义摘要或适配器修订不匹配不能领取扩展实验。全局目录的 `parameter_schemas[].compatible` 使用相同能力判定，离线为 `null`。旧租约的心跳、结束和恢复不依赖当前创建目录。

自动策略可显式保存 `configuration`，固定同一参数定义与有效值，后续子实验按该快照解析。旧策略不自动采用扩展默认值。镜像、基础权重或参数定义发生变化时仍暂停策略，要求负责人检查并重新保存。
