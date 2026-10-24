# EPIC-08：数据集工作台与质量管理

> 状态：E08-A complete；E08-B1–B5 complete；完整故事与验收矩阵待逐条复核；未分配发布版本。
> 转入日期：2026-10-22；A 切片验收：2026-10-23；初始调研基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 属性     | 草案约定                                                           |
| -------- | ------------------------------------------------------------------ |
| 优先级   | P0：独立详情与目录接入闭环；P1：保存视图、质量治理与批量效率       |
| 负责角色 | 产品/设计、前端、后端、测试；复用现有标注与训练模块负责人          |
| 依赖     | EPIC-01/02/03/05 的身份、来源、接入与读取；EPIC-11 的权限切片      |
| 协同     | EPIC-04 提供同步；EPIC-09 统一页面边界；EPIC-10 提供冻结版本       |
| 交付阶段 | M1 最小工作台；M2 质量、视图与批量管理；近似重复能力按实际需求后置 |

## E08-B4 晋升复核与实施合同

2026-10-23 用户授权整套 Epic 实施，复核基线 `a61c9a4e6`，工作树干净。E08-S04 仍缺 Scene 过滤；本轮先补完整条件，再推进 E08-B5/E10-B1 选择冻结。不增加迁移、依赖或后台任务，不重建镜像。

```text
Scene option page (safe fields, bounded) -> selected Scene ID
URL / personal view -> DatasetItemFilters -> dataset-scoped Scene validation
                                         -> Item.scene_id predicate -> count/page
```

合同：Scene 以当前数据集内的 UUID 标识，直接按 DatasetItem.scene_id 过滤，包含同 Scene 的多模态和 frame_index 为空的校准成员；不转为 Task 或主帧邻居范围。与原有条件先组合再计数/分页。非法 UUID 422；已删除、不存在、跨数据集 Scene 一律404，不悄悄扩大为全部。视图保存验证归属，旧 schema 1 可选扩展，旧视图行为保留。

新增 `GET /datasets/{dataset_id}/scene-options`，datasets:read 和现有数据集读取语义，limit 默认20/最多100、offset、名称 search（最多200）及可选 selected_id。响应 items/total/selected；只返回 id/display_id/name，selected 独立于选项页且仍检查归属，不暴露 source_metadata，不使用旧全量 scenes 接口。GET 无源 I/O。

UI 复用原筛选 owner；Scene 的选项查询有界并可搜索/翻页。选中项不在当前页、选项加载失败、删除 Scene 或 URL 无效时保留筛选，不回退全部。切换登录身份和数据集隔离迟到响应；标签在控件上、提示在下方 muted，保持紧凑字号与明暗/窄屏布局。清除更多格式/尺寸条件仍保留主条件 Scene；切换质量页后返回保留 URL。质量批量复核使用固定质量运行，不将可变 Scene 条件偷偷加入历史批次范围。

本轮超过8文件，跨 API/Web/合同/文档。准确范围：API `schemas/{dataset_view,dataset}.py`、`services/dataset_workbench.py`、`api/v1/{datasets,dataset_views}.py`、新增 `tests/test_dataset_scene_filters.py` 及相关现有测试；Web `api/datasets.ts` 及测试、`pages/Datasets/{datasetsUrlState.ts,DatasetItemFilters.tsx,DatasetDetailPage.tsx}`、新增 `DatasetSceneFilter.tsx` 及测试，URL/详情回归；OpenAPI、生成 types、API index、SDK coverage 及合同；数据集 API/用户指南、README、CHANGELOG、本计划和整套队列/路线。没有训练或质量 Worker 改动。

验证：可丢弃 test DB；两 Scene/未分组/多模态/校准、组合与分页、视图动态成员、删除/跨数据集/非法 ID、选项页边界/安全字段/权限。Web 检查条件序列化、选项分页不改范围、名称加载失败保持选中、会话切换、返回和更多条件清除。OpenAPI/SDK、tsc/lint/css、文档及 diff 检查；3100/8100 人工环境保持运行；独立 e2e 环境准备任务自有 Scene 数据，在真实浏览器验证明暗/窄屏、选择/搜索/分页/视图及错误不扩张、网络/控制台。验收后销毁 e2e 资源，保留已有人工数据和冻结版本。回退应用提交即可；已保存的新 Scene 条件须先导出/移除后才降级到不认识该字段的版本，禁止静默忽略条件。

## E08-B3 晋升复核与实施合同

用户已授权继续，基线 `2e9fc393b`，工作树干净。本切片将质量页已有单项复核扩展为固定范围批量复核：显式勾选（最多200项）或全部筛选结果（最多10000项），先物化影响预览，再按原快照确认。复用现有决定和审计，不放大 Task 批操作，也不增加移除样本、项目关联或源文件删除。

本次超过8个文件。准确范围：API 新增 `db/models/dataset_quality_review.py`、`schemas/dataset_quality_review.py`、`services/dataset_quality_review.py`、`workers/dataset_quality_review.py`、`api/v1/dataset_quality_review.py`；扩展 `db/models/{__init__,async_job}.py`、`services/{dataset_quality,async_job_notify,async_job_terminal}.py`、`workers/{celery_app,signals}.py`、`api/v1/{router,async_jobs}.py`；迁移 `0208_dataset_quality_review.py`、测试 `test_dataset_quality_review.py` 及必要原复核回归。Web 新增 `api/datasetQualityReview.ts`、`hooks/useDatasetQualityReview.ts`、`pages/Datasets/DatasetQualityBulkReview.tsx` 与相关测试，扩展质量页及共享任务显示/详情、取消列表。同步 OpenAPI、SDK 覆盖清单、本地生成类型、数据集用户/API/架构文档、README、CHANGELOG、本计划和路线索引。

```text
quality result filters / explicit selection
                  |
        shared SQL -> immutable preview + member proofs
                  |                   |
     centered impact confirmation    expiry / blocked pending rows
                  |
      AsyncJob -> fenced bounded worker -> Result CAS + audit + checkpoint
                  |
       paged outcomes / cancel / resume pending members
```

范围只来自当前质量运行，过滤复用列表的 finding/review/sha256 SQL，分页不参与范围。预览固定 Result、Item/Asset/SHA/size、规则/配置、检测证据与 review_revision，目标决定和原因一并纳入 SHA256 摘要。预览有效10分钟；每用户/运行最多20个有效未确认预览，创建时可清理其已过期且未执行的预览。只有预览创建者可首次确认；确认提交预览摘要及显式 snapshot 策略，不再查询动态条件。并发/重复确认返回同一执行。运行中的检测须先完成或取消，避免把 pending 证据当成可复核结果；已有 pending 结果固定为阻断项。

复核历史内容沿用单项语义：预览明确历史绑定数量，决定只写确切历史证据，不读取原图、继承到新内容或改变任务/训练资格。源离线不会被误判为无权复核；来源/数据集权限仍逐次检查。执行逐项锁定并刷新 Result，核对完整证据和复核修订；冲突跳过，结果/审计/checkpoint 同事务。已成功项不重复复核；失败或取消仅恢复 pending，冲突需重新预览。新匹配结果不扩大原范围。

两张表保存批次及成员证据，与 AsyncJob 关联 SET NULL；新 kind 为 dataset_quality_review，沿用 generation/task/job/租约 fencing、逐项提交、分块续派、专用旧信号检查及 Beat 恢复。通用任务仅保存安全摘要，列表/详情/取消同时检查当前数据集、来源权限和 API key scopes。部分成功以批次 partial 与通用任务已完成+冲突/阻断计数区分；错误和撤权不伪装成功。

验证：独立 test 库迁移往返、共享筛选跨页、预览后新增不扩大、修订与身份冲突/ABA、重复确认、部分提交取消恢复/旧任务/旧信号、过期预览、父子/创建者/来源/API key 权限、历史结果复核、每项审计唯一。前端验证选择范围、预览失效、明确按快照确认、会话/路由迟到回调、结果分页和终态刷新。人工 dev 环境升级0208并刷新 Worker/Beat，保留3100/8100与既有数据，不重建镜像；桌面/窄屏及明暗截图、网络/控制台验收。回退先停止批次并导出证据，降级仅移除批次账本，已提交复核和审计保留；测试资源与中间产物清理后提交。

## E08-B2 晋升复核与实施合同

用户已授权继续。基线 `fba96aaa1`，工作树干净。本切片交付整份数据集图片质量检测、可信精确重复与人工复核；不自动删除、合并或改变训练资格。当前筛选/个人视图不参与范围。未核验 Asset、非图片及超预算内容明确未检测，不升级旧 MD5/ETag。规则 `image-quality-1` 默认宽高各 64–8192，可在运行前配置；每次最多 10000 个固定成员。

超过 8 个文件。准确范围：API `db/models/{dataset_quality,__init__,async_job}.py`、`schemas/dataset_quality.py`、`services/{dataset_quality,content_snapshot,dataset_content,async_job_terminal,async_job_notify}.py`、`workers/{dataset_quality,celery_app,signals}.py`、`api/v1/{dataset_quality,async_jobs,router}.py`；迁移 `0207_dataset_quality.py` 与测试 `test_dataset_quality.py`；Web `api/{datasetQuality,asyncJobs}.ts`、`hooks/{useDatasetQuality,useDatasets}.ts`、`pages/Datasets/{DatasetQualityPanel,DatasetDetailPage}.tsx`、`datasetsUrlState.ts`、共享任务展示/详情及相关测试；OpenAPI、SDK 覆盖清单、本地类型；README、CHANGELOG、数据集用户/API/架构文档、本计划与路线索引。

```text
quality tab -> fixed run + member/result ledger -> AsyncJob
                      |                              |
                review + audit                leased bounded worker
                      |                              |
                paged evidence <- full SHA proof + image inspection
                                         /
                              managed original       reference context
```

两张表保存运行和成员/结果；复核有乐观版本，追加历史进入现有审计日志。证据固定 Item、Asset、SHA/size、规则/配置及时间；删除仍保留历史，内容身份变化不继承。引用来源按当前授权查询；任务载荷不含凭据。AsyncJob 关联 SET NULL，任务清理不删除质量历史。完整读取与解码分开：仅已证明字节无法解码才报损坏；缺失、变化、权限、预算分别处理。重复以本次完整读取证明的 SHA256+size 分组，分页展示成员。

运行使用租约、generation、task/job 联合 fencing，I/O 前后重新授权，逐成员提交、分块续派；取消保留结果。恢复同一固定成员且只处理未完成项；完成后的重检创建新运行。Beat 恢复过期租约，迟到 Worker 无权写入；每数据集仅一个活动运行。

验证：可丢弃 test 库迁移往返、固定成员、可信重复、坏图与不可读、EXIF 尺寸、恢复/fencing/取消、撤权及复核冲突；前端 URL、分页、终态刷新、弹层目标固定；人工环境明暗/桌面/窄屏及网络/控制台。迁移应用当前 dev 库，刷新本工作树 Worker/Beat，不重建镜像。回退先停任务并导出证据再降级；旧数据集/来源/训练版本无数据迁移。

## E08-B1 晋升复核与实施合同

用户已授权继续推进下一阶段。复核基线 `7cfe292ce`，工作树干净；E08-A/E09-A、E10-A 已交付。本轮将 E08-B 拆出可独立验收的 B1：服务端组合筛选和个人动态视图。质量检测、固定批量范围、共享视图和训练冻结视图均不在此切片。超过 8 个文件，涉及迁移、API、前端、合同及文档；不增加依赖，不重建镜像。

准确范围：`apps/api/app/{db/models/dataset_saved_view.py,db/models/__init__.py,schemas/dataset_view.py,api/v1/dataset_views.py,api/v1/datasets.py,api/v1/router.py,services/dataset_workbench.py}`；`apps/api/alembic/versions/0206_dataset_saved_views.py`；`apps/api/tests/{test_dataset_views.py,test_dataset_workbench.py,test_migration_0206_dataset_saved_views.py}`；`apps/web/src/{api/datasets.ts,hooks/useDatasets.ts,pages/Datasets/DatasetDetailPage.tsx,pages/Datasets/DatasetItemFilters.tsx,pages/Datasets/DatasetSavedViews.tsx,pages/Datasets/datasetsUrlState.ts}` 及对应 API、URL、页面/组件测试；OpenAPI 快照及本地生成类型、SDK 覆盖清单；`docs-site/{api/guides/datasets.md,api/guides/_routes.generated.md,user-guide/datasets/index.md}`、README、CHANGELOG 和本计划/路线/索引。

```text
URL conditions + personal view picker
             |            |
       bounded items    own saved queries (new table)
             |            |
       validated filter contract + current source authorization
             |
      SQL predicates -> count -> pagination -> bounded serialization
```

合同：文件名搜索、来源与已保存可用性观测可组合样本媒体类型（image/video/point_cloud/other）、文件扩展名、尺寸已知/未知及宽高范围。扩展名来自逻辑文件名的最后分量，大小写归一，jpeg/jpg 同组；不声称已探测真实编码。宽高须为正数，未知包括 NULL/非正值；反向范围及未知尺寸与范围冲突返回422。所有谓词在计数/分页之前执行。

个人动态视图只存白名单查询条件，默认且仅个人可见；按 dataset+owner 隔离，同名409，最多100个。不保存 Item 集合、权限结果、路径或凭据。个人偏好写独立于数据集资源写守卫，要求登录及 datasets:read；来源条件保存/恢复均需当前授权，来源撤权或删除必须明确失败，不静默去掉条件。URL 保留完整条件与 saved_view ID（view_query=1 表示条件快照，只有 ID 时先恢复后查询）；修改条件显示未保存，表格/网格与分页独立。详情/保存/重命名/删除使用居中弹层，下拉说明在控件外，字号遵循共享约定。

迁移接0205，仅增加 dataset_saved_views 表，父数据集/用户删除级联；旧API与数据保持兼容。回退会删除个人视图，运维应先导出；不影响样本、来源与冻结版本。一次性 test 模式验证迁移往返、模型一致性、组合分页、未知尺寸、私有CRUD/父子隔离/来源撤权/重名与动态成员。前端验证 URL、请求、个人视图恢复/修改/会话隔离；浏览器验收桌面/窄屏及明暗主题、弹层和网络/控制台。迁移随后应用当前人工验收 dev 库，保留业务数据、8100/3100入口及现有worker配置，不重建镜像。

## E08-A 晋升复核与实施合同（已完成）

用户已授权“完成 E10-A，再推进 E08/E09 页面整合”。复核基线 `74bd04c12`，工作树干净；E03/E04 已完成，E10-A 已支持引用原图严格读取后冻结独立副本。本节替代历史候选范围，仅实施 E08-A/E09-A 页面整合；保存视图、质量治理、异步容量统计和维护迁移仍属后续切片。超过 8 个文件，跨前端、API、合同和文档；不创建业务表、不改依赖/环境、不重建镜像。

### 准确文件范围

- E08 页面：`apps/web/src/pages/Datasets/{DatasetsPage,DatasetDetailPage}.tsx`、`datasetsUrlState.ts` 及同名测试；`apps/web/src/App.tsx`、`apps/web/src/api/{datasets,tasks}.ts`、`api/datasets.test.ts`、`hooks/useDatasets.ts`、`hooks/useFilterListCancellation.test.tsx`；`pages/Projects/ProjectLearningPage.tsx` 及测试、`pages/Training/trainingDatasetDraftStorage.ts` 及测试。独立详情保留上传、扫描、元数据、Scene、坐标系、关联/解绑，接入真实分页、来源/可用性、检查器、分项目进度和共用训练入口。
- E09 页面：`apps/web/src/pages/Storage/StoragePage.tsx` 及测试；`components/connections/{FilesystemLocationsPanel,StorageConnectionsPanel,StorageLocationSourceSummary,StorageLocationsConsole}.tsx`、适用测试和 `storageQueryState.ts`；`components/datasets/{ImportDatasetWizard,DirectoryReferencePanel}.tsx` 及测试、`DirectoryScanHistory.test.tsx`、`storageNavigation.ts` 及测试；`api/{datasetSources,storage,storageConnections}.ts`、`hooks/{useStorage,useStorageConnections}.ts` 及 `useStorageConnections.test.tsx`。统一位置配置、九用途和安全返回，保留 E04 来源/任务分页与视频维护。
- 集成修复：`apps/web/src/hooks/useVerifiedImage.ts` 及测试、`components/datasets/DirectoryRegistrationProgress.tsx` 及测试、`DirectorySyncSchedule.tsx`，连同上述 `DirectoryReferencePanel.tsx`。预览受控排队及容量繁忙有限重试；预检、生命周期与周期变化同步刷新工作台和单项检查器。
- 后端：`apps/api/app/api/v1/{datasets,tasks/list}.py`、`schemas/{dataset,storage_location,training_dataset}.py`、`services/{dataset,dataset_workbench,storage_location,storage_source_summary,training_datasets}.py`、新增 `apps/api/tests/test_dataset_workbench.py`；`apps/api/openapi.snapshot.json`、`packages/python-sdk/api-coverage.toml` 及生成的本地前端合同。沿用已有探测历史 API，不新增表或迁移。
- 文档：`docs-site/user-guide/datasets/{index,import-images,storage-connections}.md`、`docs-site/api/guides/{datasets,storage-locations,training-datasets,_routes.generated}.md`、`docs-site/dev/reference/storage-buckets.md`、`README.md`、`CHANGELOG.md`；E08/E09/E10 计划、backlog 索引/路线/基线/信息架构。交付状态、验证及剩余切片记入 Outcome。

### 数据流、权限和兼容

```text
/storage?location=ID <--- safe return ---> /datasets?location_id=ID&page=N
        |                                           |
existing config + nine bindings              /datasets/ID?tab=...&item=...
        |                                           |
source/job summary <------------------- source + sanitized item status
                                                    |
                                    authorized project /learning?tab=data
                                                    |
                                          existing E10 freeze composer
```

不新增 migration；仅扩展查询合同。可用性带观测时间，未实时核验不得称当前可读；预览与原图分别判断，缩略图不构成可标注证明。筛选按持久观测，页面只查询当前页。项目进度仅统计该数据集在该项目的任务，列表按既有项目权限裁剪；关联版本沿用训练模块。位置过滤沿用来源授权，按唯一 dataset ID 计数。新读取与异步回调绑定认证及资源 owner，撤权/切会话不展示迟到结果。

旧连接页签转唯一存储配置；向导仅选择已有连接，返回仅接受站内白名单路径，草稿按会话/数据集隔离且不持久保存秘密或 File。存储不再重复数据集目录，不调用逐桶全扫描；没有可靠测量合同时容量、配额、源规模和对象字节显示未提供/待统计。九用途只读，不伪装迁移。

### 验收门与回退

- 后端：worktree wrapper disposable test DB 下运行新增 workbench、storage source summary、storage locations、既有数据集/项目访问测试；覆盖分页、双项目、撤权、异常、唯一计数。同步 OpenAPI、TS、SDK route coverage 并检查合同。
- 前端：指定文件 `pnpm --filter @anno/web exec vitest run ...`，typecheck、受影响 ESLint、CSS token lint；覆盖 URL、会话、失败重试、分页/过滤、解绑预览失败阻断、九用途及导入返回。
- 浏览器：3100/8100 既有人工作环境，验证旧链接、独立详情、来源↔位置、分页网格/检查器、项目进度、引用冻结入口、九用途及深浅主题；检查网络/控制台。测试后清理临时文件和 disposable DB，保留人工环境。
- 回退提交只撤销 UI/查询扩展，保留来源、Asset、冻结版本和存储身份，不将 reference 键交旧 S3 拼接。最终检查 diff 并追加 Outcome。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                                    | 已有能力、缺口与复用约束                                                                                                            |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| [DatasetsPage.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)                   | 已有展开详情、文件/Scene、关联/解绑影响、元数据修复与 ?dataset= 入口；独立详情逐项迁移保留。                                        |
| [datasetsUrlState.ts](../../apps/web/src/pages/Datasets/datasetsUrlState.ts)             | 复用 URL 分页/过滤状态；新详情/来源/检查器请求纳入认证和资源 owner 防迟到约束。                                                     |
| [project_task_view.py](../../apps/api/app/db/models/project_task_view.py)                | 现有 ProjectTaskView 只管项目 tasks/objects/tracks，不直接作为 DatasetItem 保存视图；项目 Data Manager 保持任务/标注/分配权威入口。 |
| [training_dataset_analysis.py](../../apps/api/app/services/training_dataset_analysis.py) | 冻结分析可复用通用计算；原始媒体质量按 Asset/算法修订保存，不能继承给新字节；原图位置可用性独立观测。                               |

候选修改入口：[DatasetsPage.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)、[datasetsUrlState.ts](../../apps/web/src/pages/Datasets/datasetsUrlState.ts)、[ImportDatasetWizard.tsx](../../apps/web/src/components/datasets/ImportDatasetWizard.tsx)、[datasets.ts](../../apps/web/src/api/datasets.ts)、[App.tsx](../../apps/web/src/App.tsx)、[datasets.py](../../apps/api/app/api/v1/datasets.py)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与当前依据

当前数据集列表通过展开行承载详情，包含文件、场景、项目关联和元数据修复，基础能力已经存在；随着原地目录、多来源和来源可用性加入，展开行难以继续承载主工作流。[代码依据：DatasetsPage](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)。

当前导入向导已经支持本地文件、ZIP 和连接器，以及异步任务反馈。新方案应增加原地目录注册与预览计划，并保留现有入口。[代码依据：ImportDatasetWizard](../../apps/web/src/components/datasets/ImportDatasetWizard.tsx)。

训练相关分析已经包含统计与差异能力。本 Epic 扩展工作数据集的可发现性与问题处理，不另建一套定义不同的训练统计。[代码依据：training_dataset_analysis](../../apps/api/app/services/training_dataset_analysis.py)。

## 2. 目标、范围与暂不做

目标是让用户以“样本—来源—质量—用途”的顺序管理数据，并在源目录不搬迁的情况下完成浏览、标注关联和版本操作。

本 Epic 包含：

- 独立数据集详情页，承接原展开行中的业务能力。
- 表格/网格浏览、来源与可用性过滤、场景分组、样本检查器。
- 原地目录接入向导的前端体验；后端注册逻辑由 EPIC-03 实现。
- 保存动态过滤视图，显式创建固定版本，避免两者语义混淆。
- 质量问题的发现、筛选、复核与处理记录。
- 项目关联摘要和跳转，复用当前解除关联的影响预览。

暂不做：存储端点和凭据编辑、磁盘容量看板、全新标注任务分配系统、未经验证的自动清洗、无人工复核的近似重复删除。

## 3. 用户故事与切片

| 编号    | 用户故事                                                                       | 切片与完成结果                               |
| ------- | ------------------------------------------------------------------------------ | -------------------------------------------- |
| E08-S01 | 作为数据负责人，我希望通过稳定地址进入数据集详情，离开后恢复筛选与选中状态。   | M1：独立详情、URL 状态、返回位置             |
| E08-S02 | 作为数据负责人，我希望把已授权图片目录注册为数据集，并清楚知道是否复制原图。   | M1：目录选择、原图策略与独立缓存选项         |
| E08-S03 | 作为标注人员，我希望知道某样本能否读取原图，以及失败后该找谁处理。             | M1：原图/预览状态、原因与允许动作            |
| E08-S04 | 作为数据负责人，我希望按来源、格式、尺寸、场景和可用性筛选，并保存常用条件。   | M1 基础过滤；M2 保存动态视图                 |
| E08-S05 | 作为质量负责人，我希望集中处理无法解码、精确重复、尺寸异常及内容变化。         | M2：质量任务、候选列表、处理记录             |
| E08-S06 | 作为项目负责人，我希望看到本项目的样本使用与标注摘要，并正确进入项目管理。     | M1：分项目展示与权限裁剪                     |
| E08-S07 | 作为算法工程师，我希望在选定关联项目和标注范围后，把样本集合固定为可复现版本。 | M1：共用最小冻结入口；M2：筛选视图与版本增强 |
| E08-S08 | 作为数据负责人，我希望批量操作明确影响范围，数据更新时不会悄悄扩大选择。       | M2：范围预览、查询快照和执行结果             |

## 4. 功能与失败语义

### 4.1 数据集列表和详情

列表仅保留名称、类型、样本数、来源摘要、可用性、关联项目和最近变更。数据集名称是逻辑名称，重命名不会导致目录迁移。

详情页建议包含“数据”“来源与同步”“质量”“项目关联”“关联版本”五个页签。页头只保留业务摘要，不嵌入整套存储仪表盘。完整字段归属以 [信息架构](backlog/1792661016_dataset-storage-information-architecture.md) 为准。

表格与网格共享同一过滤器和服务端查询。网格按需加载缩略图；列表不能通过加载全部样本再由浏览器分页来实现。

样本总数定义为当前非移除成员数量，包含暂时缺失或离线样本；可读原图数单独显示，并带核验时间。变更待确认内容不自动替换已标注样本。

### 4.2 来源与同步

来源卡片展示位置别名、相对目录、原图策略、缓存策略、筛选规则、最近扫描结果与待处理变化。

用户可以管理“这个数据集扫描什么”，不能在这里修改共享位置的端点或凭据。“管理存储位置”深链受权限控制，并携带返回上下文。

位置离线时仍保留数据集和样本信息。“已登记/待校验”与“可标注”分开：当前内容修订的完整原图经核验可读且有项目权限后，才可编辑标注。仅有缓存预览时只能只读浏览，禁止编辑/提交标注、原图下载和需要原图的训练。经核验的完整原图缓存可满足同一修订的读取要求。

原地引用表示初始不强制持久复制原图，不排除受限读取缓冲、独立预览/按需缓存和后续训练冻结副本；界面分别说明空间影响。

### 4.3 质量与视图

质量结果区分检测结果和处理决定，至少包含规则版本、执行时间、Asset 内容修订和状态。旧 Asset 的内容质量结果仍是对应字节的历史事实；新内容候选不得继承，显示未检测/待检测。来源在线、缺失等可用性观测按位置和时间重新核验，不能与内容质量混为同一状态。

精确重复基于明确的内容摘要；仅有路径、文件名、大小或 ETag 的条目不能宣称“内容相同”。近似重复是候选分组，与精确重复分开显示。

默认操作是过滤、标记、补充说明、创建视图或移出当前数据集；不自动删源文件。处理已被项目或版本引用的样本必须执行 EPIC-11 的影响检查。

保存视图保存查询条件和权限范围，后续成员会随数据变化；固定版本绑定成员、内容修订和所需标注状态，交由 EPIC-10 冻结。按钮和说明明确区分两者。

关联训练版本仍归属于现有项目/训练模块。创建时必须选择有权限的关联项目及其任务/标注范围，再把筛选结果传入共用冻结流程；无项目上下文不能创建。版本编辑、详情和差异保持单一实现，数据集页仅展示关联列表与深链。

涉及标签类别和标注完成率的统计必须指定项目或冻结版本。一个数据集关联多个项目时，不产生无定义的“全局已标注数量”。

## 5. 数据与 API 草案

以下为新增/扩展建议，不是现有接口声明；字段细节由接口评审确定。

| 合同               | 关键字段或行为                                                                          |
| ------------------ | --------------------------------------------------------------------------------------- |
| 扩展数据集列表查询 | 名称、类型、来源位置、可用性；稳定排序与服务端分页                                      |
| 扩展样本查询       | source_id、scene_id、dimensions、format、availability、quality_status；明确筛选组合     |
| DatasetSavedView   | dataset_id、owner_id、query、query_schema_version、project_context、visibility          |
| QualityFinding     | dataset_item_id、asset_id、rule_id、rule_version、status、evidence_summary、observed_at |
| DatasetSummary     | 总成员/可读原图/仅预览/待核验/异常计数及计数时间                                        |
| 批量操作预览       | query_revision 或固定 item IDs、影响数量、阻断理由、有效期                              |

建议接口：`GET /datasets/{id}/items` 扩展过滤并返回同步分页资源；`POST /datasets/{id}/views` 保存并返回视图资源；`POST /datasets/{id}/quality-jobs` 提交质量分析并返回已有 AsyncJob 引用。其他后台操作同样复用 AsyncJob，不另建任务追踪模型。

动态视图的权限每次执行时重新校验。已保存视图不能成为绕过位置或项目权限的长期读取凭证。

## 6. UI 与跨页边界

- 主操作是创建/接入数据、浏览和管理业务使用；容量、连接配置和迁移入口归 EPIC-09。
- 位置问题显示精简状态和“查看位置”；无管理权限时显示“联系管理员”或可访问的错误帮助。
- 同步/质量任务卡片只做摘要，点击打开既有 AsyncJob 详情。
- 项目摘要点击进入项目权威页面，避免在数据集详情重复实现人员分配、审核流程。
- 冻结版本列表复用训练版本的状态与错误解释，不能出现两个定义不同的“版本已完成”。

## 7. 验收标准

| 编号     | 可验证标准                                                                                                                            |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| E08-AC01 | 从列表进入详情、修改筛选后返回，名称/类型/页码等上下文恢复；复制详情链接可直接打开相同数据集。                                        |
| E08-AC02 | 完成已有目录接入时，界面明确显示“原地引用”，初始原图持久副本预计为 0，并分别显示可选预览占用；未自动创建原图持久副本。                |
| E08-AC03 | 同一数据集含可读、缺失、变化待确认、仅预览四类样本时，列表和检查器展示一致；已登记不被标为已可标注，仅预览样本不能编辑或提交标注。    |
| E08-AC04 | 样本数量超过单页时，网络请求保持分页，页面不请求全量样本；网格不会一次请求全部原图。                                                  |
| E08-AC05 | 同一数据集关联两个标注进度不同的项目时，显示各自进度；切换项目上下文后标签统计同步变化。                                              |
| E08-AC06 | 动态视图保存后新增符合条件的样本会进入视图；已冻结版本的成员和内容不随之变化。                                                        |
| E08-AC07 | 内容摘要未确认的相同文件名不被显示为“精确重复”；近似重复处理不触发自动物理删除。                                                      |
| E08-AC08 | 质量检测后源出现新内容，旧 Asset 的质量结果继续绑定旧字节；新候选显示未检测，不继承旧结果；位置可用性重新观测，旧标注不被移到新内容。 |
| E08-AC09 | 全部筛选结果批量操作先显示固定影响范围；执行前范围变化时重新确认或明确按原快照执行。                                                  |
| E08-AC10 | 无位置管理权限用户可在被授权任务中读取样本，但无法通过检查器、URL 参数或保存视图获取凭据和额外目录内容。                              |

## 8. 迁移与回滚

1. 先引入独立详情页并复用现有数据读取、关联和任务组件，保留旧展开入口跳转。
2. 将连接器页迁移至存储侧，与 EPIC-09 的兼容路由同批上线。
3. 新来源/可用性字段缺失的旧数据按“待核验”或已验证的旧托管位置映射展示，不猜测所有数据健康。
4. 保存视图和质量结果使用新增表/字段，旧接口可以继续工作；不用删除旧数据来完成 UI 切换。
5. UI 回滚可恢复旧列表，但不得把新引用型数据集伪装成旧 MinIO 文件；功能开关控制其管理入口，读写走兼容访问层。

## 9. 测试与验证

- 组件测试覆盖权限、空列表、筛选无结果、扫描中、统计过期、局部失败、键盘焦点。
- 集成测试覆盖旧上传数据集、新目录数据集、混合来源数据集和不同项目上下文。
- 端到端执行“目录注册—预览—关联项目—读取样本—源离线—显示仅预览—恢复读取”。
- 质量用例包含同名不同内容、同内容不同名、无权限目录、损坏图片和内容变化。
- 在代表性样本规模下验证分页、缩略图请求数和交互预算；目标在 M0 记录，不把草案指标当作实测结果。

## 10. 风险与待决策

| 风险/问题                          | 草案处理                                             |
| ---------------------------------- | ---------------------------------------------------- |
| 同一数据集跨项目的标签语义不同     | 所有标注相关过滤/统计携带项目或版本上下文            |
| 质量计算影响正常读取               | 异步低优先级任务；复用已确认元数据，明确更新时间     |
| 管理员希望直接删除重复原图         | 本轮只提供逻辑处理；外部源保持只读                   |
| 保存视图是否跨用户共享             | 默认个人；共享需显式授权，查询结果仍逐次鉴权         |
| 独立详情是否需要同时保留大型展开行 | 默认收敛为跳转；仅保留轻量摘要，避免两套交互长期共存 |

## 11. 工程任务与完成定义

- [x] A：完成独立详情、样本检查器和既有接入向导迁移，覆盖本切片的异常与权限状态。
- [x] 复用现有 URL 过滤工具，增加详情路由和旧入口兼容。
- [x] 接入来源与可用性摘要接口，移除数据集页 MinIO 固定文案。
- [x] 完成项目上下文统计，验证不重复计算跨项目标注。
- [x] B1：实现个人动态视图与组合筛选合同、查询修订和权限检查。
- [x] B2：实现质量结果的最小数据合同、内容修订与规则版本检查。
- [x] B3：将质量批量复核接入固定影响预览和统一任务模型。
- [x] A：完成独立工作台验收与迁移回归，更新用户说明及接口文档；B 范围另行验收。

完成定义：M1 核心故事可以演示端到端目录闭环；M2 增量故事分别验收。所有 UI 状态有后端语义支撑，不以假统计、静态状态或只隐藏按钮完成验收。

## 本项目推进切片与晋升条件

前端列旧展开行能力迁移清单，后端固定分页/过滤/allowed_actions。现有项目 Data Manager 显式 1–200 项批操作不能直接变成全部筛选结果执行；新增快照批处理单独约束预算和并发。近似重复仅在明确需求与夹具存在时启动。

| 切片  | 独立结果               | 交付范围                                                                          | 验收 / 回退边界                                            |
| ----- | ---------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| E08-A | 独立详情和最小目录体验 | 先迁现有文件/Scene/项目功能，再接 E03/E05 真实状态；与 E09 同批兼容旧连接器链接。 | 筛选/分页/深链返回不丢；各项目进度分别展示；仅预览禁编辑。 |
| E08-B | 动态视图和质量         | 服务端组合过滤、个人视图、解码/尺寸/可信精确重复、固定批量范围。                  | 动态视图非冻结版本；新内容不继承质量；近似重复独立触发。   |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[DatasetsPage.test.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.test.tsx)、[datasetsUrlState.test.ts](../../apps/web/src/pages/Datasets/datasetsUrlState.test.ts)、[ImportDatasetWizard.test.tsx](../../apps/web/src/components/datasets/ImportDatasetWizard.test.tsx)、[test_datasets_list_has_scenes.py](../../apps/api/tests/test_datasets_list_has_scenes.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[index.md](../../docs-site/user-guide/datasets/index.md)、[import-images.md](../../docs-site/user-guide/datasets/import-images.md)、[data-versions.md](../../docs-site/user-guide/online-learning/data-versions.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

E08-A 独立工作台、E08-B1 组合筛选/个人动态视图、E08-B2 图片质量、E08-B3 固定范围批量复核、E08-B4 Scene 筛选与 E08-B5 筛选冻结衔接已完成。完整故事/验收矩阵仍需逐条复核，本计划保留在活动目录，未宣称完整 Epic 完成。

- 提交：`e2fe33d5f` 晋升复核，`525f79026` 查询/权限/训练范围合同，`36248a30c` 存储页面，`a683a824f` 数据集页面，`55d0c3051` 实际验收发现的观测刷新和预览并发修复。未分配发布版本。
- 后端：既有 129 项及最终新增 13 项共 142 个不同用例通过。首次组合运行 135 项；审查补充后 workbench 13 项全部通过，重复用例不累计。覆盖两来源共享成员、可见来源选择、旧差异不复活、扫描观测排序、并发删除、权限撤销、唯一位置计数、分项目进度、真实 MinIO 预览、数据集限定冻结和项目任务分页。
- 前端：集成分支 16 文件 160 项通过；观测/预览修复 5 文件 28 项通过；预览有限重试补充后该 hook 8 项通过。去重合计 20 文件 188 个不同用例。typecheck、受影响 ESLint、CSS token lint、完整 pre-commit 和 `git diff --check` 通过；CSS lint 仅保留既有训练比较页两处 z-10 建议。没有宣称全量 Web 或远程 CI 验收。
- 合同与文档：OpenAPI 一致性和本地 TS 生成通过，SDK OpenAPI 合同 4 项通过；新增 Web 专用接口在 coverage manifest 明确排除。文档 frontmatter 196 文件、navigation 198 页面通过。用户说明位于 `docs-site/user-guide/datasets/{index,import-images,storage-connections}.md`，接口说明位于 `docs-site/api/guides/{datasets,storage-locations,training-datasets}.md`；`README.md`、`docs-site/dev/reference/storage-buckets.md` 和 `CHANGELOG.md` Unreleased 同步。
- 实际浏览器：当前工作树 3100/8100，无 mock。列表名称筛选返回、旧 dataset/source/scan 链接直接跳详情、独立四页签、网格和单项检查器、来源↔位置保留扫描/列表上下文、按位置过滤数据集、关联训练版本深链、深浅主题截图和检查器 Escape 焦点恢复通过。独立存储页面网络没有数据集全量目录或 `/storage/buckets` 请求；真实节点探测成功，九用途和未知容量显示正确。导入向导往返后路径/匹配规则/递归选项恢复，未创建测试连接或数据集。
- 真实数据观测：D-7 专用来源只读扫描和完整预检显示 1 项已核验、1 项缺失、1 项内容变化，三个原成员/任务/标注保留；预检完成后工作台和检查器刷新，三张旧内容预览正常，异常项没有原图读取动作。训练入口预选 D-7，明确确认后实际 `/tasks` 请求带 dataset_id；单项预检报告 5,959 字节并正确阻断不足两张合格图片的创建。原 ready 版本的三张原图、标签和 ZIP 摘要再次校验不变，未新建模型训练。
- 清理：仅销毁经 owner/零客户端核对的 `1822b025369a33f4:test` 资源；测试日志、报告、缓存、截图和专用浏览器会话清理。两份任务工作树在干净且补丁等价已验证后由 Orca 删除；人工 dev 环境、九用途桶、演示来源、已有冻结版本和 `.worktree/manual` 保留。未重建后端镜像、未推送、未发布。

- E08-A 验收时的剩余范围：动态视图与尺寸/扩展名组合过滤已由下述 B1 完成；质量任务与规则修订、可信精确重复治理、全部筛选结果固定批量范围继续属于 E08-B。当前训练入口限定整个所选数据集，再在项目内选择批次/任务/标注范围，不自动冻结当前页面的全部筛选结果。旧托管训练版本只能按现存任务推导数据集关联，任务删除或解绑后的历史血缘展示仍有限；已冻结快照和媒体不改写。

### E08-B1：组合筛选与个人动态视图

- 实现提交：`5bb8346cd`，基线 `7cfe292ce`。B1 独立交付，未分配发布版本，完整 E08 尚不归档。
- 合同：在既有来源/可用性/文件名条件上叠加登记媒体类别、文件扩展名、已知/未知尺寸与宽高范围，所有条件先过滤再计数/分页。未知尺寸不参与范围；jpeg/jpg 同组；多模态仍为数据集容器类型。个人视图只保存有界白名单条件，按父数据集/当前用户隔离，重名409、最多100项；源条件保存和查询时重新鉴权。目录只读浏览者可保存个人偏好，不能借此修改数据集。
- 界面：查询条件与分页/表格网格独立保留；单独视图 ID 链接先恢复再查询，失效视图或来源明确失败。支持另存、更新、恢复、重命名和删除；弹层冻结目标和条件，URL或会话变化后旧回调失效。清除高级条件同时清理未应用草稿；390px 下视图下拉占满一行，提示使用11px语义辅助色并置于控件外。
- 本地验证：后端22项（工作台、视图私有CRUD/父子隔离/重名/上限/权限撤销/动态成员、0206迁移往返、模型一致性和OpenAPI）全部通过；前端4文件39项通过，包含仅ID恢复阻止无条件查询、失效视图、范围冲突、持续输入焦点、草稿清理、切换资源/同数据集URL后迟到回调；SDK OpenAPI合同4项通过。完整 pre-commit（Ruff、Prettier、Web lint、TypeScript、OpenAPI与路由生成）及 `git diff --check` 通过。仅保留既有4条ESLint警告和训练比较页2条语义层级建议；未运行远程CI或全量Web套件。
- 实际浏览器：当前3100/8100，无mock。D-7按来源+verified+png+宽至少900/高至多700筛得1个vehicle-3；保存、仅ID深链恢复、重命名、未保存修改、更新条件再次恢复和删除均通过。Escape后焦点返回“更新条件”；居中弹层、1440×1000与390×844、明暗主题截图检查通过，窄屏根宽390，无页面横向溢出；提示为11px muted token。当前流程无失败网络请求或控制台错误。删除临时视图后原筛选保留，清除后仍为原3个演示样本。
- 运行与文档：当前人工dev库已通过所属服务重启应用0206；8100/3100、普通/维护worker与beat继续运行，未重建镜像。用户指南、API指南、README、CHANGELOG、OpenAPI快照、本地TS类型和SDK排除清单同步；文档196文件frontmatter、198页面导航检查通过。
- 清理：已通过owner/连接校验销毁 `1822b025369a33f4:test`，删除临时视图、专用浏览器会话、JUnit、截图、测试缓存及本次误建的空SDK虚拟环境；保留人工环境、演示来源和已有训练版本。未推送或发布。
- 下一切片建议：E08-B2质量任务，先处理解码、尺寸与可信精确重复的内容修订绑定和可恢复结果；全部筛选结果的固定批量范围另作后续切片，仍需独立复核与验收。当前保存视图不自动成为训练版本的固定成员集合。

### E08-B2：图片质量与可信精确重复

2026-10-23 完成，实现提交 `233577c94`。数据集新增“质量”页签，固定整份数据集的 Item/Asset/SHA/size 与尺寸规则；检测原图解码、逻辑尺寸及本次完整字节证明形成的精确重复组。未核验、非图片、预算、不可读和变化明确跳过，不误报坏图。结果支持分页、重复组与复核状态过滤、历史运行、居中复核弹层和有原因/乐观修订的审计决定；不自动删除、合并或改变训练资格。

运行复用 AsyncJob；generation/task/job/租约 fencing、逐成员 checkpoint、分块和按引用节点续派。取消保留证据，恢复只消费原固定成员中的 pending；过期租约由 Beat 恢复，迟到任务和旧失败信号无权覆盖。0207 仅增加运行/结果表，Item 删除保留历史 UUID，任务保留策略不删除证据；已完成迁移往返验证。

验证：API/DB 62 项通过（质量、严格内容读取、引用、通用任务），覆盖 MD5 提示相同而 SHA 不同、坏图/零字节/截断、不可读/变化、EXIF、PIL 预算、固定新增/删除、取消恢复、过期租约、旧信号、撤权、复核冲突和父子权限。前端相关测试分批共50项通过（质量页/终态刷新/URL/数据集详情/共享任务）；SDK 合同4项通过。Web 类型、ESLint/CSS tokens、Ruff、文档 frontmatter196/导航198 均通过；仅存在原有4条 ESLint warning 与2条无关层级语义化提示。本地检查不代替远程 CI。

人工 dev 环境已升级0207，Worker 实际注册 detect_quality/recover_quality，Beat 保持运行，未重建镜像。通过页面提交真实质量运行 `1bb72614-70dc-4a76-8cf2-448b2772bce9`：D-7 三个固定引用成员，1个正常、1个源变化、1个源路径缺失；已核对复核保存/筛选、共享任务详情回到对应质量运行、焦点恢复和网络/控制台。1440桌面及390窄屏、明暗主题截图检查完成；详情居中、提示位于下拉控件外并使用弱化文字，窄屏结果表内部横向滚动。人工环境保留这一真实检测记录与复核供用户继续查看，原3样本/来源/冻结 ready 版本保持不变。

一次性 test 数据库、Redis、九用途桶已按 owner 校验销毁；独立浏览器已关闭，截图、JUnit、测试缓存与类型中间产物在提交后清理。整个 E08 继续保留为活动计划：下一切片为 B3 固定批量范围、影响预览与统一任务执行；近似重复、未核验旧图的身份升级、共享视图和训练视图冻结仍不在本次范围。完成后重检必须新建运行，不重试本轮已完成的 skipped 项。

### E08-B3：固定范围批量复核

2026-10-23 完成，实现提交 `944ebbf08`，交付收尾同步补入 URL 批次变化时的同步 owner 核对。质量页支持跨页勾选最多200项或选择最多10000项的全部筛选结果，复用质量列表 SQL；先物化固定成员、完整检测证据、规则/配置和复核修订，预览摘要绑定决定与原因，10分钟内按原 snapshot 明确确认。预览可分页查看固定名单，历史绑定明确提示且仅复核旧证据；检测未完成成员固定为阻止项，后续新匹配结果不扩张范围。本切片覆盖 E08-S08 / E08-AC09 的质量复核路径，不放大样本删除或项目任务批操作。

0208增加批次与成员账本，复用单项复核和 AuditLog；Result 行锁刷新旧会话状态，逐项比较证据和 review_revision。决定、审计和 checkpoint 同事务提交，冲突跳过并继续；部分完成展示独立计数。AsyncJob 使用专用 kind，当前管理/来源/key 权限贯穿列表、详情、确认、取消、恢复和 Worker。重复确认仅首次派发，任务正常保留清理后仍可返回账本；恢复仅处理 pending，不重复成功审计。租约恢复、task/job/generation fencing 和旧失败信号保护均接入。共享任务详情准确返回父运行和批次。

本地验证：API/DB 51项通过（质量批量、既有质量与来源授权），含筛选跨页固定、新增不扩张、历史证据、修订/事实冲突、重复确认及任务保留清理、事务回滚、取消恢复、过期租约、旧信号、管理角色与 API key scope/轮换、迁移往返。前端6文件40项通过，覆盖固定预览确认、改动废弃预览、过期/阻止、关闭/换批次后的迟到回调、终态成员刷新、父子归属、部分完成及共享任务深链；同时修复质量筛选缩页后的页码回收和单项弹层关闭后旧请求导航。SDK合同4项通过，新增6个路径的旧操作合同比较无变化；新增端点在 SDK 清单明确为 Web 专用。类型、Ruff、ESLint/CSS tokens、文档196文件frontmatter与198页面导航检查通过；仅有原有4条ESLint warning及2条无关层级建议。本地检查不代替远程CI。

人工dev库已升级0208，普通Worker实际注册 review_quality/recover_quality_reviews，Beat恢复周期30秒，3100/8100及普通/维护Worker健康，未重建镜像。D-7实际提交并保留批次 `24b3390c-3421-47f4-aad4-ec428432b89c`：固定3项、成功3项、冲突/阻止各0；原有源变化/缺失检测证据不改写。检查全部筛选与勾选预览、固定名单分页大小、提交/终态、共享任务回到确切批次、明暗主题1440桌面与390窄屏截图；窄屏根宽390，展开名单后弹层内部滚动且确认按钮保留，提示11px muted并位于下拉框外。验收流程无失败API或控制台错误。原数据集仍3个样本，原冻结版本仍ready。

正式说明同步至 `docs-site/user-guide/datasets/index.md`、`docs-site/api/guides/datasets.md`、`docs-site/dev/concepts/storage-connections.md`、README及CHANGELOG Unreleased；OpenAPI、本地TS合同、SDK覆盖清单和生成路由同步。临时未执行预览已精确清理，已完成的演示批次保留供人工查看；一次性test库、Redis及九用途桶按owner校验销毁，独立浏览器关闭，截图、测试报告、缓存及类型中间产物在提交后清理。未推送、发布或分配版本。

剩余范围：筛选视图到训练冻结的成员衔接仍由E10-B/C协同，不把动态个人视图当作冻结版本；近似重复、未核验旧图片身份升级和共享视图不在本切片。后续候选是E12可信用量采样与E09-B运维视图，须先复核现有计量、过期状态和任务合同。

### E08-B4：Scene 筛选与视图范围

2026-10-23 完成，实现与本节记录同提交。Scene 作为主条件贯穿 URL、服务端组合筛选和个人动态视图，按当前数据集的 Item.scene_id 过滤，包含多模态与校准成员；与其他条件先组合再计数分页。新增轻量、有界、可搜索的 Scene 选项，独立保留当前选中项。失效、删除或跨数据集的 Scene 明确404，非法 UUID 为422；不会悄悄移除条件。清除更多格式/尺寸条件保留 Scene。此切片补齐 E08-S04 的场景条件，不改变质量批次或训练范围。

本地验证：API/DB 25项、Web 45项、SDK 合同4项，共74个不同用例通过。覆盖多模态/校准/未分组、组合/count/分页、选中项跨页/搜索、轻量字段、非法/跨数据集/删除 Scene、动态成员和视图保留，以及会话迟到响应和撤权隐藏名称。OpenAPI、本地生成类型、SDK 覆盖、Ruff、Web TypeScript、ESLint/CSS tokens、文档 frontmatter196/导航198和 diff 检查通过；原有4条 ESLint warning 与2条无关层级建议保留。未运行远程 CI。

真实浏览器使用独立3200/8200 e2e 环境，无 mock：23个 Scene、5个样本，其中选中 Scene 的3项含点云、摄像头和 frame_index 为空的校准成员。选项翻页、名称查找不改变当前3项，保存个人视图并仅凭视图ID恢复、切换质量页再返回均保留同一范围。无效 Scene URL 保留条件并显示错误，列表0项；只有显式选“全部 Scene”后才恢复5项。1440×1000与390×900明暗截图检查通过，窄屏选项和提示正确换行，无页面横向溢出；流程无控制台错误或非预期失败请求，故障用例的两类404为预期。

当前3100/8100人工环境、普通/维护 Worker、既有来源/质量/复核/冻结记录保持；doctor 确认 API/Web 和 Worker 健康。本切片没有迁移、Worker 改动或镜像重建。正式用户/API指南、README、CHANGELOG、OpenAPI、SDK清单及路线索引同步。一次性 test/e2e 数据库、Redis、九用途桶和任务自有23个 Scene/个人视图随模式销毁；专用浏览器已关闭，截图、日志、测试报告及缓存清理。未推送或发布。

下一切片为 E08-B5/E10-B1：从当前筛选视图固定训练成员，复用训练预检、成员摘要、标注快照和现有版本，不将动态查询当成已冻结集合。完整 E08 继续保留活动状态。

### E08-B5：当前筛选进入训练冻结

2026-10-23 与 E10-B1 同提交交付：个人视图先恢复具体条件，再通过居中项目选择进入既有冻结流程；不以当前页或动态视图ID代替完整定义。任务勾选与预检共用服务端过滤，成员变化要求重检，原创建请求和旧版本保持固定。实际验证、181个不同本地用例、浏览器ready版本/包字节及清理证据见 [E10-B1 Outcome](1792661016_dataset-versions-training-consumption.md#outcome)。E08-S07与AC06/AC09的筛选冻结路径已补齐；完整Epic仍待最终故事/验收矩阵核对，暂不归档。

## E08-B6 实施合同：项目上下文统计与准确跳转

基线 `32ccbd3a9`。本切片对应 E08-S06 / AC05：同一数据集在不同关联项目中显示各自进度与标签分布，并进入带本数据集条件的项目任务列表。复用 Data Manager summary 与现有结构化筛选，不新增接口、业务表、依赖或镜像，不展开 Tracker、会话或审核专项。

超过 8 个文件，准确范围：后端 `services/dataset_workbench.py`、`services/data_management/schema.py` 与项目统计／筛选的回归测试；Web `pages/Datasets/{DatasetDetailPage,DatasetProjectStatistics}.tsx`、`datasetsUrlState.ts` 及对应测试，`pages/Projects/data-manager/datasetScope.ts` 及 URL／首个查询合同测试；正式数据集用户／API 指南、README、CHANGELOG、本计划和执行索引。生成合同仅在公开结构实际改变时更新。隔离树分别负责项目计数、数据集统计 UI 和筛选字段／链接，主树集成、文档与运行验收。

项目卡复用既有任务可见性与实际项目身份，按直接 Item 优先、primary 主 Item 兜底的数据集归属计数。相机辅助链接不重复归属整个任务；独立原件不因项目关联而被推断属于数据集。保留仍存在的历史成员，不以来源离线、变化或注销删除统计。保持有界数据库查询，不对每个项目分别请求标签统计。

标签区仅查询当前选定的可见关联项目，URL 使用 `stats_project` 恢复选择。未指定时默认首个；明确指定但失效时显示错误，不自动替换。沿用现有 summary 的有效、未取消 Annotation 口径，显示“标注对象数”；相机标注纳入、紧凑轨迹按一条，不把标签对象数解释为样本／帧／候选数。数据集任务数使用匹配任务数，不能使用全项目可见总数。项目、数据集或认证变化时旧统计立即隐藏，取消在途请求并拒绝迟到结果；失败提供重试，不伪装零值。

项目链接通过现有 URL codec 构造全新 tasks 列表链接，带 `dataset.dataset_id eq ID`，类别链接再 AND `annotation.class_name eq 原始类别`。发布已有数据集筛选字段，保证前端接受并执行该条件，首个任务与统计请求即限定数据集。筛选作为可见普通条件；用户后续主动清除或切换视图沿用既有语义。本切片不新增覆盖项目整页的固定范围机制，也不改成员、绩效或选择恢复。

验证使用所属一次性 test/e2e 环境：两个项目的类别差异、其他数据集排除、隐藏批次和真实成员身份、主样本兜底与相机辅助归属、无效／取消标注；Web 选择恢复、失效选择、切换和迟到响应、请求失败、标签链接与实际首个筛选查询。实际浏览器检查明暗主题、桌面／窄屏、切换项目、标签跳转、刷新返回、控制台及失败请求。保留 3100／8100 人工环境与既有业务，清理任务自有夹具和中间产物。回退仅还原查询与页面代码，无业务数据迁移；未完成整份矩阵和代表规模性能验收前不归档 E08。

### 2026-10-25 剩余验收复核

本节记录 B6 实施前的只读核对，不代表本节当时新增了功能或运行验收。AC05 当时仍缺所选项目标签统计和带数据集条件的正确项目链接；具体实施与本轮证据见下方 B6 Outcome。

AC01–04、AC06–10 的核心能力分别已有 A/B1–B5、E03 原地注册、E04 内容新修订和 E10 固定训练成员的实现与历史证据，最终矩阵仍须逐项关联，不能只依据切片完成状态归档。分页已有 count/limit/offset 与当前页序列化，缩略图采用惰性加载；代表规模的 p50/p95、查询数及请求预算实测尚未记录，不宣称十万图性能资格。

数据集视图仍仅个人可见，项目 Data Manager 的共享视图不能代替数据集共享合同；近似重复依原计划等待明确需求和夹具。两者保留为条件后续，不为结束 Epic 擅自扩大范围。固定批量操作目前资格化质量复核和训练冻结，不宣称任意全部筛选结果的删除或关联。用户指南已补上实际存在的“质量”页签；整项 E08 继续活动。

## E08-B6 Outcome：项目上下文统计与准确跳转

2026-10-25 随本节提交交付。项目卡按当前实际项目职责、批次和任务分派裁剪，直接样本优先、primary 主样本兜底；相机辅助链接不重复归属，来源状态不删除历史成员。按权限组复用既有谓词，一次聚合，不逐项目追加数据库查询。后续只按原始 Epic 故事／验收推进，独立技术债和额外演示形式不升级为验收前置。

“项目关联”复用现有 summary 展示所选项目内的数据集任务数和有效标注对象分布，明确对象与任务数量区别。`stats_project` 支持刷新、返回与显式失效选择；切回项目重新查询，数据集／项目／认证变化、撤权错误和迟到响应均不展示旧结果。项目名称进入普通数据集筛选，标签入口再组合类别条件；发布已有 `dataset.dataset_id` 字段，首个实际任务及统计请求均执行该筛选。未引入固定项目整页范围机制。

主树本地 105 个不同 API／数据库／筛选与 OpenAPI 用例、100 个 Web 用例通过。真实 SQL 覆盖两项目类别差异、其他数据集、direct／primary／camera 归属、实际项目身份变更、隐藏批次、撤销成员、无效／取消标注和零匹配；关联项目由 1 增至 16 时查询次数保持不变。这是查询次数回归，不是代表规模的延迟证明。首轮旧训练用例缺内容登记被严格预检拒绝，已仅修正其夹具并复验；新筛选测试删除了依赖 SQL 临时别名的重复结构断言，改验公开支持的运算符，实际归属由真实数据库用例证明。生产校验未放宽。

独立 3200／8200 浏览器无 mock：车辆项目 3 个可见任务、4 个对象（car 3、person 1），道路项目 2 个任务、road 2；隐藏批次及其他数据集不混入。car 链接准确返回 2 个任务，项目名称准确返回本数据集的 3 个任务；实际 HTTP 请求正文均核对数据集条件。项目选择刷新／返回保持，失效项目不查询并允许明确重选。1440×1000 与 390×900 明暗截图检查通过；根宽 390、提示 11px muted 且位于控件外，无横向溢出、页面异常、控制台错误或失败 API 请求。单元回归另覆盖生产默认 30 秒缓存下的切回重查，以及撤权后隐藏旧项目卡和统计。

人工 3100／8100 健康，十二表 155 行、二十四对象及十二源文件指纹保持；没有迁移、Worker 或镜像变化。正式用户／API 指南、README、CHANGELOG 与执行索引同步。本轮仅本地检查与实际浏览器验收，不代表远程 CI、真实部署规模或整个 Epic 完成。

一次性 test／e2e 模式已按 owner 销毁，复查数据库、Redis、各九用途桶均不存在且所属会话停止；专用浏览器关闭。夹具脚本、截图、原始网络／控制台记录、日志及本轮缓存已清理，保留不含凭据的结构化验收报告。

### 原始故事／验收收口矩阵

“历史”引用上文 A／B1–B5、E03／E04 和 E10 Outcome，本轮未重复全部历史验收；不能用切片状态代替仍缺的证据。

| 验收                        | 对应故事 | 证据与当前结论                                                                                                                 |
| --------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------ |
| AC01 详情、筛选与返回       | S01      | A／B1／B4 历史与详情 URL 回归已证。                                                                                            |
| AC02 原地登记与独立预览体量 | S02      | E03 历史、`test_dataset_registration.py` 的零原图复制与分列体量已证。                                                          |
| AC03 四态与预览禁写         | S03      | 工作台状态、检查器和严格引用写入的分项历史已证；归档时关联这些证据，不额外要求重新集中演示。                                   |
| AC04 分页与网格请求         | S04      | 分页／当前页序列化／惰性预览已证；代表规模的请求量及交互预算尚未测量。                                                         |
| AC05 项目进度与标签上下文   | S06      | 本轮 B6 的实际 SQL、Web 和浏览器证据通过。                                                                                     |
| AC06 动态视图与固定版本     | S04／S07 | B1／B5／E10-B1 历史、动态成员与已就绪包保持的测试已证。                                                                        |
| AC07 可信重复与无自动删源   | S05      | B2／B3 与质量测试证明完整 SHA 精确重复及人工复核；近似重复仍为条件后续。                                                       |
| AC08 新内容不继承旧结果     | S05      | 质量证据固定 Item／Asset／SHA，新修订创建新 Item／空白任务、保留旧标注的分项历史已证；归档时核对证据衔接，不默认新增串联专项。 |
| AC09 固定全部筛选范围       | S08      | B3 质量复核与 B5 训练冻结已证，不外推任意删除／关联操作。                                                                      |
| AC10 任务授权与位置管理隔离 | S02／S03 | 引用任务读写、检查器／来源裁剪与个人视图重新授权的历史已证。                                                                   |

下一阶段补原始第 9 节的规模测量，并关联既有验收证据：记录实际数据规模、一个放大档位、机器／存储／格式分布与并发，预先写明测量预算，再测列表／详情分页／组合过滤／表格网格／检查器／项目统计的 p50／p95、SQL 数、响应体与预览请求数。没有预先预算只称建立基线，本地合成档位不冒称生产代表规模；不绕过导入护栏。共享视图、近似重复、额外集中演示和其他项目管理机制不自动成为该验收前置。完整 E08 继续活动。
