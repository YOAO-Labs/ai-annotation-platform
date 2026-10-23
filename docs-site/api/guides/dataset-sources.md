---
audience: [dev, project_admin, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# 目录来源与观测扫描

这些接口提供只读目录清单、可恢复扫描及两阶段批量登记，要求当前活跃超级管理员，或数据集创建者为本人的活跃项目管理员且拥有该存储位置的使用权。来源的 `mode` 固定为 `inventory`：扫描不创建 DatasetItem、Asset 或标注任务，不读取原图字节，扫描本身不生成内容摘要，也不把历史文件判为 missing。扫描完成后可运行内容预检并确认后台登记，也可逐条登记；扫描、预检和登记是独立步骤。

先用[存储位置 API](./storage-locations)登记受控根内位置，成功探测指定 worker 的 list 能力，再创建来源。所有路径均以 `/api/v1` 为前缀。

| 方法  | 路径                                 | 行为                                                              |
| ----- | ------------------------------------ | ----------------------------------------------------------------- |
| POST  | `/dataset-sources`                   | 为已有数据集创建只读观测来源，返回201                             |
| GET   | `/dataset-sources`                   | 按 dataset_id 可选过滤，limit 默认100、最多200，offset 分页       |
| GET   | `/dataset-sources/{source_id}`       | 查看来源配置，不返回绝对根或内部节点指纹                          |
| PATCH | `/dataset-sources/{source_id}`       | expected_revision + name/enabled；活动扫描及登记须先取消          |
| POST  | `/dataset-sources/{source_id}/scans` | 启动手动扫描，返回202；同来源已有活动运行时复用                   |
| GET   | `/dataset-sources/{source_id}/scans` | 分页查看独立扫描历史                                              |
| GET   | `/dataset-scans/{run_id}`            | 查看状态、阶段、完整性、累计数和当前 async_job_id                 |
| GET   | `/dataset-scans/{run_id}/entries`    | limit/offset 分页读取观测、verified_content 与 registered_item_id |
| POST  | `/dataset-scans/{run_id}/resume`     | 恢复未完成运行，保留目录检查点，返回202和新任务ID                 |

来源、扫描、登记与明细的直接 ID 访问和列表/total 使用同一管理范围；权限不足返回404，员工不能枚举来源。读接口要求 `datasets:read`，写接口要求 `datasets:write`。后台任务按真实 Run→Source 关联授权，包括恢复前的历史尝试，不以任务提交者代替当前来源权限；缺少读取 scope 的 Key 不会在任务列表取得这些记录。

撤回位置使用权后，项目管理员不能继续浏览、扫描、登记或管理预览，排队与在途执行在读取前后复查授权。已分配任务仍按任务权限读图；如需停止全部业务读取，应停用来源或位置。目录数据集的旧修改、删除、上传和关联入口同样检查数据集创建者；混合上传、ZIP、桶扫描、连接器复制导入和分块建任务保留初始授权，在 I/O 后和提交前复查。撤权后保留已提交部分，后续未提交部分停止，公开作业 payload 不返回内部授权证明。

## 创建来源

```json
{
  "dataset_id": "已有数据集 UUID",
  "location_id": "已登记位置 UUID",
  "name": "车辆目录观测",
  "relative_path": "traffic/vehicle_detection",
  "node_id": "celery@scan-host",
  "recursive": true,
  "include_globs": ["*.jpg", "*.png"],
  "exclude_globs": ["excluded", "temp/*"]
}
```

`relative_path` 相对存储位置，默认空字符串；不能使用绝对路径、父级跳转、链接或跨子挂载。`node_id` 必须是完整 Celery hostname。过滤使用区分大小写的 fnmatch，相对来源根匹配，include 默认 `["*"]`，exclude 默认 `[]`；排除的目录不递归，未匹配文件、链接和特殊类型记录为 skipped，不递归链接。源路径、节点、递归和过滤配置创建后不可重定向，需要新建来源；名称/启停用 expected_revision 防止覆盖并发修改。

## 完整性与恢复

Run 固定来源/位置修订、执行节点、根身份和预算。每个目录完整枚举后，观测、子目录队列及完成标记在同一事务提交；中断最多重列当前未提交目录。恢复及完成前复核已捕获目录的身份和摘要，变化返回 `directory_changed`，需要重新启动完整扫描。修改位置配置后旧 Run 不得恢复。

单目录最多5000原始条目，循环预算2秒；单次运行最多50000条观测、5000目录。相对路径最多1000字符、32层，支持中文和空格。过滤项也占预算；目录过大、路径不可读、节点不可用、超时及取消都保留已提交清单并保持 `complete=false`。单次 worker 尝试软限240秒、硬限300秒，内核阻塞调用仍取决于运行平台。

Run 的 `completed` 和 `complete=true` 只表示约定范围枚举与复核完成，不是全树某一时刻的原子快照，不证明内容一致。文件大小、inode 和时间只是观测；未单独核验时 `content_verified=false`。通过[内容核验接口](./verified-content)成功核验后，条目保存独立 verified_content 并返回 true，不表示原图持续在线。任何结果均不自动改变历史样本或 missing 状态。

取消复用 `POST /async-jobs/{async_job_id}/cancel`，已提交观测保留。失去60秒租约后可调用 resume 创建新 job/task_id；仍有有效租约时返回409。旧消息、旧 worker 和旧失败信号不能写入新尝试。发布失败返回的 Run 标记 `failed / dispatch_unavailable`，可显式恢复；API在提交后、发布前中断所留的 pending 同样可恢复。

任务中心显示“已扫描数量 / 总量未知”，不展示伪造百分比。终态任务可能被30天保留策略删除，ScanRun 和观测继续保留，async_job_id 会变为 null；失败/取消运行仍可在当前配置和节点验证有效时恢复。数据集删除后来源失效，账本保留，外部原图不受影响。

## 后台预检与批量登记

以下写接口均要求 `datasets:write`，读接口要求 `datasets:read`。JWT 沿用上述当前角色、创建者和位置权限；API Key 的初始凭据证明贯穿 I/O、排队和逐项提交，轮换、撤销或降权后旧尝试不能继续写入。

| 方法 | 路径                                                                   | 行为                                                         |
| ---- | ---------------------------------------------------------------------- | ------------------------------------------------------------ |
| GET  | `/dataset-scans/{scan_id}/registration`                                | 查看持久登记账本，尚未开始时返回 null                        |
| POST | `/dataset-scans/{scan_id}/registration`                                | 完整扫描创建预检账本；重复调用返回同一账本，返回202          |
| POST | `/dataset-registrations/{registration_id}/confirm`                     | 完整预检后确认登记；重新核验 API 当前节点可读性，返回202     |
| POST | `/dataset-registrations/{registration_id}/resume`                      | 继续取消或中断的阶段，创建新执行尝试，返回202                |
| POST | `/dataset-registrations/{registration_id}/entries/{path_digest}/retry` | 显式重试异常条目；未核验内容允许修复后重读，已有回执保持摘要 |
| GET  | `/dataset-registrations/{registration_id}/entries`                     | state 可选过滤；limit 默认100、最多200，offset 分页          |

账本的 `phase` 为 `preflight/registering`，`status` 为 `pending/running/completed/failed/cancelled`。counts 分列索引数及字节、待核验、已核验待登记、已登记、相同路径跳过、异常、原已登记路径、预计新增及扫描排除；原图复制和预览占用独立计量。预检读取完整图片并保存不可变核验回执，但不创建业务样本。确认后每项重新读取固定摘要，在同一事务内提交 Item/Asset/Reference、关联任务与该项登记进度，外部原图不写入对象存储。

单次 worker 分片最多25项或90秒，软/硬限150/180秒。60秒租约逐项续期；beat 每30秒恢复到期或待派发分片，部署须运行更新后的 beat。发布失败保持 `pending/dispatch_unavailable`；可等待自动恢复或显式 resume。旧 task ID、代次或 token 不能修改新分片。取消复用 `/async-jobs/{async_job_id}/cancel`，保留已提交部分。任务历史清理不删除登记账本。

根不可达、权限或配置变化中断整个阶段；损坏或缺失的单文件记异常并继续。数据集锁繁忙和快照容量不足按暂态退避，不能把正常文件记为损坏。预检完成表示所有候选已处理，仍可包含异常；确认成功不保证最终登记数不变。若确认期间文件变化，异常项会从 ready 移出；重新读取账本后再确认。新的预检尝试在确认 I/O 期间完成时，旧确认返回409，必须刷新后重新确认。

## 手动增量差异

`GET /dataset-registrations/{registration_id}/changes` 要求上述来源管理权限与 `datasets:read`。`kind` 可选 `added/unchanged/changed/not_seen/excluded/unreadable`，`limit` 默认20、最多200，`offset` 默认0。返回 `complete`、预检开始时间 `observed_at`、六类 `counts`、筛选后 `total` 和明细 `items`。每项提供路径摘要、相对路径、类型、原因、可空 `before_item_id/before_sha256/candidate_sha256` 和当前登记状态/错误码，不返回内部根身份或绝对路径。

新账本提供 `comparison_available=true`；`comparison_complete` 仅在完整预检完成后为 true。取消、中断或修复重试期间返回 `complete=false`、空计数和空列表，不发布未见候选。基线在预检开始时从已接纳的来源路径映射固定；先前仅扫描而未登记的路径仍为新增。单来源基线最多50000条，与本轮候选的并集最多100000条，超出返回409 `comparison_budget_exceeded`。

核验读取完整字节后比较 SHA、资格锚点和文件观测。摘要不同、资格改变或相同摘要但观测改变均列 `changed`，登记明细记 `reference_content_changed`，不重绑旧样本。已观察但排除的旧路径列 `excluded`；本轮没有枚举到的旧路径列 `not_seen`，包括没有遍历的后代，不能据此推断删除、标记 missing 或删除业务历史。扫描完成不构成原子目录快照；S3 历史版本可读不证明当前键仍存在。

确认接口可带 JSON `{"scope":"additions_only"}`：须有完整差异且存在可登记新增，否则409 `comparison_incomplete/no_added_images`。选择范围固定为 `registration_scope`；重复同范围确认幂等，改变范围返回409 `registration_scope_changed`。本轮相同项标为 unchanged，不再登记，只有新增 ready 项进入正式登记；异常变化或未见项不接纳。正式登记仍逐项复读固定摘要和复查权限，确认后的外部覆盖会记异常。差异分类与候选摘要保留为预检证据，当前执行结果见 `registration_state/error_code`。已确认仅新增后，非新增错误不得通过 retry 扩大选择范围；变化项返回409 `source_change_requires_rescan`，需要新扫描及后续裁决能力。

无请求体或 `{"scope":"all_safe"}` 保留既有确认方式；新预检中已变化项仍被阻断，旧账本 `comparison_available=false` 不支持仅新增。异步任务清理不清除此差异证据。周期计划、接受变化形成新修订、移动重定位及删除裁决未开放。

## 逐条登记原地图片

`POST /dataset-scans/{run_id}/entries/{path_digest}/register` 要求上述当前来源管理权限及 `datasets:write`，返回200：`{item_id, created, sha256}`。Run 必须完整，条目为匹配的已观测文件；API 当前节点须有有效 read 探测。登记会重新核验字节与图片，创建不可变 Reference、verified Asset 和 DatasetItem，复用已有项目关联创建 Task，不上传原图或创建 S3 副本。只接受普通图片数据集及普通图片项目。

同 Source/path 幂等返回 `created=false`；相同摘要的其他路径不会合并样本。来源或内容变化拒绝，繁忙的数据集返回409 `dataset_busy`，可重试。登记在最终锁内复查原始 API Key 和账号权限，不会把轮换后的 Key 认作原请求。接口是逐条事务；中断保留已提交条目，entries 的 `registered_item_id` 可用于恢复。

`GET /dataset-items/{item_id}/content?sha256=...` 仅当前有来源管理权限的管理员及 `datasets:read` 可预览已登记图片；响应、预算与失败语义沿用[严格内容读取](./verified-content)。员工通过 Task 内容接口读取分配任务，不开放此管理入口。

## 原图与缓存策略

创建来源接受 `ownership_mode: reference | managed_copy`（默认 reference）与 `cache_policy: none | preview`（默认 preview）。旧来源迁移为 reference/none。位置的 none 上限优先；登记响应的 `ownership_mode/cache_policy/source_revision` 是本次固定计划，不包含内部凭据。策略独立于路径配置，所有权创建后不可修改；PATCH 缓存策略需 expected_revision，活动尝试须先取消。策略或生命周期变更后，旧扫描/登记历史保留，需要新扫描与预检，旧引用摘要不变。

预检 `preview_estimated_bytes/preview_estimated_count/preview_estimate_error_count` 区分实测估算与未知项；`original_estimated_bytes` 是候选原图复制估算。正式登记的 `original_copy_bytes` 仅计本次新增复制对象，`preview_retained_bytes` 计本轮路径当前持有预览，`preview_failed_count` 独立于原图登记结果。单项结果提供可空 `preview_estimated_bytes` 与 `preview_error`。托管内容去重会影响实际复制量，不能将扫描体量视为新增托管容量。

扫描条目的 `preview_state/preview_bytes` 描述该来源的派生账本。`GET /dataset-sources/{source_id}/entries/{path_digest}/preview` 仅向当前有权管理来源的账号提供完整鉴权 WebP 快照，响应 no-store 并复核自身 SHA；不返回可脱离实时授权的预签名地址。该端点不要求外部原图在线，缓存不可用于标注。none、停用、注销及撤权均拒绝新读。已登记路径再次 register 可重试失败/缺失缓存，不改变 Item 身份。

## 来源生命周期

先 `GET /dataset-sources/{source_id}/lifecycle-impact`，返回当前修订、proof、关联样本/项目/任务/标注记录数、活动尝试数、本来源预览数/字节及观测时间。计数是影响预览，任务数不表示所有任务都会不可用：托管复制原件继续可读。

再 `POST /dataset-sources/{source_id}/lifecycle`：

```json
{ "action": "pause", "expected_revision": 1, "impact_proof": "从影响预览取得的proof" }
```

`action` 支持 pause、enable、unregister；修订或影响改变返回409 `source_impact_changed`，需刷新预览。停用/注销在同事务取消活动 Run/Job 并撤销租约，保留已提交样本、任务、标注和回执。停用可以恢复，注销不可恢复。引用停止新读与后续标注；已经核验并持有共享来源锁的标注事务先完成，生命周期操作再返回。托管复制原件独立于来源。全部操作都不修改外部文件；PATCH 的 enabled 实际变化返回409 `lifecycle_impact_required`，须走影响确认端点。

## 外部 S3 来源

相同接口接受有 connection_id 的外部 s3 位置，复用连接器的 owner/global 权限与加密凭据；平台固定 S3 位置不能用作外部来源。relative_path 是相对 base_prefix 的精确目录前缀，按需附加末尾斜线，不裁剪其他字符。扫描条目的 relative_path 与内部完整 bucket/key 分离；后续读取使用完整定位和不可变 SHA，不通过旧复制导入的路径规范化。

S3 每页最多200条，每次扫描最多50000条原始观测、1000页。页内容和下一 ContinuationToken 在同一租约事务保存；取消恢复接着读取下一页，缺失或循环游标返回 s3_invalid_cursor 且 complete=false。多页 LIST 不承诺全局原子快照，不依据不完整扫描判 missing。非递归与 include/exclude 规则按来源内精确键匹配。

首次核验将 LIST 观测与 HEAD 对照，随后以原始 ETag 条件 GET，固定可用 VersionId，核验200完整响应、长度、返回身份和实读 SHA。LIST 与 HTTP 日期统一 UTC 秒精度，时间不是内容证明。每次实际读取验证错误 IfMatch 被拒绝；条件被忽略返回 s3_conditions_not_supported，不能由仅可列举的根探测绕过。无不可变版本时不回退最新字节；有固定版本时不因最新版本改变而改写旧身份。来源整体故障停止尝试并保留未处理候选，单对象不可读独立记录。

凭据不进入 Run 快照、页检查点或作业参数。配置/密钥修订、账号/API Key、白名单及来源状态在 I/O 前后复查；连接配置被引用后不能重定向。

## SFTP 来源

相同接口接受具备部署资格的 sftp 连接器位置。部署方须在所有参与节点的 `STORAGE_SFTP_ROOTS` 中配置精确 host/port/username 和完整主机公钥，并核验账号的整个可读命名空间已由服务器只读 jail 隔离；连接 base_path 必须为 `/`。Source 的 relative_path 是该命名空间下的相对目录，仅用于筛选，不构成另一道安全根。未获资格返回 `sftp_root_not_qualified`，错误主机公钥返回 `sftp_host_key_mismatch`。

SFTP 复用逐目录检查点、取消恢复及末次目录清单核对，每目录最多5000条，扫描总预算仍为50000条；没有跨连接的持久目录游标。普通文件以路径、服务器观测和完整 SHA 固定身份，不宣称 inode、不可变版本或条件读取能力。符号链接和特殊文件不接入；已观测到的目录变化使扫描不完整。根离线或权限失效停止当前尝试，保留候选与已提交部分。

资格锚点写入扫描历史和内容绑定。调整或撤回资格须先禁用位置、递增修订，再协调 API/worker 部署；新资格不能自动重绑历史引用。部署步骤见[只读 SFTP 引用](/ops/deploy/docker-compose#只读-sftp-引用)。
