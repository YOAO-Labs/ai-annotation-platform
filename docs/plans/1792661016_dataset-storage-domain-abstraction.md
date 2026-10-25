# EPIC-01：领域模型与存储抽象

> 状态：执行中；内容／位置身份、回填、跨来源读取及受控目录接入已由后续切片交付，正在核对原始收口矩阵；不分配发布版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 已按 [backlog promotion gate](backlog/README.md#promotion-gate) 复核；实施基线为 `f98705535`。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 项目          | 内容                                                                                                                                    |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| 状态          | 身份与读取基础已交付，保留原始兼容／回退验收收口                                                                                        |
| 日期          | 2026-10-22（Asia/Shanghai）                                                                                                             |
| 优先级 / 阶段 | P0；M0 契约，M1 兼容基础，M2 收敛旧入口                                                                                                 |
| 负责角色      | 后端负责人、数据库负责人；前端与测试参与契约评审                                                                                        |
| 前置条件      | 当前数据模型、9 类平台存储用途和访问调用方盘点                                                                                          |
| 下游          | E02、E03、E04、E05、E06、E07、E10、E11、E12                                                                                             |
| 关联文档      | [总体架构](backlog/1792661016_dataset-storage-architecture.md)、[实施与验收](backlog/1792661016_dataset-storage-delivery-acceptance.md) |

## 第一阶段实施定案：E01-A

### 结果与决策

最小方案是继续按名称写对象，只补一张映射表；它无法消除同名上传覆盖旧任务原图的风险。本阶段采用唯一托管键、独立内容/位置映射和分批回填，复用唯一的 StorageService I/O owner。不引入新服务、依赖、环境变量或公开路由。

- 新上传、ZIP 和连接器复制使用 `__aap_managed_datasets__/<dataset UUID>/_objects/<upload UUID>/<原相对路径>`。每次写入独立分配 UUID，禁止覆盖其他 Item；分配逻辑文件名时持有 Dataset 行锁，重名在扩展名前追加序号，保留相对目录。
- 迁移新增四表：storage_locations、storage_bindings、dataset_assets、dataset_asset_locations。Asset 通过唯一 dataset_item_id 绑定原样本；采用反向外键，回填完全不更新 DatasetItem，避免 updated_at 导致导出快照漂移。每个样本独立内容身份，不跨数据集合并。
- 默认平台位置只引用既有运行配置，不复制凭据。运行时首次登记/回填创建九用途固定绑定；配置与已登记绑定不符时拒绝重新解释历史位置。本阶段没有切换绑定的 API。
- 回填只复制实际 file_path 和旧 hash hint；历史摘要没有 provenance，一律 unverified。新字节流同时计算兼容 MD5 和可信 SHA-256；扫描的 ETag 单列保存，不参与内容去重。上传完成按有界分块读取对象核验实际字节，不能把 ETag 当 MD5。
- Dataset 新增 storage_scan_prefix：升级时固定当时已配置的名称前缀，新建数据集固定 UUID 前缀。扫描同时覆盖新托管根与已固定旧根；改名不改扫描根。升级前已改名且未登记的旧根不猜测，须在后续来源配置显式接入。
- 新 Item 在 metadata.storage_relative_path 保存逻辑路径，Scene、点云帧分组和导出统一消费；扫描重名只改逻辑路径，不改真实键。旧样本沿用兼容解析，不回填 metadata；原 Item/Task 路径、Scene/frame、项目关系、历史冻结 JSON 和金字塔 generation 均不改。已核验 Asset 摘要在数据库中不可改写。

最脆弱的假设是旧 file_path 指向的内容仍存在。回填不读取字节，只声明已映射、未核验；即使原件离线、缺失或变化，也不会声称恢复了历史内容。

### 准确实施范围

这是一项超过 8 个文件的兼容改造，按以下 owner 划定范围：

- 模型与迁移：`apps/api/app/db/models/dataset.py`、`dataset_asset.py`、`__init__.py`，`apps/api/alembic/versions/0194_dataset_asset_identity.py`（当前唯一 head 为 0193）。
- 身份与写入：`apps/api/app/services/dataset_storage_keys.py`、`dataset_assets.py`、`dataset.py`、`apps/api/app/api/v1/datasets.py`。
- 路径语义消费者：`apps/api/app/services/scene_inference.py`、`pointcloud_import.py`、`exporting/packaging.py`、`exporting/lidar_preflight.py`、`apps/api/app/workers/export.py`。
- 回填命令：`apps/api/scripts/backfill_dataset_assets.py`，提供明确 dataset/all 目标、dry-run/apply/verify、有界 keyset、批次游标、逐批提交和只读校验；重跑从头扫描并跳过完整映射，游标可缩小续跑范围，不宣称持久任务调度。
- 验证：新增 `apps/api/tests/test_dataset_assets.py`、`test_dataset_asset_backfill.py`、`test_migration_0194_dataset_asset_identity.py`；同步 `test_dataset_import.py`、`test_datasets_upload_zip.py`、`test_export_packaging.py` 的键断言；新增 `test_export_lidar.py` 的真实导出别名回归，复用 Scene、关联、金字塔与训练桥接回归。
- 正式文档：`docs/adr/archive/0091-dataset-content-and-location-identity.md`、`docs-site/dev/concepts/storage-connections.md`、`docs-site/dev/reference/storage-buckets.md`、`docs-site/user-guide/datasets/import-images.md`、`docs-site/api/guides/datasets.md`、`README.md`、`CHANGELOG.md`，以及本系列草案导航与引用。API schema 不变，核对 OpenAPI 快照无需改动，不生成虚假 SDK 能力。

```text
上传/ZIP/连接器 -> 唯一托管键 -> 现有 StorageService -> datasets 桶
       |                              |
       +-> DatasetItem -> Asset + AssetLocation -> 固定平台位置/九用途
扫描/回填 ----------^ (真实旧键保留；未读字节不标核验)
Scene/导出 -> 逻辑相对路径助手；标注/训练/预览继续读现有实际键
```

### 验收和回退

1. 在本 checkout 的 worktree test 模式验证 Alembic 0193→0194；新增表约束和已核验摘要不可变生效，旧 writer 可继续写无映射样本。
2. 覆盖历史改名、占位上传、未知/32字符 ETag、同名不同内容、同名不同数据集、零字节、子目录及重复清理。新物理键唯一；重复内容仅按已核验同数据集原件判断；旧 hash 作为候选实际读字节一致后懒核验，不能盲信。
3. 回填 dry-run/verify 使用 PostgreSQL READ ONLY；apply 小批提交。验证重跑、中断续跑、错误回滚、真实定位一致、Item updated_at/任务/Scene/项目关系/冻结输入不变。报告只输出 ID/计数/错误码，不含对象路径或凭据。
4. 执行 `pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest -q tests/test_dataset_assets.py tests/test_dataset_asset_backfill.py tests/test_migration_0194_dataset_asset_identity.py tests/test_dataset_import.py tests/test_datasets_upload_zip.py tests/test_export_packaging.py tests/test_scene_inference.py tests/test_dataset_link.py tests/test_image_pyramid.py tests/test_training_dataset_bridge.py'`。精确剩余边界记录在 Outcome。
5. 运行变更 Python 的 Ruff、文档构建、Markdown 链接与 `git diff --check`；删除本轮临时日志/报告/pytest缓存和拥有的 test/dev 资源。当前 checkout 无运行中的 API/Celery；不重启其他 checkout 的共享服务。
6. 回退应用时保留 0194 和新增元数据，停止新写入，旧 file_path 仍直接可读；不做破坏性降级，不重算历史键。自动目录接入、全部消费者切新 resolver、外部源覆写保护、九用途历史对象迁移属于 E02/E05/E01-B，不能按本阶段宣称已验收。没有公开界面变更，不运行无关 GPU/浏览器验收。

本次 E01-AC01/02/04/06 验证对应子范围，AC03/05/07/08/09 的跨来源读取、能力与绑定切换仍由后继切片完成。下文保留完整 Epic 目标，未执行项不是本阶段已完成项。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                           | 已有能力、缺口与复用约束                                                                                          |
| ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| [dataset.py](../../apps/api/app/db/models/dataset.py)                           | Dataset/Item/Scene/ProjectDataset 已有；Asset/Location/Source 是新增关系。保留 item、task、Scene 和项目关联身份。 |
| [dataset.py](../../apps/api/app/services/dataset.py)                            | ingest_one 以名称写新键，scan_and_import 按当前名称扫描；改名不搬旧对象，回填必须使用真实 file_path。             |
| [image_pyramid.py](../../apps/api/app/db/models/image_pyramid.py)               | 已有派生资产 generation、源指纹、SHA-256 和构建租约，DerivedArtifact 复用该 owner。                               |
| [project_task_dataset.py](../../apps/api/app/db/models/project_task_dataset.py) | 项目规范任务流及 membership 已交付；位置/来源信息不得重算管理序号或历史冻结摘要。                                 |

候选修改入口：[dataset.py](../../apps/api/app/db/models/dataset.py)、[dataset.py](../../apps/api/app/services/dataset.py)、[storage.py](../../apps/api/app/services/storage.py)、[datasets.py](../../apps/api/app/api/v1/datasets.py)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 要解决的问题

当前数据集样本主要以文件路径描述原图，访问过程依赖全局 datasets 桶。外部来源通过导入流复制到该桶后才能进入现有主链路。数据集名称同时参与物理对象键的构造，名称变化与既有对象路径之间存在耦合。[代码依据：数据集服务](../../apps/api/app/services/dataset.py)、[样本模型](../../apps/api/app/db/models/dataset.py)

本 Epic 建立一个可渐进迁移的基础：数据集、样本、某一版原图内容、原图所在位置具有不同身份。以后新增 NAS、外部 S3 或远端机器时，标注与训练业务不需要知道底层品牌和绝对路径。

## 2. 目标与边界

### 目标

- 数据集重命名只更新业务名称，不移动原图、不改变样本身份。
- 同一数据集可以有多个来源，同一内容修订可以有多个可读位置。
- 现有 DatasetItem ID、项目关联、标注任务与训练版本继续有效。
- 定义统一的读取、列表、写入和能力查询契约，由实现声明支持范围。
- 为原地目录接入提供最小基础，不要求先完成全部多站点能力。

### 本 Epic 不承担

- 目录选择与扫描向导：E03。
- 自动同步与变化裁决：E04。
- 缓存、文件传输与浏览器访问：E05。
- 迁移到具体 S3 产品：E07。
- 生命周期与授权政策：E11；本 Epic 保留必要字段与服务校验入口。

## 3. 用户故事

| 编号    | 用户故事                                         | 交付结果                                           |
| ------- | ------------------------------------------------ | -------------------------------------------------- |
| E01-S01 | 作为数据管理员，我希望改数据集名称不影响已有文件 | 以稳定 ID 生成新对象前缀；保留旧对象的真实键       |
| E01-S02 | 作为标注员，我希望相同样本始终对应相同图片内容   | DatasetItem 绑定固定内容修订，不随源文件覆盖而变化 |
| E01-S03 | 作为管理员，我希望目录和对象桶可以共存           | 位置与内容的关联可表达文件系统、S3、后续 Agent     |
| E01-S04 | 作为维护者，我希望替换存储服务不修改全部业务代码 | 业务通过存储访问契约和用途绑定定位资源             |
| E01-S05 | 作为既有用户，我希望升级后旧任务和版本仍能使用   | 增量迁移、旧路径兼容、可核对的回填报告             |
| E01-S06 | 作为测试人员，我希望区分来源不可用与内容被改变   | 位置状态、引用状态、校验状态分开表达               |

## 4. 数据模型草案

以下是建议模型，字段名和路由尚未在项目实现。

| 实体            | 主要字段提案                                                                                      | 不变量                                                  |
| --------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| StorageLocation | id、kind、site_id、connection_ref、root_scope、capabilities、enabled_state、health                | 表示受控可访问位置；凭据单独加密引用；启用与健康分开    |
| StorageBinding  | purpose、location_id、prefix、generation                                                          | 业务用途与存储位置分离；切换可审计                      |
| DatasetSource   | id、dataset_id、location_id、relative_root、filters、ownership_mode、sync_policy                  | 来源 ID 稳定；路径变化需重新核验范围                    |
| DatasetItem     | 既有 id、dataset_id、asset_id、source_id、source_entry_key、scene/frame 信息                      | 样本身份与名称、位置无关；有业务引用后不静默改 asset_id |
| Asset           | id、media_type、size_bytes、checksum_algorithm/value、verification_state                          | 表示某份确定的内容修订；确认后字节和摘要不可变          |
| AssetLocation   | asset_id、location_id、relative_locator、role、object_version、etag、reference_state、verified_at | 表示该内容的一个原始、托管或缓存副本                    |

AssetLocation 的“副本”必须与所绑定 Asset 的完整内容字节一致。缩略图、转码等派生内容通过逻辑 `DerivedArtifact` 关系指向原 Asset，并记录自身摘要、规格、处理器版本与对象引用；优先扩展既有 thumbnail/media 模型，不把缩略图冒充原图副本。

对于头像、附件、审计归档、训练包等非 DatasetItem 对象，统一定义 `StoredObjectRef` 逻辑契约，记录实际位置/配置修订、对象键或不可变绑定代次。可复用现有字段，不要求新建统一大表。用途绑定只决定新写入；历史对象不能每次按当前绑定重新解释。

### 身份与校验规则

1. `source_entry_key` 只是某个来源内的条目标识，不能单独充当内容身份。
2. `size + mtime`、文件 inode、S3 ETag 都属于观测信息；不统一等价为内容摘要。
3. 新内容默认采用 SHA-256；历史 MD5 记录保留算法标识，允许后台补算，不伪造 SHA-256。
4. 未校验记录可以进入目录索引，但必须显示待校验状态；用于标注或冻结前完成规定的内容核验。
5. 内容完全相同也不自动跨授权边界合并对象；去重不能透露其他用户的数据是否存在。
6. 不同路径同内容可以形成重复组，不自动合并 Scene、标注任务或样本身份。
7. 原路径被新内容覆盖时，旧 Asset 保持不变；旧位置标记 changed，新内容等待 E04 的明确接纳流程。

### 业务用途绑定

首批显式覆盖 `annotations`、`datasets`、`bug-reports`、`media-cache`、`audit-archive`、`import`、`export`、`avatars`、`training` 九类用途。迁移初期全部可指向现有部署，不要求拆成九个服务。

现有全局存储服务已集中声明这些用途，是兼容改造入口。[代码依据：StorageService](../../apps/api/app/services/storage.py)

## 5. 服务契约草案

| 能力           | 建议契约                                     | 约束                                           |
| -------------- | -------------------------------------------- | ---------------------------------------------- |
| 元信息         | stat(locator)                                | 返回观测信息与可用性；不因列举不到就推定已删除 |
| 目录列表       | list_page(root, cursor, filters)             | 分页或流式；不得把全目录一次载入内存           |
| 读取           | open_read(locator, range, expected_identity) | 验证预期内容身份，交给 E05 执行                |
| 写入           | put / multipart                              | 只有声明可写的托管位置可用                     |
| 外部可访问 URL | resolve_access(asset, purpose, audience)     | 依据调用方与权限返回受控访问方案               |
| 能力查询       | capabilities(location)                       | 区分 Range、版本号、签名 URL、事件等能力       |

能力缺失必须返回可识别错误或采用明确降级；例如不支持签名 URL 的文件系统通过受鉴权的文件网关读取，不生成虚假的 S3 URL。

建议稳定错误分类统一使用总体架构中的 `SOURCE_UNREACHABLE`、`SOURCE_CHANGED`、`SOURCE_MISSING`、`CONTENT_MISMATCH`、`CAPABILITY_UNSUPPORTED`、`ACCESS_DENIED` 等。HTTP 状态和业务码在接口评审时统一，不复用“文件不存在”覆盖全部失败。

## 6. 兼容与迁移方案

### 第一阶段：只增不减

- 新增模型与可空外键；旧文件路径继续可读。
- 创建表示现有服务的 legacy StorageLocation 和九类 StorageBinding。
- 按现有真实 `file_path` 回填 AssetLocation，不根据当前数据集名称推测旧前缀。
- 为旧 DatasetItem 分配 Asset；没有可靠摘要的记录标记 unverified。
- 逐用途回填非样本对象的固定位置/绑定代次；未完成历史定位的用途暂不允许独立切换新写入目标。
- 回填任务使用稳定幂等键和批次检查点，可中断重跑。

### 第二阶段：双路径读取、单路径写入

- 优先使用新引用；缺少新引用时回退旧解析器，并记录回退计数。
- 新写入统一走新服务入口；禁止同一逻辑写入分别落两套相互独立的记录。
- 新对象键使用不可变 ID，不再使用可变数据集名称。
- 先迁移列表/预览，再迁移标注、导出和训练等高影响调用方。

### 第三阶段：收敛

- 当回填核对通过、旧读取回退为零且观察期通过，停止旧路径新写入。
- 删除旧列、旧分支属于后续独立变更；不与首次上线捆绑。
- 回填不能触发外部原图搬迁，也不能触发新建任务或重复关联。

## 7. UI 与跨页边界

数据集页面显示用户可理解的“原图保留在来源”“复制到托管存储”“原图不可用”等结果；存储位置名称作为可点击摘要。供应商、桶映射和凭据只在存储管理中维护。高级排障页可显示 Asset ID、位置 ID 和业务错误码，但不出现在普通标注流程。

## 8. 验收标准

| 编号     | 可执行验收                                                                                                                      |
| -------- | ------------------------------------------------------------------------------------------------------------------------------- |
| E01-AC01 | 给定旧数据集和已关联任务，完成回填后 DatasetItem ID、任务数、标注数、项目关联数不变，抽样原图摘要一致                           |
| E01-AC02 | 将数据集连续重命名两次，既有图片预览、原图、标注与导出仍正常，存储对象键和源目录均未改变                                        |
| E01-AC03 | M1 验证旧 S3 数据集与新目录数据集共存；M2 开放同一数据集多来源后，混合来源列表与任务仍只依赖样本/内容引用，不要求客户端拼接桶名 |
| E01-AC04 | 32 字符 ETag 不自动写入 content MD5 字段；摘要算法未知时显示未知并可补算                                                        |
| E01-AC05 | 原路径被不同字节覆盖，旧 DatasetItem 不自动切换 Asset，已有任务不展示新图片并沿用旧标注                                         |
| E01-AC06 | 回填进程在批次中断后重试，不新增重复 AssetLocation，不重复项目任务，结果可按批次对账                                            |
| E01-AC07 | 某位置不支持 Range 或签名 URL 时，调用获得明确能力结果或已评审降级，不能返回不可用的成功响应                                    |
| E01-AC08 | 九类用途都有明确绑定，未配置用途在启动或执行前报告可定位错误，不偷偷回落到 datasets 桶                                          |
| E01-AC09 | 关闭新读取开关可恢复旧托管数据访问；新注册外部数据不会被旧版本误写或误删，必要时受控只读                                        |

## 9. 测试计划

- 数据迁移测试：有/无摘要、历史改名、同名文件、跨项目关联、被删除缓存、不同桶前缀。
- 适配器契约测试：分页终止、空目录、Unicode 路径、Range、鉴权失败、服务超时、能力缺失。
- 集成回归：上传完成、导入、标注读图、媒体预览、导出、训练冻结、头像与问题附件。
- 数据不变量检查：无悬空引用、无跨权限自动合并、Asset 已确认字段不可变。
- 性能观察：回填锁占用和数据库查询量；具体阈值由 M0 代表性数据校准。

## 10. 回滚与风险

新增字段与兼容分支支持应用级回滚；数据库采取向前修复，首轮不删除旧列。已经创建的外部引用不是旧系统可理解的数据，回滚前必须阻止旧代码处理它们，而不能声称回滚即可继续全部新功能。

风险包括散落在标注/训练/ML worker 中的路径拼接、回填期间旧写入、物理路径误推断。应先建立调用方清单，再逐入口收敛；按真实路径回填并记录无法映射的例外。

## 11. 待评审决策

- 最小 Asset 元信息何时建立、何时达到 verified；默认按样本逐批推进，不阻塞全部目录注册。
- 历史 MD5 是否在后台全量补算 SHA-256；默认先覆盖高价值与即将冻结样本。
- 存储配置是数据库主导还是文件配置主导；默认位置元数据入库、部署级根目录由运维配置授权。
- 与现有连接器模型采用扩展还是独立映射层；先明确身份与凭据归属，再决定表结构。

## 12. 工程任务与完成定义

- [ ] 盘点路径与 URL 构造调用方，输出九类用途及消费者清单。
- [ ] 完成实体与能力契约 ADR，确认不可变内容和样本身份规则。
- [ ] 编写增量数据库迁移、legacy 映射与幂等回填工具。
- [ ] 实现兼容解析器和旧读取回退指标。
- [ ] 将新写入前缀改为稳定 ID，修复 ETag 与内容摘要混用。
- [ ] 完成核心业务回归、回填对账、回滚演练。
- [ ] 为 E03/E05 提供可使用契约；记录未迁移入口和明确阻断条件。

完成定义：上述验收通过，旧数据可用，新引用可被统一解析，没有把存储品牌或可变数据集名称重新带入业务身份。

## 本项目推进切片与晋升条件

后端负责人批准样本/内容/副本及九用途消费者清单；明确历史 content_hash 的算法与可信来源，未知 ETag 不能升级成可信摘要。新增 ORM、迁移、回填脚本在晋升时固定。

| 切片  | 独立结果               | 交付范围                                                               | 验收 / 回退边界                                     |
| ----- | ---------------------- | ---------------------------------------------------------------------- | --------------------------------------------------- |
| E01-A | 兼容身份与真实位置回填 | 新增关系、稳定新键、可恢复回填；旧上传/扫描/标注保持可用。             | 不搬字节、不改旧 ID；重跑无重复；回退保留新增映射。 |
| E01-B | 逐消费者收敛           | E05 就绪后，预览、标注、导出、ML、训练逐入口迁移，逐用途固定历史定位。 | 每类独立对账；回退不误解析外部路径，首次不删旧列。  |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_dataset_import.py](../../apps/api/tests/test_dataset_import.py)、[test_datasets_upload_zip.py](../../apps/api/tests/test_datasets_upload_zip.py)、[test_image_pyramid.py](../../apps/api/tests/test_image_pyramid.py)、[test_training_dataset_bridge.py](../../apps/api/tests/test_training_dataset_bridge.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-connections.md](../../docs-site/dev/concepts/storage-connections.md)、[storage-buckets.md](../../docs-site/dev/reference/storage-buckets.md)、[index.md](../../docs-site/user-guide/datasets/index.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

- E01-A 已完成：Alembic 0194 增加兼容身份四表和固定扫描根；三种写入入口使用唯一托管键与 SHA-256，历史真实键及不可信 hash 保留。新样本单独保存逻辑路径，Scene、点云分帧、导出标签和媒体清单均消费它，扫描重名不覆盖下载文件。
- 回填工具提供显式目标、数据库只读预览/校验、批次提交、游标续跑及幂等恢复；首次绑定与旧 Item 的锁顺序统一。历史回填不改 Item.updated_at、业务关系或冻结输入。短读候选不建立去重结论，S3 条件读取失败返回 409。
- 实现提交：本 Outcome 所在提交（见 Git 历史）；发布里程碑尚未确定，不分配版本。
- 正式用户文档：`docs-site/user-guide/datasets/import-images.md`；开发与运维：`docs-site/dev/concepts/storage-connections.md`、`docs-site/dev/reference/storage-buckets.md`；接口：`docs-site/api/guides/datasets.md`；决策：`docs/adr/archive/0091-dataset-content-and-location-identity.md`。README 导航和 CHANGELOG Unreleased 已更新。
- 本地验证：14 个相关测试文件 243 passed；另 3 个导出测试文件 49 passed（包含实际点云 ZIP 的重名逻辑路径回归）。后者与前者有重叠，不合并计数。7 条通知 AsyncMock 警告来自既有 worker 测试；新增双帧导出用例另暴露 2 条重复目录 ZIP 警告，仅为空目录条目，媒体清单路径互不重复。
- 迁移验证：全新升级、可逆前缀往返、0174 屏障后的可逆后缀往返、向前升级及环境恢复均通过，无遗留临时数据库；已使用身份/绑定的降级被明确拒绝。OpenAPI 快照一致，Python Ruff、文档构建和文档结构检查通过。
- 清理：本轮拥有的 dev/test 数据库、Redis 和存储桶在验证后销毁；pytest 临时文件、编译缓存及文档构建产物清理。未启动本 checkout 的 API/Celery，也未重启或迁移其他 checkout 的开发/生产环境。
- E01-A 完成时的剩余范围：当时 E01-B 的 resolver/能力契约与全部消费者收敛，以及 E02/E03 的受控位置和目录原地接入尚未实施。历史回填只证明定位映射完整；不证明旧原件在线或内容未变化。本计划继续留在根目录约束后继工作，不归档整个 E01。

### 当前收口状态

后续已交付受控位置登记、来源配置和原地目录接入（E03已归档），统一引用读取、内容快照与完整缓存见E05，实际用途归属和仍关闭的切换边界见E02。以上替代顶部旧“待评审／目录未实施”的现状描述，不改写第一阶段的历史验收。完整E01仍须按原始兼容／回退要求核对现有证据；不得将Tracker、会话或审核功能改造加入收口条件，也不因未核对就增设配置开关。
