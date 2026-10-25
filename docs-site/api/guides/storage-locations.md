---
audience: [dev, project_admin, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# 受控存储位置

存储位置登记 API 或指定 worker 已挂载的只读目录。部署者配置少量允许根，超级管理员登记根内相对目录；新建子目录或登记已有路径无需重启服务。挂载方法见[生产部署](/ops/deploy/docker-compose#受控存储根目录)。

这些接口提供注册、目录浏览和节点能力证据。存储页可配置位置使用范围与人员；获授权的项目管理员可在自己创建的数据集上接入该位置，扫描、预检并确认原地登记。S3/SFTP 关联位置复用[连接器](./storage-connections)的凭据与权限，不重复保存密钥。

## 端点和权限

所有路径前缀为 `/api/v1/storage-locations`。无权访问的位置返回 `404`；员工无目录浏览权限。所有 GET 要求 `datasets:read`，POST/PATCH 要求 `datasets:write`；JWT 保留当前角色权限。

| 方法  | 路径                   | 权限与行为                                                                        |
| ----- | ---------------------- | --------------------------------------------------------------------------------- |
| GET   | `/roots`               | 超管；仅返回配置的根句柄，不返回绝对路径                                          |
| GET   | `/bindings`            | 超管；只读显示九个用途，即使共用桶仍逐项返回；不初始化绑定                        |
| GET   | 空路径                 | 超管看全部，项目管理员看 global 或获授权位置；limit 默认100、最多200，支持 offset |
| POST  | 空路径                 | 超管；登记 filesystem 位置，返回201                                               |
| GET   | `/{id}`                | 可见该位置的管理员；读取脱敏详情                                                  |
| GET   | `/{id}/source-summary` | 当前可见来源及已有后台任务摘要，独立分页                                          |
| POST  | `/{id}/change-preview` | 超管；预览候选字段与已保存关系，签发五分钟确认令牌                                |
| PATCH | `/{id}`                | 超管；提交同一候选，必须提供 expected_revision 与 impact_token                    |
| GET   | `/{id}/directories`    | 可见该位置的管理员；根内单层子目录分页                                            |
| POST  | `/{id}/probe`          | 可见该位置的管理员；探测 API 或指定 Celery hostname                               |
| GET   | `/{id}/probes`         | 可见该位置的管理员；查看按位置、修订、执行节点记录的证据                          |

列表返回`StorageLocationList`，其`items`为`StorageLocationListItem`：保留位置原字段，并增加`health`和`source_usage`。详情`GET /{id}`继续返回`StorageLocationOut`。列表摘要按当前页可见位置批量查询数据库，不返回来源名称／路径，不启动探测、扫描或对象存储请求。

`health`包含`observed_node_count`、`current_success_count`、`current_failure_count`、`stale_node_count`、nullable `latest`／`fresh_until`及`measured_at`。每个节点只使用其已保存最新记录，统一数据库事务时间判断当前配置和过期；计数与位置启用／维护状态独立。`latest`为最近保存的`StorageProbeOut`，不保证当前有效。`fresh_until`为当前有效记录中最早的到期时间，达到后应刷新摘要再使用有效计数。没有节点记录表示未知，旧配置或过期记录不冒充当前失败。

`source_usage`复用`StorageSourceUsage`，与详情来源摘要按同一SQL语义计算当前可见、已采纳head的规模，按位置独立去重。`total_size_bytes=null`表示总量未知，`known_size_bytes`保留已确认部分；`observed_from/through`为文件观测区间，`measured_at`仅表示聚合时间。列表分页不截断单个位置的可见采纳范围，也不跨位置相加；不提供未经测量的位置物理容量或平台对象总量。

## 用途映射策略

`GET /bindings`只读返回九种平台用途，即使共桶或未配置仍逐行保留，不初始化配置、不探测对象存储。每行包含`purpose / location_id / bucket / generation / state`，并提供：

| 字段                       | 当前值与含义                                                                                                                                                                                                                                                                                                                                                                                                        |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `location_config_revision` | 关联位置的实际配置修订；无关联位置为null，与用途绑定`generation`独立。                                                                                                                                                                                                                                                                                                                                              |
| `read_policy`              | `consumer_owned_compatibility`：各业务负责对象读取；已固定位置的对象按记录读取，其他入口保留既有兼容路径。                                                                                                                                                                                                                                                                                                          |
| `write_policy`             | `deployment_mapping`：新写入采用当前兼容部署映射；头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件为`current_binding`，采用当前已登记绑定。两者均不赋予引用来源适配器写权限。                                                                                                                                                                                                     |
| `change_policy`            | `legacy_mapping_locked`：用途映射锁定；头像为`reviewed_avatar_switch`、报告附件为`reviewed_bug_attachment_switch`、审计归档为`reviewed_audit_archive_switch`、导出为`reviewed_export_switch`、导入暂存为`reviewed_import_switch`、数据集原件为`reviewed_dataset_switch`、媒体缓存为`reviewed_media_cache_switch`、标注文件为`reviewed_annotations_switch`、训练文件为`reviewed_training_switch`，支持下述受控切换。 |

`state=configured`只表示用途映射符合已支持的配置合同，不证明位置在线、对象可读、历史定位全覆盖或迁移完成。`missing / mismatch`保留其原有含义；策略字段描述当前路由规则，不提供写入或迁移许可。客户端遇到未提供的新字段应显示未知，不能从位置读取能力推断平台写策略。权限仍为当前超级管理员与`datasets:read`。

## 本地来源故障关联

`GET /api/v1/storage-locations/{location_id}/faults`读取持久故障状态，要求当前超级管理员／项目管理员及`datasets:read`。位置与来源均按当前权限裁剪；项目管理员不能查询其他人可管理范围或孤立来源。GET不探源、不派任务，也不修改故障状态。

查询参数：`state=active|recovered|all`（默认`active`）、`limit`（1–100，默认20）、`offset`（默认0）。响应为`items / total / counts / observation_only`；`counts.active`与`counts.recovered`按相同可见范围统计，不受状态筛选和分页影响，`observation_only`始终为true。条目包含安全关联UUID、来源名、类别、操作、首次／最近失败、恢复时间、已接收失败次数与默认处理角色；`responsible_user_id=null`表示尚未指定个人。不返回内部故障键、路径、对象键或提供方错误原文。

`scope_status=current|historical|unknown`仅表示已保存的配置／内容范围能否匹配，不是在线健康检查。新状态仅由本次接入的实际操作产生，不从旧审计补造。持续失败合并计数；首次发生与再次发生记录`storage.fault`，恢复记录`storage.fault.recovered`，明细带`fault_id`及`transition=opened|reopened|recovered`，可用`detail_key=fault_id`查询转换历史。

恢复需要同一范围的实际成功及既有权限、配置、内容和发布复核。缓存命中原图、邻目标、变更后的期望内容或一般连接探针都不能清除故障。较旧或重复观测不覆盖已接受的新结果；成功不为从未发生故障的文件建状态行。首次失败尚未落库时的并发成功可能没有行可更新，随后晚到的首次失败保守保持active，待下一次同范围成功恢复。

已授权的数据集来源核验读取、引用图片读取、扫描失败与预览生成可产生 `storage.fault` 审计，`target_type=storage_location`、`target_id` 为实际位置 UUID。沿既有 `GET /api/v1/audit-logs?action=storage.fault&target_type=storage_location&target_id=<location_id>` 查询，或用 `detail_key=source_id&detail_value=<source_id>` 按来源过滤；审计权限沿原接口，不赋予普通来源用户全局日志权限。

明细的 `category` 为 `root_unavailable / access_denied / source_changed / preview_failed`，`code`为受控错误码，`operation`为`scan / scan_content / reference_read / preview_generate`。其余关联 UUID 只在真实解析后提供；HTTP request_id在安全长度与格式内原样保留，后台无请求上下文时不提供。无路径、对象键、签名 URL、凭据或异常原文。该观测不改变读取响应、不触发源 I/O，也不构成实时位置健康或全部消费者／站点覆盖证明。

## 导入暂存用途切换

仅当前超级管理员及`datasets:write`凭据可修改。三个入口为`GET /bindings/import/candidates`、`POST /bindings/import/change-preview`和`PATCH /bindings/import`；候选分页、请求字段、五分钟确认与其他用途一致。确认令牌只用于import，绑定当前操作者／Key、用途代次、目标位置／连接修订、维护代次和影响事实，变化后需重新预览。映射返回`change_policy=reviewed_import_switch`。

预览`history`包含`lease_active / lease_cleanup_pending / lease_deleted`、`creation_staging`、`mask_uploads / mask_batches`。探针完整验证条件写入、读回、准确删除、版本能力及受控前缀对象／版本列举；目标桶须启用版本控制，这是现有预标临时输入的固定版本读删要求。失败或未知收尾不更新绑定，不要求排空全部在途上传。

仅新分配使用新目标，保存的预标输入、Dataset／Task直传暂存、Mask上传及batch保持原位置，后续完成／重试不重取当前绑定。最终原件的datasets／annotations位置独立，跨端点时由源client条件完整读取交给原目标写入链。Mask的logical object_key及格式计划保持，内部物理前缀不进入解析合同；旧无引用batch只按明确legacy位置及原SHA解释，不指向新目标。

目标限默认位置或已登记全局S3，外部对象使用连接基前缀下`_aap_managed/import/`。外部上传／ML URL按该目标自身endpoint签名，不能使用全局代理改向；该端点须供调用方访问。正常密钥轮换使用当前位置凭据，已签URL仍受首次截止及提供方凭据撤销约束。预标输入继续原准确版本清理；直传与Mask暂存没有新增远端删除或GC。切换不复制历史输入。

## 头像用途切换

仅当前有效超级管理员可修改，API Key 要求 `datasets:write`。报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件使用下述独立切换端点。

| 方法  | 路径                               | 行为                                                                                         |
| ----- | ---------------------------------- | -------------------------------------------------------------------------------------------- |
| GET   | `/bindings/avatars/candidates`     | 已启用的默认托管位置或全局 S3 位置；`limit`默认20、最多200，支持`offset`；只读，无对象探测。 |
| POST  | `/bindings/avatars/change-preview` | 预览新写入位置与历史影响，实际条件写入、完整读回核验和准确删除探针。                         |
| PATCH | `/bindings/avatars`                | 提交同一候选和五分钟内有效的`impact_token`，原子递增头像绑定代次。                           |

预览请求为 `{expected_generation, target_location_id, target_config_revision}`。桶和前缀来自已登记目标，不接受任意宿主机路径或客户端指定桶。返回 `previous`、`target`、`history`、`capabilities`、`can_apply`、`measured_at / expires_at / impact_token`；目标包含位置修订、连接修订、桶和键前缀，能力分别为`conditional_write / full_read / delete`。探针在业务行锁外执行；仅预览不会修改绑定。

确认请求使用同一请求体并增加`impact_token`。服务器锁内复核操作者／原Key、用途代次、目标配置／连接修订、维护代次和历史影响；变化、过期或无效凭证分别返回409 `storage_binding_preview_changed / storage_binding_preview_expired / storage_binding_preview_invalid`，需重新预览。目标不可写、完整读回不符或无法准确清理时不切换。成功审计`storage_binding.update`不保存密钥或令牌。

`history`分别统计pending、ready、retired上传及缺少准确owner的用户引用；`unlocated_users>0`时`can_apply=false`，确认返回409 `avatar_history_unlocated`。相关用户应重新上传或选择内置头像，不自动认领旧路径。切换仅影响后续分配；已分配上传和历史头像继续按保存位置处理，无需先排空全部pending上传。切走后仍有头像记录的位置不能被重定向或删除；正常密钥轮换复用当前连接凭据，不重写历史物理定位。

## 报告附件用途切换

仅超级管理员与现有 `datasets:write` 凭据可预览或确认。三个入口与头像共用目标资格和物理探针，分别为 `GET /bindings/bug-reports/candidates`、`POST /bindings/bug-reports/change-preview`、`PATCH /bindings/bug-reports`。候选接受 `limit / offset`；请求／目标／有效期字段与头像一致，确认凭证的 audience 独立，不能跨用途复用。

`history` 只返回已登记上传的 `pending / ready / retired`，不扫描历史报告或以未知历史附件阻断 `can_apply`。缺 owner 的旧键依旧由附件下载和绑定入口拒绝。预览成功后只变更新分配目标；已分配 pending 和旧 ready／retired 均按保存 owner 处理，不要求先排空。位置、连接、用途代次、维护代次、操作者／Key 或影响事实变化返回冲突，必须重新预览。

目标限定 legacy 或已登记全局 S3，桶和前缀不由请求任意指定。新目标使用连接基前缀下 `_aap_managed/bug-reports/`；公开 `storageKey` 保持 `bug-report-attachments/{owner_uuid}/{upload_uuid}.{ext}`，内部物理定位和凭据不写入报告 DTO。此用途与头像、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件可切换。

## 审计归档用途切换

仅当前超级管理员与现有 `datasets:write` 凭据可修改。三个入口为 `GET /bindings/audit-archive/candidates`、`POST /bindings/audit-archive/change-preview`、`PATCH /bindings/audit-archive`。候选分页、请求字段、真实探针、五分钟确认和竞争冲突规则与上述用途一致；确认令牌仅限审计归档，不能跨用途复用。

`history` 返回所有已登记归档的 `pending / ready` 数量。切换前已分配的 pending 和已发布 ready 永久保留原位置，后续分配才使用新绑定，不要求先排空 pending。预览不扫描或认领缺少独立归属的旧对象。

新目标限定默认托管位置或已登记的全局 S3，物理键位于连接基前缀下 `_aap_managed/audit-archive/{YYYY}/{MM}/{archive_uuid}.jsonl.gz`。公开归档列表继续返回 `{YYYY}/{MM}/{archive_uuid}.jsonl.gz`。历史读取使用保存的物理地址和当前连接凭据；密钥轮换无需修改旧归档，端点、桶或前缀改向仍拒绝。所有 pending／ready 归档保护位置及连接不被重定向或删除。

该用途不提供归档删除或历史搬迁。完整读取沿用 HEAD、IfMatch、SHA 与解压语义核验，不增加供应商版本历史读取要求；恢复边界见[存储恢复](/ops/deploy/storage-recovery#审计归档用途切换的部署顺序)。

## 登记和修改

部署配置示例：`STORAGE_FILESYSTEM_ROOTS={"ceph":"/mnt/cephfs"}`。API 请求只发送句柄和相对路径：

```json
{
  "name": "公司 CephFS",
  "root_handle": "ceph",
  "relative_path": "traffic",
  "scope": "restricted",
  "use_user_ids": []
}
```

`restricted` 默认仅超管可用；`use_user_ids` 可授权活跃项目管理员，`global` 允许所有平台管理员使用。授权只允许项目管理员管理自己创建的数据集，不授予其他数据集管理权；普通员工沿用任务读取权限。浏览及探测在 I/O 前后复查账号、位置授权和最初 API Key，撤权后不交付目录结果或发布探测证据。位置只读；绝对路径、父级跳转、符号链接和跨子挂载被拒绝。根目录和位置基目录在每个节点固定身份，替换后返回 `root_identity_changed`，不能通过重试静默重新绑定。

PATCH 允许 `name / enabled / cache_policy / original_cache_quota_bytes / original_cache_ttl_seconds / scope / use_user_ids`，不允许改根或基目录。每次更新递增 `config_revision`；旧 `expected_revision` 返回409。外部 S3/SFTP 位置的启用、缓存策略、原图额度与TTL可由超管经此接口修改；名称、使用范围、定位和密钥需通过原连接器端点更新。平台固定位置不可修改。配置或密钥变化使位置修订递增；已被内容或用途引用的连接配置不能改向其他目标，也不能删除。

## 配置变更预览与确认

位置配置更新须先调用 `POST /storage-locations/{id}/change-preview`，再以同一候选调用 PATCH。两个端点均要求当前有效超级管理员和 `datasets:write`，预览无源I/O、配置写入或任务投递。例如：

```json
{ "expected_revision": 3, "scope": "restricted", "use_user_ids": [] }
```

预览返回 `location_id`、`config_revision`、`measured_at`、`expires_at`、`changes: [{field,before,after}]`、`impact` 和 `impact_token`。`impact` 沿用下方只读维护盘点的范围、计数及历史覆盖限制。令牌有效五分钟，绑定操作者、原API Key、位置/连接修订、标准化候选及所盘点关系的身份和相关状态。人员列表去重排序；未变化的字段不列入差异，保存仍递增配置修订。

```json
{
  "expected_revision": 3,
  "scope": "restricted",
  "use_user_ids": [],
  "impact_token": "<本次预览返回的令牌>"
}
```

PATCH在当前权限和配置锁下重新盘点；预览之后新增关系、等量替换记录或相关状态改变，均要求重新预览。旧版直接PATCH缺少令牌返回422。失效令牌不修改位置：

| 状态 | code                                | 处理                                 |
| ---- | ----------------------------------- | ------------------------------------ |
| 409  | `storage_change_preview_expired`    | 超过有效期，重新预览                 |
| 409  | `storage_change_preview_invalid`    | 签名/用途无效，重新预览              |
| 409  | `storage_change_preview_changed`    | 候选、身份或关系变化，刷新并重新预览 |
| 409  | `storage_location_revision_changed` | 位置修订已变化，刷新当前配置再预览   |
| 403  | 当前权限错误                        | 重新核对身份、Key及权限              |

成功的 `storage_location.update` 审计保存安全字段差异、前后修订、预览及复核时间与影响计数，不保存令牌、源路径或连接秘密；失败请求由独立审计记录状态。重新启用位置也须走新的预览，不能重放旧令牌。

关系复核以提交时单条SQL快照为准，位置锁保护配置CAS；Task、标注、冻结等owner仍可按自身合同并发工作。此确认只授权候选逻辑配置更新，所有者的停止/撤权检查、保留和回收协议仍各自执行。预览没有归档、物理退役或删除外部文件的权限。

## 目录分页

`path` 相对于已登记位置，默认空字符串；`limit` 默认100、最多200。响应包含 `entries: [{name, relative_path}]` 与 `next_cursor`。游标有效15分钟，绑定位置、修订、API节点、路径摘要与目录快照；目录变化、换节点、改路径或旧修订返回409，调用者应从第一页重读。

每页最多检查5000个原始条目，文件和链接也计入预算；遍历循环预算2秒，超限明确失败。目录名需有效 UTF-8，中文和空格可用；不支持编码返回 `unsupported_path_encoding`。不返回文件内容、符号链接或任意宿主机目录。

## 节点探测

```json
{ "node_id": "celery@scan-host", "sample_file": "vehicle_detection/sample.jpg" }
```

省略 `node_id` 或传 `api` 探测当前 API。其他值必须是完整 Celery hostname，结果用 `worker:<hostname>` 标识。任务只投递到该 worker 的专属队列，执行端核对实际 hostname、操作者及修订；不会由普通共享队列随机选择节点。

`list / read / range` 分别返回 `verified / unknown`；只传目录时不会推断 read 已验证。显式样本以只读方式采样；`write / delete / presign` 为 `unsupported`，不写探针文件。离线返回 `node_timeout` 或 `node_unavailable`，未配置根返回 `root_not_configured`。证据5分钟过期，配置变更或停用后显示 `stale`；API可达不能代替 worker 可扫描。

API本地探测等待窗口3秒，远端等待窗口5秒。超时结果不会自行更新有效证据；已启动的受控线程仍须实际收尾，响应可能晚于等待窗口。操作系统阻塞调用无法由线程取消，窗口到期不表示底层 I/O 已终止。首次探测某节点信任其部署根映射；跨节点 dev/inode 不比较，能力通过也不代表字节一致或原地预览已验收。

## 只读维护影响

`GET /storage-locations/{location_id}/maintenance-impact` 要求当前有效超级管理员和 `datasets:read`；返回前再次检查账号、API Key及位置修订。支持 filesystem、连接支持的外部位置和已知默认托管位置；其他无连接的未知 S3 位置返回409、`platform_maintenance_impact_unsupported`。默认位置仅增加只读盘点，其配置变更、drain和stop限制保持不变。查询期间修订变化返回409、`storage_location_revision_changed`。无写入、审计追加、源I/O或任务投递。

响应含 `location_id`、`config_revision`、数据库 `measured_at`、`scope=stored_dependencies`、固定大小的 `counts`。全部计数来自同一条SQL的数据库快照，不受来源列表分页影响：

| 计数                                                                            | 范围                                                                                                          |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `sources` / `enabled_sources` / `unregistered_sources`                          | 位置全部保留来源，包含无数据集关联历史                                                                        |
| `items` / `reference_items` / `datasets` / `projects` / `tasks` / `annotations` | 样本按采纳修订、引用及该位置Asset关系去重；项目含无任务的数据集关联                                           |
| `active_scans` / `active_registrations` / `enabled_schedules`                   | pending/running持久轮次及enabled周期配置，不依赖AsyncJob保留                                                  |
| `ready_previews` / `failed_previews` / `preview_bytes`                          | 来源预览生成记录，字节仅累加ready记录，不做对象存在性检查                                                     |
| `cache_entries` / `cache_bytes` / `active_cache_read_leases`                    | 按scope_location_id聚合全部状态账本，含无业务FK保留记录；读租约按数据库时间尚未到期                           |
| `active_input_leases` / `cleanup_input_leases`                                  | 通过当前Task可归属样本的临时输入记录，按状态区分，不代表全部预测或修复作业                                    |
| `ready_lineage_versions` / `unfinished_lineage_versions`                        | snapshot.samples内source_lineage.location_id明确匹配的位置版本；同版本多成员只计一次，未完成仅pending/running |

以上计数保持来源关系范围。新增保存目标与当前绑定计数与其分开：

| 计数                                                                                                                                                                                       | 范围                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `target_records`                                                                                                                                                                           | 输出保存在所选位置的责任角色总数，包含在途、失败、退休和删除责任                           |
| `target_annotations` / `target_datasets` / `target_training` / `target_media_cache` / `target_audit_archive` / `target_import` / `target_export` / `target_avatars` / `target_bug_reports` | 相同保存位置范围，按九种用途分别统计                                                       |
| `target_related_datasets` / `target_related_projects` / `target_related_tasks`                                                                                                             | 沿保存业务ID和现有明确关系去重的关联数，关系丢失时不猜测                                   |
| `target_versions` / `target_runs`                                                                                                                                                          | 自身输出保存在此的训练版本／实验数；Run.dataset_id是训练版本，不作为业务数据集ID           |
| `current_bindings`                                                                                                                                                                         | 当前指向此位置的用途映射项数，与历史保存目标独立                                           |
| `unlocated_targets`                                                                                                                                                                        | **全平台**未保存位置的保留责任数，不归到所选位置，不进入配置或访问维护确认的计数摘要／指纹 |

保存目标按27类持久owner的 `storage_ref`、`staging_ref` 或明确物理 `location_id` 定位，不随今日端点、绑定或启用状态改写。Creation最终原件与实际存在的暂存分别计角色，可能分属不同用途和位置；无暂存键不虚构角色。AssetLocation只将 `managed_original` 计作目标，Preview须有物理桶／键；OriginalCacheEntry以 `location_id` 作为目标，原 `cache_entries` 仍按来源 `scope_location_id` 统计。NULL引用但已有明确标量位置的记录仍可归属；真正缺位置的历史不按当前Binding回填。

角色数不是文件数、容量或清理许可。同一文件可能同时有Creation、当前定位或退役声明；不从对象键反解业务ID，不遍历完整私有业务文档。配置和访问维护两个确认路径复用相同单条SQL，覆盖所选位置的角色身份、保存定位、状态、显示的业务关系和当前绑定；等量替换也需重新预览，心跳、访问及仅重试时间不使预览失效。全局未知历史的变化不阻塞无关位置确认。

`frozen_coverage=recorded_location_lineage_only` 和 `input_coverage=current_task_links_only` 继续限定原来源计数；缺位置血缘的历史、托管冻结和失去Task关系的输入未计入这两类来源统计，其保存目标另由上述字段表达。输入按持久状态计数，包含已到期但尚未转入清理的active记录，不代表运行作业或有效下载凭证。`execution_permission=false`，本响应不生成impact proof，不可作为停用/归档/回收的提交许可。位置修订更新可能使旧扫描、同步、预测上下文及完整缓存失配，各owner在自己的检查点复核；任务人员授权、预览和已保留冻结内容有独立合同，详见[维护影响](/user-guide/datasets/storage-connections#位置维护影响)。

## 位置访问维护

以下接口仅支持外部逻辑位置，要求当前超级管理员与对应的 `datasets:read` / `datasets:write`。维护不会修改位置的 `enabled`、配置修订或来源启用状态。

| 接口                                      | 用途                                                                                              |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `GET /{location_id}/maintenance`          | 读取当前维护状态、修订和未收尾访问，不访问源存储。                                                |
| `POST /{location_id}/maintenance-preview` | `{operation, expected_revision}`；只读预览并返回五分钟 `impact_token`、目标状态及已保存关系盘点。 |
| `POST /{location_id}/maintenance`         | 同一 `operation`、`expected_revision` 和 `impact_token`；提交时重新检查权限、状态与影响指纹。     |

`operation=drain` 将 `active` 转为 `draining`，关闭新访问；`stop` 仅允许 `draining` 且未收尾为零时转为 `stopped`；`resume` 将 `draining/stopped` 转回 `active`。每次转换递增独立维护修订，旧委托缺少修订时按 1 处理，恢复不能复活旧证明。既有 Task 的新读取使用当前修订，保留原授权与固定内容检查。

GET 与提交返回 `location_id / state / revision / changed_at / changed_by / measured_at / protocol_enabled`，以及 `open_operations`、最多 50 条 `operations:{id,purpose,node_id,revision,started_at}` 与 `has_more`。未收尾记录无 TTL，取消或超时须等真实线程和句柄结束后才移除；进程丢失或结束落库失败保留记录，不能由作业终态、租约到期或空初始账本推断静止。没有强制清零接口。预览另含 `operation / target_state / expires_at / impact_token / impact`。关闭新访问的预览不绑定高频 I/O 数量；停止的预览和提交均要求零记录。

`coverage=registered_location_access_only` 与 `physical_cleanup_permission=false` 限定该逻辑位置的受控访问；共享根的其他位置、候选连接、外部客户端和已复制输入不在物理静止证明内。独立冻结媒体与平台自有缓存清理仍走原合同。`STORAGE_MAINTENANCE_PROTOCOL_ENABLED` 默认 false，仅状态查询可用；全部同命名空间 API/Worker 切换新协议并完成部署核验后才可开启变更。

维护拒绝错误包括 `storage_maintenance`、`storage_maintenance_changed`、`storage_maintenance_busy`；预览错误为 `storage_maintenance_preview_invalid / expired / changed`；尚有记录返回 `storage_maintenance_unsettled`；结束落库无法确认时可能返回 `storage_operation_unsettled` 并保留责任，部署未资格化返回 `storage_maintenance_protocol_unqualified`。冲突后应刷新并重新预览，不能自动重放旧令牌。

## 完整原图缓存

位置 `cache_policy` 为 `none / preview / on_demand`，默认 `preview`。来源同样选择请求策略，`effective_cache_policy` 按 `none < preview < on_demand` 取两者较小值；来源响应同时返回 `location_cache_policy`。`on_demand` 也允许预览，不会因启用完整缓存而停止预览生成或读取。

仅超级管理员可配置位置的 `original_cache_quota_bytes` 和 `original_cache_ttl_seconds`。额度为严格整数，范围0至2^50字节、默认0；TTL为严格整数，范围60至2592000秒、默认86400。`on_demand` 必须配置正额度；PATCH 合并现有配置后验证，不能只传零额度继续启用按需策略。来源接口不接受位置额度或TTL。保存前后保持当前活跃超管和原始Key身份，等待锁期间撤权或轮换Key后拒绝提交。

修改位置配置递增修订，使旧扫描/登记不能继续发布；旧修订完整缓存停止参与读取并由后台回收。关闭策略不等于立即释放空间，重新读源后可按新配置保留副本。已有预览实时检查策略上限；来源原件、托管原件和训练副本不归缓存清理。

| 接口                                                  | 合同                                                                                                                                                  |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /storage-locations/{id}/original-cache`          | 超管、`datasets:read`；返回 `owned_original_cache_ledger` 账本快照、修订、额度/TTL、上传中/已登记/待删除各自数量与字节、失败/不确定写入数和观测时间。 |
| `POST /storage-locations/{id}/original-cache/cleanup` | 超管、`datasets:write`；携带 `expected_revision`、上次用量的 `measured_at` 作为 `recorded_before`，以及1至100的 `limit`（默认100）。                  |

清理返回202，仅将截止观测前已验证的、具有精确归属的可重建完整缓存封存为待删除；Entry锁内复查并跳过活跃读取，不强清上传中代。响应包含 `retired_count / skipped_leased_count / remaining_ready_count` 和当前用量。后台周期任务执行实际删除，删除失败、保存目标无法核实或不确定写入保留责任时继续计费，可再次查询用量。它不在请求内等待对象删除，也不提供任意对象键参数。

用量不访问源目录或对象存储，不等于桶盘点或物理容量；`ready`是账本登记状态，实际读图仍完整校验。并发账本变化可能使下一次查询不同。

## S3 浏览与探测

有连接器关联的 s3 位置支持相同 directories/probe 接口。path 与 sample_file 为精确前缀/对象键相对 base_prefix 的字符串，不使用文件系统路径规范化。目录页只返回 CommonPrefixes，JWT 游标固定位置、修订和前缀，查询 cursor 最多12288字符。读前读后均复查当前授权。

根探测只证明 list；sample_file 的成功探测才返回 read/conditional_read=verified。每次实际内容读取仍重新验证条件生效。range/write/delete/presign 均为 unsupported。目标受连接器主机白名单限制；严格客户端关闭代理，发送目标限定为配置 endpoint，不跟随跨目标重定向。DNS 解析仍存在 SDK 内再次解析的既有边界，不等同于 IP 固定。

## SFTP 浏览与探测

具备部署资格的 sftp 位置也支持 directories/probe。`declared_capabilities.reference` 表示当前 API 节点的引用准入配置：filesystem 和外部 S3 可用，SFTP 须匹配 `STORAGE_SFTP_ROOTS`；这不代表节点已通过实时可达性探测。平台固定 S3 位置不作为外部来源。

SFTP 连接 base_path 须为 `/`，整个账号命名空间为位置授权范围。path/sample_file 只接受其下严格相对路径，拒绝绝对路径、父级跳转及探测到的符号链接。浏览对最多5000项的完整目录清单排序分页；游标固定清单摘要，目录变化须重选。探测仅验证 list 和显式样本 read；range/write/delete/presign/conditional_read 均为 unsupported。

客户端仅信任部署指定的完整主机公钥，使用白名单校验后固定的 socket 地址连接。服务器只读 jail 由部署者实际核验，不能从探测成功推断。API 与指定 worker 必须具有相同资格；错误公钥、未获资格、目录不可读或超时均不能发布成功证据。

## 来源与任务摘要

`GET /{location_id}/source-summary` 返回 `location_id / items / total / dataset_count / usage / jobs:{items,total}`。来源行包含 source_id/source_name、可空的 dataset_id/dataset_name、enabled/unregistered_at 和下列 `diagnostics`；任务行只有 id/kind/status/created_at/completed_at。不返回凭据、执行证明或内部配置，不探测或遍历外部存储。

`usage` 独立于来源分页，基于本位置全部当前可见的采纳头，包含 reference 与 managed_copy。它仅描述来源文件逻辑规模，不与平台对象字节相加。固定字段如下：

| 字段                                                       | 含义                                                                           |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `scope / source / unit`                                    | 固定为 `visible_current_accepted_source_heads / accepted_source_index / bytes` |
| `coverage / is_estimate`                                   | `complete / partial / none`；未完整覆盖时为估算范围，总量未知                  |
| `reference_count / file_count / duplicate_reference_count` | 当前采纳记录、已确认去重文件、已合并重复引用数                                 |
| `known_size_bytes / total_size_bytes`                      | 已确认部分字节；只有完整覆盖时提供总量，未知为 null                            |
| `unknown_reference_count`                                  | 未计入已确认部分的记录数，包含同址冲突与不可比较域                             |
| `conflict_file_count / incomparable_file_count`            | 同址内容冲突组、节点或挂载不可比较组数                                         |
| `inactive_reference_count`                                 | 位置／来源已停用或来源已注销范围内仍保留的采纳记录                             |
| `observed_from / observed_through / measured_at`           | 对应完整扫描的保存观测时间范围与本次数据库计算时间；无观测不补造时间           |
| `deduplication_basis / includes_versions`                  | `location_frozen_address_and_object_version`；S3 包含已采纳的对象版本          |

没有可见采纳头的受支持位置返回完整零值；未支持的位置类型返回 `none` 与 null 总量。定位／收据缺失或不一致时保留未知记录，同址 SHA／大小冲突排除整组；不同文件不按 SHA 合并。来源或位置名称、授权和缓存修订变化不抹去保存规模，也不证明源文件当前可用。历史修订、未采纳扫描与缓存不计入。具体去重域见[来源规模计量](/dev/concepts/storage-connections#当前已采纳源索引规模)。

`diagnostics` 仅聚合**当前来源页**的已授权来源，固定范围为保留的原地引用业务样本（`DatasetReference`），包含历史修订；不是路径头数量、已扫描文件数或容量，不含托管复制和未登记路径：

- `references.scope=stored_reference_items`、`total`、`original_counts:{verified,missing,changed,preview_only,unavailable,unverified}` 复用 Workbench 持久观测。`observed_from/observed_through` 是观测时间范围，无样本为 null；已验证不保证当前原件仍可读。
- `preview_counts:{ready,failed,unrecorded,not_requested,blocked}` 独立于原件计数。ready/failed 要求当前业务 Item、Asset、SHA、来源路径头及预览规格匹配；ready 另核对当前平台位置、桶和用途绑定。旧内容或旧规格记录不计为当前成功/失败；允许同内容预览在重新启用策略后复用。策略关闭为 not_requested，来源/位置停用或注销为 blocked。`preview_recorded_at` 为匹配的成功或失败生成记录的最近时间；ready 不验证对象此刻存在。
- `scan` 与 `preflight` 各含 `recent`、`last_complete`，无记录为 null。recent 优先活动轮次，再按创建时间排序，旧轮恢复不会被新失败记录遮住；last_complete 为最近完整成功轮次。预检按来源独立查找，不仅限最新扫描。
- 轮次摘要只含 `run_id/scan_run_id/job_id/status/phase/complete/matches_current_config/created_at/completed_at`。complete 对扫描表示完整枚举，对预检表示完整比较，不能替代 status。配置匹配核对来源/位置修订，预检另核对来源策略修订；历史原件观测仍保留，不因配置变化被当成新验证。任务历史删除后 job_id 可为 null，领域轮次仍保留，可用 scan_run_id 导航。

GET 只查询数据库，不读源文件、不 HEAD/LIST 对象、不启动扫描或预览生成。manager 角色和当前位置/来源权限仍逐请求复查，API Key 需要 `datasets:read`。无样本返回明确的零计数与空时间，不推断为健康。

来源使用 limit/offset，任务使用 jobs_limit/jobs_offset，各自默认20、最多200。任务范围覆盖该位置全部可见来源，独立于当前来源页，并包括恢复前的历史尝试。项目管理员仍需拥有数据集管理权和位置使用权；全局位置不公开其他人的来源数量。停用、注销历史保留；原数据集已删除的来源仅超管可见，dataset 字段为空。任务详情、取消和恢复沿用原任务及来源接口。

位置输出增加 `allowed_actions`：read、probe、browse、manage_location、manage_connection，按当前账号及实际端点权限裁剪；全局连接仍仅超管可管理。它不代替服务端操作时复查。source-summary 的 `total` 为可见来源数，`dataset_count` 为所有这些来源关联的非空数据集 ID 去重数，与来源分页无关。

## 平台对象占用盘点

以下端点属于 `/api/v1/storage`，与上方只读目录位置接口分开。两者均要求当前有效超级管理员；API Key 必须包含全权 `*`。相同限制适用于 ML 集成 overview 中的存储摘要以及 `storage_usage` 类型任务的列表、详情和取消；原任务发起者降权后不能继续访问。

| 方法 | 路径                                 | 行为                                                         |
| ---- | ------------------------------------ | ------------------------------------------------------------ |
| GET  | `/api/v1/storage/buckets`            | 只读成功快照与最近尝试，不执行 HEAD/LIST                     |
| POST | `/api/v1/storage/usage-inventory`    | `202 {job_id, reused}`，复用现有活动任务或创建盘点           |
| PUT  | `/api/v1/storage/usage-schedule`     | `{enabled, interval_hours=24}`，显式设置周期，间隔1至168小时 |
| POST | `/api/v1/async-jobs/{job_id}/cancel` | 合作式取消，保留成功值及已完成桶                             |

每条 `items` 是一个 endpoint 下的唯一桶：`name`、`roles`（多个用途）、兼容字段 `role`（首用途）、`object_count`、`total_size_bytes`、`measured_at`、`measurement_started_at`、`attempt_status`、`attempted_at`、脱敏 `error`。`source=s3_list_objects_v2`、`scope=current_objects`、`coverage=none|complete`、`is_estimate=true` 明确观测范围。`status=unknown|ok|stale` 表示快照新鲜度，不代表存储健康。

首次无成功值时计数和时间为 null；真空桶成功值为零。超过 24 小时或最近失败/取消为 stale，继续返回原成功值和原时间。`measured_bucket_count/bucket_count` 表示覆盖；只有全部桶都有成功值时才返回 `total_object_count/total_size_bytes` 合计，否则为 null。`physical_total_bytes`、`physical_available_bytes`、`quota_bytes` 仍为 null。`inventory_job_id/inventory_status` 指向最近盘点；任务过期清理后 ID 可为 null，但成功快照保留。

响应的 `breakdown` 给出同一平台范围的对象分类快照。`categories` 固定包含 `managed_original`、`preview_cache`、`training_version`、`temporary`、`other_platform`、`unclassified`，每项含 `object_count/size_bytes`，取实际 LIST 大小。同一 endpoint／bucket／key 只计一次；跨类别归属冲突计入 `unclassified` 并增加 `conflict_object_count`。

`breakdown.measured_bucket_count/bucket_count` 是拥有有效分类快照的桶覆盖；`sampled_object_count/sampled_size_bytes` 是这些桶的小计，全部桶有有效分类后 `total_size_bytes` 才非 null。`coverage=none|partial|complete` 同时表达桶缺测或未归类对象：全部桶已测但仍有未归类时为 partial，实际字节合计仍可已知。`measured_from/measured_through` 表示采样窗口，`has_stale_data` 表示含过期成功值。旧快照或旧检查点缺少分类证据时不从桶角色补造，原对象总量继续有效。旧服务响应也可能没有 `breakdown`，客户端应按未知处理。

固定口径为 `scope=configured_platform_buckets_current_objects`、`source=s3_list_objects_v2_and_saved_ownership`、`deduplication_basis=endpoint_bucket_key`、`includes_versions=false`、`is_estimate=true`。分类不返回对象地址或逐对象清单，不证明内容完整性，也不授予清理权限；外部源规模不纳入。分项与桶总量属于同批对象，不得重复相加。

调用方兼容说明：此 GET 已从实时统计变为快照读取；应处理 nullable 计数和新增状态，不能将未知值渲染为零或依赖每次 GET 重新盘点。ML overview.storage 使用相同结构。按桶去重并完整覆盖九用途，不能按角色重复相加。后台任务取消、崩溃与授权/配置变化不会发布未完整桶的累计数。更多口径和预算见[桶布局与快照](/dev/reference/storage-buckets#平台对象占用快照)。

### 配置源迁移清单任务

仅当前有效超级管理员与全权API Key可调用。来源固定当前部署端点及九用途桶映射，请求不能传入任意地址、路径或凭据。以下接口位于`/api/v1/storage`，GET只读数据库，不做源端HEAD／LIST或自动重投。

| 方法与路径                                      | 行为                                                                                                           |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `POST /migration-inventories`                   | 202，接受`{limits?,handling?}`，返回`{item,reused}`；复用当前操作者相同范围和输入的活动任务，活动输入冲突409。 |
| `GET /migration-inventories?limit=20&offset=0`  | 返回`{items,total,limit,offset}`，分页读取历史及报告状态。                                                     |
| `POST /migration-inventories/{job_id}/dispatch` | 仅显式重投pending的同一task ID；不接管或重复执行running任务。                                                  |
| `GET /migration-inventories/{job_id}/report`    | 重新授权后下载封存JSONL，安全附件文件名与no-store，报告缺失时不返回空的成功文件。                              |

`item`含`job_id,status,created_at,started_at,completed_at,scope,source_location_id,migration_ready,limits,handling,cancel_requested,complete,counts,error_code,report_available,report_sha256,report_size_bytes`。`scope=selected-configured-source`，`migration_ready=false`；尚无执行结果时`complete`与`counts`为null。counts为核心的桶、对象、删除标记、multipart、核验对象／字节、读取字节、页、问题及条目计数。对象键、端点、凭据和报告内容不放入公开DTO、AsyncJob结果或通知。

limits为`max_pages`（默认1000，上限100000）、`max_entries`（默认10000，上限100000）、`max_object_bytes`（默认256MiB，上限1GiB）、`max_total_bytes`（默认2GiB，上限16GiB）、`max_seconds`（默认600，上限3600）、`read_timeout_seconds`（默认30，上限60）和`page_size`（默认1000，上限1000）；各值必须大于零。报告在生成时受32MiB硬上限约束。handling仅接受九用途的`migrate|rebuild|retain`，缺项保持`unreviewed`，不是默认迁移许可。

任务kind为`storage_migration_inventory`，复用`POST /api/v1/async-jobs/{job_id}/cancel`及共享详情／通知。pending取消可立即结束，running取消先记录请求，实际线程、Body、客户端和输出收尾后才发布终态。未封存、取消、关闭不明或最终撤权／配置围栏失败时不发布成功报告；已封存但`complete=false`的观测以失败及准确摘要表达，可下载时单列。报告沿现有终态Job保留30天，级联删除不会修改源对象。

完整清单也不是原子快照或全业务迁移资格；用途已切外部目标时如实保留未覆盖，不要求先切回A。此接口不建立目标、复制对象、修改绑定、执行停写或物理退役。

### 初次复制计划与续跑

以下接口位于`/api/v1/storage`，仅当前有效超级管理员与全权API Key可访问。目标只接受已登记、启用的全局S3位置及连接，不接受任意端点、桶、主机路径或明文凭据；不会修改用途绑定。GET只读保存状态和检查点，不访问源／目标S3或自动投递任务。

| 方法与路径                                                           | 行为                                                                                                                                                                                                     |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /migration-copy-targets?inventory_job_id=...&limit=20&offset=0` | 分页返回`{items:[{location_id,name,config_revision}],total,limit,offset}`。候选只表示已登记，实际能力在执行时核验。                                                                                      |
| `POST /migration-copies`                                             | 接受`{inventory_job_id,target_location_id,handling,limits?}`，202返回`{item,reused}`；先准备并封存计划，不向目标PUT。同一冻结输入、目标、处理选择和预算复用原计划。                                      |
| `GET /migration-copies?limit=20&offset=0`                            | 独立复制计划的分页历史，返回`{items,total,limit,offset}`。                                                                                                                                               |
| `GET /migration-copies/{copy_id}`                                    | 返回安全计划详情及当前／最后attempt结果。                                                                                                                                                                |
| `GET /migration-copies/{copy_id}/failed-objects?limit=20&offset=0`   | 返回`{items:[{object_id,size_bytes,error_code}],total,limit,offset}`；仅当前failed对象，ID为64位小写十六进制身份摘要，不含原始键。                                                                       |
| `POST /migration-copies/{copy_id}/attempts`                          | 接受operation（prepare、run、verify、recover）、retry_failed和可选retry_object_ids；202返回`{item,reused}`。沿同一持久计划创建独立AsyncJob，相同活动attempt复用；recover克隆上次操作及选择，不重算范围。 |
| `POST /migration-copies/{copy_id}/dispatch`                          | 仅显式重投当前pending的原task ID，返回`{item,reused}`。                                                                                                                                                  |
| `GET /migration-copies/{copy_id}/checkpoint`                         | 认证下载私密SQLite检查点快照，包含实际映射／nonce／回执；固定附件名、no-store与`X-Content-SHA256`，不是公开下载链接。                                                                                    |

item含`copy_id,created_at,updated_at,source_inventory_job_id,source_report_sha256,source_complete,target_location_id,target_name,handling,limits,prepared,job_id,operation,status,cancel_requested,counts,decisions,source_summary,catalog_summary,copy_subset_complete,migration_ready,error_code,io,checkpoint_available`。counts为`verified,pending,writing,uncertain,failed,skipped`六类累计数，准备前为null；准备成功只表示计划可审阅。`job_id`及原清单引用可在对应Job过期后为null，复制计划及事实继续保留。公开DTO、共享Job和通知不包含实际对象键、端点、凭据或完整元数据。

handling必须明确九用途的`migrate|rebuild|retain`；同桶处理歧义、未核验源与超限对象按既有核心列为跳过。已封存的部分清单仍可准备合格子集，源／目录覆盖与全计划状态分别报告，`migration_ready`始终为false。

limits为每次执行预算：`max_object_bytes`默认256MiB、最大1GiB；`max_source_bytes,max_upload_bytes,max_verify_bytes`各默认2GiB、最大16GiB；`max_objects`默认10000、最大100000；`max_seconds`默认1800、最大3600；`connect_timeout_seconds`默认10、最大60；`read_timeout_seconds`默认60、最大60。各项均为严格正整数；固定计划后不重算目标映射。应用封存目录上限64MiB，SQLite检查点在生成时受256MiB硬上限及100000行约束。

`prepare`仅重试尚未封存基线的准备任务；`run`普通续跑不重传verified，也不重试failed，显式`retry_failed=true`才将全部失败项加入续跑。附加非空`retry_object_ids`时仅执行选中的failed对象，其他pending／uncertain／verified保持。选择至多1000个去重ID，未知或已改变状态的ID在建立attempt前409拒绝，不启动I/O；本次选中结果单列`attempt_subset_complete,selected_count,selected_verified_count`，普通执行为null，不替代全计划`copy_subset_complete`。`verify`只读保存的准确目标版本，不PUT。`recover`不接受新的失败选择，在服务端克隆原操作和原选择范围；已成功项不重传，原选择中的writing／uncertain可沿nonce恢复，其他对象不执行。

共享取消入口仍为`POST /api/v1/async-jobs/{job_id}/cancel`。pending可立即结束，running只登记请求，真实原生资源收尾后发布终态；已写及不明回执保持。硬中断后只有确切Worker进程结束证据才能解除对应未决执行，不以TTL接管；无法确认时保留unknown。目标凭据轮换后，新attempt重新解析当前凭据，但端点、桶、前缀及计划身份必须保持。源、目标的保存责任及维护许可独立保留，不从任务终态推导切换或删除许可。

### 增量账目与周期对账

`BucketsResponse.accounting` 为可空固定摘要：`confirmed_added_bytes`、`confirmed_removed_bytes`、`net_change_bytes`、`pending_operation_count` 和 `unknown_operation_count`。当前 `producers=[prediction_inputs, managed_original_creation, thumbnail_outputs, original_cache, image_pyramid_outputs, video_derivative_outputs, export_media_outputs]`，固定 `scope=current_objects`、`coverage=partial`、`projected_total_bytes=null`。已确认事件按准确物理地址及操作身份幂等累计，不从额度预约、业务删除或当前绑定补算历史；新增字节与移除字节分别为非负值，净变化可为负数。

托管原件生产者复用 Creation 最终条件 PUT／CompleteMultipart：冻结地址及该次尝试的意图与 writing 原子提交，覆盖 Dataset 各 origin、Task 和辅助原件。四个媒体缓存生产者覆盖共享缩略图、完整原图缓存、金字塔和视频派生物的逐对象条件 PUT；多对象部分失败时分别保留已发生事实，不以整个生成任务成功代替物理回执。

`export_media_outputs`覆盖ExportMediaBundle中图片、视频、LiDAR与NuScenes共用复制入口的逐对象受控新增，分类为`temporary`。地址来自保存的Bundle，每次实际条件PUT独立登记并保留返回版本；后续业务拒绝或部分生成失败仍保留物理事实。ExportOutput ZIP没有可关联的返回版本，仍未接入；导出清理与退休也不补造移除量，不因增加该生产者宣称整个export用途已完整覆盖。

完整 SHA／大小读回、返回不可变非 null 版本与当前 HEAD 一致且原生收尾完成后才记新增。412 只记该次零，无版本、应答不明或版本替换保持 unknown；后续业务回滚不抹去物理事实。原件退役和 Reclamation 没有实际 DELETE，因此不计移除；媒体缓存本片只接新增，现有按 key 清理、退休或业务行删除也不补造移除量。浏览器暂存、其他生产者和外部写入仍未覆盖，GET 不访问物理源或触发盘点。

`reconciliation` 为可空最近成功桶汇总，含 `status=no_baseline|partial`、`measured_at`、`bucket_count`、`compared_object_count`、`uncertain_object_count`、`unexplained_object_count`、`confirmed_change_bytes`、`known_unexplained_change_bytes` 和 nullable `unexplained_change_bytes`。首次没有对象级基准、跨观测窗口操作或未决事实使完整差额未知；已知部分仍单列。未解释变化可能来自未接入用途或外部写入，不表示账目损坏，更不授予删除许可。LIST快照及事件均只统计当前可见内容字节，不把全部历史版本写删相减。

`usage_schedule` 含 `enabled`、`interval_hours`、`status=off|active|paused`、`next_due_at`、`last_started_at` 和 `pause_reason`。默认关闭；PUT开启保存当前操作者／API Key授权证明及精确配置范围，首次立即到期，既有30秒恢复任务负责调度。活动盘点不重叠；撤权或配置变化暂停并报告 `access_revoked|configuration_changed`，权限恢复后仍须显式重新开启。关闭计划不取消已有任务，PUT不直接访问对象源。GET不包含逐对象地址、事件清单或授权证明。

## 托管原件保留预演

`GET /api/v1/storage/managed-original-reclamation?limit=50&offset=0` 要求当前超级管理员及API Key全权`*`。`limit`为1至100，`offset`为0至1000000；按关系移除时间、记录ID稳定排序。查询只读取已登记托管原件的独立保留账本和当前引用，不创建任务、修改状态、获取签名或访问对象存储。

返回`scope=registered_managed_original_retirement_assertions`、`unit=retirement_assertion`、`measured_at`、`coverage`、`counts`、`items`及`limit/offset/has_more`。`execution_permission=false`，`reclaimable_bytes=null`，响应不能用作执行令牌或后续删除依据。

`counts`包含`total_assertions/protected_assertions/retained_assertions/blocked_assertions`，后三者互斥且合计为总数；另有`policy_candidate_assertions`、`unknown_size_assertions`、`known_asserted_bytes`及无法完整解析的`unknown_training_versions/unknown_active_learning_rounds`。按记录累计的大小可能重复同一物理原件，不是可释放字节。计数、逐项引用及本页来自同一SQL观测；分页之间不保证数据库保持不变。

每项返回历史`id/asset_id/dataset_id/dataset_item_id`、`retired_at/retain_until/size_bytes`、`policy_revision`、`identity_state=current|unknown|mismatch`、`status=protected|retained|blocked`、`policy_candidate`、`roots`和`blockers`，不返回桶、对象键、端点或秘密。`current`只表示保存的配置证据与当前配置匹配，不代表字节存在或内容已现场核验。`retain_until`仅是本条期限，null表示无限期；另返回`effective_retain_until/indefinite_retention/peer_assertions`及`declared_backup_holds/declared_migration_holds`，在全量同桶同键保守相关记录上取任意无限期、最大有限期限及所有有效要求，再分页。不同端点也可能保守归组，不宣称相同副本。已知引用或有效要求优先归为protected，否则有效期限未结束/无限期归为retained，其余归为blocked；候选仍受所有未知保护条件阻断。

`roots`分别表示内容位置、样本、任务、任务数据集关联、所有保留状态的训练版本、主动学习历史及无法确认物理桶的旧任务。`blockers`解释这些引用、无限期/未到保留期限、未知/变化的存储身份、未核验内容/定位、不支持的历史快照和旧任务桶、未证明独占所有权、未知备份/迁移保留与未检查现场副本。未知历史结构不能因无法展开而视为零引用。

`coverage`明确旧位置不回填、未登记原件未记录、现场对象未检查、独占所有权未建立、备份/迁移保留未知，仅解析已支持的训练和主动学习记录。`declared_requirements=registered_retirement_peers_only`只覆盖已登记要求，不将备份/迁移未知改成完整覆盖。`blockers`另含`related_indefinite_retention/related_retention_window/declared_backup_hold/declared_migration_hold`。前后复验身份/Key与本地及持久配置，撤权拒绝返回，配置变化返回409 `configuration_changed`。本读请求不锁住后续业务变更，刷新后仍须重新核对；其他物理清理owner的合同不因此改变。

## 托管原件保留设置

以下路径均要求当前超级管理员及API Key全权`*`，只操作已登记保留断言的元数据，不读写对象、访问外部编号或发起任务：

| 方法 | 路径                                                                     | 结果                                       |
| ---- | ------------------------------------------------------------------------ | ------------------------------------------ |
| GET  | `/api/v1/storage/managed-original-retirements/{retirement_id}/retention` | 当前策略、共同保护摘要与有界要求历史       |
| POST | 同路径加`-preview`                                                       | 五分钟候选确认，纯数据库读取               |
| POST | 与GET同路径                                                              | 复验候选、修订和权威后提交设置与同事务审计 |

GET接受`limit=20`（1至100）及`offset=0`（0至1000000），返回`retirement_id/policy_revision/measured_at`、`summary`、`holds/total_holds/limit/offset/has_more`。`summary`包含自身`retain_until`，相关记录的`effective_retain_until/indefinite_retention/peer_assertions`及有效`backup_holds/migration_holds`。历史包含所有相关记录，按有效优先、created_at降序和ID稳定分页；每项有`id/retirement_id/kind/reference_code/reason/created_by/created_at/revision/released_at/released_by/release_reason`，没有物理定位。

预览与提交共同接收`action/expected_revision/reason`，原因去首尾空白后须1至500字符；仅允许该动作的候选字段，其他字段拒绝：

- `set_retention`必须显式提供`retain_until`；null为无限期，否则必须带时区且在数据库当前时间之后，标准化为UTC。允许显式缩短本条期限，不修改peer期限或hold。
- `add_hold`必须提供客户端生成的`hold_id` UUID、`hold_kind=backup|migration`及`reference_code`。编号匹配`[A-Za-z0-9][A-Za-z0-9._:-]{0,99}`；UUID绑定确认且数据库唯一，防止丢响应后重复同一登记。相同锚点/类别/编号不能同时存在两个有效要求。
- `release_hold`必须提供`hold_id/expected_hold_revision`，只允许该ID的原始锚点操作。有效修订1显式解除为2，保留创建字段和历史解除原因；没有TTL，也不能重复解除或修改已解除历史。

预览返回`before/after`共同保护摘要、`shortens_retention`、动作及`hold_id/hold_kind/reference_code`、观测/到期时间和`impact_token`。提交额外携带该令牌，返回当前State；每次成功动作只将目标policy_revision递增一次。令牌绑定操作者、当前Key权威、锚点/规范候选/原因/修订、平台配置和完整peer/hold指纹；确认期间截止时间已到也拒绝。错误包括401/403、404缺失或非原锚点、409冲突/锁忙/过期/重复以及422输入或过去期限；失败不留半状态。

所有保留控制写入按User→Key→保守locator组事务锁→锚点→hold排序，不锁任意peer或业务对象。新业务删除仍可能并发追加默认无限期断言；摘要是提交时重查的观测，不是捕获或物理执行屏障。成功审计保存安全前后摘要、原因、修订与确认时间，不保存物理路径或令牌。所有结果固定`coverage=declared_requirements_only`、`execution_permission=false`；空要求列表、解除或策略到期不能证明独占所有权、真实恢复点、迁移或现场最小副本已获资格。

## 托管原件完整核验

`GET /api/v1/storage/managed-original-retirements/{retirement_id}/verifications?limit=20&offset=0`仅查询历史，要求当前超级管理员及API Key全权`*`。返回`retirement_id/measured_at/scope=registered_retirement_assertion_only/execution_permission=false`、`can_start/blockers`、`max_bytes/timeout_seconds`及有界`items/total/limit/offset/has_more`。每项包含核验/作业ID、状态/结果、安全错误码、请求/开始/完成/取消请求时间、期望/观测SHA与大小、`context_matches`和`unsettled_operation`，不返回locator、端点或内部权限证明。

`POST`同路径显式创建核验，202返回`retirement_id/verification_id/async_job_id/existing`。同锚点的活动记录复用；pending可重派同task ID，running不重复读取。当前平台配置或历史身份不能匹配、历史内容不可信、超出5GiB预算、已有未收尾责任等条件阻断首个对象I/O。领域记录独立保留，不随通用任务清理而消失；缺失或不匹配的活动作业不能直接重派。

核验任务只读完整对象，使用条件GET、完整SHA与准确大小、最终当前键观测，单次协作读取预算60秒。结果为`matched/missing/changed/unverifiable/failed/cancelled`之一，活动时尚无结果。未找到对象与权限不足分别表达，ETag和HEAD不能代替完整摘要；缺少可信历史摘要不能通过当前内容静默补齐历史证明。

通过`POST /api/v1/async-jobs/{job_id}/cancel`取消，授权同样要求当前超级管理员与全权Key。pending可直接取消；running返回`status=cancel_requested`，实际读取收尾后才终态。进程丢失或关闭不确定保留operation责任，信号/超时不会自动清空；未收尾期间不可重新启动同锚点。

`context_matches`仅说明已记录锚点与配置仍适用，不承诺对象此刻未被覆盖。SHA匹配仍不是独占所有权、最小副本、可恢复备份或物理删除许可；所有结果维持`execution_permission=false`，不调整期限或声明要求。配置/定位在恢复后变化，旧观测按历史保留且不再匹配，需要显式核验新目标。

## 上传头像的固定位置

`POST /api/v1/auth/me/avatar` 保持原multipart图片上传和 `UserOut` 返回格式。新token在写入前保存独立归属与规范化内容摘要；上传期间账号或Key失效、头像已被其他请求改变、维护代次变化时拒绝发布。并发头像变化返回409，撤权返回403；失败候选保留内部pending事实，不通过未确认的物理删除补偿发布错误。

`PATCH` / `DELETE /api/v1/auth/me/avatar` 仍选择内置头像或恢复默认，客户端不能指定 `upload:` 引用。引用变化和旧对象退休同事务提交；物理清理失败不撤销已生效的选择，cleanup队列沿保存位置重试。

`GET /api/v1/avatars/{token}` 仍允许匿名读取，返回WebP及内容ETag，支持 `If-None-Match` 的304。仅独立ready记录可读；格式错误、未知token、pending或retired为404，已知归属的端点或完整字节核验失败明确拒绝，不能回退当前桶。命中ETag仍核验实际完整字节。历史只有 `users.avatar_ref`、没有上传归属记录的token同样404；需重新上传或选择内置头像，GET不执行接管或写库。

## 导出用途切换

仅当前有效超级管理员和 `datasets:write` 凭据可修改。入口为 `GET /bindings/export/candidates`、`POST /bindings/export/change-preview` 和 `PATCH /bindings/export`；候选分页与其他用途一致。预览请求包含 `expected_generation`、`target_location_id`、`target_config_revision`，确认再附五分钟有效的 `impact_token`。令牌绑定此用途、当前操作者／Key、当前用途代次、目标位置／连接修订、维护状态及影响数量，不能跨用途使用。

`history` 分别返回 ZIP 的 `zip_pending / zip_ready / zip_retired`，媒体包的 `media_building / media_ready / media_retiring / media_deleted`。真实探针返回 `conditional_write / full_read / delete / list / list_versions`。失败或未知收尾不切换；竞争变化返回冲突并要求重新预览。用途映射使用 `change_policy=reviewed_export_switch`。

ZIP 和独立媒体包共同使用 export 绑定。仅新分配采用新目标，旧 ready、在途及缓存仍按保存位置读取；配套 ZIP 从媒体包继承位置。目标为默认托管位置或已登记的全局 S3，外部对象位于连接基前缀下 `_aap_managed/export/`。下载端点、固定七天期限及密钥轮换语义见[用户指南](/user-guide/datasets/storage-connections#切换导出的新写入位置)；未覆盖的新定位由[恢复工具](/ops/deploy/storage-recovery#导出用途切换的部署顺序)在修改映射前拒绝。

## 数据集原件用途切换

仅当前超级管理员和具有`datasets:write`的有效凭据可修改。入口为`GET /bindings/datasets/candidates`、`POST /bindings/datasets/change-preview`和`PATCH /bindings/datasets`；候选支持`limit / offset`。预览请求包含`expected_generation / target_location_id / target_config_revision`，确认再附五分钟有效的`impact_token`。令牌只用于datasets，并绑定当前操作者／Key、用途代次、目标配置、维护状态和影响事实，变化后需重新预览。绑定返回`write_policy=current_binding`及`change_policy=reviewed_dataset_switch`。

响应`history`包含`asset_locations / creation_allocations / auxiliary_originals / retirements`四项元数据计数，不代表物理在线或迁移完成。目标限默认位置或已登记global S3；外部目标在连接基前缀下使用`_aap_managed/datasets/`。真实探针验证空PUT及非空multipart条件创建、完整读回与准确收尾，失败不改变绑定。完整对象键同时限制1000字符／1024 UTF-8字节；平台生成物理键，请求不能任意指定桶、键或宿主路径。

切换只影响后续新分配。现有DatasetItem、辅助原件和在途Creation保持保存地址，读取在原有授权和内容校验下使用当前位置的当前凭据。正常密钥轮换不改冻结事实，当前绑定不参与旧原件定位；Task独立原件使用独立的annotations用途绑定。此入口不搬迁、重签历史原件或授予物理回收许可。

## 媒体缓存用途切换

仅当前超级管理员及具有`datasets:write`的有效凭据可预览和确认。入口为`GET /bindings/media-cache/candidates`、`POST /bindings/media-cache/change-preview`和`PATCH /bindings/media-cache`；候选分页使用`limit / offset`。预览请求为`{expected_generation, target_location_id, target_config_revision}`，确认附五分钟有效的`impact_token`。确认令牌仅用于media-cache，绑定当前操作者／Key、用途代次、目标配置与连接修订、维护状态及影响事实；变化或过期需重新预览。绑定返回`write_policy=current_binding`和`change_policy=reviewed_media_cache_switch`。

`history`分别返回`directory_previews / original_cache_entries / image_pyramid_outputs / video_derivative_outputs / thumbnail_outputs / legacy_thumbnails`。这些是不同所有者的责任记录数，包含需保留的历史状态，不能相加作为对象总数。目标限默认托管位置或已登记global S3，使用连接基前缀下`_aap_managed/media-cache/`；完整受控根最多436 UTF-8字节，完整key另限制1000字符／1024字节。客户端不指定任意桶或对象键。

真实探针返回`conditional_write / full_read / delete / list`，验证条件创建、完整读回、准确清理及独占前缀列举。它不要求分段上传、空文件或版本化桶资格。失败、未知写入或关闭收尾不更新绑定；目标消失、竞争变化及过期确认沿现有冲突合同处理。

仅新分配使用新目标。原图缓存、目录预览、缩略图、金字塔和视频派生按各自保存地址继续使用当前连接凭据，不依赖当前用途绑定解释历史。旧NULL和schema1保留原默认位置解释，不自动补认。新缩略图及派生输出使用[同源短期能力 URL](./verified-content#媒体缓存短期读取)，完整原图缓存仍通过原件读取合同访问。所有保存状态保留位置保护；切换不授予历史搬迁、物理退役或跨目标恢复许可。

## 标注文件用途切换

仅当前有效超级管理员可修改，API Key 要求 `datasets:write`；候选 GET 要求 `datasets:read`。三个入口为 `GET /bindings/annotations/candidates`、`POST /bindings/annotations/change-preview` 和 `PATCH /bindings/annotations`。候选只含默认托管位置或已登记且启用的全局 S3，支持 `limit`（默认20、最多200）和 `offset`。请求使用 `AnnotationsBindingChange`：`expected_generation`、`target_location_id`、`target_config_revision`；确认额外提交 `impact_token`，桶及前缀不能由请求任意指定。绑定行返回 `write_policy=current_binding` 和 `change_policy=reviewed_annotations_switch`。

`AnnotationsBindingPreview` 返回 `previous / target / history / capabilities / can_apply / measured_at / expires_at / impact_token`。目标仅公开位置ID、名称、配置修订、连接修订、桶和受控前缀，不返回端点或凭据。`history` 的五项为 `task_originals`、`creation_allocations`（仅 `owner_kind=task`）、`raster_mask_objects`、`comment_attachments`、`guide_assets`，覆盖全部保存状态；它们是责任记录，不能相加作为对象总数。未知历史不触发全库归属扫描或成为新切换门槛。

探针在数据库锁外、既有维护许可内执行条件 PUT、非空条件 MPU、完整读回与准确清理；能力字段为 `conditional_write / full_read / delete / empty_put / multipart`。成功后签发五分钟凭证，独立 audience 绑定原操作者及 Key、用途代次、目标／连接配置、维护代次和历史摘要。确认重新授权，按 Connection→Location→Binding 锁序复核并写入 `storage_binding.update` 审计；过期、跨用途或重复确认、目标变化、权限撤销及数量变化都拒绝，需要重新预览。失败或不明收尾不会改变绑定。

仅后续新分配使用目标。独立 Task 原件、持久 RLE 对象、评论附件和指南图片按保存地址继续读写，已有及在途 A 不因当前绑定 B 失效；正常凭据轮换不改写保存引用。新 schema2 内部引用固定十字段及完整物理 key，外部根为连接基前缀下 `_aap_managed/annotations/`，公开逻辑键保持。普通几何、Mask 格式暂存与缩略图分别保留数据库、import、media-cache 责任。此接口不搬迁历史对象、不扩大清理权限，不提供跨目标恢复资格。恢复顺序见[标注文件部署说明](/ops/deploy/storage-recovery#标注文件用途切换的部署顺序)。

## 训练文件用途切换

仅当前有效超级管理员可修改，API Key 要求 `datasets:write`；候选 GET 要求 `datasets:read`。三个入口为 `GET /bindings/training/candidates`、`POST /bindings/training/change-preview`、`PATCH /bindings/training`。候选限定默认托管位置或已登记且启用的全局 S3，分页 `limit` 默认20、最多200，`offset` 默认0。`TrainingBindingChange` 提交 `expected_generation / target_location_id / target_config_revision`；确认额外提交 `impact_token`，请求不能任意指定桶或前缀。用途返回 `write_policy=current_binding`、`change_policy=reviewed_training_switch`。

`TrainingBindingPreview` 返回 `previous / target / history / capabilities / can_apply / measured_at / expires_at / impact_token`。目标沿现有摘要公开位置、名称、修订、桶和受控前缀，不返回端点或凭据。`history` 固定六项：`training_versions / training_runs / training_previews / sample_evaluations / active_learning_rounds / deletion_intents`，包含全部保存状态和未固定历史。这些是不同责任记录，不能相加作为物理对象总数；不要求先全库接管。

真实探针在 SQL 锁外、既有维护许可内验证微小普通 PUT、非空 MPU、完整 SHA 读回、精确前缀 LIST 和批量 DeleteObjects／收尾。`capabilities` 为 `write / multipart / full_read / list / delete`，不额外要求条件写入、空文件、版本化桶、历史版本列举或提供方性能资格。失败或未知收尾不更新绑定。预览凭证五分钟有效，独立 audience 绑定原操作者／Key、用途代次、位置／连接修订、Gate 和历史摘要；确认重新授权并按 Connection→Location→Binding 锁序复核、写入 `storage_binding.update` 审计。重放、竞争变化和撤权拒绝后须重新预览。

只有后续新分配采用新目标，涵盖五类 owner；模型与 attempt 沿来源 owner，不另分配。已有、在途、重试和续训使用保存地址，当前凭据轮换不改冻结内容、首次期限或保存引用。历史 NULL 的五个 `storage-pin` 入口仍显式核验已登记 legacy A，不跟随当前 B，不由普通 GET 自动接管。切换不迁移旧文件、不新增清理权限；[训练文件合同](./training-datasets#训练文件的新分配位置)和[部署恢复边界](/ops/deploy/storage-recovery#训练文件用途切换的部署顺序)分别说明读取与部署限制。
