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

- `GET /{id}/items`：`limit` 1–200、`offset`，可组合 `search`（文件名）、`source_id`、`availability`。来源 ID 必须属于该数据集且当前账号可管理，否则404。
- `GET /{id}/items/{item_id}`：直接读取单个样本及同一可用性合同；父子不匹配返回404。
- `GET /{id}/items/{item_id}/preview`：管理员的鉴权图片流，复用来源预览读取的绑定、策略和实时授权检查；`Cache-Control: no-store`，不支持 Range。不会读取外部原图；源文件缺失时仍可交付获授权的独立派生。
- `GET /{id}/workbench`：可用性分类计数、当前可管理来源和可见关联项目。项目统计仅包含该数据集的任务；`can_manage` 复用项目权限。旧 `/projects` 也按项目可见范围裁剪。
- `GET /{id}/training-versions`：分页列出有管理权限项目中的关联版本，返回 id/project_id/project_name/name/status/created_at，不另建版本详情。新引用快照或显式数据集选择保存 dataset_id；旧托管快照通过仍存在的源任务关联，任务删除后的旧快照不能推导未保存的数据集血缘。

每个 item 增加 `availability`：state（verified/missing/changed/preview_only/unavailable/unverified）、reason_code、observed_at、preview_available、脱敏 source_id/source_name/location_id 和 allowed_actions。动作值为 read_original/read_preview/manage_source/view_location；来源信息和管理动作只提供给当前获授权的来源管理者。旧托管 file_url 继续原合同，不能凭其存在推断已核验。

分类来自已保存的内容/位置绑定、接纳记录和**完整预检**差异，不同步探测或逐张读取原图。变化待确认和完整扫描未见保留原身份；预览与原图独立。verified 仅指已保存的核验观测，实际读取与编辑必须经过既有严格读取层；不能用该字段绕过任务权限。停用/注销来源、停用位置或禁用缓存策略会阻止预览动作。
