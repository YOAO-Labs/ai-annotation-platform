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

## 用途映射策略

`GET /bindings`只读返回九种平台用途，即使共桶或未配置仍逐行保留，不初始化配置、不探测对象存储。每行包含`purpose / location_id / bucket / generation / state`，并提供：

| 字段                       | 当前值与含义                                                                                               |
| -------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `location_config_revision` | 关联位置的实际配置修订；无关联位置为null，与用途绑定`generation`独立。                                     |
| `read_policy`              | `consumer_owned_compatibility`：各业务负责对象读取；已固定位置的对象按记录读取，其他入口保留既有兼容路径。 |
| `write_policy`             | `deployment_mapping`：新写入采用当前兼容部署映射，不表示引用来源适配器获得写权限。                         |
| `change_policy`            | `legacy_mapping_locked`：当前用途映射锁定，受控绑定切换尚未开放。                                          |

`state=configured`只表示兼容部署映射匹配，不证明位置在线、对象可读、历史定位全覆盖或迁移完成。`missing / mismatch`保留其原有含义；策略字段描述当前路由规则，不提供写入或迁移许可。客户端遇到未提供的新字段应显示未知，不能从位置读取能力推断平台写策略。权限仍为当前超级管理员与`datasets:read`。

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

`GET /storage-locations/{location_id}/maintenance-impact` 要求当前有效超级管理员和 `datasets:read`；返回前再次检查账号、API Key及位置修订。仅支持 filesystem 或连接支持的外部位置，平台用途位置返回409、`platform_maintenance_impact_unsupported`；查询期间修订变化返回409、`storage_location_revision_changed`。无写入、审计追加、源I/O或任务投递。

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

`frozen_coverage=recorded_location_lineage_only` 和 `input_coverage=current_task_links_only` 显式限定归因；缺位置血缘的历史、托管冻结和失去Task关系的输入未计入。输入按持久状态计数，包含已到期但尚未转入清理的active记录，不代表运行作业或有效下载凭证。`execution_permission=false`，本响应不生成impact proof，不可作为停用/归档/回收的提交许可。位置修订更新可能使旧扫描、同步、预测上下文及完整缓存失配，各owner在自己的检查点复核；任务人员授权、预览和已保留冻结内容有独立合同，详见[维护影响](/user-guide/datasets/storage-connections#位置维护影响)。

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

清理返回202，仅将截止观测前已验证的、具有精确归属的可重建完整缓存封存为待删除；Entry锁内复查并跳过活跃读取，不强清上传中代。响应包含 `retired_count / skipped_leased_count / remaining_ready_count` 和当前用量。后台周期任务执行实际删除，删除失败、目标绑定变化或不确定写入保留责任时继续计费，可再次查询用量。它不在请求内等待对象删除，也不提供任意对象键参数。

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

| 方法 | 路径                                 | 行为                                               |
| ---- | ------------------------------------ | -------------------------------------------------- |
| GET  | `/api/v1/storage/buckets`            | 只读成功快照与最近尝试，不执行 HEAD/LIST           |
| POST | `/api/v1/storage/usage-inventory`    | `202 {job_id, reused}`，复用现有活动任务或创建盘点 |
| POST | `/api/v1/async-jobs/{job_id}/cancel` | 合作式取消，保留成功值及已完成桶                   |

每条 `items` 是一个 endpoint 下的唯一桶：`name`、`roles`（多个用途）、兼容字段 `role`（首用途）、`object_count`、`total_size_bytes`、`measured_at`、`measurement_started_at`、`attempt_status`、`attempted_at`、脱敏 `error`。`source=s3_list_objects_v2`、`scope=current_objects`、`coverage=none|complete`、`is_estimate=true` 明确观测范围。`status=unknown|ok|stale` 表示快照新鲜度，不代表存储健康。

首次无成功值时计数和时间为 null；真空桶成功值为零。超过 24 小时或最近失败/取消为 stale，继续返回原成功值和原时间。`measured_bucket_count/bucket_count` 表示覆盖；只有全部桶都有成功值时才返回 `total_object_count/total_size_bytes` 合计，否则为 null。`physical_total_bytes`、`physical_available_bytes`、`quota_bytes` 仍为 null。`inventory_job_id/inventory_status` 指向最近盘点；任务过期清理后 ID 可为 null，但成功快照保留。

响应的 `breakdown` 给出同一平台范围的对象分类快照。`categories` 固定包含 `managed_original`、`preview_cache`、`training_version`、`temporary`、`other_platform`、`unclassified`，每项含 `object_count/size_bytes`，取实际 LIST 大小。同一 endpoint／bucket／key 只计一次；跨类别归属冲突计入 `unclassified` 并增加 `conflict_object_count`。

`breakdown.measured_bucket_count/bucket_count` 是拥有有效分类快照的桶覆盖；`sampled_object_count/sampled_size_bytes` 是这些桶的小计，全部桶有有效分类后 `total_size_bytes` 才非 null。`coverage=none|partial|complete` 同时表达桶缺测或未归类对象：全部桶已测但仍有未归类时为 partial，实际字节合计仍可已知。`measured_from/measured_through` 表示采样窗口，`has_stale_data` 表示含过期成功值。旧快照或旧检查点缺少分类证据时不从桶角色补造，原对象总量继续有效。旧服务响应也可能没有 `breakdown`，客户端应按未知处理。

固定口径为 `scope=configured_platform_buckets_current_objects`、`source=s3_list_objects_v2_and_saved_ownership`、`deduplication_basis=endpoint_bucket_key`、`includes_versions=false`、`is_estimate=true`。分类不返回对象地址或逐对象清单，不证明内容完整性，也不授予清理权限；外部源规模不纳入。分项与桶总量属于同批对象，不得重复相加。

调用方兼容说明：此 GET 已从实时统计变为快照读取；应处理 nullable 计数和新增状态，不能将未知值渲染为零或依赖每次 GET 重新盘点。ML overview.storage 使用相同结构。按桶去重并完整覆盖九用途，不能按角色重复相加。后台任务取消、崩溃与授权/配置变化不会发布未完整桶的累计数。更多口径和预算见[桶布局与快照](/dev/reference/storage-buckets#平台对象占用快照)。

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
