---
title: 在线学习与训练体系
audience: [dev, ops]
type: explanation
status: stable
last_reviewed: 2026-10-24
---

# 在线学习与训练体系

在线学习与模型训练体系（Online Learning & Training Subsystem）为标注平台提供从「人工审核标注沉淀」到「数据集物化」、「独立算力训练」、「模型指标核验与决策」再到「自训权重动态回流预标注」的端到端闭环能力。

---

## 一、体系全景与核心架构

### 1.1 闭环数据流全景

在线学习的核心目标是让高质量标注成果快速转化为算法模型的迭代收益，并通过预标注流水线与主动选样机制反哺标注生产。整个生命周期包含六个关键阶段：

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/online-learning-loop.svg"
  alt="在线学习端到端闭环架构：从审核标注到数据版本快照、独立执行器训练、逐样本评估、模型注册发布、动态回流预标注，再到主动选样与自动触发策略"
  caption="在线学习端到端闭环数据流全景"
/>

1. **审核标注沉淀**：从业务项目中筛选通过质检审核的有效标注，按任务契约过滤。
2. **冻结快照与数据物化**：语句级一致性读取，生成不可变数据集版本，物化上传至 MinIO `training` 存储桶。
3. **独立算力受控训练**：独立拉取式执行器（`apps/training-executor`）获取任务凭据，通过共享状态目录的 `flock` 文件锁协调同一资源的执行器，守护原生训练子进程。
4. **产物评估与质量核验**：对确切最佳权重执行原生验证，核验 SHA-256 与最终报告；负责人注册候选后可另行发起冻结逐样本评估。
5. **动态回流预标注**：YOLO Backend 通过认证端点安装已核验的检测权重，完成固定样本试用并人工发布后接入单阶段整图预标注。
6. **主动选样与自动触发**：通过难例挖掘启发式算法圈选高价值样本推入新批次，或由自动策略监听增量数据自动触发重训，形成自增益闭环。

### 1.2 核心设计哲学与安全边界

为确保高并发生产环境下的稳定性与数据一致性，在线学习体系遵循四项根本设计原则：

- **不可变快照与版本确定性**：数据版本一经发布即进入只读状态，历史快照、标签文件与 ZIP 清单永不原地篡改。所有派生分析与后续训练引用均绑定完整 SHA-256 摘要。
- **独立执行器与协作锁**：训练任务脱离普通 Web/API 的 Celery 异步队列，运行在独立的拉取式执行器上。使用同一资源的执行器共享持久状态目录，并通过 OS `flock` 防止重复认领。该文件锁不阻止其他进程占用 GPU，部署仍需限制可见设备并管理训练与推理算力。
- **不可变 Attempt 与断点续训隔离**：失败重试或从检查点续训均沿用原任务配置，但会分配全新的 `TrainingAttempt`，新尝试绝不覆盖历史指标曲线或改写前次权重产物。
- **严格收据协议与双向凭证校验**：模型回流时推理后端与平台必须双向核验类别映射顺序、家族规格与运行时资格，防止因标签错位导致几何变形或脏数据污染。

### 1.3 核心领域模型速查

| 实体模型                     | 数据库表 / 模块             | 核心职责与生命周期                                                                    |
| :--------------------------- | :-------------------------- | :------------------------------------------------------------------------------------ |
| **`ProjectTaskDataset`**     | `project_task_datasets`     | 按 `(project_id, canonical_task)` 唯一标识项目派生的任务版本流，隔离不同视觉任务      |
| **`TrainingDatasetVersion`** | `training_dataset_versions` | 冻结快照、物化图片与 ZIP 归档包的唯一持久所有者，序号单调递增                         |
| **`TrainingRun`**            | `training_runs`             | 训练实验配置的不可变载体，锁定训练参数、基础模型与数据版本引用                        |
| **`TrainingAttempt`**        | `training_attempts`         | 单次物理执行尝试，绑定实际执行器、GPU 租约、指标采样与退出回执                        |
| **`TrainingResourceLease`**  | `training_resource_leases`  | 物理 GPU / CPU 算力租约，关联 OS 文件锁状态与心跳租期                                 |
| **`TrainingStudy`**          | `training_studies`          | 有预算约束的串行超参数搜索计划，解析有限参数网格并调度子实验（`TrainingTrial`）       |
| **`SampleEvaluationRun`**    | `sample_evaluation_runs`    | 针对特定数据版本执行的冻结逐样本一对一匹配评估                                        |
| **`TrainedModelVersion`**    | `trained_model_versions`    | 训练产出的权威权重资产，记录完整训练来源、评估报告与类别绑定                          |
| **`ModelRelease`**           | `model_releases`            | 模型在项目中的不可变发布记录，支持版本回退与当前选用锁定（`ProjectModelSelection`）   |
| **`LearningPolicy`**         | `learning_policies`         | 监听项目增量数据的自动重训策略，维护语义基线、冷却窗口与触发记录（`LearningTrigger`） |
| **`ActiveLearningRound`**    | `active_learning_rounds`    | 人工审核的主动学习选样轮次，基于难例启发式评分圈选待标任务                            |

---

## 二、训练数据版本化与快照物化

### 2.1 任务契约与格式规范

系统由纯 Python 共享包 `apps/_shared/task_contract` 统一管理任务名称和历史别名的标准库实现：

- **视觉操作契约**：以规范名称标识视觉任务：
  - `detect`（矩形框目标检测）
  - `segment`（简单多边形或原生实例掩码分割）
  - `classify`（图像分类）
  - `pose`（关键点估计）
  - `obb`（旋转目标框）
- **格式与序列化**：统一规范 AAP、COCO、Ultralytics YOLO 等格式转换。能力由 API registry 管理，前端名称及历史别名均由其快照生成。
- **多边形几何约束**：新简单多边形实例分割采用 `schema 3`（历史 `schema 2` 继续读取），由 `training_contract.dataset` 校验简单多边形：
  - 强制使用规范起点与闭合方向，拒绝存在自相交、顶点重复或有损降采样的几何。
  - 严格限制顶点总数与每图实例数量，每一行保留来源标注版本的精确映射。
- **负样本处理准则**：新数据契约显式固定单一任务、工具与类别范围。混合标注项目在构建时按所选任务过滤，未选工具产生的标注仅作单独统计，绝不隐式视作负样本。

::: warning 视频衍生数据边界
视频抽帧派生图片标注时，样本媒体标记为 `media_type=image`，来源则明确记录 `media_type=video`。抽帧操作必须直接读取原始视频文件，禁止使用浏览器端播放转码后的衍生素材，以确保逐帧时间戳与绝对零基网格帧号严格精确。
:::

### 2.2 项目派生任务版本流与序号机制

项目拥有标注数据与标注工具绑定，而底层存储数据集仅提供可关联的媒体资产。

1. **版本流唯一身份**：
   `ProjectTaskDataset` 采用 `(project_id, canonical_task)` 作为联合唯一键。同一项目内的检测（`detect`）与实例分割（`segment`）各自拥有独立的版本流；分类、旋转框等暂未开放训练的任务仅声明能力，不参与版本流分配。
2. **单调自增序号（Ordinal）**：
   - 关系表 `task_dataset_version_memberships` 以版本 UUID 为主键关联任务数据集与正整数序号。
   - 分配序号前先获取项目级 PostgreSQL Advisory Lock，确保在并发创建时严格按序推进 `next_ordinal`。
   - **序号占号与不可变性**：失败或取消的版本永久占号，重试构建时更新 `training_dataset_versions.attempt_id` 并沿用既有序号，删除版本不回收历史序号。
3. **版本继承语义**：
   父版本代表独立的分区继承关系，允许从旧分支派生，并不自动指向上一自然序号。

### 2.3 语句级快照与素材物化流水线

数据版本的导出与物化由 Celery `export` 队列中的 `workers/training_dataset.py`（`run_training_dataset`）驱动：

```
[API 准备：单语句选择快照、原件完整核验与解码]
        │
        ▼ (重新锁定并复核选择、标注与原件后创建版本)
[Celery Export Worker]
        │
        ├── 1. 优先核验版本保留媒体，缺失时读取已登记原件
        ├── 2. 完整校验 SHA-256、大小、格式与逻辑图像尺寸
        ├── 3. training_dataset_bridge 几何归一化与连通分组
        ├── 4. 组装规范目录，上传媒体、掩码与 ZIP 并完整读回
        └── 5. 复核版本/Job/权限/来源后原子发布 ready 与通知
        │
        ▼
[版本固定的训练存储位置]
(projects/{project_id}/datasets/{version_id}/)
```

#### 冻结前缀的存储归属

`TrainingDatasetVersion.storage_ref` 在新版本创建事务中、首个对象写入前保存平台 Location、配置修订、training 绑定代次、精确 endpoint/bucket/prefix 和物理身份。固定前缀为 `projects/{project_id}/datasets/{version_id}/`，涵盖 `media/`、`masks/` 及各 attempt 的 ZIP；归属与内容 snapshot/manifest 分开，不改变历史训练摘要。`training_dataset_storage` 是共同解析 owner，已有版本不再依据当前 `training_bucket` 解释历史键。

Worker 的保留媒体读取/写入、包发布与尝试清理，analysis 的图片/掩码读取（包括逐样本评估），训练 assignment 的 dataset URL 和增强 preview 的源 URL 均解析同一归属。解析在 I/O 前核对实际客户端端点和精确子键；当前用途桶变化不改已有归属，同端点凭据仍由原 StorageService 管理。端点变化、未知身份或越界键明确失败，不试探其他桶，也不改冻结 `run.config`、preview.spec 或 input_digest。实验权重由下述 Run 归属解析，预览结果仍保留独立存储 owner。

旧 NULL 归属通过显式 `storage-pin` 操作核验当前保留副本后一次性固定，迁移和普通 GET 不自动接管。候选只读已持久绑定，版本 advisory 执行锁排除活跃构建；有界完整核验后再次复查当前管理权限、API Key、版本状态/attempt/内容摘要、候选及维护代次，再原子保存归属和核验范围/清单摘要。就绪包与独立媒体/掩码都要验证；失败/取消版本已有副本须有独立输入 SHA，对象自报摘要不能补成历史内容证据。未知键、无法解释的旧 attempt 或核验不确定均不写归属、不删对象。

删除在原业务事务复制完整归属到 `TrainingStorageDeletion`，提交成功后只清理该 bucket/prefix；身份不符和未知旧数据版本意图保持待核对。尝试清理仍先复查 package 引用，发布提交确认丢失不删除已发布包。数据库拒绝普通更新改写已固定归属；离线恢复仅允许表 owner 在显式恢复事务标记下映射 bucket/identity，其余证据保持。含固定事实时拒绝破坏性降级。部署步骤见[存储恢复说明](/ops/deploy/storage-recovery#训练版本存储归属的部署顺序)。

#### 快照读取一致性

`services/training_datasets.py` 通过单条 PostgreSQL 查询一次性读取工具类别、任务审核状态、素材引用与活跃标注。`prepare_selection` 在独立读取阶段完成原件读取、完整核验与解码；调用者先释放项目、身份及业务行锁，媒体 helper 不提交调用者的业务修改。`finalize_selection` 不执行对象 I/O，在调用者重新取得串行化锁后复核选择、标注、原件与权限，再由同一业务事务保存版本、版本流关联和 Job。预检后输入变化使创建返回 `409`。

#### 托管图片的原件身份与物化

新快照声明 `managed_original_contract=1`，托管样本的 `original` 收据固定 `schema=1`、`owner_kind`、内容 UUID、SHA-256、大小和逻辑位置 UUID。`training_dataset_originals` 通过 `original_sources.resolve_task` 解析 direct/primary Item 或独立 TaskOriginal 的真实归属；端点、保存桶、对象键与维护代次仅来自当前规范解析，不按用途桶猜测，不写入安全原件收据。先用登记大小检查版本总量，再完整捕获字节并解码；保持单图 256 MiB、1 亿像素、JPEG/PNG/WebP/BMP/TIFF、EXIF 逻辑尺寸及每版本 20,000 样本、20 GiB 上限。

HTTP 捕获准确请求凭证，私有 `snapshot.authorization` 保留原 Key 身份指纹，不保存 Bearer 或将短期 JWT 委派给 Worker。读取前后、真实关闭后和发布前复核创建者、原 Key、项目管理权限、原件关系与维护门禁；托管训练保持 `datasets:read`、`annotations:read`，不借用通用下载接口扩大 scope 要求。受信内部调用显式声明调用身份，不能将缺少 Request 解释为 HTTP 已授权。

Worker 优先从版本输出 owner 读取保留媒体，除快照归属与实际摘要外，新合同还要求字节 SHA/大小匹配冻结 `original`。仅保留媒体缺失时使用同一原件收据沿保存位置读取；本次完整 OriginalSource 只留在内存，最后发布时重核。命中合格副本不要求源任务或来源仍存在，已冻结标签也不重新选择。旧就绪包和合格历史保留媒体继续可用；旧快照缺少原件事实且保留缺失时，零原件 I/O 拒绝回源，不从现行桶、ETag 或同名键补造身份。

原件读取使用独立两槽和耐久 `storage_access` 许可，覆盖暂存、解码及真实关闭。所有原生线程工作经 `physical` 等待实际收尾后，才能关闭临时目录或释放执行锁；未知关闭保留许可和容量责任。媒体、mask 与 ZIP 的 PUT 后完整条件读回核对真实 SHA/大小，失败不能置为 ready。Worker 按 Version→Job 加锁，与取消顺序一致；发布同时复核 attempt、Job/Celery 身份、快照摘要、输出 owner、当前权限、原 Key、维护代次及本次回源事实，再统一提交结果与通知。未收尾的版本输出 `StorageOperation` 阻止重试与删除，失败清理仍使用保存位置及耐久许可。

#### 原地引用的冻结读取

组合筛选冻结复用 `dataset_workbench.item_query`，返回数据库关系并在训练的单条 PG 任务/Item/类别/标注查询内限定成员，不先把分页列表搬到客户端。`dataset_filter` 必须绑定已关联数据集；来源和 Scene 的归属/权限验证与工作台共用。新过滤选择在 snapshot.dataset_selection 中固定定义、task/item/version 成员，source 保存定义、数量和证据摘要；它与最终合格样本一起进入原预检摘要。创建重新核对，之后 Worker 只消费原 snapshot，不重跑动态视图。旧请求无过滤字段时 canonical 字节保留，历史快照和包不回填。

`training_dataset_sources.py` 将引用样本接入 `dataset_references.reference_snapshot`，固定 DatasetItem、Asset、SHA-256 与字节数。来源身份使用独立的 `asset_source_contract` / `asset_reference` 嵌套 schema（均为 1）；图片/几何训练合同保持原定义。仅新含引用的 snapshot/manifest 写入这些字段，历史内容与摘要不回填。导出清单只记录业务身份和摘要，不包含挂载路径或凭据。

新引用快照另保留 `source_lineage_contract={schema:1, scope:accepted-reference}`。每成员 `source_lineage` 只含安全业务ID、路径摘要、采纳修订及前驱、扫描轮次、持久采纳/扫描完成/内容验证时间，以及明确命名的来源/位置配置修订。不序列化resolver的完整context；locator、宿主锚点、路径、名称和凭据不进入此证据。不存在的历史时间保留null，不取预检当前时间，保证相同事实的摘要稳定。读取前捕获、读取后核对当前logical context；预检后事实变化使创建409。

来源证据是不可变版本正文的一部分：两条打包路径均在计算source/content/validation及包摘要前复制。Native Executor验证独立合同后继续核对实际图片与标签。历史无此合同的包保持原字节，不回填；未知合同及额外字段明确拒绝。Worker重试仍以固定Asset/SHA读当前授权locator，不重新捕获来源证据，只消费原snapshot，不因正常重定位改写历史事实。

分析缓存schema更新，历史比较仅读冻结snapshot/manifest。每个SHA保留关系与证据的多重集合，独立产生 `source_relation_changed/source_evidence_updated/source_unknown`，不影响原有标签/切分/重复次数及规范化语义比较。新 `source_comparison_status` 表达来源覆盖程度；旧 `status` 仍表达训练语义可确认性。样本详情新增可空 `source_lineage`；未入包记录保留原详情行为并说明未记录。运行/模型继续绑定既有原始摘要及版本关系，不增加同义血缘资源或内容摘要去重机制。

API 预检逐项执行有界严格读取并及时关闭临时快照，报告引用数量及预计新增原图字节。Worker 将自身真实 Celery 节点传入 resolver，使用该节点已核验的 anchor，不能借用 API 节点的挂载证明。原图满足固定内容与尺寸后写入既有 training 版本前缀；失败/取消重试先核验已保留媒体的快照摘要、实际字节 SHA 及固定 Asset SHA。没有保留副本时才读取当前来源。

标注和类别仍由同一 PG 语句固定。构建期间来源改写会失败，标注改写不会改变所选快照；ready 后的预览和 ZIP 永远只读取冻结字节。引用单图沿用 E05 严格读取的 64 MiB、3200 万像素、JPEG/PNG/WebP 限额。当前不引入节点缓存或跨版本内容复用。

#### 连通分组与验证集防泄露

`training_dataset_bridge.py` 严格按相同内容与 scene 的连通分组划分训练集（train）与验证集（val）：

- 具有相同内容 SHA 或属于同一连续场景的样本强制分配至同一数据分区。
- 清单累计保存祖先版本的 task/content/scene 分区映射，防止版本迭代中中间样本被省略后导致历史划分丢失或验证集泄露。

### 2.4 数据版本特征分析与分布度量

数据版本物化完成后，系统在服务端异步生成数据分析报表（`training_dataset_analyses`）：

- **服务端延迟聚合**：
  派生生成固定大小的统计指标，前端无需加载全量标签：
  - 图像宽高比（六档分布）
  - 标注框几何热力图（20×20 归一化中心点与尺寸分布）
  - 目标频次阶梯（0、1、2–5、6–10、11–20、21+ 实例每图分布）
- **版本间差异比对**：
  - 以图像内容 SHA 为唯一比对键，结合类别名称和舍入坐标识别实际修正，将类别重排与标注 ID 变动剥离。
  - 新增与移除基于已知 SHA 集合差异计算，未决内容或未知分区明确独立标记，禁止在数据不全时将空匹配断言为无修改。
- **媒体检查安全防护**：
  媒体预览仅读取 `MINIO_TRAINING_BUCKET` 中的版本保留字节，经 EXIF 校正后输出临时文件流或受限 PNG。预览采用带权限校验的认证 Blob，不向前端暴露长期有效的静态签名 URL。

---

## 三、受控训练执行与算力调度

### 3.1 统一训练入口、项目学习工作区与筛选协议

前端提供全局训练中心与项目内在线学习工作区两层入口，后端由 `/api/v1/training/{catalog,projects,runs}` 与项目级路由提供支撑：

| 入口层级               | 前端路由与标签页                         | 核心职责                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| :--------------------- | :--------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **全局训练中心**       | `/training?tab=start\|queue\|warehouses` | • **开始训练（`start`）**：展示可训练项目列表、训练目录卡片与顶栏共享 GPU 状态徽章<br>• **队列与历史（`queue`）**：按权限范围（`managed_project_scope_clause`）聚合全局实验，支持 `active` / `finished` / `failed` / `all` 状态与框架筛选<br>• **训练仓库（`warehouses`）**：展示 Ultralytics 与 LibreYOLO 框架规格与快速发起入口                                                                                                                                                               |
| **项目在线学习工作区** | `/projects/:id/learning`（4 个主标签页） | • **训练实验（`training`）**：实验列表、新建实验向导（支持按分组重置推荐值、从历史实验/配方一键复用）及单实验详情路由 `/projects/:id/learning/experiments/:runId`<br>• **训练数据（`data`）**：数据版本列表、版本对比与单版本详情路由 `/projects/:id/learning/data/:datasetId`<br>• **模型与发布（`models`）**：候选模型列表与独立详情、来源与使用记录、逐样本评估及项目当前发布切换<br>• **自动训练（`automation`）**：自动重训策略（`LearningPolicy`）与主动选样轮次（`ActiveLearningRound`） |

#### 列表多维筛选协议

项目内的训练数据版本列表（`GET /projects/{id}/training-datasets`）与训练实验列表（`GET /projects/{id}/training-runs`）共享统一的筛选与计数契约（`services/training_list_filters.py` + `TrainingListControls.tsx`）：

- 支持 `q`（名称/ID 模糊搜索）、`status`、`task`、`framework`（训练实验）、`created_from`、`created_to` 组合查询。
- 响应同时返回 `filtered_total`、`unfiltered_total` 与 `status_counts`，使前端能够准确区分「筛选条件无匹配」与「项目尚无任何记录」的空状态。

### 3.2 独立拉取式执行器与资源文件锁

训练任务脱离普通异步 Celery 队列，由常驻宿主机的 `apps/training-executor` 独立拉取与执行：

```
[Platform API (/api/v1/internal/training)]
        │
        ▲ (轮询 Claim / 心跳保持 / 状态回执)
        │
[apps/training-executor (守护进程)]
        │
        ├── 1. 持久目录取得同一 resource_id 的 OS flock 文件锁
        ├── 2. 子进程 Popen 启动训练并继承文件描述符
        ├── 3. 立即启动单调时钟 watchdog 监控进程组
        ├── 4. 实时截获 stdout/stderr 脱敏并落盘采样
        └── 5. 退出时确认整个进程组消亡，固化退出回执
```

#### 实验产物的固定归属

`TrainingRun.storage_ref` 固定 `projects/{project_id}/runs/{run_id}/`，`purpose=training-run`；平台 API、自动策略与参数搜索在新实验创建事务中、首个 attempt 前显式分配。`new_attempt` 和续训不隐式分配。`training_run_storage` 复用归属校验器，并强制键位于该实验下规范的 `attempts/{attempt_id}/` 非空子路径；执行还须通过 `run_id` 外键属于该实验。输入数据使用 `TrainingDatasetVersion` 自己的归属，与产物独立。

上传签名、完成核验、检查点续训、报告及观测日志文件、脱敏副本、模型注册和后续消费均解析 Run owner。`TrainedModelVersion` 通过同项目 Run/Attempt 外键与精确发布清单解析来源，不另存物理归属；模型安装、下载、评估和初始化不从当前 training 桶或目标实验推断来源。冻结配置、初始化 pin、模型 artifact/provenance、内容摘要和共享训练协议保持原合同。

增量迁移 0221 保留旧 NULL，元数据读取仍允许，文件 I/O、签名、上传、领取、续训和注册明确拒绝；停止、失败及资源收尾继续遵守原 fence。显式核验只接收所有 attempt 已终态且无资源租约的实验，从持久 training 绑定发现候选，完整验证独立 upload/publication 清单、SHA/大小和 attempt metadata，拒绝未知额外对象。脱敏副本须能从独立核验的原日志按已知规则重建；不反序列化权重、不回读可变输入、不修补产物。读取后复核权限/API Key、配置与清单指纹、绑定及维护代次，再原子固定当前保留副本证据。

删除事务将完整归属复制到独立 `TrainingStorageDeletion`；NULL 旧实验或数据版本清理意图拒绝物理删除。数据库拒绝普通改写和含固定事实的破坏性降级，离线恢复仅允许表 owner 显式映射 bucket/identity。部署与旧执行器收尾见[运维步骤](/ops/deploy/storage-recovery#训练实验产物归属的部署顺序)。增强预览、逐样本评估和主动学习的自身输出仍保留独立合同，整个 training 用途切换仍未开放；不增加 ML/执行器协议，也不构成实际 GPU 输入或生命周期资格验收。

#### 资源租约与协作文件锁

- 同一 `resource_id` 的执行器通过共享状态目录中的锁文件取得排他锁，不锁定 GPU 设备本身。
- 训练子进程派生时直接继承该文件描述符。执行器主进程异常后，仍存活的子进程继续持锁，其他遵守该协议的执行器不能重复认领同一资源。

#### 心跳与失联（`lost`）保护

- 当执行器因网络断连超时未能上报心跳时，平台状态机仅将其标记为 `lost`，**绝不立刻释放 GPU 租约**。
- 系统持续等待执行器重连或管理员人工核实，防止网络抖动时重复派发实验导致显存踩踏。

#### 单调时钟 Watchdog 与退出回执

1. 子进程 `Popen` 后立即拉起独立的单调时钟 watchdog，优先于日志读取管道与 PID 日志的初始化。
2. 任务终止时先对进程组发送信号，并持续轮询直至确认进程组完全消亡。
3. 退出回执先在本地固化保存，再随不可变的 `finish body` 向平台重试提交，彻底杜绝迟到汇报或虚假成功。

### 3.3 原生训练框架适配与原生权重初始化

平台内置适配器实现框架抽象，支持 Ultralytics YOLO 与 LibreYOLO：

| 维度                   | Ultralytics YOLO                                                                | LibreYOLO                                                                                                    |
| :--------------------- | :------------------------------------------------------------------------------ | :----------------------------------------------------------------------------------------------------------- |
| **支持模型规格**       | `yolo11n/s/m/l/x`（检测）、`yolo11n-seg`（实例分割）                            | `LibreYOLO9t/s/m/c/e`                                                                                        |
| **支持训练任务**       | `detect`、`segment`                                                             | `detect`                                                                                                     |
| **模型回流与评估资格** | 检测和已合格的 YOLO11n 实例分割支持受控回流；分割使用独立 RLE、评估及初始化资格 | YOLO9 t/s/m/c 检测权重通过资格核验后可回流、试用与人工发布；e 仅训练与下载，共享逐样本评估和主动选样尚不支持 |

- **受控项目权重初始化**：
  - `training_initialization` 允许选择同项目内保留的权威候选模型作为初始化起点。
  - 原生 YOLO 以明确的 `weights-only` 路径构建训练器，在首个批次前核对模型骨干网规格、类别数及全部状态张量（state tensors）。
  - 独立证据记录证明 epoch 计数、优化器、调度器、标量缩放器与 EMA 均已重置为干净初始态。
- **平台不可变检查点**：
  - 完整检查点在框架剥离优化器状态之前即复制为只读文件。
  - 断点续训时平台直接下载本地不可变检查点，沿用原配置与验证集基准，生成新的 `TrainingAttempt`，保持历史记录完全隔离。

### 3.4 原生增强预览与 CPU 槽位管理

系统为 Ultralytics YOLO11 图片检测提供原生增强预览，帮助在正式训练前检查增强效果；其他任务和框架不因支持训练而自动获得预览能力。

- **CPU 槽位租约**：使用独立的 `TrainingPreviewResourceLease`，不占用宝贵的 GPU 算力租约与 `TrainingAttempt`。
- **沙箱隔离运行**：
  - 预览子进程对 CUDA 环境进行屏蔽，在导入训练框架前限制虚拟内存上限为 16 GiB、CPU 软上限 30 秒、硬上限 35 秒、墙钟时间 45 秒、单核单线程运行。
  - 原生镜像仅运行 `YOLODataset` 增强流水线，剥离优化器与反向传播计算。
- **产物与缓存回收**：产物输出至 `projects/{project}/previews/{preview}/`，带有 24 小时自动过期机制，由 `cleanup` 队列的后台任务清理过期引用与存储前缀。

### 3.5 有预算的串行超参搜索

针对需要调优学习率、图像尺寸等超参数的场景，系统提供有预算约束的串行搜索（`TrainingStudy` & `TrainingTrial`）：

- **确定性网格解析**：完整参数网格在创建时确定性派生（上限 20 项，禁止随意截断），子实验由 Beat 定时任务 `check_studies`（每 10 秒）驱动推进，并沿用统一的 GPU 资源租约机制。
- **并发锁与防重保护**：
  - 协调器按 `项目 advisory lock -> 授权检查 -> Study FOR NO KEY UPDATE -> Run` 的严格顺序加锁。
  - 非键写锁（`FOR NO KEY UPDATE`）允许子实验外键检查顺利进行，避免与 Run 完成事务产生反向死锁。
- **单调计时账本**：
  - 每次尝试在入队前预扣 `native_budget` 额度。
  - 系统根据已确认提交的 `native_accounting` 动态派生可用余额，失败时间、停止宽限与超时均如实计入，防止额度漂移或透支。

### 3.6 执行观测与日志安全脱敏

执行过程中的资源利用率与输出日志由 `training_observability` 服务独立管理：

- **双流序号与游标分页**：
  - 实时采样流与标准输出日志流分别维护单调自增序号。
  - 客户端通过增量游标拉取，每次最多追赶三页，终态时增量补齐最后轮次，避免提前关闭轮询导致尾部数据丢失。
- **有界日志管道与凭据脱敏**：
  - 管道线程按行持续读取，单行上限硬性截断为 8192 原始字节，超限直接丢弃至下一换行符，防止保留长凭据后缀。
  - 在写入任何临时文件前，自动脱敏 ANSI 转义符、控制字符、外部 URL、已知 API Token、赋值表达式及宿主机绝对路径。单个实验总日志上限限制为 8 MiB。

---

## 四、模型评估、决策与预标注回流

### 4.1 冻结逐样本评估

为衡量模型在真实场景下的泛化能力，系统支持基于冻结数据版本运行离线样本评估（`SampleEvaluationRun`，由 `ml.cpu` 队列执行）：

```
[已注册模型 / 权威权重] ────┐
                            │ (同类一对一贪心匹配)
[冻结数据版本 (验证集)] ────┴──► [SampleEvaluationAttempt] ──► [发布固定评估报告]
```

- **确定性匹配逻辑**：
  - 采用固定阈值、置信度优先的同类一对一贪心匹配算法，评估结果与验证集图像顺序无关。
  - 明确输出精确的精准率（Precision）、召回率（Recall）及各类别漏检/虚警分布，不夸大宣称为原生 COCO AP。
- **推理端持久受控目录与计量封条**：
  - 受控推理实例（`yolo-backend`）在 managed 状态目录中维护固定数量的 `flock` 条带锁，覆盖真实模型加载、预测推理与结果写入的全过程。
  - 推理端暴露 `/managed-evaluation/profile` 与 `/managed-evaluation/requests/{request_id}[/seal]` 端点；封存或取消操作复用同一把条带锁持久化墓碑，防止取消确认后未到达的推理请求再度启动。

### 4.2 实验指标比较与横向对齐

比较视图支持多实验横向对比：

- **指标可比性校验**：系统自动比对各实验的验证集快照摘要、类别顺序与评估器语义。仅在验证集与类别空间完全一致的前提下，才允许曲线叠加对比，防止因基准不一致产生误导。
- **最终报告读取防护**：
  - 报告解析器施加 32 MiB 输入字节预算，严格限制 JSON 嵌套层级与键名长度，拒绝非有限数（NaN / Infinity）。
  - 进程级纯解析缓存（上限 64 项）通过 SHA-256 摘要复用，读取时对实验记录持 `FOR SHARE` 锁，避免与删除操作发生竞态。

### 4.3 模型谱系、决策与发布历史

平台通过 `model_lineage` 服务查询训练来源与使用记录，并用 `ModelDecision` 追加保存候选标记和说明：

- **单跳关系投影**：
  - 谱系查询采用 SQL 显式字段投影，每次仅读取当前授权节点及其直接父子关系，禁止无边界的递归查询，并在页面访问路径中实施环路拦截。
- **追加式决策记录**：
  - 负责人在模型详情保存普通候选、待进一步评估、暂不采用或已归档标记及说明时，按自增序号追加写入 `model_decisions` 表。发布独立写入 `model_releases`，候选标记不会自动发布或停用模型。
  - 记录与模型、项目之间采用 `RESTRICT` 外键保护，即使用户账号被注销，历史操作显示名与审核意见依然完整保留。
- **发布代次控制**：
  - 项目当前选用模型通过 `(project_id, task)` 的 `ProjectModelSelection` 记录；发布代次、回滚和 CAS 均限定同任务。更新发布时采用 generation 代次比对与原子交换，防止迟到的旧配置覆盖最新有效发布。

### 4.4 预标注动态回流与收据协议

自训检测（`detect`）和已合格 YOLO11n 实例分割（`segment`）经过核验与发布后，可回流至预标注子系统；分割的原生像素契约与预算见[实例分割闭环](./instance-segmentation-learning)。

- **安装与固定样本试用**：推理服务的 `POST /managed-models` 与 `GET /managed-models/{version_id}` 使用 Backend Bearer 认证。平台发送权威权重下载地址，服务验证字节数、SHA-256、原生检测或分割任务、框架版本和类别顺序，原子保存权重及 `receipt.json`。收据字段以[受控模型部署协议](/dev/reference/ml-backend-protocol#_12-受控自训模型部署协议-managed-models)为准。
- **明确的人工发布**：平台核对收据后，调用标准 `POST /predict`，携带 `context.model_id = "trained:{version_id}"` 与 `context.frozen_model_ref`，在原冻结版本的一张验证图片上试用。成功将部署置为 `ready`；负责人确认结果后才创建项目发布，不自动发布。
- **项目发布解析与固定路由**：`trained_models.resolve_reference()` 将检测别名 `project-release`、分割别名 `project-release:segment` 或 `trained:{version_id}` 解析为具体候选和已核验部署。预标注入队保存完整冻结引用，wire 请求携带最小权重身份；返回结果的原生类别索引经验证后映射为项目类别。检测支持已合格的整图或父框裁剪管线；分割仅支持整图源阶段，输出原尺寸实例掩码，不支持裁剪、交互提示或视频。目标实例不可用时不会改用其他权重。

实例、服务池、路由模式和访问 Token 的关系见[模型服务接入、路由与认证](./ml-backend-routing-and-auth)，界面验收见[模型回流与默认预标](/user-guide/online-learning/models)。

---

## 五、持续迭代与运维规范

### 5.1 自动学习策略与语义基线

对于数据持续流入的业务项目，管理员按任务分别配置自动学习策略（`LearningPolicy`）；两个任务共享项目级自动候选串行限制，默认均关闭：

策略检查先保存本次配置与调度上下文并释放业务锁，调用同一 `prepare_selection` 核验原件；重取项目串行锁后，重新检查策略修订、启用状态、配置、基线、权限和活动 Trigger，再调用 `finalize_selection`。较新的编辑或并发检查优先，旧准备结果不能覆盖它们。内部可信调用仍执行策略自身的 scope 与身份校验。

- **语义基线对账**：
  - 系统以有序类别、图像实际素材的 SHA-256 及规范化标签计算数据版本的「语义摘要」。
  - 仅统计训练分区中真正发生图像新增或标签修改的增量样本数，任务分配、标注员身份与元数据变动不计入增长基线。
- **自动触发约束**：
  - `LearningTrigger` 记录每次候选构建与终态审计（`building → queued → running → succeeded / failed / cancelled`）。
  - 数据库部分唯一索引限制每个项目**同时仅允许存在一个活跃的自动实验**（包含排队、准备、训练与失联状态）。
  - 每次成功训练完成后方可推进语义基线；若实验失败或被取消，系统保留原基线并阻止相同增量数据的重复触发。

### 5.2 人工审核主动选样

主动学习机制（`ActiveLearningRound`，由 `ml.cpu` 队列执行）用于从海量未标注池中精准筛选高价值样本：

```
[海量未分派图片池]
        │
        ▼ (过滤已标注、已分派或存在编辑历史的数据)
[候选代表连通组筛选]
        │
        ├── 75% 难例挖掘：max(1 - score) + 跨类别 IoU 重叠启发式评分
        └── 25% 随机探索：保留探索子集，避免采样偏置
        │
        ▼
[独立 Batch 生成] ──► [分派至标注员工作台优先作业]
```

- **候选图片资格边界**：
  - 候选必须是未分派、无任何人工编辑或处理历史的待标图片，严格核验 Task、Item 与 Batch 初始快照。
  - 相同 content SHA 或同场景的图片先形成连通组，排除规则向整组扩散，再选出稳定代表图片。
- **启发式评分机制**：
  - 难例组采用 `max(1 - score)`（低置信度检测）结合不同类别的最大 IoU（高重叠冲突）作为综合不确定性启发式指标。
  - 系统保留向上取整的四分之一比例用于纯随机探索，不依赖未知的类别概率分布向量，确保选样客观性。
- **零标注污染流转**：
  - 选样过程仅调用 `BatchService` 移动代表任务切片至新批次，绝不向任务写入任何模型伪真值或改变既有发布。

### 5.3 版本化参数契约

为保证超参数配置的确定性与可复现性，体系引入静态参数快照：

- **AST 静态源码解析**：
  - 开发阶段运行 `tools/snapshot_native.py`，通过 AST 静态分析已安装训练框架的原生源码，提取可用参数字段、类型范围与默认值。
  - 参数定义生成不可变快照纳入 `apps/_shared/training_contract`。运行时执行器完全脱机工作，禁止联网动态发现或猜测参数。
- **配方版本化快照**：
  - 训练配方由 `training_recipes` 身份与 `training_recipe_revisions` 不可变快照构成。
  - 配方保存用户显式指定的原始入参（`user_values`），重放时不将平台派生的生效参数（`effective_values`）回写覆盖，保障历史重放精确一致。

### 5.4 存储生命周期、队列拓扑与本地联调

#### 存储桶规划与前缀布局

训练数据版本、实验、预览、逐样本评估和主动学习轮次分别持有不可变内部 `storage_ref`。共享 owner 领域校验实际 endpoint、物理身份、purpose 与规范根前缀；薄适配器再约束预览文件名、评估 attempt+内容 SHA、选样 ordinal。只有新建事务显式分配，重试／重放不为旧 NULL 自动补齐。输出与输入数据版本、来源模型的 owner 独立，冻结 spec、内容摘要与执行器 wire 不携带物理信息。

辅助输出的历史接管复用有界 Reader、共享进程容量和耐久 `storage_access` permit：终态及封存代际围栏、预览全部 CPU 租约与签名 PUT 期限通过后，仅从持久绑定的当前副本核验。完整条件读取与读后清单／HEAD重核完成，再复核权限／Key、全部父子行指纹、绑定及 Gate 代次。取消等物理关闭；未知关闭不释放责任。旧评估报告的独立发布回执与当前共享 Sample 证据分开，不能重写旧语义状态来通过接管。

Preview 原行、EvaluationRun 的删除意图行分别承担耐久清理责任；I/O 前释放 CPU／业务锁，删除失败或未知归属保留原行。主动学习只保留现有报告、图片与引用，没有物理删除入口。受控离线恢复对五类 owner 只映射 bucket／storage_identity，其他嵌套事实不变；完整用途切换仍未开放。

`MINIO_TRAINING_BUCKET` 环境变量默认配置为 `training`，存储桶内部禁止配置自动过期删除，共包含五类受管对象前缀：

- **数据版本素材**：`projects/{project_id}/datasets/{version_id}/media/`
- **数据版本打包产物**：`projects/{project_id}/datasets/{version_id}/attempts/{attempt_id}/dataset.zip`
- **训练产物与权重**：`projects/{project_id}/runs/{run_id}/attempts/{attempt_id}/`
- **增强预览临时图**：`projects/{project_id}/previews/{preview_id}/`
- **逐样本评估与主动学习产物**：`projects/{project_id}/sample-evaluations/{run_id}/{attempt_id}/{sha}.json` 与 `projects/{project_id}/active-learning/{round_id}/media/{ordinal}.png`

#### 两阶段存储清理意图

当用户发起删除数据版本或终态实验时，系统遵循两阶段安全清理机制：

1. **第一阶段（事务提交）**：在同一事务中删除业务数据库记录，并向 `training_storage_deletions` 写入包含确切存储桶与前缀的清理意图。
2. **第二阶段（异步确认）**：事务提交后尝试物理删除对象。若遇存储故障或网络中断，后台每分钟运行的 `maintain_evaluations` 与 `maintain_rounds` 周期任务会自动按 `FOR UPDATE SKIP LOCKED` 重试清理（每次最多 100 条），仅在对象完全删除后才移除清理意图行。

#### Celery 队列与周期任务拓扑

在线学习后台任务分布在四个 Celery 队列中：

| 队列          | 任务函数（模块）                                                                                                                                                                                                 | 触发方式 / 周期                        | 核心职责                                                                                    |
| :------------ | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------- | :------------------------------------------------------------------------------------------ |
| **`export`**  | `run_training_dataset` (`workers/training_dataset.py`)                                                                                                                                                           | 按需异步触发                           | 冻结快照校验、原图拉取与训练数据集 ZIP 物化                                                 |
| **`ml.cpu`**  | `run_sample_evaluation` (`workers/sample_evaluations.py`)<br>`run_round` (`workers/active_learning.py`)                                                                                                          | 按需异步触发                           | 执行冻结逐样本评估、主动学习候选推理与选样评分                                              |
| **`default`** | `check_policies` (`workers/learning_policy.py`)<br>`check_studies` (`workers/training_study.py`)<br>`maintain_evaluations` (`workers/sample_evaluations.py`)<br>`maintain_rounds` (`workers/active_learning.py`) | Beat 周期调度（60s / 10s / 60s / 60s） | 自动学习策略巡检、超参搜索 Trial 推进、孤儿作业回收与 `training_storage_deletions` 补偿清理 |
| **`cleanup`** | `purge_expired_previews` (`workers/training_preview_cleanup.py`)<br>`purge_observations` (`workers/training_observability_cleanup.py`)                                                                           | Beat 周期调度（60s / 3600s）           | 清理过期增强预览与过期训练观测事件                                                          |

#### 本地联调与开箱即用种子

本地验证前先按[在线训练执行器部署](/ops/deploy/docker-compose#_8-5-受控训练执行器-training-executor)准备固定镜像、基础权重、凭据文件、节点与 GPU 身份，令 API 训练目录和实际执行器一致。执行器不通过 Celery 启动，Compose 只运行已准备的镜像，不承担训练镜像构建。

在确认目标开发数据库与对象存储后，运行 `cd apps/api`，再运行 `PYTHONPATH=. uv run python scripts/seed.py --profile online-training`。这个显式 profile 创建 8 张已审核检测图片和初始数据版本，保留原始 4 张训练图 / 4 张验证图。默认 `demo` profile 不创建这份在线训练基线；夹具也不会自动创建实验、策略或模型发布。失败、取消或中断后可重跑同一命令修复初始版本，仍有执行或清理时按提示等待。

在线训练种子先提交已登记的原图与业务事实，再以显式可信身份执行准备阶段；重新取得项目锁后复查固定请求是否已创建，并完成最终复核。种子不持有源事务锁跨越图片 I/O，也不由媒体 helper 隐式提交种子修改。

配置与命令见[本地开发](../tutorials/local-dev#在线学习与训练执行器联调-可选)；验收过程按[模型回流链路排查](../how-to/debug-model-return)逐段核对。

---

## 六、深入阅读与相关架构决策

- [状态机总览](./state-machines) — 批次、任务与在线学习多套状态机流转
- [仓库地图与模块归属](./repository-map) — 在线学习调用链与组件代码位置
- [ML Backend 协议规范](/dev/reference/ml-backend-protocol) — `/managed-models` 与 `/managed-evaluation/*` 协议详细规范
- [内部 API 端点参考](/dev/reference/internal-api-endpoints) — 执行器与平台内部通信端点
- [ADR 0076: 受控训练模型身份与发布](/dev/adr/archive/0076-controlled-trained-model-identity)
- [ADR 0078: 执行观测与脱敏日志](/dev/adr/archive/0078-attempt-observability-and-redacted-logs)
- [ADR 0079: 冻结逐样本评估](/dev/adr/archive/0079-frozen-sample-evaluation)
- [ADR 0080: 模型谱系与决策](/dev/adr/0080-model-lineage-and-decisions)
- [ADR 0081: 受控项目权重初始化](/dev/adr/archive/0081-controlled-training-initialization)
- [ADR 0082: 有预算的串行搜索](/dev/adr/archive/0082-budgeted-training-studies)
- [ADR 0083: 人工主动选样](/dev/adr/archive/0083-human-reviewed-active-learning)
- [ADR 0084: LibreYOLO 模型回流资格](/dev/adr/archive/0084-qualified-libreyolo-model-return)
- [ADR 0085: 简单多边形实例训练](/dev/adr/0085-qualified-polygon-instance-training)
- [ADR 0086: 规范任务数据契约](/dev/adr/0086-canonical-task-data-contracts)
- [ADR 0087: 项目任务版本流](/dev/adr/archive/0087-project-task-dataset-streams)
