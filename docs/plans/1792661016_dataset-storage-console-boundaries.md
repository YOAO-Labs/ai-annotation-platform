# EPIC-09：存储控制台与页面职责去重

> 状态：E09-A、E09-B1/B2/B3 complete；B3与E11-B1交付只读位置维护盘点，E11-B2位置配置确认、E11-C1逻辑位置访问维护、E11-C2-A托管原件保留预演及E11-C2-B显式策略/声明要求已交付；其余切片仍待独立晋升，未分配发布版本。
> 转入日期：2026-10-22；A 切片验收：2026-10-23；初始调研基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 属性     | 草案约定                                                           |
| -------- | ------------------------------------------------------------------ |
| 优先级   | P0：信息归属、位置入口、正确统计口径；P1：迁移与运维工作流         |
| 负责角色 | 产品/设计、前端、后端、运维、测试                                  |
| 依赖     | EPIC-02 的位置与用途合同；EPIC-11 的能力授权；EPIC-12 的统计与健康 |
| 协同     | EPIC-08 数据集工作台；EPIC-07 迁移任务；EPIC-04 既有任务扩展       |
| 交付阶段 | M1 完成职责去重和最小位置控制台；M2 丰富容量、迁移和维护           |

## 本次晋升复核与实施合同

2026-10-24 基于 `802e6e4df` 定稿 E09-B3，与已晋升的 [E11-B1](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-b1--e09-b3-位置维护影响盘点) 同次交付超级管理员的只读位置维护盘点。准确文件范围、单SQL/当前授权/历史覆盖合同与验证矩阵以E11本轮合同为准。现有居中详情内默认折叠，仅展开/刷新GET，无额外写操作；位置配置变更影响与全部owner安全执行许可分开。归档/恢复、版本化变更门禁和物理回收仍待后续实施。

用户已授权“完成 E10-A，再推进 E08/E09 页面整合”。复核基线 `74bd04c12`，工作树干净；E03/E04 已完成，E10-A 已支持引用原图严格读取后冻结独立副本。本节替代历史候选范围，仅实施 E08-A/E09-A 页面整合；保存视图、质量治理、异步容量统计和维护迁移仍属后续切片。超过 8 个文件，跨前端、API、合同和文档；不创建业务表、不改依赖/环境、不重建镜像。

本轮E11-B2补齐位置配置候选预览与版本绑定确认，范围与验证以[E11实施合同](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-b2-位置配置候选预览与确认)为准；使用范围及缓存保存入口共用居中确认，不将只读盘点误作全部owner静止或物理回收许可。

E11-C1在同一位置详情内补充默认折叠的访问维护，范围以[E11实施合同](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-c1-位置访问维护准入与排空)为准；关闭新访问、未收尾记录、停止与恢复各自明确，居中版本化确认。只表达本逻辑Location的受控访问，不能作为共享挂载/桶、候选连接或外部客户端的物理静止证明。

E11-C2-A在平台对象占用卡片内补充默认折叠的托管原件保留与回收预演，范围以[E11实施合同](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-c2-a-托管原件保留账本与回收预演)为准；只查看业务删除时保存的已登记原件断言和当前保护关系，不按位置统计或推算可释放字节。分页/居中详情、观测时间、无限期保护及未覆盖条件清楚表达，无物理删除操作。

E11-C2-B进一步在原件居中详情内提供默认折叠的保留设置与要求：单条期限与整组保护分开，已声明备份/迁移要求经预览登记或显式解除，相关历史分页和原锚点导航保留。范围、验证及未知覆盖以[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-b-显式保留策略与声明要求)为准；不提供物理删除许可，也不把声明编号解释为真实恢复验证。

## 准确文件范围

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

| 当前 owner / 代码锚点                                                                                | 已有能力、缺口与复用约束                                                                   |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [StoragePage.tsx](../../apps/web/src/pages/Storage/StoragePage.tsx)                                  | 已有桶/重复数据集概览与视频失败重试；删除重复表时保留媒体维护入口及管理权限。              |
| [StorageConnectionsPanel.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.tsx) | 复用现有表单和 hooks，迁到位置详情；导入向导中的内嵌连接创建也需同步迁移。                 |
| [storage.py](../../apps/api/app/api/v1/storage.py)                                                   | 现有统计六类、health 只 HEAD annotations；新位置健康/九用途合同由 E02/E12 提供，E09 消费。 |

候选修改入口：[StoragePage.tsx](../../apps/web/src/pages/Storage/StoragePage.tsx)、[StorageConnectionsPanel.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.tsx)、[ImportDatasetWizard.tsx](../../apps/web/src/components/datasets/ImportDatasetWizard.tsx)、[DatasetsPage.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)、[storage.ts](../../apps/web/src/api/storage.ts)、[App.tsx](../../apps/web/src/App.tsx)、[storage.py](../../apps/api/app/api/v1/storage.py)、[storage.py](../../apps/api/app/schemas/storage.py)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与当前依据

当前存储页同时展示后端、桶、数据集数量、数据集存储概览和项目关联；数据集页又管理完整数据集与连接器，两页重复的信息和操作分散了用户注意力。[存储页](../../apps/web/src/pages/Storage/StoragePage.tsx)、[数据集页](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)。

存储页硬编码 MinIO 和 HTTP 开发环境；“总容量”使用 `total_size_bytes`，实际上来自对象大小汇总。存储服务包含九类业务用途，而存储 API 当前仅覆盖部分桶。后续 UI 应使用位置能力与服务端统计合同，而非写死产品名称和单一后端。[存储服务](../../apps/api/app/services/storage.py)、[存储 API](../../apps/api/app/api/v1/storage.py)。

既有 JobsBell 与任务详情已有统一任务入口，本 Epic 仅接入其上下文展示，不另造任务中心。[JobsBell](../../apps/web/src/components/shell/JobsBell.tsx)。

## 2. 目标、范围与暂不做

目标是让存储页成为位置、连接和平台占用的权威入口，并通过深链连接数据集业务工作台。

范围：

- 把连接器配置收敛到存储位置详情，迁移旧入口和可见权限。
- 位置列表/详情、用途映射、健康与统计、缓存维护、迁移/退役影响预览。
- 消除重复数据集 CRUD 表，保留必要的影响计数和定位入口。
- 统一平台对象字节、源数据逻辑字节、物理容量与配额的口径。
- 将既有视频资产失败与重试保留在合适的上下文中。

暂不做：在存储页编辑样本标签、项目标注任务管理、训练版本管理、代替各对象存储产品的完整管理员控制台。

## 3. 用户故事与切片

| 编号    | 用户故事                                                                     | 切片与完成结果                       |
| ------- | ---------------------------------------------------------------------------- | ------------------------------------ |
| E09-S01 | 作为管理员，我希望只维护一份存储位置和连接配置。                             | M1：旧连接器入口迁移，配置所有权唯一 |
| E09-S02 | 作为运维人员，我希望看到各位置真实类型、健康和统计时间。                     | M1：动态属性、未知/过期/失败状态     |
| E09-S03 | 作为数据负责人，我希望从来源问题跳到位置，再返回原数据集。                   | M1：带权限和上下文的双向深链         |
| E09-S04 | 作为管理员，我希望分清源数据规模、平台保存量、缓存、配额和真实容量。         | M1 正确口径；M2 分项统计             |
| E09-S05 | 作为管理员，我希望明确九类平台用途分别使用哪里，迁移时不会漏项。             | M1 展示映射；M2 受控变更工作流       |
| E09-S06 | 作为运维人员，我希望清理平台缓存、重试失败媒体资产，而不损害原图和冻结版本。 | M2：影响预览、阻断理由与异步结果     |
| E09-S07 | 作为管理员，我希望禁用、迁移、退役位置前知道哪些业务会受影响。               | M2：按引用范围预览，接入既有任务详情 |

## 4. 页面归属与功能规则

### 4.1 导航收敛

存储管理只保留“位置”和“用途映射”两类主入口，必要的运行状态直接融入位置详情。不新增与位置同义的独立连接器页面。

数据集页不再有完整“数据连接器”页签；接入向导只选择可用位置、填写数据集来源规则。如果需要新增位置，按权限进入存储配置，再返回原向导。

无权新增位置的用户看到“请选择已授权位置”及管理员操作说明，不以报错堆栈或服务器路径提示代替引导。

### 4.2 位置列表与详情

列表字段：名称、类型、站点、启用状态、最近探测健康、索引源规模、平台对象字节、配额和更新时间。

启用状态与运行健康分开。管理员主动禁用不能显示为网络离线；一次探测成功也不能宣称全部历史样本永久可用。健康详情应说明检测了连接、列举、读取还是写入能力。

只读外部源不能通过写测试文件来探活。允许写的托管位置采用受控测试前缀与清理策略，具体由 EPIC-02/12 定义。

位置详情包含连接配置、授权范围、容量/缓存、平台用途和维护操作。秘密字段只展示“已配置/待更新”，不回显明文。

### 4.3 数据集引用与跨页跳转

存储页不再重复完整数据集目录表。显示“受影响数据集 N 个/来源 N 个”及“查看数据集”链接，将 `locationId` 等上下文带入数据集列表过滤。

该计数与深链按用户权限裁剪；没有数据集读取权限的运维账号只展示经授权的运维摘要，不泄露数据集名称、样本或项目归属。

来源异常在数据集页显示摘要，配置变更在存储页执行；两页使用同一状态合同。跨页返回保留数据集筛选、页码和选中来源。

### 4.4 用途映射

映射应覆盖 annotations、datasets、bug-reports、media-cache、audit-archive、import、export、avatars、training 九种现有用途。用途是业务标签，不要求所有后端都暴露同名物理桶。

每条映射展示位置、桶/前缀或受控目录、读写策略、配置版本、生效状态。普通数据集管理人员不因此获得修改平台全局用途的权限。

修改映射属于受控配置/迁移流程，不能把一个下拉框保存成功当作数据迁移完成。旧数据读取与新数据写入的过渡期由 EPIC-01/07 统一处理。

## 5. 统计口径与错误语义

| 展示指标                        | 定义与要求                                                                 |
| ------------------------------- | -------------------------------------------------------------------------- |
| 已索引源数据量                  | 已登记源文件的逻辑字节；按物理引用去重，同一文件被多个数据集引用不重复累计 |
| 平台对象字节                    | 平台拥有的托管原图、缓存、冻结副本和其他对象的逻辑大小；明确是否含历史版本 |
| 托管原图/预览/按需缓存/冻结副本 | 按对象用途分类；多用途引用必须有稳定归属规则，不能在总计中多次相加         |
| 物理总/剩余容量                 | 仅使用后端或主机可信报告；对象字节不能推导磁盘容量或副本冗余开销           |
| 配额与已用额度                  | 平台策略值；显示计费/计量范围，不能冒充底层磁盘余量                        |
| 统计时间与范围                  | 提供 measured_at、完整/部分/估算状态以及数据覆盖范围                       |

不能将多个不同存储后端的“总容量”随意相加。共享物理盘、外部配额、重复挂载和未知值存在时，默认按位置展示，并说明汇总范围。

查询失败保留上次成功数据并标记过期。未知值使用“未提供/待统计”，不能变成 0；部分探测失败不能将位置下全部样本标为已删除。

“刷新状态”触发轻量查询或后台统计任务，前端请求不等待逐桶全量扫描完成。异步统计与对账实现归 EPIC-12。

## 6. 数据与 API 草案

| 合同                   | 关键字段或行为                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------------------------- |
| StorageLocationSummary | id、name、kind、site_id、enabled_state、health、capabilities、usage、quota、measured_at                        |
| StorageLocationDetail  | 脱敏连接属性、授权范围、能力测试结果、用途引用、allowed_actions                                                |
| StorageUsage           | metric、value、unit、scope、source、coverage、is_estimate、deduplication_basis、includes_versions、measured_at |
| StorageBinding         | purpose、location_id、container_or_prefix、generation、effective_state                                         |
| ImpactPreview          | 受影响引用数、允许迁移范围、阻断项、目标配置版本和有效期                                                       |

建议接口包括 `GET /storage/locations`、`GET /storage/locations/{id}`、`GET /storage/bindings`、位置维护预览与任务提交接口。EPIC-02 拥有最终配置合同；本 Epic 负责消费与 UI 体验。

健康/用量接口应读取最近成功采样及对账结果。接口返回可执行动作和拒绝理由，前端不以当前用户角色字符串自行推断全部权限。

## 7. 验收标准

| 编号     | 可验证标准                                                                                          |
| -------- | --------------------------------------------------------------------------------------------------- |
| E09-AC01 | 数据集页不再存在完整连接器配置页；旧入口可跳转到唯一存储位置，原权限不扩大。                        |
| E09-AC02 | 存储页不再复制完整数据集 CRUD 表；“查看受影响数据集”正确过滤数据集列表并保留返回上下文。            |
| E09-AC03 | 后端分别配置目录、S3 和 SFTP 类型时，界面显示实际类型和能力，不硬编码 MinIO 或 HTTP。               |
| E09-AC04 | 无物理容量数据时显示“未提供”，不绘制伪造磁盘占比；已索引源字节与平台对象字节分开展示。              |
| E09-AC05 | 同一目录文件被两个数据集引用，位置源数据汇总不重复计算；具有两个实际平台副本时按真实副本范围计量。  |
| E09-AC06 | 九类业务用途在映射视图中均可定位；未配置项明确提示，不能被缺省隐藏。                                |
| E09-AC07 | 健康或用量刷新失败后保留旧值和时间，标记过期；不会显示用量归零或所有样本已删除。                    |
| E09-AC08 | 外部源只读位置没有删除原文件按钮和写探测；平台自有无引用缓存可在影响预览后按策略清理。              |
| E09-AC09 | 禁用/退役含活跃引用的位置时显示阻断或影响预览；提交任务后展示实际任务状态，不提前显示迁移完成。     |
| E09-AC10 | 无位置管理权限用户通过直接 URL 或修改请求无法读取凭据和配置；运维摘要也不泄露无权查看的数据集名称。 |

## 8. 迁移与回滚

1. 先新增位置展示合同，把旧单一存储后端及连接器映射为位置，保留连接配置身份与所有权。
2. 修正文案与指标：总容量改为准确对象字节/占用描述；移除固定产品名和协议。
3. 将旧数据集页的连接器入口改为兼容跳转，保持用户工作流和授权。
4. 删除存储页重复数据集管理表，以受影响计数和筛选深链替代。
5. UI 回滚仅回退展示，不撤销已确认的位置配置或用途迁移。若旧页面不能正确表达新位置，保留新版只读入口，不把新位置伪装成 MinIO。

## 9. 测试与验证

- 覆盖单一旧托管后端、只读 NAS、外部 S3、SFTP、离线站点等位置组合。
- 使用已知重复引用数据核验统计口径，测试历史版本和缓存共用对象不被重复累计。
- 覆盖普通标注人员、数据集管理者、位置管理者、超管的直接 URL 与 API 访问。
- 测试未知容量、部分结果、缓存过期、探测失败与维护中状态。
- 验证存储页不触发全量数据集拉取、不在页面请求中同步执行全桶扫描。
- 执行旧连接器入口迁移、任务查看、失败媒体资产重试和上下文返回回归。

## 10. 风险与待决策

| 风险/问题                    | 草案处理                                                  |
| ---------------------------- | --------------------------------------------------------- |
| 用户习惯在数据集页创建连接器 | 向导提供有权限的跳转与返回，不保留第二份表单逻辑          |
| 不同后端的容量指标不可比     | 明确逻辑对象字节/物理容量/配额，未知值不上估计占比        |
| 位置名和来源名混淆           | 位置描述共享物理入口，来源描述某数据集的扫描范围          |
| 多个位置实际指向同一目录     | 配置层标注别名/共享物理范围；无法确认时不宣称全局去重汇总 |
| 存储操作影响大量业务         | 统一影响预览、配置版本校验和异步任务，禁止立即隐藏失败    |

## 11. 工程任务与完成定义

- [x] 确认信息所有权矩阵、导航和兼容路由，移除重复数据集管理入口。
- [x] 完成位置列表/详情与用途映射组件，复用现有设计系统。
- [x] 接入权限裁剪、实际连接属性与能力展示。
- [ ] 接入分项用量、统计时间、未知/部分/过期状态。
- [x] 完成位置到数据集的过滤深链及返回状态恢复。
- [x] 保留媒体资产失败/重试功能并接入统一任务详情。
- [ ] 完成维护影响预览、任务提交与结果展示。
- [x] A：通过页面整合验收和回归，更新页面归属、迁移说明及未知指标口径；运维统计和维护工作流另行验收。

完成定义：用户能明确区分“管理数据集”和“维护存储”，每项配置只有一个权威入口；所有指标可追溯口径，不依赖写死的后端名称或虚假的容量假设。

## 本项目推进切片与晋升条件

产品/前端确认字段 owner 和旧链接迁移表；后端提供脱敏 allowed_actions、覆盖与时间。位置配置/绑定归 E02，采样归 E12，本 Epic 不重复建 API。

| 切片  | 独立结果           | 交付范围                                                                     | 验收 / 回退边界                                            |
| ----- | ------------------ | ---------------------------------------------------------------------------- | ---------------------------------------------------------- |
| E09-A | 归属去重和准确指标 | 位置/九用途展示，迁移页签及向导连接入口，重复数据集 CRUD 换成计数/过滤深链。 | 唯一配置入口、权限不扩大、未知容量不画占比、视频维护保留。 |
| E09-B | 运维工作流         | 消费 E12 统计/健康、E11 影响预览、E07 迁移任务；不造第三份配置或任务状态。   | 刷新不实时扫描全桶；过期不归零；版本条件下提交维护动作。   |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[StoragePage.test.tsx](../../apps/web/src/pages/Storage/StoragePage.test.tsx)、[StorageConnectionsPanel.test.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.test.tsx)、[DatasetsPage.test.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.test.tsx)、[test_storage_event_loop.py](../../apps/api/tests/test_storage_event_loop.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-connections.md](../../docs-site/user-guide/datasets/storage-connections.md)、[storage-buckets.md](../../docs-site/dev/reference/storage-buckets.md)、[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

E09-A 归属去重和准确指标及 E09-B1 平台占用快照完成；E09-B 其余范围保留在本计划，未宣称完整 Epic 完成。

- 提交：`e2fe33d5f` 晋升复核，`525f79026` 查询/权限/训练范围合同，`36248a30c` 存储页面，`a683a824f` 数据集页面，`55d0c3051` 实际验收发现的观测刷新和预览并发修复。未分配发布版本。
- 后端：既有 129 项及最终新增 13 项共 142 个不同用例通过。首次组合运行 135 项；审查补充后 workbench 13 项全部通过，重复用例不累计。覆盖两来源共享成员、可见来源选择、旧差异不复活、扫描观测排序、并发删除、权限撤销、唯一位置计数、分项目进度、真实 MinIO 预览、数据集限定冻结和项目任务分页。
- 前端：集成分支 16 文件 160 项通过；观测/预览修复 5 文件 28 项通过；预览有限重试补充后该 hook 8 项通过。去重合计 20 文件 188 个不同用例。typecheck、受影响 ESLint、CSS token lint、完整 pre-commit 和 `git diff --check` 通过；CSS lint 仅保留既有训练比较页两处 z-10 建议。没有宣称全量 Web 或远程 CI 验收。
- 合同与文档：OpenAPI 一致性和本地 TS 生成通过，SDK OpenAPI 合同 4 项通过；新增 Web 专用接口在 coverage manifest 明确排除。文档 frontmatter 196 文件、navigation 198 页面通过。用户说明位于 `docs-site/user-guide/datasets/{index,import-images,storage-connections}.md`，接口说明位于 `docs-site/api/guides/{datasets,storage-locations,training-datasets}.md`；`README.md`、`docs-site/dev/reference/storage-buckets.md` 和 `CHANGELOG.md` Unreleased 同步。
- 实际浏览器：当前工作树 3100/8100，无 mock。列表名称筛选返回、旧 dataset/source/scan 链接直接跳详情、独立四页签、网格和单项检查器、来源↔位置保留扫描/列表上下文、按位置过滤数据集、关联训练版本深链、深浅主题截图和检查器 Escape 焦点恢复通过。独立存储页面网络没有数据集全量目录或 `/storage/buckets` 请求；真实节点探测成功，九用途和未知容量显示正确。导入向导往返后路径/匹配规则/递归选项恢复，未创建测试连接或数据集。
- 真实数据观测：D-7 专用来源只读扫描和完整预检显示 1 项已核验、1 项缺失、1 项内容变化，三个原成员/任务/标注保留；预检完成后工作台和检查器刷新，三张旧内容预览正常，异常项没有原图读取动作。训练入口预选 D-7，明确确认后实际 `/tasks` 请求带 dataset_id；单项预检报告 5,959 字节并正确阻断不足两张合格图片的创建。原 ready 版本的三张原图、标签和 ZIP 摘要再次校验不变，未新建模型训练。
- 清理：仅销毁经 owner/零客户端核对的 `1822b025369a33f4:test` 资源；测试日志、报告、缓存、截图和专用浏览器会话清理。两份任务工作树在干净且补丁等价已验证后由 Orca 删除；人工 dev 环境、九用途桶、演示来源、已有冻结版本和 `.worktree/manual` 保留。未重建后端镜像、未推送、未发布。

- 剩余范围：E12-A1 的平台异步对象盘点与过期快照已由 E09-B1 接入；外部源分项、物理容量/配额，以及 E11/E07 维护影响预览、迁移/退役提交仍待独立合同。当前用途映射只读；未知指标不估算，不提供伪迁移或源文件删除。来源摘要中的 dataset_count 是按当前权限去重的关联数量，不是源字节或磁盘容量。

### E09-B1 本轮范围

按 [E12-A1 实施合同](1792661016_dataset-storage-operations-recovery.md#本轮定稿e12-a1--e09-b1)接入平台九用途的异步对象盘点与成功快照：默认折叠的统计明细、采样窗口、未知/零/过期/失败/部分覆盖、独立刷新与启动、统一居中任务详情。配置与数据集导航不重建。E12 进一步分项归因、物理容量、配额，以及 E11/E07 的清理/迁移/退役仍保留为后续范围，不宣称 E09-B 全部完成。

E09-B1 已随 `0e85ce496` 完成，最终验收与清理见 [E12 Outcome](1792661016_dataset-storage-operations-recovery.md#outcome)。91 项本地用例及真实盘点、取消保留、重新盘点、居中弹窗、明暗/窄屏验收通过；不重复计为 E09 的另一套统计或任务体系。

### E09-B2 来源诊断

随 `58a9725cf` 和 [E12-A2 实施合同](1792661016_dataset-storage-operations-recovery.md#本轮定稿e12-a2--e09-b2已交付)交付位置内来源的原件/预览/扫描/预检只读诊断；只统计保留的原地引用业务样本，不混入托管复制或源容量。来源分页和任务历史独立，活跃轮次、最近失败及上次完整成功分别表达，继续使用现有居中任务详情和上下文深链。

59 个不同本地用例及明暗/窄屏真实浏览器验收通过，详细证据与清理见 [E12 Outcome](1792661016_dataset-storage-operations-recovery.md#outcome)。E09 仍保留 E11 清理与 E07 迁移/退役等维护影响工作流，不宣称整个 Epic 完成。

### E09-B3 位置维护盘点

2026-10-24 随 [E11-B1](1792661016_dataset-permissions-lifecycle.md#outcome) 完成只读位置维护盘点：现有居中详情默认折叠，展开与刷新GET汇总全部保留来源和业务关系、活动轮次、副本账本/租约及有证据的冻结版本，说明未覆盖历史和配置副作用。超级管理员与datasets:read前后核验，失败保留旧时间证据，撤权隐藏；不生成提交许可或新增维护写操作。

43个API/DB、21个Web和4个SDK不同用例通过，明暗/桌面/窄屏实际浏览器、Enter/Escape/焦点及零溢出/失败请求验收通过，详细记录和清理见E11 Outcome。正式用户/API/架构文档与OpenAPI同步；无迁移/Worker变更、不重建镜像，人工环境保留。候选配置、版本化变更、归档/恢复、回收和迁移仍属后续合同，整项E09继续活动。

### E11-B2 位置配置确认

使用范围和缓存配置共用候选预览与居中确认，绑定五分钟令牌、位置/连接修订、当前身份/Key及关系指纹；变更或到期要求重预览。189个不同本地用例、明暗/桌面/窄屏与真实临时位置三次保存验收通过，既有人工位置配置及22项计数未变。完整证据及清理见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-b2-位置配置候选预览与确认)。全owner静止、归档/恢复、迁移/退役与物理回收仍在后续合同中。

### E11-C1 逻辑位置访问维护

位置详情默认折叠展示当前维护状态、修订、未收尾访问及安全节点/目的，停止须零记录，drain/stop/resume均采用居中预览确认。控制开关默认关闭并解释全API/Worker升级条件；过期/冲突要求重预览，会话/对象/配置隔离及撤权隐藏旧证据。维护组件24项随完整Web6541项通过，实际持有FD的访问阻止停止，句柄结束后停止/恢复成功；明暗、1440/390宽度、Escape/焦点及零溢出/失败请求通过。后端、部署、621项API/DB、SDK4项、原件/业务保留及清理详见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c1-逻辑位置访问维护)。其余受控物理回收、迁移/退役和整项E09仍活动。

### E11-C2-A 托管原件保留预演

平台对象占用卡片新增默认折叠的保留预演，独立分页和居中详情；按断言展示完整计数、重复大小口径、观测时间及引用/保留/未知资格原因。默认不查询，撤权隐藏，会话修订隔离，未知期限为无限期保护，没有删除按钮。含新增16项共36项Web用例通过，真实11样本删除后10+1分页、1440/390明暗及Escape/焦点通过；后端、隔离恢复、187个不同本地用例和精确清理见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-a-托管原件保留账本与只读预演)。C2-A不覆盖所有物理owner，也不提供回收许可，E09继续活动。
