---
title: 数据集上传与扫描
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-22
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
