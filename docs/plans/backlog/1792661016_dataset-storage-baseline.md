# 现状基线、复用边界与参考资料

> 状态：research-draft；未排期，未批准实施，未分配发布版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](README.md#promotion-gate)。[草案总览](1792661016_dataset-storage-roadmap.md)、[架构契约](1792661016_dataset-storage-architecture.md)和[交付验收](1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

> 固定代码基线：[`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`](https://github.com/yyq19990828/ai-annotation-platform/tree/e169d9ef53a9eda60abd4e67bf553ab4e6ab618e)。已核对本工作区 HEAD 与该提交一致；未来实施仍需重新审计。

## 1. 证据如何使用

代码现状来自静态阅读，不能证明生产部署的规模、性能、真实故障或配置。文档中的用户故事、数据对象、API 和 UI 是拟议设计。性能目标、候选存储选择、RPO/RTO 和默认配额必须通过实施前试验确定。

本次未修改项目代码，未执行数据迁移，未把规划创建为仓库 issue。以下链接供评审和后续实现定位。

## 2. 已有能力：继续复用

| 编号 | 已有能力                                                       | 主要依据                                                                                                                                                                          | 规划处理                                                |
| ---- | -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| B01  | Dataset、DatasetItem、Scene/frame_index、ProjectDataset 等模型 | [dataset.py](../../../apps/api/app/db/models/dataset.py)                                                                                                                          | 保持样本与任务身份，新增内容/位置关系                   |
| B02  | S3 和 SFTP 外部来源适配                                        | [SourceAdapter](../../../apps/api/app/services/sources/base.py)、[s3.py](../../../apps/api/app/services/sources/s3.py)、[sftp.py](../../../apps/api/app/services/sources/sftp.py) | 扩展为持续引用/读取能力，不把现有复制导入误称为原地引用 |
| B03  | 连接凭据加密、owner/global 作用域                              | [storage_connection model](../../../apps/api/app/db/models/storage_connection.py)、[service](../../../apps/api/app/services/storage_connection.py)                                | 复用密钥存储与现有边界；更细授权是新增方案              |
| B04  | 后台导入任务、阶段/进度、取消和错误                            | [dataset_import worker](../../../apps/api/app/workers/dataset_import.py)                                                                                                          | 补齐文件粒度恢复、扫描世代与幂等                        |
| B05  | 全局后台任务入口和任务详情                                     | [JobsBell](../../../apps/web/src/components/shell/JobsBell.tsx)、[AsyncJobDetailModal](../../../apps/web/src/components/jobs/AsyncJobDetailModal.tsx)                             | 延用，按数据集/位置过滤上下文；不另建重复全局任务中心   |
| B06  | 训练版本父子关系、snapshot、manifest、package SHA-256          | [training_dataset model](../../../apps/api/app/db/models/training_dataset.py)、[training_datasets service](../../../apps/api/app/services/training_datasets.py)                   | 扩展外部源冻结，保留项目/标注快照语义                   |
| B07  | 按内容和 Scene 分组切分、重复内容标注冲突处理                  | [training_dataset_bridge](../../../apps/api/app/services/training_dataset_bridge.py)                                                                                              | 延续防泄漏规则，不另做互相冲突的随机切分实现            |
| B08  | 训练数据统计与版本分析                                         | [training_dataset_analysis](../../../apps/api/app/services/training_dataset_analysis.py)                                                                                          | 复用可通用逻辑；活跃数据集与冻结版本统计分别注明口径    |

## 3. 当前限制与改造落点

| 编号 | 静态证据                                                                  | 对产品的影响                                                   | 对应 Epic   |
| ---- | ------------------------------------------------------------------------- | -------------------------------------------------------------- | ----------- |
| G01  | `ingest_one` 对外部 stream 上传到 datasets 桶；已有 bucket key 可直接登记 | 目前外部来源接入主要仍是复制托管路径                           | E01/E03/E05 |
| G02  | DatasetItem 有 file_path、content_hash，但没有位置与副本模型              | 一个样本难以表达原地文件或跨位置的同内容副本                   | E01         |
| G03  | 数据集名称参与新键/扫描前缀；rename 主要更新数据库名称                    | 需要解除展示名称和定位的耦合；不能假设历史数据可按新名推算路径 | E01         |
| G04  | `_unique_file_name` 每次获取数据集文件名集合                              | 大批次接入可能放大查询与内存开销，需要基准验证和批量策略       | E04/E12     |
| G05  | 上传完成逻辑对 32 位 ETag 用作内容 hash                                   | 后端迁移时必须区分对象 ETag 与可靠内容摘要                     | E01/E07     |
| G06  | `summarize_bucket` 列举对象后计数求和                                     | 面板不应对大桶每次实时完整扫描                                 | E09/E12     |
| G07  | 存储服务定义九种用途，现有存储面板 API 只覆盖其中六类                     | 迁移盘点不能只按当前面板显示的桶执行                           | E02/E07/E09 |
| G08  | 两处页面含 MinIO 静态文案；存储页“总容量”来自对象字节和                   | 名称和容量口径误导，多后端支持需要真实配置字段                 | E09/E12     |
| G09  | 数据集详情主要嵌在列表展开行，存储页重复展示数据集概览                    | 样本浏览空间不足，业务和运维入口重合                           | E08/E09     |

对应代码：[dataset service](../../../apps/api/app/services/dataset.py)、[datasets API](../../../apps/api/app/api/v1/datasets.py)、[storage service](../../../apps/api/app/services/storage.py)、[storage API](../../../apps/api/app/api/v1/storage.py)、[DatasetsPage](../../../apps/web/src/pages/Datasets/DatasetsPage.tsx)、[StoragePage](../../../apps/web/src/pages/Storage/StoragePage.tsx)、[ImportDatasetWizard](../../../apps/web/src/components/datasets/ImportDatasetWizard.tsx)。

## 4. 需要避免的现状误读

| 不能这样表述                                     | 正确理解                                                                                        |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| “MinIO 只能存容器内部，退出容器数据就没了”       | 当前 Compose 已有 `${MINIO_DATA_DIR:-miniodata}:/data` 映射；新需求是已有目录直接纳入逻辑数据集 |
| “项目没有连接器/后台任务/训练版本，需要全部新建” | 这些能力已有；本计划扩展而不是重复建设                                                          |
| “当前连接器已支持完善的项目级授权”               | 现有 model 明确 owner/global；更细粒度授权需设计和迁移                                          |
| “已有 SFTP 导入，所以已能持续原地标注 SFTP 文件” | 导入适配和持续读流/版本一致性是不同能力                                                         |
| “替换 S3 endpoint 就完成 MinIO 迁移”             | 需验证签名、媒体、任务、训练、生命周期、所有用途与回滚                                          |
| “冻结一个文件列表就能永远复现外部可变原图”       | 必须有可验证且保留的内容/版本，hash 无法恢复丢失字节                                            |
| “只要缩略图能看就能继续标注”                     | 编辑和提交要求绑定修订的完整原图可读；预览和原图状态独立                                        |

部署依据：[docker-compose.yml](../../../docker-compose.yml)、[docker-compose.hostvols.yml](../../../docker-compose.hostvols.yml)。

## 5. 开源存储与接口参考

本次查阅 AWS 对象/预签名文档及候选官方资料入口，未执行候选兼容测试；实施 E07 时重新核查状态并锁定版本与镜像摘要。下面是决策依据的入口，不代替本项目的实测报告。

| 主题                  | 官方资料                                                                                                                                                                                 | 本计划如何使用                                                    |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| MinIO 社区仓库状态    | [minio/minio](https://github.com/minio/minio)                                                                                                                                            | 退役/替换动因；避免将商业产品或免费许可与开源社区维护状态混为一谈 |
| SeaweedFS 项目及许可  | [seaweedfs/seaweedfs](https://github.com/seaweedfs/seaweedfs)                                                                                                                            | 优先进入候选试验，核查实际部署与维护要求                          |
| SeaweedFS S3 支持范围 | [Amazon S3 API](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)                                                                                                               | 按本应用用到的 API 验证；原生复制能力不等于所有 S3 复制/通知 API  |
| RustFS 发布与项目     | [Releases](https://github.com/rustfs/rustfs/releases)、[README](https://github.com/rustfs/rustfs)                                                                                        | 并行候选；以稳定版本实测，不沿用早期阶段标签                      |
| RustFS S3 支持范围    | [S3 compatibility](https://docs.rustfs.com/en/reference/s3-compatibility)                                                                                                                | 兼容矩阵是输入，不代表本应用所有行为已经验证                      |
| Garage 能力与 S3 子集 | [Features](https://garagehq.deuxfleurs.fr/documentation/reference-manual/features/)、[S3 compatibility](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/) | 仅在需求吻合时作为轻量分布候选，核对版本和保留语义                |
| Ceph RGW 多站点       | [Squid multisite](https://docs.ceph.com/en/squid/radosgw/multisite/)                                                                                                                     | 有现成 Ceph 运维能力时评估，不默认增加集群复杂度                  |
| ETag 的语义           | [AWS Object API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html)                                                                                                        | 不把 ETag 普遍当作 MD5，保存独立内容校验信息                      |
| S3 预签名请求         | [AWS SigV4 query authentication](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sigv4-query-string-auth.html)                                                                | 将签名 URL 的公开端点/代理路径纳入部署验证                        |

## 6. 尚未掌握的输入

- 实际图片总量、增量速度、单图大小、目录层次、已有数据总字节数。
- 当前服务器数量、NAS/共享目录情况、GPU 训练机器网络及磁盘。
- 各站点是否能访问中心、带宽/时延、是否允许部署 Agent。
- 对历史原图可恢复时长、训练版本保留和数据删除审批的要求。
- 可接受停机窗口、迁移可用空间、备份目标、RPO/RTO 与运维投入。

这些信息用于校准配置、估时和存储选型。计划采用了可验证的默认方案，不把未知部署信息包装成事实。

## 7. 本地复核补充与既有计划关系

| 核对项           | 证据和对本系列的约束                                                                                                                                                                          |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 训练版本流已交付 | [项目按任务版本计划](../archive/1792140305_project-task-dataset-versions.md)已有 Outcome；E10 扩展来源和读取，不重建版本流、任务筛选及已有版本分析。                                          |
| 任务/媒体资格    | [ADR 0086](../../../docs/adr/0086-canonical-task-data-contracts.md)与[实例分割计划](../archive/1792480513_segmentation-online-learning.md)拥有任务资格；目录接入不意味着视频/点云训练已支持。 |
| 已有派生 owner   | [ImagePyramidAsset/Generation](../../../apps/api/app/db/models/image_pyramid.py)已有 generation、摘要和租约，E01/E05 应复用。                                                                 |
| 现有删除         | Dataset/Item 当前硬删除，unlink 返回 soft=false；[训练删除 intent](../../../apps/api/app/db/models/training_storage_deletion.py)不是原始 Dataset 的软删除恢复。                               |
| 并发与扫描       | 现有建任务是顺序查询去重，项目/样本对无唯一约束；持久游标、扫描租约/代次、逐项恢复和并发幂等均是新增。                                                                                        |
| 当前预算         | [settings](../../../apps/api/app/config.py)定义复制导入默认 50,000 文件 / 200 GiB；worker 先收集限额内对象。原地扫描预算另行明确，不套用或绕过现有护栏。                                      |
| 文档与代码差异   | [桶参考](../../../docs-site/dev/reference/storage-buckets.md)称面板显示九类，实际 [API](../../../apps/api/app/api/v1/storage.py)只统计六类；规划按九用途盘点，实施相应切片时纠正文档。        |
| 数据集视图       | 已有 [ProjectTaskView](../../../apps/api/app/db/models/project_task_view.py)属于项目任务/对象/轨迹，不能直接当 DatasetItem 视图。复用校验/组件模式，保留项目 Data Manager 权限和查询粒度。    |
| 目录与网络安全   | SSRF 多入口校验已有，但非已验证 DNS pinning；SFTP list/open 不能直接证明本地根安全打开。                                                                                                      |
| 备份基础         | 既有部署文档含数据库/对象备份命令及九桶/密钥要求；缺统一恢复代次、清单和隔离恢复核验，而非完全没有备份。                                                                                      |

现有下载签名默认请求 TTL 为 3600 秒并按 600 秒网格对齐，实际有效期可更长；新用途需明确 TTL/严格到期策略，即时撤销走实时鉴权。不能把现有链接描述为已具备即时撤权。

本次静态核验不包含真实应用测试、生产 DB、目录挂载或性能测量；结论仅覆盖固定提交。外部目录/站点/数据量/维护窗口由相应负责人在晋升时提供。

## 8. 外部资料采用边界

2026-10-22 查阅了 [AWS Object](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html)的 ETag/独立 checksum 区分与[预签名 URL](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)的时限访问模型，分别用于 E01 的摘要规则与 E05/E11 的授权窗口；不代替本项目代理签名验收。

[SeaweedFS S3 API](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)与 [RustFS 兼容矩阵](https://docs.rustfs.com/en/reference/s3-compatibility)只作为隔离 PoC 输入。候选版本、发布日期、维护状态和兼容表会变化，转入时不沿用固定发布日期/版本断言，也不将供应商声明标为本项目实测。Garage/Ceph 保留条件候选，晋升时再核查实际版本与拓扑。

## 9. 来源映射与内容追溯

整套输入来自用户指定的桌面目录，源文件未修改。下表 SHA-256 识别本次输入，不能证明拟议功能已实现。保留原需求/故事/验收编号，新的内部链接均指向仓库草案。

| 来源文件                                       | 仓库草案                                                                                                    | 来源 SHA-256                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `00-README.md`                                 | [dataset-storage-roadmap](1792661016_dataset-storage-roadmap.md)                                            | `9750d6f214aef5d0c20043ce4eb3de407582e35c1c19f75804335850fd50589b` |
| `01-ARCHITECTURE-AND-DECISIONS.md`             | [dataset-storage-architecture](1792661016_dataset-storage-architecture.md)                                  | `f1e0ab17849e6e699ce38c79c6610de90f6401501430803af68c66a16bd116e7` |
| `02-UX-INFORMATION-ARCHITECTURE.md`            | [dataset-storage-information-architecture](1792661016_dataset-storage-information-architecture.md)          | `11bc909a28571b08661ff4bd8f456488e79c63eb6ea81d8eea3ba2e49d0c66c0` |
| `03-DELIVERY-AND-ACCEPTANCE.md`                | [dataset-storage-delivery-acceptance](1792661016_dataset-storage-delivery-acceptance.md)                    | `e6b6f19e8b1a011a4a87d3c915ef4c8f72e871d4c2f57b558c29d3308adbab49` |
| `04-BASELINE-AND-REFERENCES.md`                | [dataset-storage-baseline](1792661016_dataset-storage-baseline.md)                                          | `5fd36bdc26ccc5004e7d59adb7f959a06b3f4ff549d369e7fda797ec5322cd22` |
| `EPIC-01-DOMAIN-AND-STORAGE-ABSTRACTION.md`    | [dataset-storage-domain-abstraction](../1792661016_dataset-storage-domain-abstraction.md)                   | `7e4438159ead5d0a304114907513bba56a519d677173a141621b2de8920d13b8` |
| `EPIC-02-STORAGE-LOCATIONS-AND-CONNECTIONS.md` | [dataset-storage-locations-connections](../1792661016_dataset-storage-locations-connections.md)             | `466e7498ddae54a0ccddaa20002e11400b6e079fb479c48cc93272dcd0f194b2` |
| `EPIC-03-IN-PLACE-DIRECTORY-DATASETS.md`       | [dataset-in-place-directory-registration](../archive/1792661016_dataset-in-place-directory-registration.md) | `d4090222645515cb4ae49a8967290165ff882e855c4b4bf46b56ab423445c5b7` |
| `EPIC-04-INCREMENTAL-SYNC-AND-JOBS.md`         | [dataset-incremental-sync-jobs](../archive/1792661016_dataset-incremental-sync-jobs.md)                     | `0733e5bd59276e84331cb68c200013f96600b8773e08b75bd8b1fcc21f3c4242` |
| `EPIC-05-ASSET-ACCESS-AND-CACHE.md`            | [dataset-asset-access-cache](../1792661016_dataset-asset-access-cache.md)                                   | `c89ff2bb1d839692ac042cf1dfea4576d5d5c037e5c3028bc1018ae921eb517d` |
| `EPIC-06-MULTI-SITE-AND-AGENT.md`              | [dataset-multi-site-agent](1792661016_dataset-multi-site-agent.md)                                          | `8fdbf4bfa20849529301206c89919f0a8e3f03b463e5ba6a9e1e894c252c88f9` |
| `EPIC-07-S3-SELECTION-AND-MINIO-MIGRATION.md`  | [dataset-s3-selection-migration](1792661016_dataset-s3-selection-migration.md)                              | `b5a5a6095b9430e16006cb3b364b20f54c9059b14acd71e14f6b0eabe0393a1a` |
| `EPIC-08-DATASET-WORKBENCH-AND-QUALITY.md`     | [dataset-workbench-quality](../archive/1792661016_dataset-workbench-quality.md)                             | `c81205444d3100f7930fb5e54ff18109299e73243f9fd87ed5a13b30359e4417` |
| `EPIC-09-STORAGE-CONSOLE-AND-BOUNDARIES.md`    | [dataset-storage-console-boundaries](../1792661016_dataset-storage-console-boundaries.md)                   | `c8be76b36127851641915af9cd4c93806c041677efa9865d024e02f6d8c4bb4a` |
| `EPIC-10-DATASET-VERSIONS-AND-TRAINING.md`     | [dataset-versions-training-consumption](../1792661016_dataset-versions-training-consumption.md)             | `a09ce79c3edb14e1cee18b48e6d72e877b23f4f0a20577969f7e40e5546ecca3` |
| `EPIC-11-PERMISSIONS-AND-LIFECYCLE.md`         | [dataset-permissions-lifecycle](../1792661016_dataset-permissions-lifecycle.md)                             | `f689bccbc76946b628bb9d9e37b4c891d3e2ca1bbc69d13b7d4282c4987e9d54` |
| `EPIC-12-OPERATIONS-CAPACITY-AND-RECOVERY.md`  | [dataset-storage-operations-recovery](../1792661016_dataset-storage-operations-recovery.md)                 | `fe7fbfcb63d1d7c160f466ca792cf53e36b0de0593effab06a95fe1e89eee3bc` |
