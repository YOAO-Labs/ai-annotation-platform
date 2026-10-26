---
audience: [dev, project_admin, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# 目录来源与观测扫描

这些接口提供只读目录清单、可恢复扫描及两阶段批量登记，要求当前活跃超级管理员，或数据集创建者为本人的活跃项目管理员且拥有该存储位置的使用权。来源的 `mode` 固定为 `inventory`：扫描不创建 DatasetItem、Asset 或标注任务，不读取原图字节，扫描本身不生成内容摘要，也不把历史文件判为 missing。扫描完成后可运行内容预检并确认后台登记，也可逐条登记；扫描、预检和登记是独立步骤。

先用[存储位置 API](./storage-locations)登记受控根内位置，成功探测指定 worker 的 list 能力，再创建来源。所有路径均以 `/api/v1` 为前缀。

| 方法  | 路径                                               | 行为                                                                    |
| ----- | -------------------------------------------------- | ----------------------------------------------------------------------- |
| POST  | `/dataset-sources`                                 | 为已有数据集创建只读观测来源，返回201                                   |
| GET   | `/dataset-sources`                                 | 按 dataset_id 可选过滤，limit 默认100、最多200，offset 分页             |
| GET   | `/dataset-sources/{source_id}`                     | 查看来源配置，不返回绝对根或内部节点指纹                                |
| PATCH | `/dataset-sources/{source_id}`                     | expected_revision + name/enabled；活动扫描及登记须先取消                |
| POST  | `/dataset-sources/{source_id}/scans`               | 启动手动扫描，返回202；同来源已有活动运行时复用                         |
| GET   | `/dataset-sources/{source_id}/scans`               | 分页查看独立扫描历史；complete 可选过滤完整性，active_only 只取活动扫描 |
| GET   | `/dataset-sources/{source_id}/active-registration` | 返回当前来源的活动预检/登记或 null，不限于最新扫描                      |
| GET   | `/dataset-scans/{run_id}`                          | 查看状态、阶段、完整性、累计数和当前 async_job_id                       |
| GET   | `/dataset-scans/{run_id}/entries`                  | limit/offset 分页读取观测、verified_content 与 registered_item_id       |
| POST  | `/dataset-scans/{run_id}/resume`                   | 恢复未完成运行，保留目录检查点，返回202和新任务ID                       |

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

`relative_path` 相对存储位置，默认空字符串；不能使用绝对路径、父级跳转、链接或跨子挂载。`node_id` 必须是完整 Celery hostname。过滤使用区分大小写的 fnmatch，相对来源根匹配，include 默认 `["*"]`，exclude 默认 `[]`；排除的目录不递归，未匹配文件、链接和特殊类型记录为 skipped，不递归链接。源路径和节点创建后不可重定向，需要新建来源；递归和过滤仅通过下文影响确认接口调整；名称/启停用 expected_revision 防止覆盖并发修改。

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

无请求体或 `{"scope":"all_safe"}` 保留既有确认方式；新预检中已变化项仍被阻断，旧账本 `comparison_available=false` 不支持仅新增。异步任务清理不清除此差异证据。不同摘要内容可逐项接受为新修订，同摘要观测变化可逐项重新核验；移动可按下文明确重定位；未见项不自动删除。

## 逐条登记原地图片

`POST /dataset-scans/{run_id}/entries/{path_digest}/register` 要求上述当前来源管理权限及 `datasets:write`，返回200：`{item_id, created, sha256}`。Run 必须完整，条目为匹配的已观测文件；API 当前节点须有有效 read 探测。登记会重新核验字节与图片，创建不可变 Reference、verified Asset 和 DatasetItem，复用已有项目关联创建 Task，不上传原图或创建 S3 副本。只接受普通图片数据集及普通图片项目。

同 Source/path 幂等返回 `created=false`；相同摘要的其他路径不会合并样本。来源或内容变化拒绝，繁忙的数据集返回409 `dataset_busy`，可重试。登记在最终锁内复查原始 API Key 和账号权限，不会把轮换后的 Key 认作原请求。接口是逐条事务；中断保留已提交条目，entries 的 `registered_item_id` 可用于恢复。

`GET /dataset-items/{item_id}/content?sha256=...` 仅当前有来源管理权限的管理员及 `datasets:read` 可预览已登记图片；响应、预算与失败语义沿用[严格内容读取](./verified-content)。员工通过 Task 内容接口读取分配任务，不开放此管理入口。

## 原图与缓存策略

创建来源接受 `ownership_mode: reference | managed_copy`（默认 reference）与 `cache_policy: none | preview`（默认 preview）。旧来源迁移为 reference/none。位置的 none 上限优先；登记响应的 `ownership_mode/cache_policy/source_revision` 是本次固定计划，不包含内部凭据。策略独立于路径配置，所有权创建后不可修改；PATCH 缓存策略需 expected_revision，活动尝试须先取消。策略或生命周期变更后，旧扫描/登记历史保留，需要新扫描与预检，旧引用摘要不变。

预检 `preview_estimated_bytes/preview_estimated_count/preview_estimate_error_count` 区分实测估算与未知项；`original_estimated_bytes` 是候选原图复制估算。正式登记的 `original_copy_bytes` 仅计本次新增复制对象，`preview_retained_bytes` 计本轮路径当前持有预览，`preview_failed_count` 独立于原图登记结果。单项结果提供可空 `preview_estimated_bytes` 与 `preview_error`。托管内容去重会影响实际复制量，不能将扫描体量视为新增托管容量。

扫描条目的 `preview_state/preview_bytes` 描述该来源的派生账本。`GET /dataset-sources/{source_id}/entries/{path_digest}/preview` 仅向当前有权管理来源的账号提供完整鉴权 WebP 快照，响应 no-store 并复核自身 SHA；不返回可脱离实时授权的预签名地址。该端点不要求外部原图在线，缓存不可用于标注。none、停用、注销及撤权均拒绝新读。已登记路径再次 register 可重试失败/缺失缓存，不改变 Item 身份。

## 外部原件保护责任

`PUT /api/v1/dataset-sources/{source_id}/external-protection` 沿既有来源管理权限和 `datasets:write` scope 登记责任。来源详情、列表及存储位置来源摘要中的 `external_protection` 为该声明；未登记为 `null`，首次提交 `expected_revision=0`。

```json
{
  "expected_revision": 0,
  "responsible_party": "数据运维组",
  "recovery_method": "external_backup",
  "recovery_reference": "OPS:restore-source"
}
```

`responsible_party` 为1至100字符的团队／人员名称；`recovery_method` 可为 `external_backup / reacquire / unrecoverable / unknown`。可空的 `recovery_reference` 为最长100字符的内部编号，仅用字母、数字和 `. _ : -`，不接受路径、URL或凭据。服务器返回声明的独立 `revision`、`recorded_at` 和 `recorded_by_id`；客户端不得指定操作主体或时间。后续提交使用读到的声明修订，冲突返回409及 `external_protection_revision_changed`，不会覆盖他人新记录。

更新不触发源I/O，不改变来源 `config_revision / generation / policy_revision`，不取消活动扫描或登记。离线、停用、注销、归档来源仍按现有授权维护；数据集删除后的孤立来源仅超级管理员可见。声明不授予读取／清理权限，也不证明备份存在、恢复已完成或原件可用。

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

## 周期扫描与预检

| 方法 | 路径                                                  | 行为                             |
| ---- | ----------------------------------------------------- | -------------------------------- |
| GET  | `/dataset-sources/{source_id}/sync-schedule`          | 查看计划；尚未配置返回 null      |
| PUT  | `/dataset-sources/{source_id}/sync-schedule`          | 配置间隔或暂停，返回当前计划     |
| POST | `/dataset-sources/{source_id}/sync-schedule/continue` | 核对后跳过本轮差异，安排下一周期 |

写操作要求当前来源管理权与 `datasets:write`；启用还要求 `datasets:read`。PUT 请求 `{"expected_revision":0,"enabled":true,"interval_minutes":60}`；首次修订为0，后续传响应的 revision。间隔15–10080分钟，默认60；支持 `timezone`（默认 UTC，校验 IANA 名称）显示时间；间隔按实际分钟计算，不支持 cron、本地日历定时或错过周期补跑。每分钟检查，繁忙或离线会延后。暂停传 enabled=false；仅停止后续协调，已经创建的扫描/预检/人工登记独立继续，取消仍走任务接口。

计划按启用人的实时权限运行，固定账号和可选 API Key 的原证明，不保存 JWT 或 Key 明文。响应包含 revision、enabled、interval_minutes、actor_id、state、scan_id、registration_id、next_run_at、last_checked_at、last_started_at、last_error；不返回内部证明、根身份或派发令牌。state 为 idle/scanning/preflighting/awaiting_review/registering/blocked。修订或核对对象过期返回409 `schedule_revision_changed/schedule_review_changed`；活动扫描或预检期间更换执行账号或凭据返回409 `schedule_cycle_authority_pinned`，应先完成、取消该轮或处理阻断。

后台在来源指定 worker 重新探测自身可读性，再扫描和完整预检；不冒充 API 节点，默认不自动登记样本。`new_file_policy` 默认为 `manual`，可显式设置 `pure_additions`：仅完整、无错误、只含 added/unchanged 且有新增的轮次由真实来源 worker 复读并确认 additions_only。任何其他类别整轮等待人工核对。扫描开始时固定策略，后续启用自动策略不接管人工轮次；关闭自动策略可阻止尚未确认的自动接纳。人工确认仍由真实 API 节点复读。只包含相同项的完整预检直接安排下一周期；任何其他差异等待核对。可通过原确认接口登记新增，完成后继续周期；或调用 continue，传 `{"expected_revision":当前修订,"scan_id":"当前扫描UUID"}`，显式跳过本轮，不接纳任何样本，也不修改差异证据。计划不接管手动活动扫描/预检；人工正式登记仅观察，不刷新其执行身份。

权限撤销、Key 轮换/到期/降权、来源或位置配置变化、根身份或资格变化会阻断并停用计划，核对后显式重新启用。暂态探测或派发错误保留当前指针并按间隔重试。过期协调消息被拒绝；失租的本计划扫描/预检及原授权相符的自动正式登记可恢复，已取消任务不自动恢复。位置或来源自身停用仍按生命周期合同撤销在途业务任务；这与仅暂停周期不同。

## 接受内容为新修订

`POST /dataset-registrations/{registration_id}/changes/{path_digest}/accept-revision` 逐项接纳同路径、不同 SHA 的候选。要求当前来源管理权限和 `datasets:write`；请求示例：

```json
{
  "expected_generation": 1,
  "before_revision_id": "旧修订UUID",
  "candidate_sha256": "候选的64位小写SHA256"
}
```

只能接纳 `phase=preflight/status=completed/comparison_complete=true` 中的 `changed/content_changed`。差异响应增加 before_revision_id、accepted_revision_id、accepted_item_id 和 can_accept_revision。后者仅表示候选类型与阶段允许；实际提交仍复核权限、配置、无活动扫描/登记、前驱及严格内容读取。开始仅新增登记后须重新扫描和预检，普通单项重试不能接纳内容变化。

成功返回 revision_id、previous_revision_id、revision_number、item_id、sha256、created。新建独立 Item/Asset，以及关联普通图片项目中的空白 Task；旧 Item/Asset/Task/Annotation 不变。托管复制即使摘要已存在也新建业务样本。重复同一确认返回原结果、created=false，不把已前进的路径头退回。历史扫描重放仍须严格读取，只返回该观测原来接纳的样本，不推进当前绑定或重发当前预览。旧差异分类和原异常数保持冻结，接纳结果单列。周期计划仍等待明确核对/继续。

409 `revision_preflight_required`、`revision_selection_changed` 或 `source_revision_changed` 要求重新扫描和预检；`source_busy` 表示来源有在途扫描或登记。已接纳的新样本被删除后返回 `accepted_revision_item_deleted`，不会复活旧候选。文件再次变化、暂停/撤权或资格变化沿用严格读取错误。数据库或对象存储提交结果未知时，先重放原确认查询结果，避免推断未提交。

`GET /dataset-sources/{source_id}/revisions?path_digest=摘要&limit=20&offset=0` 按路径和修订号倒序分页，要求来源管理权限与 `datasets:read`。返回接纳人/时间、观测 scan_run_id、确认 registration_id、前驱及 item_available；删除业务对象后保留原 Item 身份。历史回填的接纳人/时间为 null。历史原地引用保持原内容身份；显式同内容重新核验可追加读取观测，旧目录字节覆盖后可能不可读，不能以新图或管理预览替代。

## 同内容重新核验

`POST /dataset-registrations/{registration_id}/changes/{path_digest}/revalidate` 使用与内容接纳相同的请求和权限，只接受完整预检的 `changed/observation_changed`，要求前后 SHA 相同、前驱仍为当前头。差异行的 `can_revalidate` 表示可发起该操作；来源资格变化仍被阻断。

重新完整读取后，追加同 Item/Asset 的接纳修订并推进当前路径头，`created=false`；不新建样本、Task 或原图副本，不改原 Reference、标注和样本元数据。托管复制继承原媒体所有权。原地任务只使用同来源、同 Item/Asset 的最新已明确接纳观测严格读取；失败不会自动尝试其他凭据或同摘要样本；仅可沿管理员明确发布的[备用来源关联](./datasets#引用样本备用来源)读取同一准确内容。读取期间凭据切换会拒绝交付。

重复确认返回原记录；过期前驱或候选变化返回409。差异、原错误和周期核对门禁保持历史语义。修订历史的 `acceptance_kind` 区分 `initial`、`content_revision` 与 `observation_revalidation`。已打开且读图失败的任务需要点击重新读取或重开页面。此操作不恢复外部已丢失的字节，路径移动使用下文独立重定位入口，资格变更仍被阻断。

## 移动重定位

对完整预检中的 `not_seen` 旧项，`GET /dataset-registrations/{registration_id}/changes/{旧path_digest}/move-candidates?limit=20&offset=0` 返回 SHA 相同、尚待登记的新增路径候选。摘要只用于筛选，不能自动认定同一文件；必须明确选择目标。没有候选或已处理时返回空页。差异行的 `can_relocate` 表示可打开选择。

`POST /dataset-registrations/{registration_id}/changes/{目标path_digest}/relocate` 使用相同的 generation、before_revision_id、candidate_sha256 请求结构；前驱必须来自本预检的未见旧项。当前旧头必须匹配，目标必须是未接纳的 added/ready，同来源、原资格及完整摘要必须一致。API 重新严格读取目标，事务内追加定位修订、退役旧头、建立目标头并消费新增候选。原 Item/Asset/Task/Annotation 和托管原图不变，`created=false`；重复确认返回原结果，竞争返回409。旧路径未见不是删除证明，平台不改动外部文件。

差异两端仍保留原分类，通过 `relocated_to_path`、accepted_revision_id 和 accepted_item_id 显示共同处理结果。remaining_added_count 是尚待登记的新增数，与保留历史分类的 counts.added 分开；仅新增登记不会再次处理已消费目标。旧路径日后重新出现视为新候选，不自动取回已移动样本。历史 `acceptance_kind=relocation`，previous_path_digest 指示原路径，acceptance_order 提供与路径内修订号无关的接纳顺序。

原引用保留初始凭据，严格读取按同来源、同 Item/Asset 的最新接纳定位进行；新定位失败不回退旧路径。当前支持同一来源内移动，跨位置/根/来源迁移及资格变更仍需独立处理。预览只清理原路径拥有的派生对象，不删除原件或业务历史。

## 规则变更

| 方法 | 路径                                                           | 行为                         |
| ---- | -------------------------------------------------------------- | ---------------------------- |
| POST | `/dataset-sources/{source_id}/rules/preview?limit=20&offset=0` | 分页预览已接纳路径的规则影响 |
| POST | `/dataset-sources/{source_id}/rules`                           | 固定影响凭据确认规则变更     |

预览请求 `expected_revision`、`recursive`、`include_globs`、`exclude_globs`；仅允许这三个选择字段，不能修改位置、相对路径、节点或所有权。响应含 `proof`、`counts`、`total`、`items`、`active_attempt_count`。影响分类为 within_scope、leaving_scope、entering_scope、outside_scope。默认每页20，最多200；同一快照各页 proof 相同。预览只评估最多50000条已接纳路径，不访问外部存储；扩大范围后的未知新文件数需重新扫描。

确认在相同请求中增加 `impact_proof`。当前账号、原 Key 权限、Source/Location修订、接纳头身份或活动尝试变化返回409 `rules_impact_changed`；仍有活动尝试返回409 `source_busy`。必须结束或取消任务后重新预览。成功只更新选择规则并递增来源修订；旧扫描、预检和检查点不得复用，已有周期计划立即阻断，需重新配置启用。无变化请求保留现状。

本地目录与 SFTP 的排除目录会剪掉子树；外部 S3 按原始对象相对键作平面 fnmatch，排除 `dir` 不隐含排除 `dir/...`，应使用 `dir/*`。不归一化 S3 重复斜线或编码。新预检将规则外旧路径列为 excluded，未遍历到不误判为未见。已接纳 Item、Task、标注及原内容身份保持，旧任务不因过滤条件改变而拒读；定位、根、资格和内容仍按原严格合同验证。

## 历史查询与留存

扫描响应包含固定的 source_revision。历史列表按创建时间倒序；`complete=true&limit=1` 按完成时间取最近完整扫描，独立于默认历史页；`active_only=true` 查询来源的活动扫描，即使它是恢复的较早轮次。来源级 active-registration 返回至多一个 pending/running 预检或登记，避免以最新扫描推断后台状态。

扫描、预检、差异和接纳修订账本不随短期 AsyncJob 清理，不自动按时间压缩或删除。当前只提供分页查询与每来源单活动扫描、单活动登记、单周期计划的约束，没有租户级计划数量上限；调度每轮最多100个到期计划，单扫描继续遵守50000条观测等固定预算。not_seen 没有自动等待期或删除倒计时，只保留候选，不能因持续未见而推断物理删除。

## Agent 来源连接状态

已获授权的 Agent 来源返回可空 `agent_status`：`state`、`reason_code`、`site_name`、`agent_name`、`last_seen` 和 `measured_at`。状态区分 online／offline／revoked／pending／expired／degraded／unavailable；degraded 的原因区分暂停派发和绑定根修订变化。状态只读取当前数据库事实，不调用远端 I/O，来源列表按页批量查询。凭据和宿主路径不进入此字段。

心跳超过六十秒为离线。离线不改写历史扫描、接纳记录或样本身份；样本元数据、预览及完整缓存状态分别保留。具体读取仍按当前权限和完整内容校验决定，不能凭连接状态获得授权。
