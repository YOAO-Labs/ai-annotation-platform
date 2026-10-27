---
title: 对象存储迁移、站点 Agent 与容量观测架构
audience: [dev, ops]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 对象存储迁移、站点 Agent 与容量观测架构

本页解释平台的对象存储受控迁移架构（配置源清单、可恢复复制计划与增量批次、迁移维护窗口、原生 MinIO 源端写封闭、已封存读取映射、九用途原子切换与回滚）、跨网络只读站点 Agent 与显式同内容引用副本，以及平台对象六分类占用盘点、增量账目与窗口观测率。

- 连接器、受控存储位置与九用途映射见 [存储位置、连接器与用途绑定](./storage-connections)
- 数据集内容身份、目录来源扫描与增量同步见 [数据集、目录来源与增量同步架构](./dataset-sources-and-sync)
- 统一原件读取、按需缓存与保留回收见 [原件验证读取、缓存与保留回收架构](./original-access-and-retention)
- 面向使用者与运维的操作手册见 [对象存储迁移准备](/user-guide/datasets/storage-migration)、[跨网络站点与 Agent](/user-guide/datasets/site-agents)、[存储诊断与容量账目](/user-guide/datasets/storage-operations) 与 [迁移清单与切换运维](/ops/deploy/storage-migration-inventory)

## 配置源迁移清单与可恢复复制计划

### 配置源迁移清单任务（迁移 0255）

`storage_migration_inventory` AsyncJob 将只读清单核心用于应用执行；核心位于 API 服务模块，运维脚本保留兼容入口，不从 Worker shell 启动 CLI，也不复制一套 LIST／版本／SHA 算法。固定范围为部署端点和九用途桶映射，当前绑定及在途写入仅记录为观测，不能据此证明已停写或全业务覆盖。其他位置的新绑定不使配置源清单失去启动资格。

`storage_migration_inventories` 保存冻结范围、当前授权证明、预算、领取／取消状态、32 MiB 以内的私密报告 BYTEA 及 SHA。单记录和总输出都在写入前约束，公开 DTO 与通知只返回白名单摘要；报告经独立当前超级管理员与全权 Key 校验下载，GET 不访问源。报告沿现有终态 Job 30 天保留及删除级联，不引入新 S3 对象用途或无限保留。

Worker 对保存 task ID 仅作一次 `pending → running` 领取，显式重投只针对 `pending`，不按 TTL 接管运行中的原生读流。读取在无数据库锁的受控线程中执行，权限／Key、取消和冻结源范围在检查点及最终发布复核；数据库 Session 只在所属事件循环使用。协作停止穿透核心 issue 捕获路径，SDK／Body／输出真实关闭后才发布终态，未封存或关闭不明不提供成功报告。已封存的 partial 报告、未知覆盖、取消和失败分开表达，`migration_ready` 始终 `false`。

### 应用侧初次复制（迁移 0256）

`storage_migration_copy` 使用同一 27 家族保存业务引用目录及复制核心，先从保存的封存报告生成配对目录及 SQLite 计划，审阅后再执行初次复制。0256 的复制 owner 独立于单次 AsyncJob，保存冻结输入、目标物理身份、处理选择、基线及摘要；逐对象 delta 在独立短事务中提交。执行时恢复同一基线＋delta，`writing/nonce` 先于 PUT，ACK 版本先于完整读回，权限或取消的晚期变化不删除已发生事实。

目标为已登记全局 S3 位置／连接，由薄适配器复用 `S3Access` 当前凭据、allowlist 与请求 guard，在保存 `base_prefix` 下生成独立迁移命名空间；不借用业务 purpose 或改变 Binding。仅轮换密钥不会重解释旧地址，运行中修订变化停止下一次 I/O，后续 attempt 重新解析当前配置。新 owner 计入位置引用保护与维护影响，任务终态和 30 天清理不解除实际保存责任。

源与目标分别取得 Permit，两个 operation ID 先提交，再启动原生线程；回到所属 loop 的短 Session 处理检查及对象 delta，不跨线程共享 Session，不跨 S3 I/O 持有事务。流、客户端、SQLite 和临时正文真正关闭后才发布；应用正文使用匿名临时文件，检查点权威在数据库。未知关闭保留责任，不按超时清除。显式续跑创建新 attempt，共享 Job 只描述该次执行。

### 复制计划与增量批次（迁移 0257）

迁移复制复用已登记的全局 S3 连接；`StorageMigrationCopy` 独立于短期 AsyncJob 保存源报告、配对目录、初始检查点和逐对象 delta。每个后续批次保存唯一父引用、批次号及父逻辑证据摘要，继承同一目标、源范围、用途声明和预算。父批次在短事务锁内确认没有活动 attempt 或未收尾许可，物化并计算稳定摘要后才能预留唯一后继；取消摘要计算时仍等待原生线程关闭，不能提前释放冻结锁。

新检查点自包含当前对象、祖先创建身份及去重历史。复用行保留原创建 UUID、目标键、nonce 和准确回执，状态从 `pending` 开始；完整读回准确版本才计入本批次 `verified`。没有准确应答的上传沿原 nonce 恢复，不能另建身份重传；可变源重新复制，显式源版本的内容身份冲突保持独立拒绝。delta 物化同时校验创建 UUID、目标命名空间及既存 nonce，后继不能借祖先身份写任意目标。

原批次一旦存在后继即只读，详情和私密检查点仍可读取；清理原 Job 不删除 lineage 或复制事实。检查点的逻辑摘要与 SQLite 页面布局无关，用于校验封存证据的一致性。操作见[迁移清单手册](/ops/deploy/storage-migration-inventory#后续清单与增量批次)。

## 维护窗口、源端封闭、读取映射与原子切换

### 迁移窗口的专用来源准入（迁移 0258）

`StorageMigrationWindow` 独立保存默认来源范围、配置修订、父复制计划和目标身份，不依赖短期 Job 保留。开启、停止和关闭使用窗口修订 CAS，并与同一来源 Gate 的准入锁串行：`draining` 拒绝普通准入，登记操作归零后才可停止；`closing` 拒绝新窗口读取，实际排空后才恢复 `active`。普通 `storage_access.validate` 仍只接受 `active`。

Inventory 和 Copy 的 nullable 窗口引用保留旧任务兼容。窗口任务不能仅凭 purpose 字符串取得例外：独立准入事务联查保存的任务／Attempt、task ID 和运行状态、窗口修订、固定源身份与停止 Gate，再登记原有 `StorageOperation`。目标始终走普通许可；来源客户端按冻结端点／桶和 SDK 只读方法白名单限制能力，原 Body、client、原生线程、未知关闭及晚到回执责任继续由既有 owner 承担。

窗口只证明登记访问的控制与排空。外部预签名、未登记 SDK 调用、相同物理桶的其他逻辑位置和提供方自动过期不受该 Gate 证明，公开结果固定保留 `source_quiescence_proven=false, migration_ready=false`。

### 原生源端写入封闭

独立运维命令 `storage_source_fence.py` 将耐久控制、Docker 观察／操作和终端入口分开。私有日志保存配置摘要、CAS 修订、操作 UUID、原生命周期规则和准确进程屏障回执；所有原生修改前先保存意图。正常状态读取重新核对唯一入口配置、源与入口进程代次及当前后台写入资格，中断不自动恢复写入。固定拓扑的拒写入口配合完整源进程退出，处理已签发 URL 和已经接受的请求；数据库 Gate 本身不能承担这一职责。

当前适配器限定本机回环 HTTP、单实例固定 MinIO 构建及精确登记的私有 Docker 拓扑。宿主机／Docker 管理员为可信运维边界，实际资源漂移会使证明失效。该命令不写应用数据库，不授予窗口专用读取权限，也不把其 JSON 导入为应用停写证明。部署、资格核对与显式恢复见[MinIO 原生源端封闭](/ops/deploy/storage-migration-inventory#minio-原生源端封闭)。

### 最终清单与复制封存（迁移 0262）

可信本地运维命令将原生 Journal 的耐久应用 claim 与数据库最终核验记录绑定。原生锁和 claim 阻止独立解封；应用以递增后的窗口修订约束新 Inventory 和 B5 后继，不给 API／Worker 增加 Docker 或源 root 权限，也不提供导入原生 JSON 证明的 HTTP 接口。绑定先固定源、Gate、配置、父批次和目标，并确认旧任务与许可已经收尾。

最终封存先以短事务冻结准确后继批次，在事务外重新物化 baseline 与全部 delta、核验完整源清单／目录摘要和逐对象决策，再在 Journal 锁内实际核验原生状态，以短 CAS 保存封存摘要。最后一次任务必须是包含复用目标版本的完整 `verify`；准备成功或普通复制子集成功不能替代。历史目录未知、旧 issue／multipart 及 previous-only 证据保留，不与当前失败混算。封存仅表示这次观测与核验，现有 `migration_ready=false` 和原报告原文保持。

窗口关闭、原生解封和冻结批次的新尝试／后继均服从最终核验责任。晚到物理 delta 继续持久保存，若改变已封存证据则进入待解除状态；不能为维持绿色状态丢弃回执。显式解除先阻止新任务、等待真实收尾并提交数据库失效，再移除原生 claim；原生恢复写入仍是另一次明确动作。命令与恢复步骤见[最终增量封存](/ops/deploy/storage-migration-inventory#绑定窗口并封存最终增量)。

### 已封存的原件读取映射（迁移 0263）

`StorageMigrationReadPublication` 与不可变 Route 保存已 sealed 全量 Copy 的准确映射，发布 ID 沿用 Finalization ID。可信本地运维在 Journal 锁内前后观察真实栅栏，事务外物化 baseline 和全部 delta、重算最终证据，再以短 CAS 事务发布有界映射；不接收 HTTP 证明 JSON。当前清单内已完整 verify 的历史源版本可发布，previous-only 历史、retain、rebuild 和 marker 保留原事实。

`OriginalSource` 继续保存逻辑 A 的位置、内容身份和关系事实，只附独立不可变的 `read_route`。`PhysicalReadPlan` 选择实际 B 位置及 Gate 修订，以 `migration_read_<publication UUID hex>` 登记实际许可；原业务授权、源关系和冻结 receipt 继续复核。普通内容身份读取以完整逻辑地址及 SHA／大小匹配，同内容的多个源版本按 latest 优先及对象 ID 稳定排序；旧创建 `VersionId` 不变成新的读取条件。

reader 的两次 HEAD、条件探测和 GET 均固定目标回执 `VersionId` 及 ETag，完整 SHA／大小保持原合同。当前目标连接、物理身份、发布修订及 Gate 在准入和交付边界重验，凭据从当前连接获取；SDK 响应不伪装成源 A。活动或撤回中的 Publication 独立保留 Copy 责任，并禁止普通 Finalization 解除。撤回先提交 `withdrawing`，禁止新路由 I/O，实际所属许可全部结束后提交 `withdrawn`；历史不删除、同一发布不重新激活。晚到物理回执照常保存并使活动发布进入 `withdrawing`。

保存产物可显式使用 `storage_migration_artifact_reads` 只读接缝：按原地址及保存 SHA 选择准确映射；有独立大小或保存源版本时同时限定。缺少独立大小的冻结成员可使用映射的已核验大小，不从同名键或今日 Binding 猜测。适配器只开放指定对象的 HEAD、完整 GET 和 GET 签名，固定目标版本及 ETag；验证目标属性和迁移创建元数据后才向原业务核验交付内容。实际 B 同时登记发布许可和原 owner 专属读取 purpose，供撤回与既有回收查询排空；写入、写后核验和删除不经过此接缝。直接 S3 签名在其原有效期内独立于应用许可，撤回不会撤销已经签发的 URL；旧入口的观察期须覆盖已有 A 签名的自然失效时间。

头像和报告附件把保存源版本与实际响应目标版本分开核对；审计归档仍完整解压并校验 JSONL 行数和内容。RLE 路由采用 stored SHA／大小，canonical 身份独立；持久 Publication Guard 继续保存 A Gate 修订，内存路由单独复核 B 发布与实际许可。独立导出媒体冻结同一发布及物理修订的完整 read-set，最终业务发布持共享锁复核。`prefix_inventory` 只读投影同一封存源目录中当前可见的逻辑键及 SHA／大小；仅当源桶全部选择 `migrate` 且输入摘要仍与发布证据一致时，完整 copy 映射才能提供此集合。

Mask 导入暂存以保存的完整定位及预期 SHA 选择单对象读取计划，完整下载、配额及临时文件关闭后复核 Publication；preflight 最终事务保持同一路由核验。terminal 训练日志复用产物只读接缝，先完整核验大小、SHA 和执行元数据，再返回有界脱敏内容。Creation、`PredictionInputLease`、训练产物创建和 export building 仍保存原写入责任。

### 九用途事务与回滚责任（迁移 0264）

`StorageMigrationCutover` 以 Publication ID 为耐久组身份，固定已发布映射摘要、源窗口及前后九用途绑定向量。运维 CLI 事务外执行各用途原有探测，短期签名 review 覆盖当前管理员、全部代际、配置和 Gate 修订。服务同事务比较并更新九行、创建组记录；不调用九次单用途 apply，不改已有 owner 保存地址。连接、位置、绑定及控制责任按一致顺序加 `NOWAIT` 锁，冲突不产生部分切换。

组状态为 `active → rollback_draining → rollback_bound → rollback_restoring → rolled_back`。B 排空后用独立 Journal 封闭原生写入；rollback claim 先 fsync，再提交 B Gate 停止及九项 A 地址恢复，代际仍递增。A release UUID 先持久保存，再撤回读取发布、解除 Finalization、解绑 A claim、实际恢复 A 入口并关闭 Window。最后复核完整回滚向量及 B 仍 held，原子恢复 B 应用读取准入。B 物理仍只读、claim 继续保留；应用 Gate active 不代表 B 可写，既有 B pending owner 不能跟随 Binding 重定向。

B claim 采用独立闭合 schema，与 A application claim 互斥；首次绑定提升 Journal schema，旧命令不能忽略新责任直接解封。两端配置使用不同私有父目录及锁，凭据按实例独立选择。B 应用入口仅从完整核验的唯一 gateway/frontend 网络推出，不接受用户别名或内部 MinIO 地址。有组责任时禁止降级移除该表，回滚完成也仍承担 B 只读责任。成功切换后的正向观察期核对与旧源实例退役留档见[迁移运维手册](/ops/deploy/storage-migration-inventory#九用途原子切换与新写入回滚)。

## 只读站点 Agent 与显式引用副本

### 只读站点 Agent 架构（迁移 0265–0266）

`kind=agent` 使用站点身份、`root_handle`/修订和位置相对路径，实际根仅保留在独立 Agent（`apps/storage-agent`，共享协议包 `apps/_shared/site_agent_contract` 与安全读取原语 `apps/_shared/filesystem_read`）。中心仍持有来源、AsyncJob、扫描 generation／租约、登记记录和内容授权。Agent 不监听入站端口，仅发起出站 HTTPS 请求（`/api/v1/site-agents/pair` 与 `/api/v1/site-agents/commands/{poll,chunks,result}`）。

Agent 执行本地受控 I/O，传输命令的 `received` 与业务 `applied` 分开；扫描 checkpoint 和业务 ACK 同事务，探测结果先复核当前权限再保存和确认。PostgreSQL 保存命令及关闭证据（`site_agent_commands`），有界 Redis 只中继临时分块（单块最多 `256 KiB`）。读取先在 Agent 完整计算 SHA 并复核来源生成 `0600` 本地 stage（单次最多 `64 MiB`），中心再次核验完整内容后进入既有 Snapshot 图片检查。中心命令携带可选带宽上限，与本地配置取较低值。离线不删除索引，撤销或过期凭据只允许旧命令向结果接口回报关闭证据（`retired=True`），不允许领取新任务或上传新字节。部署和边界见[站点 Agent](/ops/deploy/site-agents)与 [API 指南 · 只读站点 Agent](/api/guides/site-agents)。

管理界面沿现有存储页增加超级管理员专属视图，查询按登录会话隔离；配对秘密只归当前弹窗持有，不进入查询缓存。位置与 Agent 列表的 `agent_id` 过滤先保留权限，再计算总数和分页。探测按 `agent:<UUID>` 保存和返回，中心 API／Worker 身份不替代实际执行站点。

### 引用图片的显式副本（迁移 0267）

`DatasetReferenceReplica` 保存同数据集、同 SHA-256 与大小的定向 Asset 关联，包含优先级、创建者与撤销记录。数据库约束保护关联身份，已撤销记录不能重新启用；新增关联不改 Item／Task／已接受修订或历史版本。候选发现只是管理端元数据查询，不能按摘要自动授予读取。

统一 `reference_snapshot` 先检查 A 业务权限并尝试 A 完整缓存，随后按显式关联选择物理来源。管理员直读继续要求 B 来源管理权限；既有任务授权可通过管理员发布的准确 `A → B` 关系委托物理读取，不构成 B 枚举或传递授权。实际 I/O、维护责任和故障观测归 B，业务身份及冻结血缘仍归 A；交付前复核两端及关联。每个候选完整捕获和校验，Range 只截取最终同一完整快照，新缓存留存取 A/B 策略更严格者。

## 容量观测、增量账目与健康诊断

### 平台对象占用快照与六分类归属（迁移 0209、0242）

`GET /api/v1/storage/buckets` 与 ML 集成 overview 读取 PostgreSQL `storage_usage_snapshots`，不执行 S3 HEAD/LIST。仅显式 `POST /api/v1/storage/usage-inventory` 开始盘点；当前有效超级管理员与全权 API Key 可读/启动/取消。九用途复用 `PURPOSE_BUCKET_ATTRIBUTES`，按 endpoint、TLS 和桶名固定统计身份，共用桶合并 `roles`。

`storage_usage_inventories` 保存单例活动轮次、内部授权证明、分页 token、累计计数、租约及执行代次。每页最多 1,000 个对象，每段最多 10 页或 30 秒，段间延迟 1 秒；每桶最多 10,000 页、整轮最多 24 小时。每页 I/O 在数据库锁外完成，发布前重新锁定并核对任务 ID、代次、租约、账号/Key 与配置；整桶分页成功才替换该桶成功值。Celery `default` worker 执行盘点，Beat 每 30 秒回收超过 120 秒租约的任务。

每页 LIST 后，平台以一个 SQL union 查询已保存的准确定位或候选主键，核对保存的端点身份、桶及准确键／归属前缀，将实际字节归入以下六类：

| 分类             | 保存的归属依据                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------- |
| **托管原件**     | 原件创建责任、Dataset 原件定位／保留账本、`TaskOriginal` 和辅助原件的当前独立定位。      |
| **预览与缓存**   | 来源预览、完整原图缓存、图片金字塔、视频派生和训练预览的保存归属。                       |
| **训练版本文件** | `TrainingDatasetVersion` 整个已登记前缀及其删除意图，包含准备中、各 attempt 和保留副本。 |
| **临时产物**     | 创建暂存区、预测输入、导出 ZIP 及独立媒体交付。                                          |
| **其他平台对象** | 训练实验／评估／选样、头像、报告／评论附件、指南图片、原生 Mask 和审计归档。             |
| **未归类**       | 缺少一致归属或存在跨分类冲突的实际对象。                                                 |

同类重复声明合并，跨类冲突只在未归类计一次；大小仅取 LIST，不取业务行或额度预约。分类累计随分页检查点提交，整桶成功后与对象总数、字节、采样时间一起写入 `storage_usage_snapshots.category_totals`。

### 当前对象增量与周期核对（迁移 0246）

`storage_usage_mutations` 在物理 I/O 前由独立 owner 事务保存幂等意图，覆盖受控写入与删除生产者：

- **写入生产者**：涵盖 `prediction_inputs`、`managed_original_creation`、媒体缓存四类输出（`thumbnail_outputs` / `original_cache` / `image_pyramid_outputs` / `video_derivative_outputs`）、`export_media_outputs`、`export_zip_outputs`、五类条件写入（`bug_attachment_uploads` / `avatar_uploads` / `comment_attachment_uploads` / `guide_asset_uploads` / `raster_mask_objects` / `audit_archive_outputs`）以及服务端训练四类写入（`training_dataset_outputs` / `active_learning_media` / `sample_evaluation_reports` / `training_redacted_logs`）。计量写入使用独占单次客户端，只有单次明确成功、不可变非 `null` 版本、完整 SHA/大小读回与当前 HEAD 一致且原生资源与 permit 真实收尾后才确认净变化。
- **删除生产者**：涵盖准确版本删除（`export_media_removals`、`thumbnail_removals`、`avatar_removals`、`bug_attachment_removals`）与按 key 删除标记观测（`original_cache_removals`、`image_pyramid_removals`、`video_derivative_removals`、`export_zip_removals`）。
- **基准对账与周期计划**：整桶盘点成功保留两个成功代次，数据库按对象比较两次观测，只有完全位于观测之间的已确认事件参与增量核对；未接入写入方和外部变化单列为未解释变化，生产者摘要保持 `partial`。周期计划存于 `storage_usage_schedules`（间隔 1 至 168 小时），默认关闭，由 30 秒 recover 任务复核授权并调度。

### 任务推进观测（迁移 0259）

`AsyncJob.last_progress_at` 与 `progress_observation` 记录五类存储任务（扫描、登记、容量盘点、迁移清单、迁移复制）的实际工作推进。观测 helper 在原 owner 围栏通过后核对当前运行作业与 task，仅新 marker 增加序号和时间；`updated_at`、百分比、lease 与权限检查均不是进展证明。前端只对运行中有效观测达到 900 秒（15 分钟）显示核实提示（处置见 [Runbook · Celery Worker 卡死](/ops/runbooks/celery-worker-stuck#存储任务的无进展提示)）。

### 来源故障状态（迁移 0249）

`storage_fault_states` 是独立于对象占用、业务许可和保留账本的稀疏观测表。现有来源扫描、核验／原件读取和预览生成在实际 I/O 前取得数据库观测序号；故障去重、次数和水位更新与转换审计同事务，成功仅更新已有匹配状态，不为所有健康文件建行。恢复必须位于现有最终验证／业务提交之后；原图缓存命中不证明源在线。观测通过当前调用者 Engine 的独立有界连接执行，不提交调用者业务、不持有父 Location／Source 锁。

### 原图与预览的窗口观测率（迁移 0261）

`storage_observation_rates` 为实际严格回源和新建预览提供独立的完成尝试计数。`scan_snapshot` 与 `reference_snapshot` 的回源阶段建立内存尝试；成功在现有最终身份、内容、Key 及提交核验后结算，缓存命中不计。预览在 `directory_previews.prepare` 真正 encode/create 前建立尝试，原登记最终 commit 成功后按发布的 `ready/failed` 一次结算。

迁移 0261 新增无父外键的 `StorageObservationBucket`，主键为完成时 UTC 日期、Location、Source 和 metric，仅保存成功／失败数与首次／最近时间。独立事务以原子 UPSERT 累加（锁等待 250 ms、语句 1 秒、协作预算 2 秒）。现有 `faults` GET 在独立只读短事务中以同一 SQL 的服务器时间和当前来源 ACL 汇总当日 UTC 窗口；零分母比例为 `null`，覆盖为 `recorded_attempts_only`。
