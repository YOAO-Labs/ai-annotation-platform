---
title: 数据集上传与扫描
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# 数据集上传与扫描

端点均位于 `/api/v1/datasets`，沿用已有登录和数据集操作权限。文件位置属于服务端契约，客户端应使用返回的上传地址、item ID 与文件 URL，不按数据集名称拼接桶名或对象键。

## 直传

1. `POST /{id}/items/upload-init`，请求 `{ "file_name": "a.jpg", "content_type": "image/jpeg" }`，返回 `item_id`、`upload_url`、`expires_in`。
2. 向 upload_url 执行 PUT。
3. `POST /{id}/items/upload-complete/{item_id}`，完成后返回 `status`、`item_id` 和 `linked_tasks`。

每次 init 分配独立物理对象键；同一数据集内重名逻辑路径追加序号，不覆盖旧内容。complete 在线程中按 8 MiB 分块读取实际字节，校验 HEAD/GET 身份与大小，记录 SHA-256；ETag 不作为内容摘要。上限为 5 GiB，超过返回 `413`；读取身份或已核验内容变化返回 `409`。传入不安全相对路径返回 `422`。

同数据集内已核验的相同内容返回 `409`，detail 包含 `duplicate_of`；只清理本次唯一上传对象和占位 Item。旧 hash 只作为候选，实际内容一致后才认定重复。旧候选无法读取时不会据此丢弃新文件。

## ZIP 与连接器

`POST /{id}/items/upload-zip` 保留相对目录，逻辑路径冲突时追加序号；物理键采用独立上传身份。内容重复计入 deduped，其他响应与预算保持原契约。连接器复制导入沿用 `POST /{id}/import-from-connection` 和 AsyncJob，仍将原件复制到托管桶。

## 扫描

`POST /{id}/items/scan` 同时扫描固定的 storage_scan_prefix 和该数据集的稳定托管根。升级前数据集保留升级时的名称根，新建根为 `__aap_managed_datasets__/<dataset UUID>/`；改名不改扫描根。托管根下 `_objects/` 是平台写入保留区；手工扫描目录放在该保留区以外。扫描遇到重名时保存独立逻辑路径，Scene 与导出使用该路径，真实对象键保持原样。

外部写入流程可在新根下直接写 `sub/image.jpg`，无需伪造平台上传 UUID；扫描保留真实键并计算正确逻辑相对目录。已登记 file_path 的重复扫描跳过；扫描 ETag 仅作观测，不自动写成 content_hash。此接口补登记已有托管对象，不提供服务器目录原地引用。

内容及其物理位置映射和核验状态仍是内部数据。[受控存储位置](./storage-locations)另行提供目录登记、浏览与节点探测，不改变数据集原图读取路径。既有 Item/Task 的 file_path 与冻结版本输入保持原样。运维回填见[存储连接架构](/dev/concepts/storage-connections#内容身份与兼容回填)。

## 工作台分页与上下文

以下 GET 均要求登录及 `datasets:read`。`GET /datasets` 新增 `location_id`，仅可见该位置的管理员可用；使用现有来源管理授权，`total` 按唯一数据集计数。未获授权的位置返回404。原 search/data_type/has_scenes 与分页可组合。

- `GET /{id}/items`：`limit` 1–200、`offset`，可组合 `search`（文件名，最多200字符）、`source_id`、`availability`、`media_type`、`extension`、`dimension_state` 与 `min_width/max_width/min_height/max_height`。来源 ID 必须属于该数据集且当前账号可管理，否则404。
- `GET /{id}/items/{item_id}`：直接读取单个样本及同一可用性合同；父子不匹配返回404。
- `GET /{id}/items/{item_id}/preview`：管理员的鉴权图片流，复用来源预览读取的绑定、策略和实时授权检查；`Cache-Control: no-store`，不支持 Range。不会读取外部原图；源文件缺失时仍可交付获授权的独立派生。
- `GET /{id}/workbench`：可用性分类计数、当前可管理来源和可见关联项目。项目统计仅包含该数据集的任务；`can_manage` 复用项目权限。旧 `/projects` 也按项目可见范围裁剪。
- `GET /{id}/training-versions`：分页列出有管理权限项目中的关联版本，返回 id/project_id/project_name/name/status/created_at，不另建版本详情。新引用快照或显式数据集选择保存 dataset_id；旧托管快照通过仍存在的源任务关联，任务删除后的旧快照不能推导未保存的数据集血缘。

每个 item 增加 `availability`：state（verified/missing/changed/preview_only/unavailable/unverified）、reason_code、observed_at、preview_available、脱敏 source_id/source_name/location_id 和 allowed_actions。动作值为 read_original/read_preview/manage_source/view_location；来源信息和管理动作只提供给当前获授权的来源管理者。旧托管 file_url 继续原合同，不能凭其存在推断已核验。

分类来自已保存的内容/位置绑定、接纳记录和**完整预检**差异，不同步探测或逐张读取原图。变化待确认和完整扫描未见保留原身份；预览与原图独立。verified 仅指已保存的核验观测，实际读取与编辑必须经过既有严格读取层；不能用该字段绕过任务权限。停用/注销来源、停用位置或禁用缓存策略会阻止预览动作。

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

打开视图时查询当前数据；新接入的匹配样本自动出现。它不改变训练冻结版本的成员与副本。数据集或用户删除会级联清理视图。迁移回退会删除个人视图，回退前应导出需要保留的名称和查询条件。

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
