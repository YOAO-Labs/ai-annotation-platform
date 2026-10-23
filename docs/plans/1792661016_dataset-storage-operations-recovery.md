# EPIC-12：运维观测、容量口径与备份恢复

> 状态：approved；E12-A1/A2、E09-B1/B2 与 2026-10-24 的 E12-B1 离线隔离恢复试点已完成；其余切片仍待独立复核，不分配发布版本。
> 晋升基线：`5b764eaa5`；工作树干净，保留 `1822b025369a33f4` 的 dev 人工验收环境，不重建后端镜像。

## 本轮定稿：E12-B1 离线一致备份与隔离恢复演练

2026-10-24 基于 `2a18af83b` 重新核验，工作树干净。整套实施已获用户授权；本片补齐 E12-S06 的本地方案与试点，不将其等同于生产备份、在线/PITR、保留保护或生产 RPO/RTO。现有工作树归属/迁移验证复用；现有分步 dump/mirror 不是一致恢复证据。3100/8100、两 Worker、beat、业务库及镜像均保留。

```text
new random owned source -> real synthetic asset/label/freeze + interrupted task
             | offline, no API/Worker/Beat, exact owner / no clients
             v
one backup_generation -> PG custom dump + all 9 purpose objects + SHA/metadata
                       -> configuration/heads/frozen facts + separate key material
             | verify complete archive, then destroy source resources
             v
new random owned empty target -> full PG restore -> explicit physical mapping ledger
                              -> object restore + key + immutable business verification
                              -> unfinished work / missing external original held for review
             v
bounded timing/report -> clean both owned namespaces and private artifacts
```

只接受脚本本次新建的两个随机 test root，无指向既有环境的参数；由 LocalResources 验证本机服务器、DB comment、九桶 tag 和 Redis label。先检查全部归属，锁模式，未知客户端或非空目标拒绝；不强断连接、不创建/修改共享PG roles。目标只创建空库，full restore 前不执行 Alembic upgrade；恢复后验证迁移头再授权runtime。源无运行服务，合成数据准备完成后零客户端；清单完成前复核数据库和对象集合不变，来源停写是本试点的进程隔离条件，不能宣传为生产停写门禁。

备份目录0700、文件0600；统一代次包含代码提交、schema/head、数据库dump SHA、九用途映射、逐物理对象key/大小/流式SHA及读取所需metadata、外部原件保护责任、未完成作业和冻结事实。总对象/单对象/总字节/dump设明确预算；S3分页，不以ETag替代SHA。首版仅保护当前对象；遇版本化、删除标记或未完成分片拒绝发布。备份最后原子写完成标记；缺对象、读中变化、中断或篡改没有成功代次。密钥独立受限文件保存，仅合成演练专用Fernet key，不复制真实.env，不写公共清单/日志/Git；缺失/错误key必须阻止恢复验证。

恢复到独立九桶，不能仅换环境变量。只在恢复目标DB按白名单变换 StorageBinding、平台 DatasetAssetLocation、SourcePreview、OriginalCacheEntry、PredictionInputLease、TrainingStorageDeletion 的物理桶/存储身份，并保留变换账本；记录未知/不匹配绑定须拒绝。源与目标使用独立配置子进程，原dump、业务ID、Asset/SHA、采纳修订、snapshot/manifest/package及来源证据不得改写。容量观测保留其原位置/时间身份，不冒充目标实时统计。旧Redis队列不恢复、不启动调度；未完成版本/作业记录保留并报告待核对，不能自动重新执行含历史物理locator的冻结任务。B1不开放恢复应用流量；后续上线门禁须覆盖所有异步owner及外部映射。

实际演练须在备份后销毁源，证明目标不借源桶/源库：恢复标注—样本—Asset/修订链，校验ready版本所有原图、标签、清单、ZIP和Native输入；部分冻结保持未完成。合成外部原件移除后索引仍保留、实时严格读取拒绝，同名不同内容替代也须拒绝，ready保留内容独立可验。包含缓存与临时输入/删除账本的目标映射，证明不再指向源桶；缺key、坏dump/对象、owner不符、目标非空、预算和故障阶段清理设有意义回归。报告已知零写入窗口和阶段用时，只说明本次规模。

准确范围超过8文件，跨工具/DB与存储合同/运维文档：新增 `scripts/storage_backup_bundle.py`、`scripts/test_storage_backup_bundle.py`、`scripts/verify_worktree_recovery.py`、`scripts/worktree_recovery_fixture.py`、`scripts/worktree_recovery_mapping.py`、`scripts/test_worktree_recovery.py`；必要复用现有helpers，不修改生产API/Worker/依赖/schema。新增正式 `docs-site/ops/deploy/storage-recovery.md`，更新部署两页、运维索引、DEV、README、CHANGELOG、本计划及整套队列。纯脚本unit命令与真实 `apps/api/.venv/bin/python scripts/verify_worktree_recovery.py` 分开记录；Ruff/格式、文档frontmatter/导航/可读性/构建、diff及提交钩子。无UI变更不重复浏览器矩阵。

回退移除演练入口及工具提交；所有源/目标为本次随机owner资源，finally按原归属销毁，不触碰dev。失败清理不能冒充成功；若资源清理失败保留private清单和具体owner供再次核对，不强删未知资源。归档所有正常和注入故障报告的脱敏摘要到Outcome，删除任务临时文件后提交；整项E12仍保持活动。

## 本轮定稿：E12-A2 / E09-B2（已交付）

2026-10-23 用户继续授权；基线 `08d5d5ed2`，工作树干净。本轮交付位置内来源诊断，不新增数据库表、任务或环境变量，不重建后端镜像。

```text
Location modal -> source-summary GET -> current-authority source page
                                      -> reference observations + preview records
                                      -> active/latest scan and preflight + last complete
              -> dataset/source/scan links + shared AsyncJob modal
```

- 只聚合本页授权来源的 `DatasetReference` 业务样本，含保留的历史修订；托管复制和未登记路径不计入。原件复用 Workbench 六态及观测时间范围，明确是历史观测，不是实时健康检查。
- 预览独立计数：当前 Item/Asset/SHA、SourceItem 与规格匹配的 ready/failed 记录，无匹配为 unrecorded；策略关闭和来源停用分别计数。当前内容可合法复用已有预览；不要求策略修订相等。ready 还核对当前平台绑定，只表明生成记录，不证明对象此刻存在。
- 扫描、预检分别展示活动优先的最近轮次，以及最近完整成功轮次；恢复旧轮不能被新失败记录遮住。完整性、时间、配置修订匹配分别表达；预检不依附于最新扫描。配置不匹配的历史原件观测不改写成当前健康。
- GET 只查数据库，不读取文件、HEAD/LIST 对象、探测节点或生成预览。页内批量 SQL，无逐来源查询。继续沿用 manager、位置和来源授权及 `datasets:read`；只返回白名单字段，配置、路径、证明和原始错误不外露。
- UI 来源列表占满宽度，原件/预览/扫描分开，细项和任务历史折叠，活动任务和失败摘要可见。保留独立分页、数据集/来源/扫描深链与 return、居中任务详情、Escape 和焦点恢复、切换账号及撤权隔离。
- 文件范围（超过 8 文件，跨 API/Web/docs，以共同只读合同提交）：`app/services/{dataset_workbench,storage_source_summary,storage_source_diagnostics}.py`、`app/schemas/storage_location.py`、`tests/test_storage_source_diagnostics.py`、现有摘要/Workbench 回归、OpenAPI snapshot、`StorageLocationSourceSummary{.tsx,.test.tsx}`、`StorageSourceDiagnostics.tsx`、README/CHANGELOG、存储位置 API/用户指南/架构文档、E09/E12 及 backlog 进度。API 路径前缀均为 `apps/api/`，Web 路径前缀为 `apps/web/src/components/connections/`。
- 验证：一次性 test DB；原件变化/失踪、历史内容身份及移动、预览规格/SHA/策略/绑定、活动旧轮/失败/不完整/旧配置、任务历史删除、权限及页范围、GET 零源 I/O。Web 验证独立分页、诊断口径、深链、撤权与刷新失败。生成 OpenAPI/types，SDK 合同、tsc/lint/css tokens、文档检查及 diff 检查。
- 人工环境保留：在当前 dev 中用已有 D-7 观测查看实际 API/UI；桌面和窄屏、明暗主题、嵌套详情/Escape/焦点/返回与无溢出，检查网络及控制台。清理本轮 test 资源与临时截图，不删除验收业务数据。
- 回退：回退应用提交即可；本轮无迁移、写入或新后台执行。E12 其余容量/配额/恢复及 E09 其余诊断不宣称完成。

## 本轮定稿：E12-A1 / E09-B1

已重新核对 CHANGELOG、E09/E08 当前阶段、API/模型、0208 迁移头、任务权限与测试夹具。E01/02 和统一 AsyncJob 已满足本切片依赖；E11 清理和 E07 迁移触发条件不属于本次。本轮只交付平台九用途的后台对象盘点、成功快照和存储页消费，不宣称完整 E12-A 已完成。

```text
StoragePage / ML overview -> snapshot read API -> PostgreSQL success snapshots
                            |
Super administrator -> inventory POST -> AsyncJob + singleton checkpoint
                                           |
                                    Celery paged S3 listing
                                           |
                           fenced checkpoints -> successful bucket snapshot
                           Beat lease recovery / cooperative cancellation
```

### 合同与边界

- `GET /storage/buckets` 只读数据库，不访问对象存储；ML overview 使用相同快照。九用途按 endpoint + bucket 去重，以 `roles` 表达共用，保留 `role` 为首个用途的兼容字段。
- `POST /storage/usage-inventory` 创建统一后台任务；重复提交复用活跃任务。读取和启动均要求超级管理员；API Key 要求现有全权 `*`，执行中重新核对账号、Key 和配置。任务列表/详情/取消遵循同一权限。
- 每页最多 1000 个当前对象，累加实际对象字节，包括目录标记；每段最多 10 页 / 30 秒，分页提交检查点，段间延迟 1 秒。单桶最多 10000 页、整轮最多 24 小时，超预算显式失败。连接/读取超时及重试有界。Worker 与 Beat 使用现有 default 队列。
- 同一轮串行盘点各桶；每桶只有完整分页成功才替换成功值。单桶失败保留原值和原时间，继续其他桶；整轮标记失败并显示成功/失败桶数。取消保留已完成桶和旧成功值。崩溃/投递失败由租约恢复；任务 ID 和执行代次隔离旧执行者。
- `measured_at`、`measurement_started_at`、`scope`、`source`、`coverage`、`is_estimate`、尝试状态和时间独立返回。超过 24 小时或最近尝试失败/取消为 stale；没有成功值返回 null，不冒充零。总数仅在全部配置桶均有成功快照时提供，同时返回已测桶数和总桶数。
- 列举为采样时间窗口内当前对象汇总，非原子快照，不含历史版本、删除标记、未完成分片、供应商副本/压缩开销。外部源索引字节不混入。物理总量、可用量和配额明确未知。
- 新增 0209 两张表：成功桶快照和单例盘点检查点。AsyncJob 被保留期删除后快照仍保留，FK SET NULL。endpoint/bucket 变更后旧身份快照不复用。
- UI 使用既有紧凑字号、语义状态色与全宽列表；统计默认折叠，活跃任务/失败摘要可见；详情复用居中 AsyncJobDetailModal。刷新状态仅刷新查询，单独按钮启动盘点。

### 精确文件范围

本轮超过 8 个文件、跨 API/DB/worker/Web/docs 五个组件，属于较大切片；以共同的快照合同保持原子提交。

实际修改清单（相对仓库根）：

- `CHANGELOG.md`
- `README.md`
- `apps/api/alembic/versions/0209_storage_usage.py`
- `apps/api/app/api/v1/admin_ml_integrations.py`
- `apps/api/app/api/v1/async_jobs.py`
- `apps/api/app/api/v1/storage.py`
- `apps/api/app/db/models/__init__.py`
- `apps/api/app/db/models/async_job.py`
- `apps/api/app/db/models/storage_usage.py`
- `apps/api/app/schemas/storage.py`
- `apps/api/app/services/async_job_notify.py`
- `apps/api/app/services/storage_usage.py`
- `apps/api/app/workers/celery_app.py`
- `apps/api/app/workers/signals.py`
- `apps/api/app/workers/storage_usage.py`
- `apps/api/openapi.snapshot.json`
- `apps/api/tests/test_admin_ml_integrations.py`
- `apps/api/tests/test_storage_event_loop.py`
- `apps/api/tests/test_storage_usage.py`
- `apps/web/src/api/adminMlIntegrations.ts`
- `apps/web/src/api/asyncJobs.ts`
- `apps/web/src/api/storage.ts`
- `apps/web/src/components/connections/StorageLocationsConsole.test.tsx`
- `apps/web/src/components/connections/StorageLocationsConsole.tsx`
- `apps/web/src/components/jobs/AsyncJobDetailModal.test.tsx`
- `apps/web/src/components/jobs/AsyncJobDetailModal.tsx`
- `apps/web/src/components/jobs/asyncJobPresentation.test.ts`
- `apps/web/src/components/jobs/asyncJobPresentation.ts`
- `apps/web/src/hooks/useStorage.test.tsx`
- `apps/web/src/hooks/useStorage.ts`
- `apps/web/src/pages/Storage/StoragePage.test.tsx`
- `apps/web/src/pages/Storage/StoragePage.tsx`
- `apps/web/src/pages/Storage/StorageUsagePanel.test.tsx`
- `apps/web/src/pages/Storage/StorageUsagePanel.tsx`
- `docs-site/api/guides/_routes.generated.md`
- `docs-site/api/guides/storage-locations.md`
- `docs-site/dev/reference/storage-buckets.md`
- `docs-site/user-guide/datasets/storage-connections.md`
- `docs/plans/1792661016_dataset-storage-console-boundaries.md`
- `docs/plans/1792661016_dataset-storage-operations-recovery.md`
- `docs/plans/backlog/1792661016_dataset-storage-baseline.md`
- `docs/plans/backlog/1792661016_dataset-storage-roadmap.md`
- `docs/plans/backlog/README.md`
- `packages/python-sdk/api-coverage.toml`

### 验收与回退

主要分层命令：

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_storage_usage.py tests/test_storage_event_loop.py tests/test_admin_ml_integrations.py -k "storage_usage or slow_storage or overview" -q'
pnpm --filter @anno/web exec vitest run src/pages/Storage/StorageUsagePanel.test.tsx src/pages/Storage/StoragePage.test.tsx src/hooks/useStorage.test.tsx src/components/connections/StorageLocationsConsole.test.tsx src/components/jobs/asyncJobPresentation.test.ts src/components/jobs/AsyncJobDetailModal.test.tsx
pnpm --filter @anno/web exec tsc -b --noEmit
pnpm --filter @anno/web lint
pnpm --dir docs-site check:frontmatter
pnpm --dir docs-site check:navigation
```

- 一次性 test 模式 API/DB：无成功快照为 null；九用途/共桶去重；多页精确累计与目录标记；失败/取消保留成功值；重复提交；配置/账号/Key 撤权；旧代次不能提交；崩溃检查点恢复；投递失败可恢复；查询和 ML overview 零 S3 调用；迁移升降级与 SET NULL 保留。
- Web Vitest：空值/真零/过期/失败/部分覆盖、启动/重复启动、查询刷新不启动任务、任务详情与切换账号晚回调隔离。
- `pnpm openapi:export`、`pnpm codegen`、SDK OpenAPI 合同、Web tsc/lint/css-token、文档 frontmatter/navigation、`git diff --check`。
- 人工环境：确认无重要活跃任务后刷新本工作树 API/Worker/Beat；核对注册任务并实际盘点。桌面/窄屏、明/暗主题截图；重复刷新只发查询；详情居中/Escape/焦点与无溢出；检查控制台/失败网络请求。仅清理本轮测试资源，不删人工验收数据。
- 回退：先停盘点/取消任务，回退应用提交，保留 0209 成功快照表；如需降级，在确认无盘点执行后降至 0208（只删除本切片观测元数据，不删除对象）。不回退为每请求扫描的默认 UI，不触发对象搬迁/清理。

## 研究背景（晋升前基线）

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                             | 已有能力、缺口与复用约束                                                                                 |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| [storage.py](../../apps/api/app/services/storage.py)              | summarize_bucket 全量物化列表，失败 error+零值，无持久成功快照；线程池避免阻塞不等于消除每请求全桶遍历。 |
| [storage.py](../../apps/api/app/api/v1/storage.py)                | health 只查 annotations，buckets 逐桶扫描；需分开节点/根/能力探测、容量采样和业务可用性。                |
| [docker-compose.md](../../docs-site/ops/deploy/docker-compose.md) | 已有 pg_dump/restore 和对象镜像说明，缺一致代次、对象清单及隔离恢复对账证据。                            |
| [lan-production.md](../../docs-site/ops/deploy/lan-production.md) | 已有九桶、配置、密钥/CA 备份要求；外部原件由明确责任方保护，索引不冒充备份。                             |

候选修改入口：[storage.py](../../apps/api/app/services/storage.py)、[storage.py](../../apps/api/app/api/v1/storage.py)、[storage.py](../../apps/api/app/schemas/storage.py)、[async_job.py](../../apps/api/app/services/async_job.py)、[StoragePage.tsx](../../apps/web/src/pages/Storage/StoragePage.tsx)。本轮准确文件范围与迁移已固定于上方实施合同；其余切片需独立复核。

## 1. 当前问题与依据

当前 `summarize_bucket` 通过列举整个桶计算对象数和字节数，页面刷新可能触发昂贵的全量统计；当前存储页“总容量”使用的是对象已用字节，并不等于磁盘物理容量。[存储服务](../../apps/api/app/services/storage.py)、[存储页面](../../apps/web/src/pages/Storage/StoragePage.tsx)

系统已有 AsyncJob 全局历史和详情、媒体失败项重试，应该扩展其证据和诊断能力，而非新建另一套运维任务中心。[任务入口](../../apps/web/src/components/shell/JobsBell.tsx)、[任务详情](../../apps/web/src/components/jobs/AsyncJobDetailModal.tsx)

外部目录原地引用后，平台需要同时表达“索引了多少外部数据”“平台实际存了多少副本”和“底层磁盘还剩多少”，不能把三者相加成一个使用量。

## 2. 目标与边界

目标是让用户判断数据是否可读、任务卡在哪里、平台真实占用多少空间，以及出现数据库或存储故障后如何恢复到可验证状态。

范围包括统一指标口径、异步容量核对、读取与同步观测、故障分类、保留策略执行观测、恢复清单和演练。具体垃圾回收授权规则归 EPIC-11；业务 UI 归 EPIC-08/09；跨站点路由归 EPIC-06。

不承诺未知外部存储的总容量，不把集群副本机制当备份，不默认平台替用户备份所有外部原件，不在普通页面暴露密钥、完整路径和签名 URL。

## 3. 用户故事

| 编号    | 用户故事                                                                         | 阶段                   |
| ------- | -------------------------------------------------------------------------------- | ---------------------- |
| E12-S01 | 作为数据负责人，我能区分原件不可读、预览失败和扫描未完成，知道应找谁处理。       | M1                     |
| E12-S02 | 作为运维，我能查看已用空间、配额与数据新鲜度，且页面刷新不扫描全部对象。         | M1/M2                  |
| E12-S03 | 作为平台负责人，我能知道外部引用、托管原件、预览、训练冻结与临时文件各占多少。   | M2                     |
| E12-S04 | 作为运维，我能关联某个用户读取错误、位置健康、后台任务和远端站点事件。           | M1/M3                  |
| E12-S05 | 作为维护者，我能在代表性规模下测量扫描、读取、同步与训练准备的资源瓶颈。         | M0/M2                  |
| E12-S06 | 作为运维，我能从数据库备份与一致的对象清单恢复，并证明版本和标注仍指向正确内容。 | M1 方案与试点；M2 演练 |
| E12-S07 | 作为负责人，我能识别缓存占满、长期失败任务和无法保护的外部原件，并做明确取舍。   | M2                     |

## 4. 统一容量口径

| 指标               | 定义                                             | 能否跨数据集直接相加                         |
| ------------------ | ------------------------------------------------ | -------------------------------------------- |
| 逻辑样本字节       | 当前查询范围内样本引用的内容大小之和             | 否，同一内容可被多个数据集引用。             |
| 已索引外部原件字节 | 已完成元数据观测的外部文件内容大小               | 仅在明确去重域和来源范围内聚合。             |
| 托管原件占用       | 平台管理的持久原件副本字节                       | 按唯一物理位置记录聚合，不按引用次数累加。   |
| 预览/缓存占用      | 平台生成或按需保存的派生文件字节                 | 按唯一缓存对象及类别聚合。                   |
| 版本冻结占用       | 为版本可复现保留的原件、标签、manifest、打包文件 | 区分复用原件与额外副本，不能重复算共享对象。 |
| 临时产物占用       | 导入暂存、导出临时包、未完成上传等               | 与持久引用分开。                             |
| 位置已用空间       | 提供方/文件系统报告的范围内已用空间              | 必须标明统计范围，不能与对象逻辑字节混算。   |
| 配额               | 管理员或提供方设定的可用上限                     | 不是物理磁盘总容量。                         |
| 物理总量/剩余      | 底层有可靠 API 或挂载指标时取得                  | 未知即显示未知，不以对象字节推算。           |

每个统计响应带 `measured_at`、`scope`、`source`、`coverage` 和 `is_estimate`。展示“已索引”时说明未扫描或失败目录不在统计内。副本、纠删码和文件系统开销由提供方物理指标反映，不由应用简单乘系数冒充真实容量。

## 5. 采集与服务设计草案

- 写入/删除成功后记录可幂等累计的字节变化；异步盘点周期性纠偏。只有最终确认的物理删除才减少实际占用。
- 统计按位置和用途聚合，页面读取持久化快照，不在每次请求里全量列桶。
- 盘点使用分页和 checkpoint，受速率限制；失败时保留上一结果并标注过期，不显示零。
- 外部目录的目录扫描提供索引量；可选节点指标提供挂载容量。两者来源不同，分别显示。
- 定时同步、容量盘点、摘要核验与缓存清理错峰和限流，避免同时读遍同一磁盘。
- 长任务仍进入 AsyncJob；位置维度可过滤任务，审计日志串联 `request_id/job_id/location_id/asset_id`，不以高基数 ID 作为所有监控指标标签。

建议摘要接口返回逻辑、托管、缓存、冻结、临时和提供方指标的独立字段；不提供无范围定义的 `total_capacity` 混合值。

## 6. 健康与告警

| 故障类别          | 可见症状                       | 建议处置                                           |
| ----------------- | ------------------------------ | -------------------------------------------------- |
| 连接/认证失败     | 位置读请求连续失败             | 校验网络或轮换凭据，不触发样本删除。               |
| 根目录未挂载      | 根标识缺失或与配置不符         | 暂停扫描，恢复正确挂载，避免把空挂载点当空数据源。 |
| 内容变化/校验不符 | 已有 Asset 与实际字节不一致    | 隔离该引用并进入变更处理，不能重试后接受新内容。   |
| 容量不足/配额耗尽 | 新副本/预览/冻结写入失败       | 暂停写入任务，允许原图读取；清理必须按保留规则。   |
| 任务无进展        | 心跳正常但 checkpoint 长期不变 | 记录阶段和最后文件，受控重试或人工处置。           |
| 预览失败          | 原图可读但派生产物失败         | 单独重建，不把数据集标为整体不可用。               |
| 站点离线          | 心跳和读取失败                 | 展示降级；由 EPIC-06 处理恢复与候选副本。          |

阈值需在 M0/M1 收集真实基线后设定。告警有去重、恢复事件与负责人，不把单次超时变成连续通知。原图不可读率与预览失败率分别统计。

## 7. 性能验证计划

建议测试档位为 1 万和 10 万张图片，混合大小、深目录、同名文件、少量损坏文件与慢文件系统；这只是验证数据集规格，未经测试不宣称处理上限。

记录首次扫描时间、每批吞吐、峰值内存、数据库查询数、目录 stat/read 次数、取消响应时间、增量重复工作量、预览首屏延迟和原图读取分位延迟。比较修改前后相同环境，避免用网络变化解释代码收益。

关键验收是有界资源与正确性：扫描不一次载入全部路径，后台任务可取消/恢复，列表页不随全库规模重复扫描物理存储。性能预算与 p95 目标在真实磁盘、网络和并发下评审后写入发布门槛。

## 8. 备份与恢复设计

### 8.1 保护范围

- 数据库：Dataset、DatasetItem、Asset、位置和副本索引、来源扫描水位、项目关联、标注、冻结版本清单、必要的任务状态。
- 密钥恢复材料：连接加密所需密钥及配置，独立受控保存；不能只有数据库密文却没有恢复密钥。
- 托管持久文件：原件、标注附件、冻结内容、训练 manifest/包与其他不可重建业务文件。
- 可重建缓存：记录重建规则和版本，可选择不备份，不能包括被业务当作唯一原件的文件。
- 外部原件：登记保护责任和恢复方式。平台只持有引用时，目录丢失不可能仅靠索引还原图片。

### 8.2 一致性方案

1. 备份建立一个 `backup_generation`，记录数据库恢复点、对象清单版本和应用配置版本。
2. 首个可靠版本可使用维护窗口暂停资产写入、删除、冻结发布及相关索引变更，再获取一致清单和数据库备份。
3. 恢复需要的对象按该清单复制并验证；保留窗口内禁止清理这些对象。冻结任务只有内容落盘、校验与数据库发布均完成后才能进入可用备份集合。
4. 后续在线备份可用数据库一致性快照/PITR 加不可变对象清单与删除日志演进，但必须证明同一恢复点不缺对象，不能仅分别执行两个定时备份。
5. 备份完成记录校验摘要、缺失列表、范围、密钥版本与恢复步骤；未完成清单不能标为成功备份。

### 8.3 恢复顺序

恢复密钥和基础配置，恢复数据库与匹配清单，恢复托管对象，核对外部位置根映射，最后恢复读服务和作业调度。遗留“运行中”任务先进入待核对状态，防止重复提交冻结版本或删除操作。

恢复后对选定项目验证标注—样本—内容修订链路、冻结版本 digest、已有 URL 重新签发和位置授权。缺失外部原件显示不可用并保留索引，不允许匹配同名文件后静默恢复。

恢复点目标（RPO）和恢复时长目标（RTO）由业务损失容忍度和实际演练确定；本文不把“每日备份”自动等同于合格恢复方案。

## 9. UI 边界

存储页负责位置健康、容量、采集时间、盘点进度和恢复任务；数据集页只展示本数据集相关的样本可用性、来源同步状态和影响摘要。备份恢复操作默认管理员可见，数据管理员不看到全平台存储拓扑。

空状态明确区分“尚未统计”“提供方不支持”“暂时失败”和“确实为零”。原地注册完成后，可解释新增的是索引与可选预览，原图仍位于来源位置。

## 10. 验收标准

| 编号     | 可验证标准                                                                                               |
| -------- | -------------------------------------------------------------------------------------------------------- |
| E12-AC01 | 外部原件、托管原件、预览、冻结和临时文件分开计量；同一物理对象被多个数据集引用时不会被计入多份实际占用。 |
| E12-AC02 | 不支持物理容量查询的位置显示“未知”；展示配额时明确其不是磁盘总量。                                       |
| E12-AC03 | 连续刷新存储页不会触发全量 list；统计过期显示时间与状态，盘点失败不把已用量变成零。                      |
| E12-AC04 | 根挂载缺失、认证失败、原件变化和预览失败产生可区分事件，并能关联具体位置/任务/样本。                     |
| E12-AC05 | 代表性规模下记录扫描资源与取消/恢复行为，证明路径遍历及查询有界；目标未达成时有具体瓶颈和门槛结论。      |
| E12-AC06 | 从备份恢复到隔离环境后，选定项目标注、内容修订与冻结版本核验一致；恢复所需密钥可用。                     |
| E12-AC07 | 在冻结任务进行中触发备份，恢复后只出现完整已发布版本或明确未完成任务，不出现“可用但缺原图”的版本。       |
| E12-AC08 | 外部目录不可恢复时保留索引和来源责任记录，系统不宣称已恢复原件，不自动读取同名不同内容。                 |
| E12-AC09 | 缓存清理、备份保留与迁移观察期并存时，不会删除冻结版本或有效恢复点仍引用的对象。                         |
| E12-AC10 | 恢复演练有实测数据损失窗口和用时，可据此批准或调整 RPO/RTO，而非仅记录备份任务成功。                     |

## 11. 迁移、回滚与风险

- 新统计口径与旧口径并行对账，页面切换时保留差异解释；撤回新统计 UI 不影响底层字节。
- 观测采集失败不能阻断正常原图读取；来源内容校验失败属于读取正确性问题，不可用“监控降级”绕过。
- 开启自动清理前先 dry-run 列表与字节估计；实际清理权限和保留优先级由 EPIC-11 控制。
- 数据库与对象仓库由不同人员维护时，恢复清单必须有统一负责人，否则两个独立“备份成功”无法证明业务可恢复。
- 待决策：具体备份目标位置、保留时间、外部目录责任人、告警接收人和夜间任务窗口。

## 12. 工程任务与完成定义

- [ ] 定义指标字典、采集范围和去重域，替换“总容量”混用字段。
- [ ] 实现持久化容量快照、异步分页盘点、增量账目与差异核对。
- [ ] 接入统一日志关联、位置健康、错误分类与告警恢复事件。
- [ ] 建立代表性性能数据集和测量报告，确认有界资源与具体发布预算。
- [ ] 设计一致备份清单、删除保护和任务恢复规则；保存密钥恢复材料。
- [x] 完成一次离线合成隔离恢复演练，覆盖冻结中断、缺失外部原件和旧任务状态；生产恢复上线仍须另行验收。

完成定义：用户能看懂实际占用与数据可用性，运维能定位故障并按演练过的步骤恢复业务，而不是只恢复一个运行中的容器。

## 本项目推进切片与晋升条件

运维提供实际目录/容量、备份目标和空间、外部原件责任；测试固定代表数据和预算。当前主机硬件只能说明本地环境，不能推断生产吞吐或多站点规模。

| 切片  | 独立结果           | 交付范围                                                                     | 验收 / 回退边界                                          |
| ----- | ------------------ | ---------------------------------------------------------------------------- | -------------------------------------------------------- |
| E12-A | 诊断与统计快照     | 位置/原图/预览分错，异步分页采样和持久成功快照；随核心链路可定位失败。       | 刷新不全桶 list；失败保留旧值/时间；未知物理容量不为零。 |
| E12-B | 分项账目与一致恢复 | 九用途占用、异步纠偏、备份代次与保留，隔离核对 DB/对象/配置/密钥/冻结。      | 冻结中断不产生假 ready；实测 RPO/RTO 后批准目标。        |
| E12-C | 规模与站点观测     | 按真实图片规模/网络记录 RSS、吞吐、查询、取消、读取与恢复，接 E06 站点证据。 | 1 万/10 万是候选试验档位非承诺，不突破文件/字节预算。    |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_event_loop.py](../../apps/api/tests/test_storage_event_loop.py)、[test_storage_public_url.py](../../apps/api/tests/test_storage_public_url.py)、[test_training_storage_deletion.py](../../apps/api/tests/test_training_storage_deletion.py)、[StoragePage.test.tsx](../../apps/web/src/pages/Storage/StoragePage.test.tsx)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-buckets.md](../../docs-site/dev/reference/storage-buckets.md)、[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)、[lan-production.md](../../docs-site/ops/deploy/lan-production.md)、[network-security.md](../../docs-site/ops/deploy/network-security.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

### E11-C2-C 完整核验历史恢复回归

2026-10-24 扩展既有合成演练，在源准备 matched、同大小不同内容的 changed 及从未派发的 pending 三条观察。销毁源后实际恢复150表91行、九桶20对象；核验历史所有列继续受完整/保护行指纹约束，未加入物理重定位白名单。目标原件定位改变后旧 context=false，pending不自动运行；另一个独立断言在目标显式完整读取匹配。既有冻结/中断、密钥、外部原件缺失及同名替换拒绝验证通过，队列仍待审核。11个恢复纯测试与实际源/目标清理通过；这仍是本地合成恢复资格，不能当作生产恢复点或删除许可。完整交付、342个不同本地用例与人工环境保留证据见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-c-已登记原件完整内容核验)。

### E12-A1 / E09-B1 平台对象盘点交付

E12-A1 / E09-B1 已完成。主实现提交 `0e85ce496`；本次交付记录同时补上经回归复现的“终态先于快照读取”竞态修复，避免已完成响应携带较早统计而停止轮询。没有宣称完整 E12-A、E12 或 E09-B 完成，也未分配发布版本。

- 已交付：九用途按实际桶去重、显式后台分页盘点、逐页检查点与有界预算、租约投递恢复、进程丢失信号隔离、合作式取消、逐桶成功快照、脱敏失败与旧值/时间保留。GET 和 ML overview 只读快照，超级管理员与全权 Key 限制覆盖任务列表/详情/取消及执行中的授权复核。
- API/DB：30 个不同用例通过（首批 28、进程丢失 1、完成读取竞态 1；竞态修复后附带重验 2 个已有用例不重复累计）。覆盖多页/跨段/重投、共享桶、真零、目录标记、失败/预算、取消后旧值、读前后撤权、配置变化、窄 Key、无外部 I/O 的 GET/overview、0209 迁移升降级、Job 清理后快照保留。读取竞态先出现 `completed + 0/9` 的失败，再修正为先读轮次状态、后读快照，相关 3 项通过。
- Web：6 个目标文件共 57 个不同用例通过，覆盖原存储导航和媒体重试、未知/真零/过期/部分覆盖、显式启动、会话晚结果、拒绝后隐藏及重新授权恢复、触发按钮消失后的焦点回退。异步弹窗显式返回焦点补充通过；最后两项面板边界与位置提示修改后重验 23 项，不重复累计。SDK OpenAPI 合同 4 项通过，合计 91 项本地用例；未宣称全量测试或远程 CI。
- 静态与文档：Web tsc、完整 Web lint、CSS token 检查、Ruff、Prettier、OpenAPI 导出/类型生成与完整提交钩子通过；只有既有 4 个 Web lint 警告和训练比较页 2 处样式建议。OpenAPI 保留全部旧端点，只新增盘点 POST；nullable 计数/状态与全权 Key 的调用兼容性已写明。文档 frontmatter 196 文件、navigation 198 页面通过。
- 真实环境：保留本工作树 dev 3100/8100，0209 已应用，两个 Worker 均注册盘点/恢复任务，Beat active；没有重建后端镜像。最终刷新后的实际任务 `0122136c-969f-4c48-8708-6b81dff6d4b8` 完成 9 个桶、失败 0。人工验收对象总计 20 个、38911 字节；这不是物理容量。取消任务 `79b08671-45aa-4372-8ba1-3f15a0a269db` 保留此前九桶的值与时间，随后 `100972e1-ceb5-4bed-a5fb-df4e7e7d89ee` 再次盘点全部成功。
- 真实浏览器：使用独立会话、无 mock。首次未知、成功零值、九桶采样时间、默认折叠和展开、连续刷新不产生盘点 POST、任务居中与 Escape 返回焦点通过。1440×1000 和 390×844 明暗主题均检查截图；窄屏明细改为容器内横向滚动，页面宽度仍为 390，无页面溢出；窄屏任务弹窗为 342×318，居中且可关闭。控制台没有 error/warn，仅 debug/info；页面错误与失败网络请求均为零。权限、失败、部分覆盖及进程丢失故障以本地测试验证，未在保留的业务环境注入故障。
- 正式文档：`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/api/guides/storage-locations.md`、`docs-site/dev/reference/storage-buckets.md`、`README.md`、`CHANGELOG.md` Unreleased 与生成 API 索引同步；SDK coverage 明确将平台运维入口排除为 Web 专用。
- 清理：一次性 `1822b025369a33f4:test` 数据库/Redis/九桶按 owner 校验销毁；本轮截图、报告、日志、临时脚本、测试缓存及专用浏览器会话清理。保留 dev 人工环境、原演示数据、已有冻结版本与本轮可查看的成功/取消任务记录。
- A1 交付时剩余：E12-A 的原件/预览/扫描关联诊断、E12-B 分项归因/配额与一致恢复、E12-C 规模基准仍需独立复核；E09-B 的 E11 清理影响预览和 E07 迁移/退役合同未实施。本计划继续保留为活动计划，外部源索引字节、历史版本/分片与底层物理容量不冒充已测量。

### E12-A2 / E09-B2 来源诊断交付

实现提交 `58a9725cf`；无迁移、无依赖或 Worker 改动，不重建后端镜像。本切片已完成，不宣称完整 E12-A/E12 或 E09-B 完成，未分配发布版本。

- 交付：位置摘要按当前来源页批量聚合原地引用样本的六态原件观测、当前内容的独立预览记录、扫描与预检的活动优先轮次和最近完整成功；显示历史配置和观测时间。包含历史修订，移动保持身份；托管复制和未登记路径不混入。GET 零源 I/O，不改变既有读取/接纳合同。
- 界面：全宽来源列表，四类证据分开，数量细项和后台任务历史折叠；任务历史独立分页并保持展开状态。保留来源/扫描/返回深链，任务居中展示，Escape 仅关闭上层并恢复焦点。刷新失败保留并标明旧结果，撤权隐藏私有数据。
- API/DB：36 个不同用例通过（来源摘要 8、诊断 14、Workbench 14；夹具修正后仅重验失败项，不重复累计）。覆盖权限/孤儿历史/页范围，GET 禁止存储调用，预览 SHA/Asset/规格/目标绑定，策略停用/恢复，历史修订与移动，空/缺失 Asset，旧活动轮与新失败、配置修订及任务删除。1 与 5 个来源的查询次数未随行数增长。
- Web：摘要 7、位置控制台 12，共 19 项通过；SDK OpenAPI 合同 4 项通过。tsc、完整 Web lint、CSS tokens、Ruff、OpenAPI/TS 生成、API 索引、文档 frontmatter（196）/navigation（198）及 diff 检查通过。仅保留既有 4 条无关 ESLint warning 与训练页两处 z-10 建议。总计 59 个不同本地用例；不宣称全量套件或远程 CI。
- 实际浏览器：保留的 3100/8100 dev 无 mock；D-7 来源为已验证 1、失踪 1、变化 1，独立预览记录为 3。历史来源最近扫描失败、不完整、历史配置及上次完整成功同时可见。刷新只有一条摘要 GET 200，未创建任务。任务详情居中、Escape 和焦点恢复；来源往返存储原位置、历史任务第二页与来源第一页独立保持。1440×1000 和 390×900 明暗主题截图已检查，无横向溢出、页面 JS 错误或失败 HTTP 请求。
- 正式文档：`docs-site/api/guides/storage-locations.md`、`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/dev/concepts/storage-connections.md`、`README.md`、`CHANGELOG.md` Unreleased 及 OpenAPI 同步。API 路由未新增，生成 API 索引与 SDK coverage 无需变更。
- 清理：已按 owner 销毁本轮 test DB/Redis/九桶及临时资源，清理日志、报告、截图、测试缓存和专用浏览器；保留 dev、D-7 来源、质检/复核记录及原 ready 冻结版本。完整 pre-commit 通过；首次 hook 仅自动排版路线表格，暂存格式化结果后重新完整通过。
- 剩余：运维告警/日志关联与跨节点失败分类仍需独立合同；E12-B 分项归因、配额和一致恢复，E12-C 规模基准、E09 的 E11/E07 维护影响预览仍未完成。本次记录不是实时读取证明，也不提供物理容量或完整源索引字节。活动计划继续保留，不归档整套 Epic。

### E12-B1 离线一致备份与隔离恢复交付

2026-10-24 完成，仅资格化本机离线合成试点；生产 RPO/RTO 未定，整项 E12 继续活动。实现、正式文档和本记录同次提交，不分配发布版本。

- 交付：私有完整备份代次、最后发布完成标记、流式 SHA 与 metadata、全部九用途及共桶范围、逐项预算和无版本历史门禁、独立演练密钥；源销毁后 full restore 至随机空目标，迁移头匹配后授权，六类物理 owner 白名单映射并保留17项变换账本。旧队列不恢复、不启动应用/调度，未完成任务明确待核对。
- 纯测试：30个不同用例通过，包含备份与恢复的完整性、预算、分页/中途变化、空/共享桶、私有文件、完成标记、篡改、metadata、错误归属/非空目标/未知客户端、密钥类型与身份、优化模式拒绝、六类映射及拒绝前零变更。首次29项及新增空目标复查1项分别通过，不重复累计；均不连接业务基础设施。
- 最终真实演练：代次 `a769013d-6b7e-4b38-856f-86ac140337c8`，迁移头0213，145表71行、九桶19对象4700字节。备份源库/源桶/Redis及外部文件销毁、源dump和旁路事实文件移除后，只使用完整代次与独立密钥恢复。原始全库指纹一致；映射后的受保护业务指纹一致，连接密文成功解密，完整缓存与预览回读校验通过。
- 业务验证：1个ready版本的3张冻结图、标签、manifest、ZIP、来源证据与Native输入校验通过；1个已有部分媒体的running版本保留未完成，无伪造清单/包。两条外部引用索引保留，缺失原件严格拒绝；旧路径重建同名不同内容也拒绝，冻结内容仍独立可验。目标Redis为空。
- 用时与窗口：源准备20.529秒、备份3.197秒、源销毁1.508秒、DB恢复13.605秒、对象恢复2.825秒、应用核验3.169秒；含归属/清理的总用时47.284秒。已知停写期间写入为0，仅适用于无服务的随机合成源；不能据此宣称生产数据损失窗口或恢复承诺。
- 故障：源准备后注入中止、对象篡改、错误密钥、坏dump及DB恢复后中止均按预期退出1并验证清理。坏dump明确返回 `dump_integrity`，26.286秒结束并完成清理；恢复中止场景先销毁源再恢复目标，证明失败清理不依赖源。单元补充完成标记前DB回调失败、坏dump及缺key等边界。
- 文档与检查：新增 `docs-site/ops/deploy/storage-recovery.md`，同步部署两页、运维索引、DEV、README、CHANGELOG Unreleased及整套队列。Ruff检查/格式、文档frontmatter/导航/严格可读性及构建通过；仅有既有大包和未加载代码语言的构建提示。无应用UI/API/Worker/schema改动，不重复浏览器矩阵，不重建镜像。
- 清理与保留：演练正常/失败均按owner销毁随机DB/Redis/九桶并清理私有目录，最终清理状态verified；任务日志、报告、构建和Python缓存在记录后清理。当前dev 3100/8100、两个Worker及beat保留，服务只读核验；未向人工业务数据写入夹具。
- 剩余：生产停写与删除保护、保留策略/PITR、恢复应用上线/异步owner处置、规模基准/告警和实际外部来源恢复仍需独立验收。此切片不提供通用生产恢复命令。接下来复核E11/E09维护影响与受控生命周期，不归档仍活动的E12。
