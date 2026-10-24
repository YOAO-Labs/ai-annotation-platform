---
title: 数据集上传与扫描
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# 数据集上传与扫描

端点均位于 `/api/v1/datasets`，沿用已有登录和数据集操作权限。文件位置属于服务端契约，客户端应使用返回的上传地址、item ID 与文件 URL，不按数据集名称拼接桶名或对象键。

## 生命周期与列表状态

`GET /datasets` 支持 `state=active|archived|all`，默认active，可与原search/data_type/has_scenes/location_id和分页组合。详情与列表的Dataset包含 `archived_at`、`archived_by`（可为空）和 `lifecycle_revision`（初始1）。归档不改变详情/成员GET、个人视图、已有Task或冻结版本的权限。

当前有效的项目管理员且为创建者，或超级管理员，持有 `datasets:write` 时可调用：

| 方法 | 路径                      | 请求与结果                                                                           |
| ---- | ------------------------- | ------------------------------------------------------------------------------------ |
| POST | `/{id}/lifecycle-preview` | `{operation:"archive"或"restore",expected_revision}`；返回五分钟影响预览             |
| POST | `/{id}/lifecycle`         | 同一请求加 `impact_token`；返回dataset_id、state、lifecycle_revision、archived_at/by |

预览包含 `dataset_id`、`lifecycle_revision`、`state`、`operation`、`measured_at`、`expires_at`、`impact_token` 和counts。计数为items、sources、projects、tasks、annotations、active_scans、active_registrations、enabled_schedules、pending_jobs、ready_lineage_versions和unfinished_lineage_versions。任务含多文件关联的次要成员；Job只统计当前payload.dataset_id可归因且pending/running的记录。冻结只统计持久sample.dataset_id可证明的版本，返回 `frozen_coverage=recorded_dataset_membership_only`，不把未知历史解释成无引用。

提交绑定原操作者/Key、候选、数据集配置与生命周期及单SQL关系快照，重新复核计数、身份和相关状态。令牌不可跨人/Key/对象重用；配置/关系变化或等量替换返回409、`dataset_lifecycle_preview_changed`，过期为`dataset_lifecycle_preview_expired`，无效签名/用途为`dataset_lifecycle_preview_invalid`，陈旧修订或已提交令牌重放为`dataset_lifecycle_changed`，忙碌为`dataset_busy`。当前状态与操作不符也返回409。缺少令牌或非法字段返回422，权限/对象不可见沿用403/404。

归档仅变更Dataset生命周期，阻止数据集业务写入：上传/导入、配置/成员/Scene结构与标定、关联/解绑、来源写操作、质量作业/复核以及包含归档合格样本的新冻结。写请求返回409、`dataset_archived`；历史异步票据跨生命周期返回`dataset_lifecycle_changed`。已有Task标注/读取、冻结打包与下载仍按原权限工作，取消/失败清理可收尾。归档状态的DELETE拒绝，恢复后才可使用原硬删除合同。

每次归档/恢复递增修订。恢复不回放权限或来源/同步配置，也不重启旧作业；历史缺少执行修订按1解释，首次生命周期变化后失效。成功审计为`dataset.archive`或`dataset.restore`，记录前后修订、影响及预览/复核时间，不保存令牌或对象路径；失败由独立请求审计记录。此合同不删除原件，也不授予物理回收许可。

## 直传

1. `POST /{id}/items/upload-init`，请求 `{ "file_name": "a.jpg", "content_type": "image/jpeg" }`，返回 `item_id`、`upload_url`、`expires_in`。
2. 向 upload_url 执行 PUT。
3. `POST /{id}/items/upload-complete/{item_id}`，完成后返回 `status`、`item_id` 和 `linked_tasks`。

每次 init 耐久分配独立最终键，但 upload_url 只允许 PUT 到 import 用途的暂存对象。complete 在后台线程完整读取暂存内容、冻结 SHA-256/大小，再条件创建最终对象，保存真实写入响应并完整回读后才关联业务。上限为 5 GiB；ETag 只用于条件读取，不作为摘要。旧暂存 URL 重放不会覆盖最终原件，重复 complete 复用已核验创建结果。最终托管键拒绝签发客户端 PUT URL。

超预算返回 `413`，内容/配置/维护代次变化、目标键冲突或不可复用的创建状态返回 `409`；不确定写入或收尾保留责任记录，不能自动重试同一最终写入。传入不安全相对路径返回 `422`。升级前已签发的旧直传继续使用历史完成合同，不补造新创建证据。

同数据集已核验的相同内容返回 `409`，detail 包含 `duplicate_of`，移除新占位 Item；新封存对象和创建回执保留待核对，不按裸键删除。旧 hash 只作为候选，实际内容一致后才认定重复；不可读的旧候选不使新文件被误判为重复。

## ZIP 与连接器

`POST /{id}/items/upload-zip` 保留相对目录，逻辑路径冲突时追加序号；内容重复计入 deduped。连接器复制沿用 `POST /{id}/import-from-connection` 和 AsyncJob。两者与目录 managed_copy 共用独立创建账本、条件最终写入、真实回执及完整 SHA 回读；只有 verified 才能在原业务事务中关联样本，回滚不丢创建责任，也不裸删最终对象。

ZIP 仍限制整包200MiB、5000文件、单文件100MiB；连接器受受理时固定的总文件/总字节额度约束，在同次 Worker 执行内按真实消费字节累计，清单少报大小、失败或去重不能重置额度。崩溃后重新执行的跨轮传输总量尚不提供累计保证。扫描已有对象不产生创建回执。创建证明、真实 VersionId 观察和独占所有权分开表达；本片不启用桶版本化或物理回收。

## 扫描

`POST /{id}/items/scan` 同时扫描固定的 storage_scan_prefix 和该数据集的稳定托管根。升级前数据集保留升级时的名称根，新建根为 `__aap_managed_datasets__/<dataset UUID>/`；改名不改扫描根。托管根下 `_objects/` 是平台写入保留区；手工扫描目录放在该保留区以外。扫描遇到重名时保存独立逻辑路径，Scene 与导出使用该路径，真实对象键保持原样。

外部写入流程可在新根下直接写 `sub/image.jpg`，无需伪造平台上传 UUID；扫描保留真实键并计算正确逻辑相对目录。已登记 file_path 的重复扫描跳过；扫描 ETag 仅作观测，不自动写成 content_hash。此接口补登记已有托管对象，不提供服务器目录原地引用。

内容及其物理位置映射和核验状态仍是内部数据。[受控存储位置](./storage-locations)另行提供目录登记、浏览与节点探测，不改变数据集原图读取路径。既有 Item/Task 的 file_path 与冻结版本输入保持原样。运维回填见[存储连接架构](/dev/concepts/storage-connections#内容身份与兼容回填)。

## 工作台分页与上下文

以下 GET 均要求登录及 `datasets:read`。`GET /datasets` 新增 `location_id`，仅可见该位置的管理员可用；使用现有来源管理授权，`total` 按唯一数据集计数。未获授权的位置返回404。原 search/data_type/has_scenes 与分页可组合。

- `GET /{id}/items`：`limit` 1–200、`offset`，可组合 `search`（文件名，最多200字符）、`source_id`、`scene_id`、`availability`、`media_type`、`extension`、`dimension_state` 与 `min_width/max_width/min_height/max_height`。来源 ID 必须属于该数据集且当前账号可管理，否则404。
- `GET /{id}/items/{item_id}`：直接读取单个样本及同一可用性合同；父子不匹配返回404。
- `GET /{id}/items/{item_id}/preview`：管理员的鉴权图片流，复用来源预览读取的绑定、策略和实时授权检查；`Cache-Control: no-store`，不支持 Range。不会读取外部原图；源文件缺失时仍可交付获授权的独立派生。
- `GET /{id}/workbench`：可用性分类计数、当前可管理来源和可见关联项目。项目统计仅包含该数据集内当前调用者可见的任务，沿用项目身份、批次与分派规则；直接样本优先，缺少直接样本时按 primary 主样本归属，相机辅助链接不会重复归属整个任务。`can_manage` 复用项目权限。旧 `/projects` 也按项目可见范围裁剪。
- `GET /{id}/training-versions`：分页列出有管理权限项目中的关联版本，返回 id/project_id/project_name/name/status/created_at，不另建版本详情。新引用快照或显式数据集选择保存 dataset_id；旧托管快照通过仍存在的源任务关联，任务删除后的旧快照不能推导未保存的数据集血缘。

项目上下文标签统计复用 `POST /api/v1/projects/{project_id}/data-manager/summary`，请求 `filter_json={"field":"dataset.dataset_id","op":"eq","value":"<dataset UUID>"}`，沿用该项目当前访问权限。`scope.matched_task_total` 是此数据集匹配的可见任务数，`visible_task_total` 仍是全项目可见任务数。`annotations.by_class` 统计匹配任务内有效、未取消的 Annotation 对象，包含相机成员；不展开视频轨迹帧，不包含未采纳 Prediction。数据集页面仅允许从当前可见关联项目中选择上下文；通用 Data Manager 筛选仍按任务实际数据集归属执行。

Data Manager schema 发布 `dataset.dataset_id` 条件，支持 UUID 的 `eq`、`ne` 和 `in`。类别任务跳转将该规则与 `annotation.class_name eq <类别>` 组合，不携带旧视图或任务选择。此条件是普通可编辑筛选，不是跨项目页面操作的固定范围。

每个 item 增加 `availability`：state（verified/missing/changed/preview_only/unavailable/unverified）、reason_code、observed_at、preview_available、脱敏 source_id/source_name/location_id 和 allowed_actions。动作值为 read_original/read_preview/manage_source/view_location；来源信息和管理动作只提供给当前获授权的来源管理者。旧托管 file_url 继续原合同，不能凭其存在推断已核验。

分类来自已保存的内容/位置绑定、接纳记录和**完整预检**差异，不同步探测或逐张读取原图。变化待确认和完整扫描未见保留原身份；预览与原图独立。verified 仅指已保存的核验观测，实际读取与编辑必须经过既有严格读取层；不能用该字段绕过任务权限。停用/注销来源、停用位置或禁用缓存策略会阻止预览动作。

## Scene 选项与样本范围

`GET /{id}/scene-options` 要求登录和 `datasets:read`，沿用数据集读取权限。`limit` 默认20、范围1–100，`offset` 非负，`search` 按 Scene 名称匹配、最多200字符；可传 `selected_id` 保留不在当前页或搜索结果中的选中项。响应为 `{items:[{id,display_id,name}], total, selected}`，selected 为同样的轻量对象或 null，不返回 source_metadata。此接口只读数据库，不遍历存储；旧 Scene 全量接口保持兼容，不用于新筛选器。

`items` 的可选 `scene_id` 与其他筛选在计数和分页之前组合，直接按 `DatasetItem.scene_id` 限定业务成员，包含同场景的摄像头、点云和 frame_index 为空的校准文件；不使用 Task 主帧或邻居范围。非法 UUID 为422；已删除、不存在或属于其他数据集的 Scene 均为404，`selected_id` 与视图写入采用相同归属验证。失效 Scene 条件不能被忽略后查询全部。

## 组合筛选与个人动态视图

`media_type` 为样本登记类别 image/video/point_cloud/other；多模态是数据集的容器类别。历史登记类别不自动重分类。`extension` 按逻辑文件名最后分量的后缀匹配，忽略大小写和可选的前导点，jpeg/jpg 同组；不代表已探测实际编码格式。`dimension_state=known` 要求 width/height 均为正；unknown 包含任一空值或非正值。宽高范围为1–10000000的整数（像素），含边界且仅匹配尺寸已知样本。最小值大于最大值、未知尺寸与范围并用、非法枚举或扩展名均返回422。所有条件先组合，再 count 和分页，不逐样本访问来源。

个人视图端点也要求登录及 `datasets:read`，它们保存浏览偏好，不授予修改数据集的权限：

| 方法与路径                           | 请求/结果                                               |
| ------------------------------------ | ------------------------------------------------------- |
| `GET /{id}/saved-views`              | 当前用户的 `items`，最多100项，按名称/ID排序            |
| `POST /{id}/saved-views`             | `{name, query, query_schema_version: 1}`，201返回新视图 |
| `PUT /{id}/saved-views/{view_id}`    | 完整替换名称和条件，200返回视图                         |
| `DELETE /{id}/saved-views/{view_id}` | 204，仅删除个人偏好                                     |

`query` 仅接受上述筛选字段，不保存 limit/offset、固定样本集合、宿主路径、权限结果或凭据。名称去除首尾空白后1–120字符；同一用户/数据集内同名或超过100项返回409。其他用户（包括超管）及父子不匹配均返回404。来源保存及执行查询时都重新检查当前授权；失效来源返回404，不能移除条件后查询更大范围。列表保留已存条件，允许用户删除或修复失效视图。

打开视图时查询当前数据；新接入的匹配样本自动出现。它不改变训练冻结版本的成员与副本；要冻结当前范围，向关联项目的[训练预检与创建](training-datasets.md#按数据集筛选定义冻结)传递具体的 dataset_filter 定义，随后核对并固定成员，不能只传动态视图ID。数据集或用户删除会级联清理视图。迁移回退会删除个人视图，回退前应导出需要保留的名称和查询条件。

## 图片质量运行

以下端点位于 `/datasets/{dataset_id}/quality-runs`。要求当前数据集管理权限（超级管理员或创建该数据集的项目管理员）；引用证据还要求当前来源可见。GET 要求 `datasets:read`，POST 同时要求 `datasets:write`；异步任务列表、详情、取消沿用相同资源授权。内部 API key 证明、存储路径和凭据不进入公开运行响应。

| 方法 | 相对路径                               | 合同                                                                                                                                                                 |
| ---- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET  | 空路径                                 | 运行历史，`limit` 1–100，`offset`；返回 items/total                                                                                                                  |
| POST | 空路径                                 | 尺寸配置，返回固定成员运行；每数据集一个活动运行，冲突409                                                                                                            |
| GET  | `/{run_id}`                            | 当前状态、固定成员数、进度、规则/配置、摘要、通用任务 ID                                                                                                             |
| POST | `/{run_id}/resume`                     | 恢复同一成员集合的未完成项；生成新 AsyncJob，保留已提交结果；完成或仍持有效租约返回409                                                                               |
| GET  | `/{run_id}/results`                    | `limit` 1–100、`offset`；finding=`decode_failed/dimensions_out_of_range/exact_duplicate/skipped`、review=`open/confirmed/dismissed`、sha256 可组合，先过滤计数再分页 |
| POST | `/{run_id}/results/{result_id}/review` | `{state,note,expected_revision}`，原因必填，过期修订409；父子不符404                                                                                                 |

创建请求默认 `{ "min_width":64, "min_height":64, "max_width":8192, "max_height":8192 }`；维度为1–100000整数，反向范围422。规则标识 `image-quality-1`。一次提交固定全部成员，最多10000，超过422；不接受任意文件路径、成员 ID 数组或动态筛选。运行历史和结果与 AsyncJob 保留策略独立。

结果固定 Item、Asset、可信 SHA/size。`bytes_verified` 仅表示本轮完整读取通过；无法解码仍可保留完整字节证明。`state=skipped` 不代表质量通过或损坏，原因区分未核验、媒体/编码不支持、读取/像素预算、不可读或内容变化。`findings` 保存解码/尺寸问题；`duplicate_count>1` 是本轮完整字节证明形成的组大小，不使用旧 MD5、ETag 或文件名。可用 `sha256` 分页读取同组。`stale` 标识当前 Item/Asset 绑定已变化或删除；不声称原图此刻仍可读。

复核只更新本条证据的决定与修订，并追加审计记录，不改检测事实、原图、任务或冻结版本。重复组与解码/尺寸异常可重叠；摘要分别提供异常样本数、重复组数及重复成员数，不能简单相加。取消使用 `POST /async-jobs/{id}/cancel`，以 fencing 阻止运行中的旧回调继续写入，不强杀 Worker。

### 固定范围批量复核

以下路径接在 `/{run_id}/review-batches` 后；当前管理权限、引用来源权限及 API key scopes 与质量运行相同。创建预览要求父检测已停止；仅预览创建者可首次确认，已执行历史对当前有权管理该数据集的账号可见，未确认预览仅创建者可见。

| 方法 | 相对路径              | 合同                                                                                 |
| ---- | --------------------- | ------------------------------------------------------------------------------------ |
| POST | `/preview`            | `{scope,result_ids?,query?,state,note}`；固定名单并返回影响，尚不创建任务            |
| GET  | 空路径                | 批次历史，`limit` 1–100、`offset`；items/total                                       |
| GET  | `/{batch_id}`         | 范围摘要、决定/原因、状态、计数、过期时间、任务 ID                                   |
| GET  | `/{batch_id}/members` | 固定证据和成员处理结果，`limit` 1–100、`offset`                                      |
| POST | `/{batch_id}/execute` | `{scope_digest,range_policy:"snapshot"}`；首次确认创建一次任务，重复确认返回同一批次 |
| POST | `/{batch_id}/resume`  | 只恢复 failed/cancelled 批次中的 pending 成员，创建新 AsyncJob                       |

`scope=selected` 要求 1–200 个不重复的本轮 result UUID，`query` 不再缩小明确勾选的名单；`scope=filtered` 禁止传 result_ids，使用与结果列表相同的 finding/review/sha256 条件，包含所有分页且上限 10000。空范围或超限422、成员不属于父运行404。预览有效10分钟，每人每轮最多20个有效未确认预览；创建时可清理本人过期未执行预览。过期、摘要不符或全部不可复核时执行409。

批次保存规则/配置，成员快照保存 Result/Item/Asset/SHA/size、检测证据与预期 review_revision；scope_digest 同时绑定成员、决定与原因。确认不重新执行筛选。pending 检测结果在预览中固定为 blocked，后续不会升级。历史绑定变化是提示而非复核阻止条件：仅复核原始证据，不读取源文件。

逐项锁定并比较完整证据及复核修订；变化记录 conflict，其他成员继续。成功决定、审计和 applied checkpoint 同事务提交；即使状态相同也追加这次复核。批次状态为 preview/pending/running/completed/partial/failed/cancelled；summary 提供 applied_count、conflict_count、blocked_count、processed_count、partial。含冲突或阻止的执行终态为 partial，通用 AsyncJob 为 completed 并附计数。取消使用通用任务接口；恢复不重放 applied/conflict/blocked。

通用任务类型为 `dataset_quality_review`，payload 中 run_id 始终指父质量运行，batch_id 指本批次；不含成员数组、凭据或内部授权证明。工作节点在每项写入前后重新授权，租约恢复及旧 task/generation fencing 保留已提交进度。任务清理 SET NULL 后重复确认仍返回账本，不重新派发。
