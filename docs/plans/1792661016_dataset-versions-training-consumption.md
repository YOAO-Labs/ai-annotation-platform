# EPIC-10：数据集版本、血缘与训练消费

> 状态：E10-A、E10-B1、E10-B2-A complete；其余血缘增强与 E10-C 缓存保留后续切片，不分配发布版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 项目          | 内容                                                                                |
| ------------- | ----------------------------------------------------------------------------------- |
| 状态          | E10-A/B1/B2-A 已完成；其余 E10-B2/C 待独立评审                                      |
| 日期          | 2026-10-22（Asia/Shanghai）                                                         |
| 优先级 / 阶段 | P0：M1 既有冻结不退化及单目录最小冻结；P1：M2 多来源与视图版本；P2：M3 按需训练缓存 |
| 负责角色      | 训练平台后端、数据集后端、算法使用者、测试                                          |
| 最小依赖      | E01 内容身份、E05 验证读取、E11 最小版本权限                                        |
| 协作          | E04 源变化；E06 训练节点可达性；E08 筛选/质量；E12 容量与恢复                       |

## E10-B2-A 晋升复核与实施合同

2026-10-24 基于 `7618fbc85` 复核，工作树干净。E10-A/B1、E05缓存已交付；运行/模型/版本关联已有授权血缘图与固定摘要，不重复建设。本片落实新增引用样本的冻结来源证据与已有样本比较中的来源解释；保留人工3100/8100，不重建后端或ML镜像，不分配版本。

独立 `source_lineage_contract` 为 `{schema: 1, scope: accepted-reference}`，不改变几何合同或 `asset_reference.schema=1`。只为新引用版本记录每成员白名单 `source_lineage`：source/location/scan ID、path_digest、accepted revision ID/number/previous ID、持久采纳/扫描完成/内容验证时间、来源和位置配置修订。无路径、名称、凭据或节点/宿主信息；缺失旧事实用null，不以当前时间制造历史。预检与创建从同一已持久事实得到稳定摘要，事实变化后须重检；Worker重试按固定Asset/SHA读取，不能用当前来源关系替换旧证据或将历史证据与当前context全量相等作为重试门禁。

两条打包路径在摘要计算前保留此合同及证据，原始snapshot/source/content/validation及ZIP摘要覆盖新证据；训练语义/分区/规范比较摘要继续只表达内容、标签和切分。历史无新合同版本原样读取、不回填、不改字节；未知或不合法新合同明确拒绝，Native Executor验证新合同后仍逐图/标签校验。没有按内容摘要合并逻辑版本的新去重owner。

来源比较作为独立覆盖层：完整证据的关系多重集合变化为 `source_relation_changed`，关系一致但采纳/观测事实变化为 `source_evidence_updated`，任一方缺少或部分证据为 `source_unknown`。不将来源变化误记为标注/分区变化；可同时显示“训练语义未变化”。新增来源比较覆盖状态，保留既有语义完整性状态。重复SHA保留所有冻结成员；筛选在分页前进行且双方版本继续项目授权。仅据完整冻结证据陈述事实，不推断物理搬迁。样本详情和比较仅读snapshot/manifest，不join当前来源/位置/任务补历史。

改动范围超过8文件：`services/{training_dataset_sources,training_datasets,training_dataset_bridge,training_dataset_analysis}.py`、`workers/training_dataset.py`、`schemas/training_dataset_analysis.py`、`api/v1/training_dataset_analysis.py`；共享新增lineage合同和Native Executor消费校验；Web `api/trainingDatasets.ts`、Projects下既有insights/样本预览与比较组件及必要小组件；对应冻结/打包/分析/API/Executor/Web测试。分析缓存schema提升而非新增业务表或迁移。同步OpenAPI/生成类型、SDK覆盖合同、用户版本指南、API/架构、README/CHANGELOG和本计划/实施队列。节点训练缓存、managed来源补齐、实际消费集合回执仍后置。

验证：owner管理的一次性test环境，覆盖稳定预检、新证据摘要、来源变化409、同SHA关系变化但语义不变、仅证据更新、历史/混合未知、多成员/分页/双版本授权、源更新或注销不改历史、检测/polygon/RLE打包、旧golden、运行/模型摘要与Executor实际输入校验。Web覆盖来源分类及冻结成员切换/迟到回调；真实浏览器明暗/桌面/窄屏检查冻结来源证据及来源比较，检查错误/请求/溢出。刷新受影响人工Worker并验证实际签名与派发，保留业务计数；任务资源和产物清理后提交。回退保留已创建包与旧合同读取，不丢弃新字段重发创建请求。

## E10-B1 / E08-B5 晋升复核与实施合同

2026-10-23 基于 `51ed89ab1` 复核，工作树干净。上一轮完成 Scene，现有单条 PG 标注快照、严格原图读取、项目规范版本流和幂等创建均已实现；本轮只补当前组合筛选到该链路的成员衔接，不重复建设多来源冻结。用户已授权整套当前环境可实施部分；3100/8100及现有数据保留，不重建镜像。

```text
Dataset filter / restored personal view -> explicit project choice
                  |                                  |
                  +---- strict dataset_filter -------+
                                      |
shared authorized item SQL -> paged project task picker
                                      |
          one PG task/item/class/annotation snapshot + selection evidence
                                      |
           preflight digest -> same digest on create -> existing frozen version
                                      |
                     existing Worker / verified copy / manifest / ZIP
```

新增可选 TrainingDatasetSelection.dataset_filter，复用 DatasetItemFilters，必须同时提供 dataset_id。无过滤字段与显式 null 的旧请求保持原 canonical 字节。具体查询定义独立于视图ID、分页、布局；不能静默忽略非法字段、尺寸或 Scene/来源。服务端把列表筛选抽成同一 SQL owner，来源/Scene验证、观测及权限、条件组合先于 Task 范围；分页任务选择与训练预检均复用。新 GET /tasks 的 dataset_filter 为最多2000字符的严格 JSON，仅项目管理者使用，必须是当前项目关联数据集。显式任务选择不在条件内则404，不静默求交后成功。

最终范围是当前项目中匹配条件的任务，再应用批次、显式任务和既有标注资格；未关联的样本不是训练任务。预检显示所选/合格/排除，源观测筛选不替代严格读取。新过滤选择在 snapshot 中保留带 schema 的定义及匹配 task/item/version 成员证据，并将摘要、数量和定义写入 source；新成员或筛选变化使预检摘要失效。固定样本、媒体及标注继续由既有版本 owner 保存；创建后条件、新成员、Scene删除或标注编辑不能改写旧 snapshot/manifest/package。历史请求、快照和包不回填。

Web 在数据页提供“冻结当前筛选”的居中项目选择；已有两处整个数据集入口明确其范围。只有查询已成功、个人视图已恢复且条件可编码时开放当前筛选入口。项目准备页显示具体条件，保留原数据集范围确认，可明确清除过滤或切换数据集。任务勾选分页与过滤一致。URL 同数据集换条件会重建 owner，旧预检回调失效；未确认创建请求优先按原 body 恢复和幂等重发，不套用新URL，不新增草稿存储 owner。无效URL/本地过滤阻断检查，不能变成整集。

本轮超过8文件、跨 API/Web/合同/正式文档。准确范围：API `schemas/{training_dataset,dataset_view}.py`、`services/{dataset_workbench,training_datasets}.py`、`api/v1/tasks/list.py`；新增 `tests/test_training_dataset_filters.py`，扩展既有 training/workbench/Scene 回归及必要任务分页测试。Web `pages/Datasets/{DatasetDetailPage,datasetTrainingSelection}.tsx/ts` 与测试、`pages/Projects/ProjectLearningPage.tsx` 与测试、`pages/Training/trainingDatasetDraftStorage.ts` 与测试、`api/tasks.ts` 及测试；必要新增轻量过滤摘要组件沿用这些 owner。OpenAPI、本地生成类型、API index、SDK合同；`README.md`、`CHANGELOG.md`、数据集用户指南、训练版本用户/API/架构指南、任务API指南，本计划/E08/队列/路线索引。没有迁移、依赖或新任务。Worker直接消费固定 snapshot，无重新求筛选；其共享训练模块若需刷新仅重启本工作树受影响Worker，确认实际注册及执行，不重建镜像。

验证：一次性 test DB 下运行新过滤测试、既有训练版本/引用冻结/工作台/Scene回归，覆盖查询等价/跨页、组合、任务范围、非法/删除/跨数据集/来源撤权、预检后新增或标注改动409、固定旧版本/幂等、旧 canonical。Web 验证URL完整定义与恢复门、过滤下任务分页、清除/切数据集、会话/URL迟到回调、原创建草稿保留，SDK合同、OpenAPI、tsc/lint/css、文档和diff。真实浏览器验证当前筛选进入项目、检查/创建到ready、保存视图变化后旧成员/包不变、明暗桌面/窄屏、网络/控制台；使用任务自有或独立e2e数据，清理所有临时模式和产物，保留长期人工环境。回退提交可关闭新选择入口；已创建版本仍按固定snapshot消费，新过滤创建请求须确认或导出后降级，不能丢弃字段重发。

## 本次实施：E10-A（2026-10-23）

用户已授权“完成 E10-A，再推进 E08/E09 页面整合”。重新核查基线 `48bda47bc`，工作树干净；E03/E04 已完成，E05-A 的严格读取已可复用。当前 API 在选择快照时返回 `reference_not_supported`，Worker 同时拒绝 reference 键。本次通过晋升复核，只实现单目录最小冻结；E10-B 的多来源视图/增强血缘和 E10-C 节点缓存不在本切片。

### 实施合同与范围

- 复用 TrainingDatasetVersion、规范任务版本流、单条 PG 标注/类别快照、attempt fencing、持久媒体和 ZIP；不新增版本表或迁移，不改历史 snapshot/manifest/包/hash。
- 仅新引用样本增加带 `schema: 1` 的来源身份：DatasetItem、Asset、完整 SHA-256、字节数；保留当前已资格检测、简单多边形、原生实例掩码桥接。几何合同与来源合同分别识别。
- API 预检按现有 resolver 真读、校验与关闭有界快照，报告选中引用数量和预计持久原图复制体量。源文件异常逐项阻止发布，不能静默跳过；保留单图/总量上限及严格读取较小的 JPEG/PNG/WebP 限额。
- Worker 用真实执行节点的已核验 anchor 读取引用；以当前项目/创建者/API Key 校验权限，按固定 Asset/SHA/字节复制，不能把 opaque reference 拼到 datasets 桶。持久冻结副本重试优先且校验 SHA；其可用性不依赖后续源状态。
- 新来源身份以独立嵌套 schema 放入新清单，ZIP 不含宿主路径、凭据或长期 URL；旧输入不增加字段。保持 SHA/Scene 分组、重复标注冲突、父分区与现有消费者。
- UI 沿用项目准备版本入口，展示复制体量、失败原因和已有后台进度；数据集入口在随后 E08-A 页面整合接入同一流程。

```text
项目/任务/标注（单条 PG 快照） -> API 预检 -> 固定样本/Asset/SHA
                                                |
Worker 当前节点/权限 -> E05 严格读取 -> training 持久媒体 -> 现有 ZIP/manifest
                                                |
                                     ready 后只读取保留副本
```

### 准确文件清单

本切片超过 8 个文件，改动跨 API、读取服务、Worker、Web 和文档，保留上述数据流边界。

- `apps/api/app/services/training_datasets.py`：去掉整体引用拒绝、固定引用身份与预检计量。
- `apps/api/app/services/training_dataset_sources.py`（新增）：引用训练身份/严格快照适配；仅复用现有 resolver。
- `apps/api/app/services/dataset_references.py`：允许内部调用显式指定真实读取节点，保留默认 API 语义。
- `apps/api/app/workers/training_dataset.py`：冻结引用内容及真实节点传播、持久副本重试与摘要校验。
- `apps/api/app/services/training_dataset_bridge.py`：独立 schema 来源证据，检测/分割/RLE 共用。
- `apps/api/tests/test_training_dataset_references.py`（新增）：真实文件与一次性 MinIO/DB 完整冻结及故障回归。
- `apps/api/tests/test_dataset_source_training_immutability.py`、`apps/api/tests/test_training_datasets.py`：现有托管与历史合同回归起点；必要时扩展。
- `apps/web/src/pages/Projects/ProjectLearningPage.tsx`、对应 `.test.tsx`：复制体量与源错误展示。
- `docs-site/user-guide/online-learning/data-versions.md`、`docs-site/user-guide/datasets/import-images.md`、`docs-site/dev/concepts/training-datasets.md`、`docs-site/api/guides/training-datasets.md`、`README.md`、`CHANGELOG.md`：当前能力与边界。
- 本计划、backlog 索引/路线图/交付验收中的引用随晋升更新；若生成的 API schema 不变则只验证 OpenAPI/SDK，不制造无效快照变更。

### 验证与验收门

1. `pnpm dev:worktree -- doctor --mode dev` 核对长期人工环境归属；所有 DB 测试只用 `pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest ...'`。
2. 新引用测试：检测/多边形/RLE；两个项目隔离；预检体量；标注修改后仍用固定快照；源覆盖/删除/不可达阻断；真实节点未经核验拒绝；部分冻结后失败/取消与同版本重试；ready 后删除源仍能读取相同媒体/ZIP。
3. 既有 `test_training_datasets.py`、`test_training_dataset_bridge.py`、`test_training_dataset_analysis.py`、`test_training_storage_deletion.py`、`test_project_task_datasets.py`、`test_dataset_source_training_immutability.py`；补充 resolver 节点兼容回归。
4. Web 准备版本测试、typecheck、lint/CSS tokens；Python Ruff、OpenAPI check/SDK 合同、受影响文档与 `git diff --check`。
5. 在当前人工环境刷新实际 export Worker，验证节点与新签名，实际浏览器创建引用版本、查看进度/冻结样本/下载；在专用演示来源覆盖/移走图片后旧 ready 仍保持原内容。保留 dev 数据，清理测试数据库/桶、临时截图、报告和专属浏览器会话。

回退：关闭新引用版本创建并保持旧 ready 内容/合同可读；不删除已发布冻结内容，不把 reference 回退到旧对象存储路径。每个实施提交需同步正式文档；完成 E10-A 后记录 Outcome，再推进 E08/E09 的独立详情与存储边界整合。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                           | 已有能力、缺口与复用约束                                                                                          |
| ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| [training_dataset.py](../../apps/api/app/db/models/training_dataset.py)         | TrainingDatasetVersion 已有 parent/snapshot/manifest/attempt/包摘要和状态，不新增同义 DatasetVersion。            |
| [project_task_dataset.py](../../apps/api/app/db/models/project_task_dataset.py) | 项目规范任务流及序号已存在；来源血缘不改 membership/ordinal 或历史 request/snapshot/manifest/package digest。     |
| [training_datasets.py](../../apps/api/app/services/training_datasets.py)        | 已有单条 PG 语句固定类别/任务/活跃标注；扩展 source_bucket/source_key 假设和来源修订证据，保留原快照边界。        |
| [training_dataset.py](../../apps/api/app/workers/training_dataset.py)           | materialize_image 已有 IfMatch/VersionId、SHA-256、完整解码、持久冻结及重试复用；接 resolver 而非重建冻结状态机。 |

候选修改入口：[training_datasets.py](../../apps/api/app/services/training_datasets.py)、[training_dataset.py](../../apps/api/app/workers/training_dataset.py)、[training_dataset_bridge.py](../../apps/api/app/services/training_dataset_bridge.py)、[training_dataset_analysis.py](../../apps/api/app/services/training_dataset_analysis.py)、[project_task_dataset.py](../../apps/api/app/services/project_task_dataset.py)、[ProjectLearningPage.tsx](../../apps/web/src/pages/Projects/ProjectLearningPage.tsx)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与现有能力

用户希望已有目录可以直接成为数据集，但外部目录持续变化。某天训练时读取的内容，必须能够与当时标注、标签定义和数据划分对应，不能只记录一个会被覆盖的路径。

项目已经有 TrainingDatasetVersion、父版本、快照、manifest、打包摘要和冻结原图。已有训练桥接逻辑按 SHA-256 与 Scene 分组，保护切分一致性并识别重复图像的标注冲突；已有训练分析与版本比较。本 Epic 扩展多来源读取、来源血缘和消费方式，保留这些能力。[代码依据：训练版本模型](../../apps/api/app/db/models/training_dataset.py)、[训练桥接](../../apps/api/app/services/training_dataset_bridge.py)、[训练分析](../../apps/api/app/services/training_dataset_analysis.py)

## 2. 目标与边界

### 目标

- 活跃数据集可持续更新，已发布训练版本始终引用确定内容。
- 外部目录、外部对象存储与托管来源都能形成可重复读取的训练版本。
- 版本明确记录样本集合、标注修订、标签映射、预处理与切分配置。
- 用户能解释两个版本的变化来源，而不仅是文件数变化。
- 训练节点按 manifest 获取所需内容，逐步减少每次全量重复打包与传输。
- 训练所用版本、实际内容摘要与模型产物之间有可追溯关系。

### 暂不做

- 重建现有训练版本系统、训练任务系统或算法管理系统。
- 把目录当前状态直接当作可复现版本。
- 自动将源文件变化后的旧标注迁移到新内容。
- 在无保留保证的外部 S3 上，仅凭存在 version_id 就宣称永久可复现。

## 3. 用户故事

| 编号    | 用户故事                                                               | 结果                                             |
| ------- | ---------------------------------------------------------------------- | ------------------------------------------------ |
| E10-S01 | 算法工程师选择有权限的关联项目及其标注范围，再从数据集创建固定训练版本 | 项目上下文、筛选条件与最终样本集合都被记录       |
| E10-S02 | 工程师在外部目录更新后仍能复现旧训练                                   | 冻结内容不依赖目录当前路径和当前字节             |
| E10-S03 | 工程师发现来源离线时知道哪些样本阻塞发布                               | 冻结预检与可操作错误清单                         |
| E10-S04 | 工程师比较父子版本时看见图片与标注变化                                 | 复用已有语义 diff，补充来源与可用性解释          |
| E10-S05 | 训练节点多次使用相同图片时避免重复传输                                 | 校验后的内容缓存命中；仍核对版本清单             |
| E10-S06 | 项目负责人检查某模型使用了哪批数据                                     | 训练运行绑定版本 ID、manifest 摘要和实际使用清单 |
| E10-S07 | 数据管理员清理数据时知道哪些版本受影响                                 | 活跃引用与保留策略阻止误删唯一冻结内容           |

## 4. “数据集”与“版本”的行为边界

| 对象                          | 是否随来源更新                   | 用途                                   |
| ----------------------------- | -------------------------------- | -------------------------------------- |
| 活跃 Dataset 与 DatasetSource | 按同步策略变化                   | 浏览、接入、质量整理和业务关联         |
| 保存的筛选视图                | 查询条件固定，结果可变化         | 重复定位“夜间、低清晰度、未标注”等集合 |
| 创建前的选择草稿              | 可以重新选择和预检，尚未创建版本 | 核对样本、标签、来源与切分             |
| 已创建的 pending/running 版本 | 快照已固定，重试复用原身份       | 构建并核验冻结内容；不原地修改快照     |
| 已发布 TrainingDatasetVersion | 固定集合与内容，不随源变化       | 训练、评估、导出和追溯                 |

保存动态筛选条件不等于冻结版本。创建版本时必须把筛选得到的具体样本、Asset 和标注修订写入清单。

## 5. 冻结流程

1. **选择**：先确定 `project_id` 及其任务/标注范围，再接受该项目可用的数据集/视图/明确样本集合及父版本。从项目发起继承上下文；从数据集发起必须选择授权的关联项目。无项目上下文拒绝创建，首期不合并不同项目的标注。
2. **预检**：校验权限、原图可读性、内容摘要、标注完整性、标签映射和目标空间。
3. **固定清单草稿**：记录 DatasetItem、Asset、标注修订与切分输入，后续读取都按这些确定标识执行。
4. **冻结内容**：通过 E05 读取并验证每个 Asset；写入训练版本的持久目标，或使用符合保留保证的不可变副本。
5. **冻结标签与配置**：固定标签映射、标注数据、导出格式、预处理参数、随机种子和分组策略。
6. **划分与核验**：保留现有 SHA-256/Scene 分组和父版本切分继承规则；冲突必须阻止发布或获得明确处理。
7. **发布**：清单、原图、标签与校验全部满足后，原子地将版本置为可消费。
8. **消费**：现有 ZIP 入口继续有效；manifest 消费作为扩展，训练记录实际读取摘要。

### 外部源的默认冻结方式

默认将选中原图复制到受平台保留策略保护的 training 位置。这是用户主动创建可复现版本时发生的副本，与“首次注册目录不复制原图”不冲突，界面必须预告预计体量。

M1 必须让一个已关联项目的原地图片目录，通过统一 resolver 完成最小冻结并继续使用现有 ZIP 消费。保存视图、多来源组合、manifest 增强和节点缓存可后置；不得让新 reference 样本落回旧的全局 datasets 桶拼接路径。

已有可信不可变版本或快照时，可以探索引用方式，但须验证平台读取权限、版本保留期限、删除防护和恢复能力。未证明上述条件的来源仍走持久冻结副本。

## 6. 冻结的并发与失败语义

- 冻结过程中来源发生变化，不能以新内容填充旧 Asset；当前项失败或从另一已验证副本读取。
- 源离线但已有匹配、受保留的冻结副本时可以复用；只有缩略图不算原图可用。
- 某些样本不可读取时默认不发布“缺了一部分却同名”的版本。用户可返回创建前选择流程显式排除，并创建新版本；已创建版本的 snapshot、请求摘要与重试身份不原地修改。
- 冻结任务重试按照版本与内容幂等写入；已验证对象可复用，未完成对象不能被视为完整。
- 标注在冻结期间继续修改时，版本读取已固定修订；若现有标注模型不足以稳定引用，先在冻结事务中复制标注快照。
- 取消/失败版本保留诊断信息；未引用临时对象经过宽限期清理，不能立即删除其他版本正在复用的内容。
- 不使用本地热缓存作为唯一的发布版本原图保存位置。

## 7. Manifest 与 API 草案

仅对新建版本扩展 manifest 字段，并赋予明确的新合同身份或 schema_version，消费者声明支持范围。历史 manifest、snapshot、请求与包字节及其摘要保持原样，继续由旧合同兼容读取；不能在回填位置或管理关联时补写历史 manifest。

| 字段组   | 建议内容                                                                                           |
| -------- | -------------------------------------------------------------------------------------------------- |
| 版本身份 | version_id、project_id、parent_version_id、created_at、created_by、manifest_digest                 |
| 样本集合 | dataset_id、dataset_item_id、asset_id、selection_definition、selection_digest                      |
| 内容身份 | checksum_algorithm/value、size_bytes、media_type、尺寸                                             |
| 标注身份 | project_id、task_id、annotation_snapshot/revision、annotation_scope、label_schema_digest、标注摘要 |
| 切分     | split、group_key、scene_id、seed、分组/继承策略版本                                                |
| 来源血缘 | source_id、location_id、观测时间、内容验证时间、原始来源关系                                       |
| 处理过程 | 导出格式与版本、预处理配置摘要、构建程序版本                                                       |
| 冻结产物 | 持久读取引用、包摘要、冻结策略、保留状态                                                           |

导出给训练节点的清单不携带凭据、长期签名 URL 或无需暴露的宿主机绝对路径。访问凭证在消费时按节点身份取得。

清单中的原图摘要指完整原图内容；缩略图不能作为原图回退。有意预处理后的训练产物单独记录输入 Asset、转换配置、处理器版本及产物自身摘要，不混淆原始内容与派生内容。

建议在现有版本 API 上增加“冻结预检”“来源可用性”“manifest 获取”和“版本消费摘要”能力；复用当前版本创建、比较和打包接口，避免同时维护两套版本资源。

## 8. 训练消费与缓存切片

### M1 / M2

- 原有打包与训练消费方式继续工作，改由统一内容解析器取得原图。
- 冻结预检显示新增持久副本体量；完成后可在版本详情核对实占与摘要。
- 旧版本保持旧 manifest 解析兼容，新字段采用显式 schema_version。

### M3

- 训练节点根据 manifest 按需预取，按内容摘要命中节点缓存。
- 节点缓存有空间限制、校验、失败重试和清理；摘要不符时丢弃重取。
- 每次训练仍核对期望集合，不能因缓存目录已有同名文件而跳过内容校验。
- 训练的实际输入清单、缺失处理与失败状态回传，不能静默跳过样本。
- 采用 ZIP、manifest 或预取缓存取决框架与任务类型，保留兼容方式。

## 9. UI 边界

数据集详情展示“关联训练版本”和创建入口，按有权限的项目过滤。创建先选择项目/标注范围，再进入现有项目/训练模块的共用冻结流程，显示样本范围、来源状态和预计冻结体量；版本详情、编辑、差异与训练运行仍保持单一权威入口，不另造无项目版本体系。

存储页显示 training 用途的持久占用和保留压力，点击进入版本引用明细；不复制一套版本编辑器。数据集页不维护训练桶 endpoint 或保留服务配置。

版本对比复用现有图像/标签语义差异，补充“来源换位置但内容相同”和“内容真的改变”的区别。

## 10. 验收标准

| 编号     | 可执行验收                                                                                                                                                   |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| E10-AC01 | M1 中已关联项目的原地目录可通过统一 resolver 创建可消费训练版本，project_id、原图、该项目标签与 manifest 摘要对应，现有 ZIP 消费成功；缺少项目上下文拒绝创建 |
| E10-AC02 | 发布版本后修改/移走源目录图片，再次读取该版本仍得到相同内容摘要；已发布版本不能退回读实时源，未完成冻结的草稿不能伪装为已发布版本                            |
| E10-AC03 | 冻结过程中同路径图片被覆盖，任务阻止发布错误内容，不生成旧标注配新图的版本                                                                                   |
| E10-AC04 | 冻结期间标注发生修改，版本只包含选定的固定标注快照；版本说明可解释使用的是哪个修订                                                                           |
| E10-AC05 | 新来源仍满足既有 SHA-256/Scene 分组与父版本切分规则，重复图像标注冲突不被静默忽略                                                                            |
| E10-AC06 | 冻结失败后重试不重复创建逻辑版本，不重复计入相同产物，未完成版本不能被训练任务选择为已就绪                                                                   |
| E10-AC07 | 动态视图新增样本不改变已发布版本；新建版本清单才纳入新增内容，版本 diff 能说明变化                                                                           |
| E10-AC08 | 训练缓存损坏或内容不符时重新获取/失败，训练实际使用集合与 manifest 可对账，不静默跳过样本                                                                    |
| E10-AC09 | 删除活跃数据集或来源不会级联删除被保留版本引用的冻结原图；清理版本按 E11 检查运行与模型引用                                                                  |
| E10-AC10 | 升级后历史版本预览、打包、比较与训练继续通过；新 manifest 不泄露存储凭据或长期访问地址                                                                       |
| E10-AC11 | 同一数据集关联两个具有不同标签/标注的项目，分别冻结后各版本只包含选定项目的快照与标签 schema；无权访问另一项目时不能查询或混入其标注                         |

## 11. 测试与兼容迁移

测试覆盖旧托管、原地目录、混合来源、来源离线、已有缓存、重复图像、标签冲突、Scene 分组和父版本继承。构建一套小型确定性夹具，固定图片与标注摘要，用于证明改造前后既有行为一致。

迁移先替换读取原图的实现，不迁移已有冻结内容的物理路径。来源血缘不足的历史版本标记未知，不从当前目录反推历史事实。回滚保留旧 manifest 读取分支；新版本如旧消费者无法解析，应禁止旧消费者启动而非忽略新字段继续训练。

## 12. 风险、待决策与工程任务

需评审冻结副本的默认保留期限、重复冻结内容的授权范围内复用方式、版本状态机与暂存空间配额。manifest 消费与节点缓存需先选一个实际训练框架试点，不承诺所有框架一次适配。

- [ ] 盘点现有版本构建、预览、打包和训练读取入口。
- [ ] 将原图读取接入统一 Asset 解析，保留旧桶兼容。
- [ ] 补齐多来源冻结预检、内容校验和失败阻断。
- [ ] 明确固定标注快照与标签映射的事务边界。
- [ ] 扩展 manifest schema 与来源血缘，不破坏旧消费者。
- [ ] 复用已有分析/diff，展示位置迁移与内容变化区别。
- [ ] 实现训练节点 manifest/缓存试点并校验实际输入清单。
- [ ] 通过源目录变更后旧训练仍可复现的演练。

完成定义：已有图片目录能进入现有训练闭环；活跃来源继续变化时，已发布版本的原图、标注和切分仍保持确定且可读取。

## 本项目推进切片与晋升条件

训练负责人确认当前任务合同和消费者资格。改变 snapshot/manifest 内容的扩展必须产生明确的新合同身份，旧字节/hash 不回写；不借目录接入扩展视频/3D 或未资格任务。

| 切片  | 独立结果         | 交付范围                                                                                  | 验收 / 回退边界                                                  |
| ----- | ---------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| E10-A | 单目录最小冻结   | E03/E05 的固定原图进入已有项目/规范任务冻结和 ZIP，保留已资格检测/分割/原生实例掩码合同。 | X16/X24/X31/X33；源改写后 ready 版本仍同内容；旧 hash/序号不改。 |
| E10-B | 多来源与血缘     | 项目/任务/标注范围显式固定，记录来源与选择证据，复用语义 diff。                           | 两项目不混标注；Scene/内容分组和父版本继承不退化。               |
| E10-C | 节点有界内容缓存 | 仅重复传输成为实测瓶颈后，为一个已资格框架试点 manifest/缓存消费。                        | 损坏重取或失败；实际输入与清单对账；ZIP 保留兼容。               |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_training_datasets.py](../../apps/api/tests/test_training_datasets.py)、[test_training_dataset_bridge.py](../../apps/api/tests/test_training_dataset_bridge.py)、[test_project_task_datasets.py](../../apps/api/tests/test_project_task_datasets.py)、[test_training_dataset_analysis.py](../../apps/api/tests/test_training_dataset_analysis.py)、[test_training_storage_deletion.py](../../apps/api/tests/test_training_storage_deletion.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[training-datasets.md](../../docs-site/dev/concepts/training-datasets.md)、[training-datasets.md](../../docs-site/api/guides/training-datasets.md)、[data-versions.md](../../docs-site/user-guide/online-learning/data-versions.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

- E10-A 完成；提交 `74bd04c12`（`feat(training): freeze verified reference originals into training versions`）。E10-B1 筛选视图冻结和 E10-B2-A 引用来源证据已由下文交付；其余血缘增强、E10-C 节点缓存仍待独立推进，本计划保留在根目录。
- 实现：项目级固定快照接入现有严格 resolver，固定 Item/Asset/SHA/字节；真实 Worker 节点读取证明；独立训练原图与 ZIP；失败/取消同快照重试，优先验证保留副本；预检展示预计持久复制量。新来源使用独立嵌套 schema，旧合同和摘要不回写，无迁移或依赖变更。
- 后端证据：既有训练/桥接/分析/删除/版本流/托管冻结不变性/RLE/引用读取共 171 项通过；新增引用冻结 18 项通过。新增矩阵覆盖检测、多边形、真实栅格 RLE、实际 MinIO 外部 S3 来源、源覆盖/离线/未核验节点、固定标注、两项目隔离、失败/取消重试、伪造保留媒体、读取中账号/API Key 撤权、保留副本重试撤权。随后扩展的 3 个合同用例再次通过训练执行器 `validate_dataset`，并验证解除关联/删除活跃数据集后冻结媒体不变。
- Web：准备版本页 53 项通过，typecheck、受影响 ESLint、CSS tokens、Python Ruff check/format、OpenAPI 一致性、SDK 合同 4 项、文档 frontmatter/navigation 及 `git diff --check` 通过。未宣称全量 Web 或远程 CI 验收。
- 实际浏览器：当前工作树 3100/8100，刷新真实 general/export Worker 后，从项目负责人确认 3 个引用任务，预检显示 17,778 字节，创建版本后实际任务 ready；浏览冻结样本和标注、点击下载、现有新建训练入口可用。版本 `da621d70-54af-47d4-aa8a-7795a56d63f5`。随后专用来源覆盖 vehicle-1、移除 vehicle-2；浏览器仍显示原 vehicle-1 与固定框，API 校验全部 3 张原图、标签和 ZIP SHA 不变，2 个实时源读取明确拒绝，原标注保留。浏览器错误为空；未启动原生模型训练。
- 正式文档：`docs-site/user-guide/online-learning/data-versions.md`、`docs-site/user-guide/datasets/import-images.md`、`docs-site/dev/concepts/training-datasets.md`、`docs-site/api/guides/training-datasets.md`、`README.md`；`CHANGELOG.md` Unreleased 已更新。
- 人工环境与专用演示数据继续保留，说明及固定摘要在忽略的 `.worktree/manual/README.md` / `training-fixture.json`。测试资源、报告、截图、专属浏览器会话按验证流程清理；不重建镜像、不推送、不分配发布版本。
- 用户指定的后续 E08-A/E09-A 页面整合已完成，记录见 [E08 Outcome](archive/1792661016_dataset-workbench-quality.md#outcome) 和 [E09 Outcome](1792661016_dataset-storage-console-boundaries.md#outcome)；后续质量、筛选和运维切片以各Epic当前Outcome为准，节点缓存仍遵守独立触发条件。

### E10-B1 / E08-B5：从筛选视图固定训练成员

2026-10-23 完成，实现与本节记录同提交，基线 `51ed89ab1`。`dataset_filter` 复用已有组合筛选模型，必须绑定当前项目关联的数据集；列表、任务选择和冻结预检共用授权 SQL。Task/Item/类别/活跃标注继续从单条 PG 语句读取。新选择固定 schema 1 的定义与 task/item/version 成员证据，预检 source 显示其摘要和数量；新未审核成员同样改变摘要，创建409要求重检。显式任务超范围拒绝，失效来源/Scene或非法条件不扩大选择。未过滤与显式null保留旧 canonical，旧快照/清单/包不回填；无新迁移、依赖或 Worker 逻辑。

数据页提供居中“冻结当前筛选”项目选择，等个人视图恢复与查询成功后开放；原有入口明确整份数据集。项目显示具体条件、范围确认及相同过滤的分页任务勾选；清除/切数据集使预检和勾选失效。URL只改条件也更换owner，旧回调无效；弹层随导航退休，返回不重开旧目标。未确认的创建请求优先原样恢复body，包括原过滤与摘要，不因新URL改范围或创建新的草稿owner。

本地验证：API/DB 66个不同用例（新过滤10、原训练/工作台/Scene38、真实引用冻结18）、Web分批111个不同用例、SDK合同4项，共181项通过。覆盖组合/count/任务分页、Scene/来源失效、显式任务越界、Key的datasets:read、数据集关联、canonical兼容、新排除成员409、幂等与快照固定、引用真实包/严格读取及旧冻结回归；Web涵盖选项与完整定义、坏URL/非有限数、范围清除、任务分页、原请求异URL恢复、同数据集换条件的迟到预检、弹层导航和焦点。TypeScript、Ruff、Web ESLint/CSS tokens、文档frontmatter196/导航198及diff检查通过；原有4条ESLint warning与2条无关样式建议保留，未运行远程CI。

真实浏览器在独立3200/8200与当前 checkout Worker中执行，无mock。3张已审核图片，个人视图仅ID恢复后以文件名car+png限定2张；居中选择关联项目，负责人模式只列2个匹配任务，回到已审核模式预检后创建 `ff396a7b-bd3f-4422-8614-a9c5a2363bb1`，实际到ready，训练/验证各1张。随后把个人视图改为other，旧版本仍为原2张。运行验证读取ZIP、逐图核对manifest SHA和包SHA，覆盖任务自有源图片后包、snapshot与manifest保持不变；包SHA256为 `37c6cb1ddafb16650a5321f635c15d13c0fcefba883395c7913a02f9d0c426ee`。项目选择与准备表单均检查1440×1000/390×900明暗截图，提示位于控件外，弹层内部滚动，无根横向溢出；全流程无控制台错误或失败请求。

正式说明同步用户数据集/训练版本指南、训练/任务API及架构文档、README、CHANGELOG与OpenAPI/本地类型。独立e2e与test数据库、Redis、九用途桶、样本、视图、版本和Worker均按owner销毁，专用浏览器关闭，测试报告、截图、脚本和缓存清理。原3100/8100、普通/维护Worker及人工演示数据保留，不重建镜像；未推送或发布。此切片完成筛选冻结衔接，E10-B2进一步血缘/diff解释、E10-C条件触发缓存和完整验收矩阵仍须独立推进，整个Epic未完成。

### E10-B2-A：冻结引用来源事实与独立来源比较

2026-10-24 完成，实现与本节记录同提交，基线 `7618fbc85`。新增独立来源合同，为新引用版本保存安全 ID、路径摘要、持久采纳/扫描/验证时间及配置修订；不保存物理路径、名称、节点、凭据。两条打包路径与 Native Executor 共用严格校验，原始摘要及 ZIP 覆盖这些事实，历史合同与包字节不回填。Worker 仅读取当前授权的固定内容，不重新捕获来源证据；新建预检仍严格校验新证据。冻结与历史版本均覆盖此兼容性回归。

分析缓存提升至 schema 6，在共同 SHA 的成员多重集合上单列来源关系变化、证据更新和证据不足；保持训练语义与评估基准比较不受来源字段影响。分页前应用新分类，双方项目权限仍生效。样本详情从保留 manifest 读取，不借当前来源或任务补历史；未入包样本继续返回详情并解释未记录。页面提供独立来源比较和默认折叠的冻结证据，ID/摘要显示并可复制，未知事实明确说明。

本地验证：API/DB 分批去重 268 个用例通过；共享合同及 Executor 独立校验 68 项、Web 43 项、SDK 合同 4 项通过。覆盖旧 golden、新合同非法字段、稳定预检/来源变化409、同SHA重定位但语义不变、证据更新、多成员、历史/混合未知、分页及权限、源更新/注销后旧冻结、检测/polygon/RLE真实打包与保留媒体。首次回归的时区字符串比较已改为时间值比较，最终无未解决失败。Ruff、TypeScript、受影响 ESLint、CSS tokens、OpenAPI/生成类型、文档构建、frontmatter/导航/可读性及 diff 检查通过；CSS checker 的两条既有建议未扩大处理。Executor 全套 adapter 测试未运行：当前 API 虚拟环境缺少其 `psutil` 依赖；已运行独立校验和实际包 `validate_dataset`，未安装依赖或宣称完整训练资格。

实际验收使用独立3111/8111、一次性数据库/Redis/九用途桶、当前checkout真实 Celery Worker；合成来源通过现有扫描/采纳服务准备。两份两图版本均由实际 Worker 到 ready，经 HTTP 下载、ZIP SHA 及 Native Executor 输入校验。同内容显式重定位后，比较为来源关系变化1、训练语义未变化2，语义验证与评估基准均未变；经 impact/proof 生命周期接口暂停来源后，两版冻结详情和媒体继续成功读取。验收脚本修正了相对下载地址入口及平台 Authorization 误传给 S3 签名请求的问题，无产品下载修改。未启动 GPU 训练或验证模型质量。

真实浏览器核对比较分类→筛选→前后对照、两侧来源事实、复制、Escape 与焦点返回；1440×1000/390×844 明暗截图无横向溢出。控制台无错误或警告，观察到的 HTTP 响应无失败；导航/切换取消的请求不计作成功响应。来源详情保持居中弹层、内滚动及角色字号。用户/API/架构、README、CHANGELOG 与 OpenAPI 同步。

人工3100/8100及普通/维护Worker、beat保留并刷新，doctor健康；两个队列实际派发当前训练签名并安全忽略不存在的验收版本。业务数据仍为8数据集、12样本、7项目、12任务、7标注、67异步记录；预测、输入租约与完整缓存仍为0，无活动任务。一次性模式、验收样本/包、浏览器、脚本、截图、报告及任务缓存清理后提交；不重建镜像、不推送、不发布。managed 来源补齐、实际消费集合回执、条件触发的训练节点缓存和完整故事矩阵仍后置，整个E10未完成。
