---
title: 在线学习与模型训练 API
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-24
---

# 在线学习与模型训练 API

原地引用图片使用相同项目版本 API。含引用的预检 `summary` 增加 `reference_samples` 与 `original_copy_bytes`（所选原图预计新增持久字节）；读取异常写入逐任务 `issues` 并令 `ready=false`。新 snapshot/manifest 以 `asset_source_contract.schema=1` 和样本 `asset_reference.schema=1` 固定业务 Asset/SHA；不改变几何合同、历史字节或现有 ZIP 消费。Worker 必须具备自身节点的读取 anchor，源变化返回明确构建错误，未就绪版本无法下载。

基路径为 `/api/v1/projects/{project_id}/training-datasets`。所有操作要求项目负责人或超级管理员；API Key 还需 `annotations:read` 与 `datasets:read`，删除和历史存储归属核验另需 `datasets:write`。

| 方法   | 路径后缀                    | 行为                                                 |
| ------ | --------------------------- | ---------------------------------------------------- |
| POST   | `:preflight`                | 校验范围、冻结候选数据，返回预检报告和 `digest`      |
| POST   | 空                          | 创建版本并排队，返回 `202` 与版本、`async_job_id`    |
| GET    | 空                          | `limit=20`、`offset=0` 分页；返回摘要列表与 `total`  |
| GET    | `/{version_id}`             | 默认读取完整清单；`include_manifest=false` 只读摘要  |
| POST   | `/{version_id}/retry`       | 对失败或取消版本重试同一快照，返回 `202`             |
| GET    | `/{version_id}/download`    | 就绪且对象可用时，返回 10 分钟 `url` 和包的 `sha256` |
| POST   | `/{version_id}/storage-pin` | 显式核验并固定历史保留副本，成功返回原版本详情       |
| DELETE | `/{version_id}`             | 删除无引用的终态版本，持久安排存储清理，返回 `204`   |

列表可组合 `q`、`status`、`task`、`task_dataset_id`、`created_from` 和 `created_to` 筛选。`q` 最长 120 字符，去除首尾空白后按版本名称做不区分大小写的字面包含匹配，`%`、`_` 与反斜杠没有通配含义；空白查询不筛选名称。`status` 为 `pending|running|ready|failed|cancelled`。

`created_from`、`created_to` 仅接受 `YYYY-MM-DD` 日历日期，按 UTC 日界处理；起止日期均包含整天，可单独传入，起始晚于结束时返回 `422`。所有条件先于 `total` 计数和分页生效，按创建时间、版本 ID 降序稳定排列。`limit` 默认 20、最大 100，`offset` 默认 0。

版本与终态实验的删除均先在同一数据库事务内移除业务记录并保存独立存储清理意图，提交后才删除对象。提交失败时原记录与文件均保留；存储故障不撤销已提交的业务删除，接口仍返回 `204`，现有 `maintain_evaluations` 周期任务重试清理。两类意图均复制各自完整存储归属，固定端点身份、bucket 与 prefix；身份不符或旧意图尚无可核实归属时保留待核对，不按当前用途桶执行删除。意图不因项目删除丢失。`204` 表示业务删除完成，存储清理可能稍后完成；后续读取或删除同一 ID 返回 `404`。归属为空的旧版本或实验须先显式核验，既有引用保护继续在存储 I/O 前生效。

### 托管图片预检与构建

托管图片使用上述预检、创建与重试接口，请求字段及权限要求保持一致。预检沿任务已登记的 DatasetAsset 或独立 TaskOriginal 的保存位置完整读取，核对 SHA-256、字节数并解码图片；不按当前用途桶或文件名回退。单图上限为 256 MiB、1 亿像素，支持 JPEG、PNG、WebP、BMP、TIFF，按 EXIF 方向检查逻辑尺寸；每版本仍最多 20,000 张、20 GiB。缺少可用原件身份会记录逐任务问题并阻止预检通过，权限、维护或读取冲突不会被当作成功预检。

新快照以 `managed_original_contract=1` 声明托管原件合同，托管样本的 `original` 固定 `schema=1`、`owner_kind`、`content_id`、`sha256`、`size_bytes` 与 `location_id`。该收据进入预检摘要，不包含端点、桶或凭据；`source_key` 不能单独证明原件身份。HTTP 预检与创建持续核对本次请求的凭证和项目管理权限，后台构建与重试继续核对创建者及原 API Key。预检与创建仍只要求 `datasets:read`、`annotations:read` 和项目管理权限，不额外要求 `tasks:read` 或 `datasets:write`。

构建先从版本固定输出位置读取保留媒体，新合同同时核对冻结原件的 SHA 与大小；仅改写对象摘要元信息不能替换固定输入。保留副本缺失时，才按完整冻结身份核验当前原件并回源；副本命中不要求源任务或源文件仍存在。旧就绪包与合格历史副本保持可用，旧版本缺少原件身份且副本缺失时，以 `original_input_unrecorded` 拒绝回源，需登记原图后创建新版本，不能通过 `storage-pin` 补造原图身份。

图片、原生掩码和 ZIP 写入后均完整读回核对大小与 SHA，失败以 `training_output_mismatch` 等构建错误保留，不能发布 `ready`。取消、超时与关闭异常等待实际文件操作收尾；存在未确认结束的版本存储操作时，重试与删除返回 `409 execution_stopping`。

### 固定存储归属与历史版本核验

新版本在排队构建前固定已登记平台位置、配置与绑定代次、端点身份、实际桶和整个版本前缀。ZIP、保留图片及掩码共用该归属；构建重试、包发布与未发布尝试的清理、下载、冻结样本读取、训练派发和增强预览的源 URL 均从版本解析。接口不接受任意桶或路径，内部归属不写入 snapshot、manifest 或训练配置。

已有归属不因当前 `training_bucket` 变化而切换，但实际存储客户端的端点身份变化会返回 `409`，不会用新端点试读旧桶。此规则不开放整个 training 用途绑定切换；[实验产物与模型权重](#实验产物归属与历史核验)通过来源实验解析，预览与逐样本评估等自身输出仍有独立存储合同。

历史 `storage_ref` 为空的版本不会从环境变量静默补值。普通 GET 不执行接管；需要由有项目管理权限的调用者显式发起：

```http
POST /api/v1/projects/{project_id}/training-datasets/{version_id}/storage-pin
```

该操作无请求体，只允许 `ready`、`failed`、`cancelled`；旧构建仍活跃时返回 `409`，须先取消并等待实际构建退出。API Key 需要 `annotations:read`、`datasets:read`、`datasets:write`。候选只来自数据库中已持久登记的 training 绑定及平台位置，不使用当前环境桶作为回退，也不在核验中自动登记绑定。成功返回 `200` 和原版本详情；已有可解析归属时幂等返回。

核验只读整个固定前缀，检查列举前后的集合、条件完整读取和实际 SHA/字节数。就绪版本还核对发布 ZIP 的摘要、大小、安全唯一成员、内外清单与逐图/标签，以及独立保留图片和掩码。去重前的全部 snapshot 样本仍属于合法保留范围；未入包不代表可删除，也仍需独立输入摘要证明。失败或取消版本可缺少尚未复制的成员，但已有副本必须对应固定输入的独立 SHA；只有对象自己的摘要元信息不足以接管。空的已停止前缀可固定，但不证明源素材可读或重试必定成功。

每次最多盘点 100000 个对象、实际读取 64 GiB，协作截止 600 秒；每个 API 进程同时只允许一个核验，容量占用返回 `429`。取消、超时或关闭异常须等待实际物理收尾；无法确认关闭时保留责任与占位。多进程预算需合计。版本/权限/绑定在读取后再次核对，变化时不保存归属。

缺失、摘要冲突、未知对象、无法解释的旧 attempt 包、预算不足及证据缺失均保留原版本和所有对象，不覆盖、不删除、不重写快照或包。`legacy_content_identity_unproven` 表示没有独立内容证据；核验保存的是当前保留副本的观察，不证明历史创建位置、独占所有权或未来不可变。未知旧版本在固定前不能通过普通重试、下载或删除绕过此边界。

### 冻结引用来源证据

新含引用版本以独立 `source_lineage_contract={schema:1, scope:accepted-reference}` 保存安全来源事实，不改变 `asset_reference.schema=1` 或几何合同。每成员 `source_lineage` 含 `source_id/location_id/path_digest/scan_run_id`、可空 `accepted_revision_id/revision_number/previous_revision_id`、可空 `accepted_at/scan_completed_at/content_verified_at`，以及正整数 `source_config_revision/location_config_revision`。时间来自已有持久记录；不返回宿主路径、连接名称、凭据或节点信息。未知合同或额外字段拒绝消费。

相同持久事实保持预检摘要稳定；创建前来源事实变化须重新预检。已创建版本重试保留旧证据，来源后续修改或停用不补写历史。来源字段进入原始快照/清单/包摘要；训练语义和规范化验证比较仍按内容、标签和切分解释。旧无合同版本不回填。

### 按数据集筛选定义冻结

预检与创建均可传 `dataset_filter` 对象，字段沿用[数据集组合筛选](datasets.md#组合筛选与个人动态视图)，必须同时指定已关联当前项目的 `dataset_id`。Scene、来源、文件名、媒体类型、扩展名、尺寸和已存可用性观测可组合；未知字段或非法范围422，失效/跨数据集 Scene、来源或未关联数据集404。未传或显式 null 保留原行为；不传个人视图ID、分页或布局作为范围。

筛选先限定当前项目的匹配任务，再应用批次、显式 task_ids 和原有标注资格；显式任务不在范围内返回404，不静默丢弃。与工作台和分页任务选择共用授权 SQL，所有任务、Item、类别与活跃标注在同一 PG 语句快照中读取。预检 `source.dataset_selection` 包含 schema、filters、matched_tasks 和成员证据 digest；snapshot 另保留 `dataset_selection` 的固定 task/item/version 成员。新过滤字段及证据仅写新版本，不回填历史摘要或包。

创建重新计算预检摘要，匹配成员、条件或合格内容变化返回409并要求重新预检；新增尚未审核的匹配任务同样改变范围证据。创建后只使用固定 snapshot，视图条件、Scene或来源后续变化不会重算其成员。读取仍经过严格原图校验，可用性筛选不是读取许可。重放相同 request_id/body 返回原版本；篡改范围不能复用同一请求标识。

### 项目任务数据集与版本筛选

项目派生的任务数据集基路径为 `/api/v1/projects/{project_id}/task-datasets`，复用上述管理权限及读取 scope：

| 方法 | 路径后缀                 | 行为                                                                 |
| ---- | ------------------------ | -------------------------------------------------------------------- |
| GET  | 空                       | 项目工具绑定及认可冻结版本中的任务、状态计数、最新就绪版本与逐项能力 |
| GET  | `/unrecognized-versions` | 无法识别冻结契约的只读诊断；`limit=20`（1–100）、`offset=0`          |

任务列表返回 `items`，每项包括可空流 UUID `id`、`project_id`、规范 `task`、`status_counts`、`version_count`、`pending_membership_count`、可空 `latest_ready_version` 和 `capabilities`。项目级 `pending_membership_count` 与 `unrecognized_version_count` 单独返回。已支持但还没有版本的任务可以有 `id=null`；只读查询不会创建版本流。

版本流身份仅由项目与规范任务决定，类别集、标注范围、批次及格式是版本属性。此管理扩展不增加导出格式或媒体物化资格，当前训练包仍使用已资格的 Ultralytics YOLO 图片路径。

`capabilities` 分别包含 `create_version`、`qualified_training`、`overview_statistics`、`task_statistics`、`sample_preview`、`semantic_comparison`，每项有 `supported` 与可空 `reason`。这些是数据资格边界，不能替代具体训练器／权重、执行器在线或媒体当前可读检查。检测及已有简单多边形分割保持支持；分类、关键点、旋转框及其他媒体仍须独立资格验证。

原版本列表新增可选 `task` 和 `task_dataset_id`。服务端先验证项目归属与过滤一致性，再按任务、状态过滤及计数分页；同时提供两项时必须匹配。不存在或其他项目的流返回 `404`，同项目但与 `task` 不一致返回 `422`。不传新参数保留原列表行为。父版本及比较基线应使用任务筛选后的 ready 分页，不能截取一个混合任务页面后在客户端筛选。

版本摘要／详情增加可空 `task_dataset_id`、`version_ordinal` 和 `contract_profile`。profile 包括原 schema、规范任务、输出／来源媒体、格式、几何与桥接；不加载完整 manifest。序号只在该任务流内唯一，与 `parent_id`、请求 UUID、包名及 release 版本无关。失败／取消版本保留序号，重试／重放沿用原序号，删除不复用。旧写入者的有效但未关联版本仍会出现在任务过滤列表，管理字段为空；不会伪造序号。

任务识别依据完整认可的冻结契约。无法识别行不归入任一任务流；诊断页只返回 `id`、`name`、`status`、`created_at`、可空 `raw_task`、`error_code` 和 `reason`。历史无任务字段只有符合完整检测旧契约时才识别为检测。原无过滤列表／详情仍采用严格版本模型，不是任意损坏 JSON 的诊断接口。新增两个管理 GET 暂列为 Python SDK 的 `web-only` 排除项，没有新增 SDK 方法。

### 冻结数据洞察

就绪版本提供以下 GET 接口，与原版本读取使用相同项目管理权限和 API Key scope：

| 路径后缀                                         | 返回与参数                                                                              |
| ------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `/{version_id}/insights`                         | 静态统计、已知／未知计数、准备记录、验证身份和媒体读取时间                              |
| `/{version_id}/insights/classes`                 | 类别图片／实例频次；`search`、`limit`、`offset`、`order_by`、`split`                    |
| `/{version_id}/insights/compare`                 | `baseline_id` 指定同项目就绪版本，返回基线到当前版本的方向性变化                        |
| `/{version_id}/insights/samples`                 | 先筛选后分页；`class_index`、`split`、`kind`、`baseline_id`、`change`、`content_sha256` |
| `/{version_id}/insights/samples/{task_id}`       | 冻结归一化框；`limit=500`（最多 1,000）、`offset`                                       |
| `/{version_id}/insights/samples/{task_id}/media` | 认证二进制读取；默认原始冻结图片，`preview=true` 返回最大边 1,600 px 的 PNG             |

类别和样本页默认 25、最多 100 项，返回 `items`、过滤后 `total`、`limit` 和 `offset`。`kind` 为 `all`、`positive`、`negative`、`excluded` 或 `unavailable`；`change` 为 `all`、`added`、`removed`、`labels_changed`、`split_changed`、`multiplicity_changed`、`task_ids_changed`、`reintroduced`、`unchanged`、`unknown`、`source_relation_changed`、`source_evidence_updated` 或 `source_unknown`。非 `all` 变化必须提供基线。移除项返回基线 `source_version_id` 和成员 `version_id`，预览应使用该身份。

样本详情新增可空 `source_lineage={status,reasons,evidence}`，状态为 `recorded/partial/unrecorded`；`evidence` 为上述冻结白名单或null。历史合同缺失、未记录引用来源、采纳修订/扫描/验证时间不足分别解释；未入包记录继续返回详情，来源原因 `not_in_package`。详情只读保留版本，不查询当前来源补历史。

比较新增 `source_comparison_status=complete|partial|unavailable`，与既有训练语义 `status` 分离。仅共同SHA比较来源：完整关系多重集合不同列为 `source_relation_changed`，关系相同但证据不同列为 `source_evidence_updated`，任一侧部分/未记录列为 `source_unknown`。这些分类可与 `unchanged` 重叠，并参与现有分页前筛选；双方仍须属于当前授权项目。

类别 `order_by=index|instances|images` 默认 `index`，频次排序在分页前完成，以类别索引稳定打破并列；可选 `split=train|val` 确定排序使用的分区频次，返回同名参数。类别索引属于各自冻结版本。样本 `content_sha256` 必须是 64 位小写十六进制，可与现有条件和分页组合；没有成员命中返回空页。使用该过滤分别读取当前／基线可找出相同内容的全部重复成员，不能以工作项 UUID 或标签行序配对。

统计按实际包内图片项计算，比较按唯一内容 SHA 计算，变化类别可重叠。`status` 区分 `complete`、`partial`、`unavailable`；标注缺失时详情 `total=null`，不能解释为无目标。`preparation.recorded=false` 表示历史准备记录未保留。比较同时返回既有清单摘要、去重语义摘要和保留重复次数／有序类别的评估摘要，不替换运行或自动策略的原摘要。

概览附加 `contract_profile`、`adapter={id, revision}`、逐项 `capabilities`、`evidence` 和类型化 `task_statistics`。`evidence` 包含 `analyzed_members`、`unknown_label_members`、`known_instances`、`geometry_unknown_members`；未知标注不能作为零实例或负样本，存在相关未知成员时类别分布与缺类结论是部分证据。

`task_statistics.task=detect` 的统计 schema 为 1：

| 字段                 | 形状与分母                                                                            |
| -------------------- | ------------------------------------------------------------------------------------- |
| `image_dimensions`   | 宽／高各六档：<256、256–511、512–1023、1024–2047、2048–4095、≥4096 像素；可测图片成员 |
| `box_centers`        | 归一化中心 x／y 各 20 档；已知有效检测实例                                            |
| `box_dimensions`     | 归一化框宽／高各 20 档；已知有效检测实例                                              |
| `objects_per_member` | `bins` 为 0、1、2–5、6–10、11–20、21+；另含已分析成员与未知标注成员数                 |

三个二维聚合均返回 `x_bins`、`y_bins`、`counts[y][x]`、`unit=member_images|instances`、`population` 和 `unknown_members`。归一化值等于 1 时进入最后一档；unknown 是无法计量的成员数，不能当成未知实例数。每图目标数不包含未知标注成员，也不表示物理面积密度。分割 payload 使用 `task=segment`、`schema_version=1`、`geometry=normalized-simple-polygon`、`known_instances` 和 `polygon_area_sum`，不包装成检测热力图。适配器修订仅改变派生分析缓存，不改原版本或训练摘要。

比较增加 `count_unit=unique_contents`、`baseline_members`、`target_members`、`overlapping_changes` 与 `semantic_unchanged_label`，不能把重叠计数相加成总变化量。`raw_contract_changed` 单独表达原始契约变化；`validation_comparison_basis=canonical-profile-validation-1` 及双方 `validation.comparison_identity` 用认可的规范 profile 表达历史／现行等价契约的比较身份，原 `manifest_digest` 等训练身份不回写。相同任务之外还须媒体／几何／桥接兼容；跨任务返回 `422 task_mismatch`，不兼容 profile 返回 `422 data_contract_mismatch`，未资格契约给出明确原因。来源任务 ID 变化、类别重排与真实训练标签变化分开报告，不能据此推断类别重命名、框移动或对象追踪。

`added`／`removed` 是双方已知内容 SHA 集合的差集。另一侧 `counts.unknown_contents > 0` 时，差集不能证明实际内容缺席；客户端应结合双方概览提示待核实，不把未匹配成员直接声明为新增／删除。共同已知内容含未知分区时计入 `unknown`，不计入 `unchanged` 或推断 `split_changed`；已确认的标注、重复次数和来源变化仍独立保留。

媒体仅读取训练桶该版本的冻结成员；验证保留身份、大小及实际字节 SHA 后才响应，不回退可变源。缺失或损坏返回明确错误；`media` 记录 `verified`、`unavailable`、`unchecked` 与最近检查时间，静态报告不保证当前对象仍可读。未就绪版本返回 `409`，跨项目版本或非成员返回 `404`；洞察失败不改变训练包状态。

预检请求示例：

```json
{
  "mode": "reviewed",
  "seed": 42,
  "validation_fraction": 0.2,
  "batch_id": null,
  "parent_id": null,
  "task_ids": [],
  "confirmed_negative_ids": []
}
```

`manager_confirmed` 模式必须显式填写 `task_ids`；`confirmed_negative_ids` 必须是其中的子集。父版本和批次必须属于当前项目。预检返回 `ready`、`digest`、`classes`、`summary`、`issues` 和 `excluded`；排除原因不阻止剩余合格数据构建，非法几何或不可用素材会阻止构建。

正式创建请求使用相同选择，增加 `name`、UUID `request_id` 和 `preflight_digest`。相同请求标识与内容重复提交返回原版本；相同标识用于不同请求返回 `409`。正式创建重新读取一致快照，摘要与预检不符时返回 `409`，要求重新预检。

`task` 使用 `detect`（默认）或 `segment`。旧名称 `image-detection`、`image-instance-segmentation` 等作为输入别名兼容；输出统一为规范名称。检测和简单多边形桥接的新快照与清单使用 schema 3；原生掩码桥接使用 schema 4 与 `format=aap-instance-rle`。两者显式包含 `task`、`media_type=image`、`format=ultralytics-yolo`、几何类型和桥接修订。历史 schema 1/2、包内容和摘要保持原样，读取时仅派生规范任务。父版本任务不一致返回 `422`；父版本还须有兼容的输出契约、来源媒体及累计 task/content/scene 分组证据，不因属于同一个任务流放宽继承限制。

一个项目可启用多种标注工具，但一个数据版本固定一个任务。`annotation_scope` 记录所选工具与有序类别；检测取 `bbox`，分割取 `region`。其他工具标注排除并计入 `summary.excluded_annotations`，不会使当前任务的合格图片失效。只有其他任务标注的图片不会自动成为当前任务的负样本；须有适用的已审核无目标证据或显式 `confirmed_negative_ids`。所选工具内的非法几何仍阻止构建。

预检同时返回规范任务、媒体、格式、范围、来源和 `summary.objects`。版本列表/详情返回 `task`、`media_type`、`source_media_type`、`format`、`class_count` 与样本/对象/分区摘要。当前物化器只支持图片项目；视频导出先保留原项目采样、协作参数和绝对源帧记录，为后续视频派生图片数据版本提供来源依据。

简单多边形桥接 `ultralytics-simple-polygon-1` 只接受启用区域工具的单一简单多边形，精确去除重复闭合点并规范起点／方向；17 位有效数字保留顶点，原始与 float32 几何均须有效。单实例最多 512 个顶点，单图最多 300 个实例及 8,192 个顶点。孔、多部件、栅格、所选工具内的不支持几何、未知类别、退化、自接触／自相交及原生重复实例阻止构建。清单 `instances` 将一基标签行、类别、行 SHA 与来源标注 ID／版本绑定；无目标成员保留空映射。

简单多边形桥接的样本详情 `labels` 返回 `type=polygon`、归一化 `points`、`area`、`annotation_id` 与 `annotation_version`；检测仍返回 `type=bbox` 和中心／宽高。洞察和样本变化筛选的基线须为相同任务，跨任务返回 `422`。去重与验证多重集语义摘要纳入分割任务／几何／桥接身份，不改变检测旧摘要。

版本状态为 `pending`、`running`、`ready`、`failed`、`cancelled`。通过 `POST /api/v1/async-jobs/{async_job_id}/cancel` 取消构建。取消和失败均同步数据版本与后台任务状态；旧执行退出前重试或删除返回 `409`。异步失败通过 `error_code` 和中文 `error_message` 展示，已复制的冻结素材可用于重试。

列表不返回完整素材清单。可用 `status=ready` 等状态筛选，过滤先于 `limit`／`offset` 分页执行。详情的 `manifest` 在就绪后包含 `summary`、`samples`、累计 `split_history`、`content_digest` 和 `validation_digest`，不会暴露存储凭据或源文件签名 URL。构建和发布时重新校验创建者与原 API Key 的当前权限。

## 人工主动选样 API

基路径 `/api/v1/projects/{project_id}/active-learning-rounds`。所有读取要求当前项目管理权限及 `annotations:read`、`datasets:read`；预览、创建、取消、恢复、决策与反馈另需 `datasets:write`。执行中重新核对创建者与原 API Key 权限。

| 方法      | 路径后缀                                   | 行为                                                                 |
| --------- | ------------------------------------------ | -------------------------------------------------------------------- |
| GET       | `/pool`                                    | 当前合格图片元数据；`limit` 2–32、默认 32，`offset` 分页             |
| POST      | `:preview`                                 | 完整媒体、历史排除和模型资格检查，返回 `preview_digest` 与独立候选   |
| POST      | 空                                         | 相同选择加 `request_id`、`name`、`preview_digest`；返回 `202` 和轮次 |
| GET       | 空                                         | `limit` 1–100、默认 20，`offset` 分页，返回 `items`、`total`         |
| GET       | `/{round_id}`                              | 固定规格摘要、预算计数、当前尝试和最近 20 次尝试                     |
| POST      | `/{round_id}/cancel`                       | 保存取消意图，等待每个已保存请求的停止确认                           |
| POST      | `/{round_id}/retry`                        | 原窗口内恢复失联或尚未推理的失败，返回 `202`；原请求不变             |
| GET       | `/{round_id}/attempts/{attempt_id}/report` | 重新核验完整证据及固定排序报告                                       |
| GET       | `/{round_id}/candidates`                   | 最多 32 个候选的当前状态、决策和已验证成功证据                       |
| GET       | `/{round_id}/candidates/{ordinal}/image`   | 认证读取轮次保留 PNG；零起始 ordinal，未就绪或不完整时拒绝           |
| GET／POST | `/{round_id}/decisions`                    | 读取保留决策／明确接纳或拒绝候选                                     |
| GET／POST | `/{round_id}/feedback`                     | 读取保留反馈／追加当前实际人工作业快照                               |

预览选择体包含 UUID `model_version_id`、`deployment_id`、2–32 个不重复 `sample_task_ids`、整数 `seed`（0–2147483647）、`batch_size`（1–16）、`max_inference_dispatches`（1–96）与 `max_review_tasks`（2–32）。`confidence` 默认 0.1、`nms_iou` 默认 0.7，均大于零且不超过一。审核预算至少 `2 * batch_size`；推理预算至少覆盖去重／排除后的所有候选，独立候选至少 `2 * batch_size`。来源单图 16 MiB／1600 万像素，总原图 128 MiB；保留历史最多 256 项和 16 MiB JSON，超限明确拒绝，不静默省略。

新轮次声明 `original_contract=1`，内部来源快照固定原件 `owner_kind`、内容 UUID、SHA、大小及逻辑位置 ID。预览／创建、Worker 缺失 PNG 的重建和接纳前整个组的复核，都从该原件的已保存位置完整读取；不使用现行用途桶，也不将同 SHA 的另一归属视为同一原件。桶、端点、凭据及后台 Key 指纹不加入公开来源快照。维护修订按本次物理操作固定，正常维护恢复不改写历史内容身份。

HTTP 请求持续复核原 JWT／Key 和原三个 scope；新后台轮次保存原 Key proof 的指纹，Key 同 ID 轮换、scope 或期限变化均失效。后台沿创建者的持续授权执行，不保存 Bearer 或依赖请求 JWT 的短期寿命。物理复制、归一化、PNG 写回／读回及关闭等待真实线程收尾；未知关闭保留访问责任。读取期间原件或门禁变化返回 `409`，失权返回 `403`，正常内容无法解码仍明确失败，不能继续生成评分或批次。

后台原 Key 复核遇到访问记录更新造成的短暂锁冲突时有限重试；仍忙则保留可恢复状态，不把锁占用视为撤权或损坏输入。真实撤权不重试，最终业务事务不会被重试中的辅助检查提前提交。

旧规格保持原样，已有合格 PNG、报告和拒绝决策可继续使用；没有原件事实时，缺失 PNG 重建与新增接纳返回 `409 original_input_unrecorded`，不能用当前 owner 补造冻结记录。既有 PNG 沿自身输出归属核验，损坏时不静默回源修复。创建和接纳的末端复核位于原业务事务中，不提前提交 Job 或人工批次。

创建、决策与反馈均使用 UUID `request_id` 加完整请求摘要幂等：同一内容返回原结果，改变内容返回 `409`。创建重复请求先读取既有轮次，无需重新访问已变化的候选。预览与创建变化返回 `preview_changed`；接纳前整个组变化或新增历史成员返回 `pool_stale`／`source_stale`。轮次状态为 `queued`、`running`、`cancelling`、`lost`、`succeeded`、`failed`、`cancelled`。仅完整成功报告允许决策；取消、封存与到期不得重建旧请求。

决策体为 `request_id`、`action=accept|reject`、1–32 个不重复 `ordinals` 和可选 `batch_name`。接纳仅允许两组计划内候选，创建普通草稿批次并返回固定 `snapshot`、`snapshot_digest` 与可空 `accepted_batch_id`。批次删除不删除决策快照。读取决策或反馈的 `limit` 1–100、默认 20，`offset` 默认 0，返回 `items` 和同权限、同轮次范围的 `total`。

反馈体为 `request_id` 和可选最多四个 `{evaluation_id, attempt_id}`。结果区分实际审核／标注事实、保留的驳回事件、部分已记录会话和未知纠正次数；`efficiency_claim=false`。会话按人和作业类型对合格区间取并集；完整工时不由这些记录推断。恰好两个完整同口径评估才能生成后续指标差值，部分报告返回不可比较原因。快照不会随后续任务编辑而重算。

长期来源引用在删除对象和子记录前检查，返回 `active_learning_reference`；当前没有轮次删除 API。排名证据不写入 Annotation／Prediction，也不触发模型发布。

## 逐样本评估 API

基路径 `/api/v1/projects/{project_id}/sample-evaluations`。读取要求当前项目管理权限和 `annotations:read`、`datasets:read`；创建、停止、恢复和删除另需 `datasets:write`。后台执行同时复核创建者及原 API Key 的当前权限。

| 方法   | 路径后缀                                                          | 行为                                                                             |
| ------ | ----------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| GET    | `/qualification`                                                  | 明确指定 `model_version_id`、`deployment_id`，验证固定部署并读取实际推理 profile |
| POST   | 空                                                                | 冻结明确成员与设置，返回 `202`、评估及当前尝试                                   |
| GET    | 空                                                                | `limit` 1–100、默认 20，`offset` 分页，返回 `items`、`total`                     |
| GET    | `/{evaluation_id}`                                                | 固定设置、当前进度与全部执行尝试                                                 |
| POST   | `/{evaluation_id}/cancel`                                         | 持久化停止意图，等待后端收据或封存确认                                           |
| POST   | `/{evaluation_id}/retry`                                          | 未过期的 `lost`／`failed` 创建新尝试，复用已验证成员                             |
| GET    | `/{evaluation_id}/attempts/{attempt_id}/report`                   | 重新验证存储与完整证据，返回固定尝试的报告                                       |
| GET    | `/{evaluation_id}/attempts/{attempt_id}/samples`                  | 先筛选后分页的误差成员                                                           |
| GET    | `/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/image`  | 认证读取同一冻结预处理的 PNG；零起始 ordinal                                     |
| GET    | `/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/source` | 当前源任务及图片、标签变化，独立于历史报告                                       |
| GET    | `/compare`                                                        | `left_id`、`right_id` 明确指定两个不同评估，比较各自当前尝试                     |
| DELETE | `/{evaluation_id}`                                                | 已确认结束的终态才可删除，返回 `204`；清理失败保留删除意图                       |

创建体包含 UUID `request_id`、`name`、`model_version_id`、`deployment_id`、`dataset_id` 及 1–32 个不重复的 `sample_task_ids`，成员必须来自就绪版本实际包内的 `val` 分区。默认 `role=validation_selection`；`held_out_test` 另须 `held_out_confirmed=true`，并拒绝与候选训练／验证 SHA 重叠的成员。`confidence` 默认 0.25、`matching_iou` 默认 0.5、`nms_iou` 默认 0.7，均为 0–1。相同 request ID 与相同内容重提返回原评估，换内容返回 `409`。

评估固定完整规格与 `spec_digest`、`benchmark_digest`、`comparison_digest`，使用 `aap-detection-greedy` 修订 1 的包含等号、稳定置信度排序和同类一对一匹配；报告固定阈值 TP/FP/FN、精确率／召回率／F1，不声称 AP/mAP。单图最多 200 个真值及 100 个预测，总真值最多 2,000，原图最多 16 MiB／1,600 万像素；标签必须可规范化且类别绑定双射。归一化输入完整验证 SHA、EXIF 方向和逻辑尺寸，记录原图与派生 PNG 摘要。推理响应上限 256 KiB，报告上限 8 MiB 与 500,000 个 JSON 节点。

状态为 `queued`、`running`、`cancelling`、`lost`、`succeeded`、`failed`、`cancelled`。停止、失联与资源退出不等价。稳定成员请求 ID 绑定模型、输入、参数、profile 与固定 24 小时窗口；恢复先读取缓存收据，后端锁和封存记录阻止原请求重叠及迟到 POST。完成报告长期保留，失败／取消报告明确标记未完成成员且 `aggregate=null`。

样本筛选支持零起始 `class_index`、`error=false_positive|false_negative|confusion|localization|failed`、可选 `confidence_min`／`confidence_max`，默认 `limit=12`、最多 100，`offset` 默认 0；界面默认每页 20 行，评估任务仍最多冻结 32 个样本。置信度只筛选预测证据，不重新计算匹配；FN 筛选忽略置信度范围。返回固定尝试、报告与设置摘要及过滤后 `total`。

比较仅在双方报告完整且冻结基准多重集、类别、用途、评估器、预处理、推理语义与阈值兼容时提供 `delta_right_minus_left` 和按冻结内容、标签、尺寸及重复次数对齐的 `sample_pairs`。候选权重和来源不参与兼容键。不兼容时返回明确 `reasons`，不补零或排名。

源解析返回 `source=found|deleted|unavailable`，图片和标签分别标记相同、变化或不可用，另含当前复核／负样本状态。当前标签可能标记 `unsupported`；不将无法读取解释为相同。任务硬删除后历史报告仍可读。原数据版本、模型部署及项目受保留引用保护，删除入口在存储破坏前拒绝；删除评估的存储失败返回 `503 cleanup_pending` 并由维护任务继续清理。上述接口尚未列为 Python SDK 已覆盖能力。

`source` 的当前图片读取先固定 TaskOriginal 或 DatasetAsset 的已核验内容及保存位置，经维护许可完整核对当前登记的 SHA 与大小，再与冻结样本摘要比较。合法登记的新原件可返回 `media=changed`；同键覆盖、缺失、解码失败、未知归属、非图片或超过 16 MiB／1,600 万像素时不返回成功的 `current_sha256`，也不按现行用途桶回退。保留单帧、既有 Pillow 格式及 EXIF 逻辑尺寸检查；不使用通用下载的较大预算。

该端点仍只要求项目管理权限及 `datasets:read`、`annotations:read`，不新增 `tasks:read`。原 JWT／API Key、当前账号与项目权限、完整原件关系和维护修订在读取与分类后复核，真实文件关闭后再核对任务及标注。凭证失效返回 `403`，读取期间原件关系或门禁变化返回 `409`；业务状态或标注变化返回不可用结果，须重新核对。断连、取消与超时等待在途线程和句柄收尾，关闭无法确认时保留未收尾责任。本合同只覆盖当前来源核对，冻结图片、报告及主动学习输入仍沿各自合同。

## 训练总览 API

基路径 `/api/v1/training`。只允许活跃的项目管理员与超级管理员，API Key 需要 `annotations:read`、`datasets:read`。项目列表、实验列表及计数仅包含当前可管理项目；项目成员身份或历史任务创建者身份不扩大管理范围。

| 方法 | 路径        | 行为                                                                                                                      |
| ---- | ----------- | ------------------------------------------------------------------------------------------------------------------------- |
| GET  | `/catalog`  | 训练框架、基础权重、能力、共享资源、执行器和最近心跳                                                                      |
| GET  | `/projects` | 用 `search` 搜索名称或编号，`limit`／`offset` 分页；返回结构资格原因、就绪版本数与活跃实验数                              |
| GET  | `/runs`     | 用 `state=active\|finished\|failed\|all`、可选 `framework`／`project_id` 筛选及分页；返回实验、项目名称与同权限范围的计数 |

`catalog.items[].compatible` 仅在执行器在线时核对实际 GPU、镜像和框架版本；离线时为 `null`。基础模型还返回 `model_family`、`model_size`、自己的 `compatible` 和 `unavailable_reason`，参数定义按家族／任务分别匹配。桶中基础资产可附 `object_key` 和 `size_bytes`，节点必须声明已校验的 SHA 缓存后才就绪；仅对象存在不能接受该节点的训练。配置已启用不代表执行器已在线或兼容。多个框架可共享一个容器，目录不会泄露执行器原始运行环境中的任意字段或凭据，也不会暴露不可管理项目的占用实验名称。

项目的 `can_prepare_dataset` 只表示媒体类型和矩形框类别满足结构条件，不代替实际样本预检。`ready_dataset_count` 只统计已有训练包校验摘要的就绪版本。实验计数在状态筛选前、框架与项目筛选后计算；`failed` 筛选包含失败和失联。队列读取沿用项目实验列表的失联对账，心跳超时标记 `lost`，继续保留资源租约。

## 训练实验 API

基路径 `/api/v1/projects/{project_id}/training-runs`，读取要求项目负责人或超级管理员及 `annotations:read`、`datasets:read`；创建、停止、续训、历史存储归属核验和删除另需 `datasets:write`。

| 方法   | 路径后缀                                                     | 行为                                                                      |
| ------ | ------------------------------------------------------------ | ------------------------------------------------------------------------- |
| GET    | `/catalog`                                                   | 受控框架、基础权重、执行器在线与忙闲状态                                  |
| GET    | `/parameter-schema?framework=ultralytics&base_model=yolo11n` | 当前项目受控模型的完整参数元数据与定义身份                                |
| POST   | `:preflight`                                                 | 检查就绪数据和有界参数，返回固定配置及 `config_digest`                    |
| POST   | 空                                                           | 使用相同请求及 `preflight_digest` 创建实验，返回 `202`                    |
| GET    | 空                                                           | 分页实验列表                                                              |
| GET    | `/{run_id}`                                                  | 实验及全部执行记录                                                        |
| GET    | `/{run_id}/events`                                           | 当前执行事件；`latest=true` 返回最近 100 条，按序排列                     |
| GET    | `/{run_id}/attempts/{attempt_id}/evaluation`                 | 已验证最终评估、身份与分类别分页；`limit` 1–100，默认 50，`offset` 默认 0 |
| POST   | `/{run_id}/cancel`                                           | 请求停止；排队实验立即取消，运行实验等待退出确认                          |
| POST   | `/{run_id}/resume`                                           | 使用当前执行的已发布完整检查点创建下一次执行                              |
| POST   | `/{run_id}/storage-pin`                                      | 显式核验并固定历史终态实验的保留产物，成功返回实验摘要                    |
| GET    | `/{run_id}/attempts/{attempt_id}/artifacts/{name}/download`  | 返回 10 分钟下载 URL、SHA-256 和大小                                      |
| DELETE | `/{run_id}`                                                  | 删除终态实验并持久安排产物清理，返回 `204`                                |

实验列表接受 `limit`（默认 20，最大 100）、`offset`（默认 0）及可组合的 `q`、`status`、`task=detect|segment`、`framework=ultralytics|libreyolo`、`created_from`、`created_to`。`q` 最长 120 字符，去除首尾空白后对实验名称或基础模型 ID 做不区分大小写的字面包含匹配；`%`、`_`、反斜杠没有通配含义。日期采用上述 UTC 日历日期规则，全部条件在 `total` 计数和分页前生效，按创建时间、实验 ID 降序稳定排列。`status` 接受实验的单个精确状态，筛选 `failed` 不包含 `lost`；不支持的状态、任务、框架或日期返回 `422`。未传筛选条件时保留原列表行为。

创建请求包含 `name`、`dataset_id`、`framework`、受控 `base_model`、UUID `request_id` 和 `parameters`。参数范围：`epochs` 1–1000、`batch` 1–64、`imgsz` 128–1280 且为 32 的倍数、`lr0` 大于 0 且不超过 0.1、`seed` 0–2147483647、`patience` 0–1000。原六参数请求继续使用原适配器及精确幂等序列化，不接受任意框架参数。相同请求标识与内容重试返回原实验，防止网络中断后重复启动。

训练创建和预检可显式传 `task=detect|segment`；省略时从冻结数据与基础模型验证得到。显式任务与数据版本不一致返回 `422`，旧请求未包含任务时的请求摘要保持原样。实验、模型和评估响应均返回规范 `task`。

`framework` 支持目录中已验证的 `ultralytics` 和 `libreyolo`。目录的 `capabilities` 返回 `tasks`、`resume`、`artifact_download` 与 `model_registration`；LibreYOLO 的模型注册能力已开放到独立通过原生 CPU 资格检查的 YOLO9 t/s/m/c 检测格式。目录能力表示存在受支持路径，具体冻结实验仍须读取详情顶层 `registration_eligibility` 的 `qualified`、`reason` 和 `qualification`，注册接口会重新计算资格；下载能力不依赖回流资格。配置固定适配器修订和框架原生选项。LibreYOLO 指标文件包含真实 `summary`、`native_raw`、`native_alias_semantics`、`per_class_available=false` 和最终评估权重 SHA-256，不将其别名解释为常规精确率／召回率。

基础模型另返回自己的 `task` 与 `capabilities`。当前 Ultralytics 支持 YOLOv8／YOLO11／YOLO26 的 n/s/m/l/x 检测与实例分割，LibreYOLO9 支持 t/s/m/c 检测；目录只投影管理员配置且有原生路径的组合，未知规格不会因文件名相近而获得资格。新 Ultralytics 实验与分割任务必须提交匹配的完整 `configuration`，分割数据契约与清单严格匹配。注册、初始化、逐样本评估和自动策略仍逐项核验冻结身份，CPU 增强预览保留其独立资格范围。

分割最终评估描述符使用 `revision=2`、`evaluator.task=instance-segmentation`，原生指标保留 `(B)` 与 `(M)` 后缀，分别表示框和掩码。逐类别包含两套 precision、recall、mAP50 与 mAP50–95；评估身份包含独立实例掩码、掩码分辨率、实际掩码处理选项及实现源码。`fitness` 是原生选择分数，不属于 0–1 指标。检测描述符仍使用修订 1，原有语义和摘要保持兼容。

状态包括 `queued`、`preparing`、`training`、`evaluating`、`uploading`，停止时为 `cancelling`，终态为 `succeeded`、`failed`、`cancelled`。`lost` 表示心跳超时但仍持有资源，不能当作已停止。事件带当前 `attempt_id` 和连续 `sequence`，重复序号必须具有相同内容。

事件读取可指定 `attempt_id` 和 `after` 游标，每次最多 100 条，默认按序递增。客户端用最后一条 `sequence` 继续分页，并为各次执行分别维护曲线；`latest=true` 用于单独读取最近事件。新执行清空 `run.metrics`，直到该执行再次上报；历史曲线与比较读取指定执行的事件。最终最佳权重评估保存在已校验的 `metrics` 产物中。

`evaluation` 只读取指定实验、成功执行及其已发布的唯一指标和最佳权重清单。每次读取均核验不超过 8 MiB 的实际报告字节、大小、SHA-256 和执行元数据；纯解析结果按摘要及解析修订缓存，不缓存授权或上下文绑定。执行不存在或跨实验返回 `404`，尚未成功发布或缺少唯一产物返回 `409`，缺失、损坏或身份冲突返回 `422`。

响应的 `summary` 合并已知指标别名，保留真实零，以 `null` 表示负哨兵；LibreYOLO 原生 precision／recall 别名不会作为常规指标返回。`per_class_available`、`per_class_total` 和分页 `per_class` 只表示实际参与评估的类别。`provenance` 包含实验、执行、配置、报告和权重摘要及运行环境。

新报告在独立 `comparison` 块保存修订、配置绑定、最佳权重候选、冻结验证与类别、实际评估器及源码、选项和指标语义。返回的 `descriptor` 与 `compatibility_digest` 供比较使用；摘要排除候选权重和来源标识，包含实际类别覆盖。未记录实际 backend 的原生报告会通过 `comparison_unavailable_reason=evaluator_not_recorded` 说明评估证据不足，零指标仍可读取。旧报告的 `identity_status=unknown`、`descriptor=null`、`compatibility_digest=null`，并说明缺少的证据；不得为它推断默认评估身份或计算提升。既有已注册模型的严格比较保护保持原有语义。

扩展请求先读取项目授权的 `parameter-schema`，然后在同一预检、创建接口提交 `configuration`，不能同时提交 `parameters`：

```json
{
  "name": "增强与优化器对照",
  "dataset_id": "<当前项目的就绪数据 UUID>",
  "framework": "ultralytics",
  "base_model": "yolo11n",
  "request_id": "<新的 UUID>",
  "configuration": {
    "schema_id": "<目录返回的 schema_id>",
    "schema_digest": "<目录返回的 schema_digest>",
    "values": { "epochs": 20, "optimizer": "AdamW", "mosaic": 0.5 }
  }
}
```

创建时仍须带预检返回的 `preflight_digest`。服务端拒绝未知、保留、不适用或尚未验证的键，拒绝隐式类型转换、非有限数、别名冲突和无效依赖。列表保持顺序与精度；目前输入尺寸只开放方形整数，冻结层选择器仍需额外资格验证。`mixup` 在 LibreYOLO 请求中是 `mixup_prob` 的别名，不能同时提交。

冻结的 `configuration` 保存 `identity`、`user_values`、`effective_values`、`origins` 与 `inactive_fields`。它与请求的 wire 结构不同，不能直接作为新请求回传。原生配置观测和核验结果通过 `native-config.json` 归档；扩展实验缺少该证据或配置不符时不能确认成功。

独立执行器使用 `/api/v1/internal/training`，通过 `X-Training-Executor-Token` 认证，调用 `claim` 以及 `attempts/{id}/heartbeat`、`uploads`、`finish`、`recover`。这些接口不使用用户 JWT。回报同时校验当前执行、执行器、启动标识和租约凭据。执行器收到短期对象 URL，没有数据库或存储管理凭据；claim 发送前持久化请求标识，丢失响应后恢复同一个分配。

### 实验产物归属与历史核验

新实验在创建事务中固定整个 `projects/{project_id}/runs/{run_id}/` 的内部存储归属，覆盖普通创建、自动训练和参数搜索派生实验。各次执行仅使用其下 `attempts/{attempt_id}/`；续训复用实验归属，不重新采用当前桶。产物上传、完成核验、报告与日志读取、脱敏副本、下载及续训检查点均核对来源实验和精确执行范围。输入数据包独立解析数据版本归属，初始化权重独立解析来源模型的实验归属，三者可以属于不同桶。

模型通过同项目来源 `run_id`、`attempt_id` 外键和已发布权重清单解析该归属；注册、下载、安装、评估及项目权重初始化均沿来源读取，不采用目标实验或当前 training 桶。内部物理归属不写入冻结配置、初始化 pin、模型产物或来源摘要，也不改变执行器与 ML 服务协议。

历史归属为空时，列表、实验详情及已有元数据仍可读取；产物读取、签名 URL、上传、派发、续训和模型注册拒绝，并返回归属未固定的明确原因。停止、失败回报和确认进程退出后的资源收尾仍保留，不能为绕过归属检查而提前释放活跃租约。显式核验入口为：

```http
POST /api/v1/projects/{project_id}/training-runs/{run_id}/storage-pin
```

该操作无请求体，只允许实验及其全部执行均为 `succeeded`、`failed`、`cancelled`，且所有执行都不再持有资源租约；`lost` 不满足条件。调用者须仍有当前项目管理权限，API Key 需 `annotations:read`、`datasets:read`、`datasets:write`。候选仅取已持久登记的 training 绑定和平台位置，不自动登记或接受任意桶。成功返回 `200` 与实验摘要；已有可解析归属时幂等返回并保留原核验证据。

核验读取整个实验前缀，使用数据库中独立保存的上传清单与发布记录核对 SHA-256、字节数和执行 metadata。所有已发布产物必须存在；成功执行的清单须完整，失败或取消执行可缺少未上传项，但已有对象都须有独立凭据。只有对象自报摘要不足以接管。脱敏日志副本须由已核验原日志按当前脱敏规则重建并匹配内容与元信息；未知规则、未知额外对象、缺失或冲突均拒绝。过程只读保留副本，不反序列化权重，不修复或删除对象。

每次最多核验 10000 次执行、100000 个对象、实际读取 64 GiB，协作截止 600 秒；与数据版本核验共用每个 API 进程的一个名额，忙时返回 `429`。条件读取后复核完整清单与对象观测；等待实际收尾后，最终再次比较当前权限与 API Key、实验配置与全部执行清单、候选绑定和位置维护代次，一致才原子固定归属。任何变化、预算不足或关闭不确定都不保存归属；未确认收尾时保留责任与占位。

固定的是当前保留副本的核验事实，不证明历史位置、独占所有权或未来不可变。新删除意图复制实验归属；旧空归属的实验清理意图拒绝物理删除，保持待核对。该能力不开放整个 training 用途切换；辅助输出使用下文的独立归属。

### 执行观测、日志与恢复

以下读取沿用项目管理权限及 `annotations:read`、`datasets:read`，基路径仍为项目的 `training-runs`：

| 方法 | 路径后缀                                    | 行为                                                            |
| ---- | ------------------------------------------- | --------------------------------------------------------------- |
| GET  | `/{run_id}/attempts/{attempt_id}/telemetry` | 近期资源与阶段采样；可指定 `after`，`limit` 1–120，默认 120     |
| GET  | `/{run_id}/attempts/{attempt_id}/logs`      | 近期脱敏日志；`after` 与 `before` 互斥，`limit` 1–120，默认 120 |
| GET  | `/{run_id}/recovery`                        | 当前执行续训资格、原因、检查点轮次与已知环境／在线状态          |

两种观测分别返回 `attempt_id`、持久 `stream_id`、`earliest_cursor`、`latest_cursor`、`next_cursor`、`has_more`、`gaps`、`summary` 和按序 `items`。省略游标读取近期尾页；日志 `before` 读取更早内容。向前读取依据 `next_cursor`／`has_more`，不能用条数判断结束；明确缺口也能推进空页游标。更早日志则取最小条目序号作为下一 `before`，不能借用向前 `has_more`。缺口 `(after, through]` 标注 `retention` 或 `producer_retention`。序号高水位在清理后仍保留，生产 boot 改变不重置持久流。

采样包含采集／接收时间、原生阶段、阶段耗时、回调年龄、固定来源／单位／归属定义和进度。数值 `0` 有效，`null` 需带不可用原因；不接受布尔、负值或非有限数。整卡 GPU 利用率与 MiB 来自指定 UUID 的设备查询；CPU 核数、RSS 来自已确认的训练进程组，分配器显存来自子进程 PyTorch。无法确认 PID 对应关系时不提供进程 GPU 利用率。LibreYOLO 回调未提供实际批次计数时，吞吐保持不可用。

每次执行保留最多 720 条采样、512 个最多 2048 UTF-8 字节的日志块，7 天前的原始观测由后台每小时分批清理，摘要保留。ETA 至少使用 3 个同增强阶段的可比较完整周期，排除首个周期，返回范围与样本数；不包含评估／上传，不跨未观测增强阶段估算。终态、失联或过期采样不能表示仍有效的剩余时间。

内部 `POST /api/v1/internal/training/attempts/{attempt_id}/observations` 使用原执行器 fence；`producer_boot_id` 单独表示实际生产者。请求含持久 `stream_id`、UUID `request_id`、独立有序 `telemetry`／`logs`、可选显式 `skipped` 和有界摘要；每批最多 12 条采样、32 块日志、128 KiB 编码 JSON。ACK 返回同流／同请求身份与两个已接受游标。已接收内容不可改写，退役游标与身份冲突返回具名 `409`；账户／项目锁忙属于可重试冲突。公开最新值与峰值由已接受采样派生。这个接口不更新状态、心跳、租约时间或资源所有权。

执行尝试可携带 `failure`：明确的 `code`、`stage`、`source`、配置摘要、脱敏诊断与可选异常类型。原生 CUDA OOM 依据异常类型，信号终止不会被猜测为 OOM。`finish.failure` 可省略，旧请求仍保留原有空字段的幂等摘要；新执行器发送前持久化完整回执。产物可带可选 `telemetry` 角色，最多 256 KiB，身份绑定实验、执行与配置；缺少可选观测不阻止成功训练。

下载 `role=log` 时核验原对象的大小、实际 SHA 和执行元数据，再在同一执行前缀生成内容寻址的脱敏副本。响应 `sha256`／`size_bytes` 表示副本，另返回 `source_sha256`／`redaction_revision`；原对象保持不变。原始日志超过 8 MiB 或校验失败时拒绝该有界下载。共享实验锁使脱敏读取／写入与删除串行。

`recovery.eligible` 仅针对当前已停止的失败／取消执行，要求原租约已释放、检查点轮次为正且未完成总轮数、对象归属／元数据可用、冻结输入及已知环境兼容、当前权限有效。`executor_online=false` 可与合格资格同时存在，表示可排队等待。POST 续训再次核验，下载检查点时核验完整字节；更换数据或参数仍需新建实验。

## 参数搜索 API

基路径 `/api/v1/projects/{project_id}/training-studies`，要求项目管理权限及 `annotations:read`、`datasets:read`；预览、创建和停止另需 `datasets:write`。

| 方法 | 路径后缀             | 行为                                            |
| ---- | -------------------- | ----------------------------------------------- |
| POST | `:preview`           | 返回完整 `plan` 与 `preview_digest`，不创建记录 |
| POST | 空                   | 核对预览摘要，创建搜索和全部试验，返回 202      |
| GET  | 空                   | `limit=20`、`offset=0` 分页，最多 100 项        |
| GET  | `/{study_id}`        | 冻结计划、当前账本、各试验及兼容结果分组        |
| POST | `/{study_id}/cancel` | 停止后续准入并按普通取消协议停止活动子实验      |

请求包含 `base`（完整 `TrainingRunCreate`，必须使用 `configuration`）、`grid`、`objective`、`direction="maximize"`、`budgets`，创建另带预览返回的 `preview_digest`。`grid` 最多 10 个可编辑原生键，每键 1–20 个不重复的显式值；全部笛卡尔组合逐项校验，不接受别名或条件不生效字段。`budgets.max_trials` 为 1–20，`max_planned_epochs` 为 1–20,000，`max_native_seconds` 为大于零且不超过 604,800 的有限秒数。超过任何计划上限返回 422，摘要变化返回 409。

目标为 `metrics/mAP50-95`、`metrics/mAP50`，Ultralytics 另支持 `metrics/precision`、`metrics/recall`，LibreYOLO 另支持 `metrics/mAP75`、`metrics/AR100`。所有目标均最大化最终验证报告中的分数。选择过程使用冻结验证集，不使用测试集。相同项目／`base.request_id` 及内容重试返回原搜索，复用标识修改内容返回 409。

`accounting` 分别返回 `consumed_seconds`、`reserved_seconds`、`remaining_seconds`、`overrun_seconds` 和 `unknown_attempt_ids`。活动子实验先预留当前余额，只有带有效所有权凭据的终态计时回执才释放未用部分；未知时间阻止新试验与续训。`trials.actual_epochs` 是当前尝试的最新完成绝对轮次，可空，不能当作跨续训累计工作量。`queue_seconds` 与 `lease_seconds` 累计各次尝试的排队、领取至终态结算时间。`rankings` 只含最终报告校验通过、指标有限且可用的组内试验 ID 顺序；报告不可用原因保留在试验项中。

内部 attempt assignment 增加可选 `native_budget`，绑定策略摘要、reservation、attempt、配置摘要及额度；此类 finish 必须携带对应 `native_accounting`。回执区分 `exact`、`not_started`、`unknown`，成功必须精确，重试沿用同一冻结 finish body。实际进程组时间和停止超额完整计入，旧执行器缺少预算能力时不能领取。普通实验省略新增字段时保留旧协议摘要。

当前无搜索删除 API。搜索、子实验与冻结输入长期保留；删除受引用项目、数据或子实验返回 409，且不会先删除对象存储。

## 原生增强预览 API

基路径 `/api/v1/projects/{project_id}/training-previews`。读取要求当前项目负责人或超级管理员及 `datasets:read`、`annotations:read`；创建、取消和删除另需 `datasets:write`。

| 方法   | 路径后缀                       | 行为                                                           |
| ------ | ------------------------------ | -------------------------------------------------------------- |
| GET    | `/capabilities`                | 当前 CPU 协议、镜像、在线／忙闲状态与固定资源策略              |
| POST   | 空                             | 创建冻结预览，返回 `202`；相同主体、请求 UUID 和内容可精确重试 |
| GET    | `/{preview_id}`                | 当前状态与经字节验证的完整报告                                 |
| POST   | `/{preview_id}/storage-pin`    | 显式核验并固定历史终态预览的当前保留副本                       |
| POST   | `/{preview_id}/cancel`         | 排队立即取消；运行中等待进程退出确认                           |
| DELETE | `/{preview_id}`                | 立即过期并隐藏，保留上传窗口关闭前的清理墓碑                   |
| GET    | `/{preview_id}/images/{index}` | 读取 0–3 号经完整校验的 PNG；认证私有流、禁止缓存              |

创建包含 `dataset_id`、`framework`、`base_model`、完整 `configuration`、UUID `request_id`、整数 `seed`（0–2147483647）、一基 `epoch`（不超过训练轮数）和 `count`（1–4）。只接受通过资格验证的 Ultralytics YOLO11 图片检测，不接受任意代码、路径、存储键或运行资源参数。每项目最多三个活跃预览。

状态为 `queued`、`running`、`cancelling`、`lost`、`succeeded`、`failed`、`cancelled`、`expired`。`lost` 不等于进程已退出。报告绑定预览、完整配置／镜像／参数定义、冻结数据和来源池、策略、种子及轮次；包含完整归一化框、输出 SHA、实际来源取用／读取记录及累计原生裁剪／过滤操作。原始来源继续通过冻结样本媒体接口授权读取。

每次报告读取核验实际 JSON 大小、SHA 和预览／输入元数据，图片另核验完整字节、PNG 格式及尺寸。纯解析缓存不代替存储验证或授权。跨项目或不存在返回 `404`，未完成／能力不匹配返回 `409`，过期图片返回 `410`，损坏或不符证据返回 `422`。结果 24 小时过期；清理等待最后一次 60 秒 PUT 授权关闭，期间保留数据引用。

执行器通过原有 `X-Training-Executor-Token` 调用 `/api/v1/internal/training/previews/claim`、`/{preview_id}/heartbeat`、`uploads` 和 `finish`。claim 的 `purpose` 固定为 `augmentation-preview`，资源为节点的 `augmentation-preview/cpu-0`；协议与策略摘要及镜像必须匹配。回报以原 claim 的执行器、boot 和 CPU 租约凭据 fencing；`finish.process_exited` 必须为 true。失联、取消和过期不直接释放未知进程的租约，撤权后允许有效 fence 确认停止，但禁止发布成功。

### 实际训练成员回执

新 Run 在版本 manifest 含完整 `samples` 时，将 `input_contract` 固定到 `config` 及其摘要：`schema: 1`、`policy_digest`、`members_digest` 与 `counts.train/val`。成员摘要包含任务、划分、图片／标签相对路径及 SHA、尺寸与大小；相同图片内容的不同任务保留为不同成员。明确存在但无效的清单返回 `422`；历史缺少清单的版本和已有 Run 保留原合同。

训练 Executor 的 runtime 必须声明匹配的 `input_receipt_policy_digest`，否则不能领取新合同，即使镜像摘要相同也不例外。同 claim 重放同样核验能力。续训固定原 Run 合同，为新 Attempt 重新观察加载器；旧 Attempt 回执不能用于新执行。CPU 增强预览使用独立合同，不要求训练成员回执能力。

成功执行的现有 `manifest.json`（role 为 `manifest`）包含 `input_membership`，绑定 `run_id/attempt_id/config_digest/dataset_id/dataset_sha256`，并包含合同策略、集合摘要、数量和按 train／val 分组的 `{task_id, member_digest}`。服务端在成功终态与释放租约前，从保存的产物位置完整核验 manifest 的字节、摘要、元数据、配置与集合；缺失或不符返回 `422`。失败、取消无需完整回执；完全相同的终态重放不重新读取存储。

成员来自实际原生加载器，允许重排，拒绝遗漏、额外、重复、跨划分与根外路径。该清单说明加载器接纳的基础成员，不表示逐 epoch 抽样轨迹。通过现有执行产物下载接口读取，不新增接口或产物数量。

### 辅助产物归属与历史核验

新预览、逐样本评估和主动学习轮次分别在创建事务固定自己的输出根前缀；所有尝试及重试复用原归属。它们与输入训练版本、来源模型及原始候选各自独立。报告读取（包括解析缓存命中）、预览 PNG、主动学习 PNG 复用或重建及候选报告清理，都先检查保存端点、桶和准确的文件／attempt／ordinal。未知归属在物理 I/O 前返回 `409`；主动学习不会先读原图补造副本。归属不进入公开响应、冻结 spec 或执行器请求。

历史归属为空时仍可读取元数据、取消并按准确 fence 确认失败或退出；成功预览的普通详情返回 `report=null`，显式图片和报告读取仍拒绝。主动学习的 DB 报告和人工决定不依赖图片归属。新执行、上传、成功发布、评估报告读取、图片读取及物理删除须先固定归属，普通 GET 和重试不自动接管。

以下操作无请求体，成功返回 `200` 与现有资源摘要，要求当前项目管理权及 `annotations:read`、`datasets:read`、`datasets:write`：

```text
POST /api/v1/projects/{project_id}/training-previews/{preview_id}/storage-pin
POST /api/v1/projects/{project_id}/sample-evaluations/{run_id}/storage-pin
POST /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/storage-pin
```

预览须为成功、失败、取消或过期，且没有任何 CPU 租约引用，最后签名 PUT 已到期。评估／轮次及当前尝试须终态；历史尝试只接受终态，或非当前、更旧 generation、claim 已清空且 completed_at 已保存的封存 `lost`。正常评估终态残留 claim 不算活动任务。这是 DB 发布围栏的证明，不证明旧线程或远程进程物理关闭，部署方仍须确认停写窗口。

候选只取已持久登记的 training 绑定。全前缀中的每个实际对象须由独立上传／发布回执或冻结派生 SHA 解释，核验完整条件读取、SHA、独立大小（存在时）、metadata、PNG 尺寸及语义。预览名称限于该次冻结 count；已发布对象缺失、未知额外对象或读中变化均拒绝。失败／取消的未发布对象可缺失，存在则核验；主动学习已保存 media、请求、证据或成功候选要求 PNG 存在，其余冻结成员的合法残留 PNG 也可核验。旧评估报告凭独立发布回执及冻结身份核对；当前报告另核对当前 Sample 证据，不把重试后共享行当作旧尝试的证据。

沿用共享的每进程一个核验名额、100000 对象、64 GiB 实际读取和 600 秒协作截止；评估／选样最多 10000 次尝试。预览自身上限为 4 张各 24 MiB PNG 和 8 MiB 报告。取消等待真实收尾，关闭不确定则保留占位与耐久访问记录；发布前再次比较权限／Key、全部记录、绑定和维护代次。已有可解析 pin 幂等返回原事实，不再次读取对象。

预览原行保留至 CPU 与 PUT 窗口结束、保存前缀删除成功；评估的 `deletion_requested_at` 原行保留至保存前缀清理成功，并继续检查主动学习引用。未知归属不执行物理删除，单项失败保留待重试。主动学习没有新增删除入口。证据只说明当前保留副本，不代表历史位置、独占权限、真实框架执行资格或整个用途可切换。

## 项目训练配方与克隆

基路径 `/api/v1/projects/{project_id}/training-recipes`。所有操作要求当前项目负责人或超级管理员及 `annotations:read`、`datasets:read`；写入和纯配置解析另需 `datasets:write`。

| 方法 | 路径后缀                                        | 行为                                                                   |
| ---- | ----------------------------------------------- | ---------------------------------------------------------------------- |
| GET  | 空                                              | `limit`、`offset` 分页，默认排除归档，`include_archived=true` 包含归档 |
| POST | 空                                              | 创建配方及首个不可变修订，返回 `201`                                   |
| GET  | `/{recipe_id}?revision_id=UUID`                 | 读取具体修订，省略修订时读取当前 head                                  |
| GET  | `/{recipe_id}/revisions`                        | 分页修订历史                                                           |
| POST | `/{recipe_id}/revisions`                        | 带 `expected_head_revision` 创建新修订                                 |
| POST | `/{recipe_id}/archive?expected_head_revision=N` | 归档配方，保留历史与现有引用                                           |
| POST | `:resolve`                                      | 不选择数据，解析当前完整配置，用于生效值差异预览                       |
| GET  | `/clone/{run_id}?attempt_id=UUID`               | 返回克隆候选，校验实验与尝试属于当前项目                               |

创建与修订请求包含 UUID `request_id`、`name`、可选 `description`，以及唯一来源：`configuration` 或 `source_run_id`。完整配置来源的结构为 `{framework, base_model, configuration: {schema_id, schema_digest, values}}`；实验来源可带 `source_attempt_id`，只允许已验证成功且具有完整配置的实验。修订保存完整解析结果、原始基础权重 ID/SHA/文件名、镜像与定义身份。响应的 `compatible`、`incompatibility` 和 `source_available` 分别说明当前是否可直接采用、具体不兼容原因及原实验是否还存在。来源实验删除不会删除保存的配置。

新配方的预期 head 为零；新修订必须匹配当前 head，冲突返回 `409`。同一请求及内容重试返回原修订，来源后来删除或归档也不破坏已接受请求的回放。归档禁止新的采用与修订，既有实验、策略和历史快照继续保留。定义、默认值、镜像或原始基础权重不匹配时，直接采用返回 `409`，不会静默解释旧值。

克隆候选包含受控配置快照、摘要与 `origin`，不复制数据选择、请求 ID、自动触发、检查点、运行状态、租约或下载 URL。新草稿仍需明确选择数据并预检。旧六参数实验缺少完整默认值，克隆与直接另存返回明确的迁移说明。

训练预检与创建可带 `origin`：配方为 `{kind: "recipe", recipe_id, revision_id, digest}`，克隆为 `{kind: "clone", run_id, attempt_id?, digest}`；必须同时提交完整 `configuration`。`digest` 分别绑定修订快照摘要或来源实验配置摘要。服务端校验原始基础权重及定义，允许手动实验修改参数并记录 `modified`。来源独立保存在实验 `origin`，不加入执行配置或改变旧配置摘要；已创建请求的幂等回放优先于来源重新校验。

自动策略也可带具体配方 `origin`，但提交的完整配置必须与该修订一致。策略固定展开配置与来源，新 head 不会自动更新策略。保存时仅保留同一已采用修订可绕过归档的新采用限制；其他修订必须显式采用。已有完整配置策略不能省略 `configuration` 后静默降为六参数配置。

## 项目模型权重初始化

`GET /projects/{project_id}/training-runs/initialization-sources/{model_id}` 使用当前项目管理权限与读取 scopes，接受 `framework`、`base_model` 和可选 `dataset_id`。不存在或跨项目的模型返回 `404`；当前不可用的同项目来源返回 `eligible=false`、有界 `reasons`。`pin` 保存服务端解析的固定身份，`storage_verified=false` 明确表示这里只做对象元信息检查。

训练预检／创建、配置解析和策略保存可带 `initialization`：

```json
{ "kind": "trained_model", "model_version_id": "<UUID>", "source_digest": "<SHA256>" }
```

省略或 `{kind: "catalog"}` 沿用目录权重，保持旧请求与配置摘要形状。项目模型初始化要求完整 `configuration`，只支持已保留的同项目成功 Ultralytics 产物、相同框架版本／家族／规格／任务与完全一致的原生类别顺序。历史策略保持原范围，新策略由目标模型的精确版本选择；不默认接受旧版本来源跨版本初始化。当前资格限定锁定的 Ultralytics、Torch、TorchVision、Python 与 CUDA 组合；归档和部署／发布状态不决定训练资格。接口不接受 URL、路径、任意检查点或类别重映射。

`config.initialization` 固定项目／模型／实验／尝试／数据身份，权重 key、SHA、大小，类别与绑定摘要，来源配置／数据／验证摘要，运行时与评估证据，以及资格策略摘要。权重不得超过 2 GiB。预检、创建和首次领取分别完整核验权重及评估对象；来源丢失或摘要变化明确失败，不回退目录权重。旧执行器未声明匹配初始化能力时不能领取。

权重和评估对象的位置由同项目来源实验与精确执行解析，独立于新实验的输出位置和输入数据版本；旧来源实验归属为空时须先显式核验，不能用目标桶补齐。初始化 pin 及其摘要保持原合同。

配方修订与自动策略保存完整 pin；克隆保留原初始化来源，不自动使用被克隆实验的产物。`origin` 仍单独记录配置来源；解除它不清除初始化。已接受 UUID 的幂等回放先于来源重新校验。三个接受记录的 `initialization_model_id` 与 pin 一致并保留模型引用。

初次执行下载独立权重文件，从第 0 轮和新训练状态开始。`native-config.json` 的 `evidence.initialization` 记录来源 SHA／摘要、所有状态张量指纹与数量，以及优化器、调度器、scaler、EMA 和最佳成绩重置证据；服务端成功提交再次验证。后续恢复优先使用本实验 checkpoint，回执为 `checkpoint_resume` 并保留原 pin。模型谱系的 `weights_initialization`、`initialized_runs`、`initialization_recipes` 分别表达直接来源及反向记录，读取不访问权重。

## 模型注册、部署与发布

基路径 `/api/v1/projects/{project_id}`。这些接口要求项目负责人或超级管理员及 `annotations:read`、`datasets:read`；变更另需 `datasets:write`。

| 方法 | 路径后缀                                | 行为                                                            |
| ---- | --------------------------------------- | --------------------------------------------------------------- |
| POST | `/trained-models`                       | 注册同项目成功执行的权重，返回 `201`                            |
| GET  | `/trained-models`                       | 按 `limit`、`offset` 分页，支持名称、框架、日期及归档筛选       |
| GET  | `/trained-models/{version_id}`          | 固定模型信息、部署状态与短期试用图片 URL                        |
| POST | `/trained-models/{version_id}/deploy`   | 指定 `registry_id`，校验、安装并试用；返回 ready 或 failed 部署 |
| GET  | `/trained-models/{version_id}/download` | 重新鉴权后签发权重下载 URL、大小与摘要                          |
| GET  | `/model-releases`                       | 当前选择、generation 及分页历史                                 |
| POST | `/model-releases`                       | 按预期 generation 发布、回退或清除选择，返回 `201`              |

模型列表的 `q`（最多 120 字符）按名称进行不区分大小写的字面子串搜索，`%`、`_` 和反斜线不会作为通配符。`framework` 可选 `ultralytics` 或 `libreyolo`；`created_from`、`created_to` 接受 `YYYY-MM-DD`，按 UTC 日期包含起止整天，反向范围返回 `422`。所有筛选与 `archive` 共同作用于总数和分页，按创建时间、ID 倒序稳定排序。

注册包含 UUID `request_id`、`name`、`run_id`、`attempt_id` 和与原生类别顺序一致的完整 `bindings`。相同标识和请求返回原模型，内容冲突返回 `409`。同项目同一次执行只能注册一个模型，使用新请求标识重复注册也返回 `409`；归档不解除限制，另一条成功执行可以独立注册。支持本平台已验证的 Ultralytics 图片检测产物及独立通过原生 CPU 资格检查的 LibreYOLO YOLO9 检测产物。

模型下载与安装通过保留的同项目来源实验及执行外键解析固定存储归属，并核对权重仍属于该执行的已发布清单；旧来源实验须先完成 [storage-pin](#实验产物归属与历史核验)。模型本身不复制物理归属，原产物、来源及内容摘要保持不变；试用图片和逐样本评估的输入媒体仍解析独立数据版本归属。

实验详情 `GET /training-runs/{run_id}` 的顶层 `registered_models` 返回该实验已回流模型的 `id`、`attempt_id` 和 `name`，包括已归档模型；没有回流时为空数组。客户端按所选执行匹配回流状态，服务端在注册事务中再次检查。模型引用的实验继续拒绝删除。

发布包含 UUID `request_id`、`expected_generation`、`model_version_id`、`deployment_id` 和 `sample_confirmed=true`。回退增加 `rollback_from_id`，且模型与部署必须匹配该历史记录。清除时模型和部署均为 null。并发选择变动返回 `409`，应刷新。即使使用新的 `request_id`，目标模型版本与部署均未变化时也返回 `409`，不会追加历史或增加 generation；已停用时再次清除同样拒绝。切换到同一模型的其他已就绪部署仍是有效变更。丢失响应后的原请求重试返回同一发布。发布不会自动发生。

既有 `/preannotate` 接受 `model_id="trained:<uuid>"` 或 `"project-release"`。服务端解析并冻结权重、类别绑定、部署与发布来源，客户不能提供冻结引用或权重 URL。`Idempotency-Key` 重放沿用原任务冻结身份。受控模型只接受单阶段整图图片检测，使用现有任务作用域和写入检查；不支持的范围在入队前拒绝，worker 再次校验。结果校验实际模型身份、任务身份及原生类别索引，来源保存在 Prediction 元数据中，失败重试继续使用原身份。

YOLO 的 `POST /managed-models` 和 `GET /managed-models/{version_id}` 使用 `Bearer` 凭据，仅供平台部署。平台自行生成短期内部对象 URL；后端以大小、SHA-256、原生类别及框架版本校验后原子保存。有效版本不能被其他身份覆盖，损坏副本可按固定摘要重建；缺少版本直接失败，不降级到内置模型。

LibreYOLO 注册仅接受已核验的 YOLO9 t/s/m/c 原生检测最佳权重，版本与 CPU 运行时资格独立固定。来源执行必须成功，且 `weights`、`metrics` 各有唯一已验证产物。安装收据追加 `model_family`、`model_size`、`artifact_format`、`artifact_size_bytes` 与 `runtime_qualification`；试用、发布和预测继续绑定确切实例。旧 Ultralytics 收据与 `frozen_model_ref` 的三字段 wire 不变。原生报告中的 precision／recall 别名不作为常规指标保存；负哨兵为 null，逐类别缺口保留。逐样本评估与主动学习创建仍拒绝 LibreYOLO，共享 evaluator 需要独立资格。

## 自动训练策略

接口前缀 `/api/v1/projects/{project_id}/learning-policy`，只允许项目负责人／超级管理员，读取要求 `annotations:read` 与 `datasets:read`，变更另需 `datasets:write`。

| 方法与路径                          | 行为                                                            |
| ----------------------------------- | --------------------------------------------------------------- |
| `GET`                               | 返回当前策略；未配置时为 `null`                                 |
| `PUT`                               | 用 `expected_revision` 保存显式配置；首建为 0，版本冲突返回 409 |
| `POST /disable?expected_revision=N` | 独立关闭入口，训练目录不可用时也可关闭                          |
| `POST /check`                       | 立即检查或对账，复用周期任务的协调逻辑                          |
| `GET /triggers?limit=20&offset=0`   | 分页读取触发历史及关联数据／实验                                |
| `DELETE`                            | 仅移除已关闭且没有活跃触发或实验引用的策略与审计记录            |

保存请求明确提供父数据版本、训练器、基础模型、训练参数、变化门槛、检查间隔和冷却时间；可指定当前项目批次。平台固定实际目录的版本、镜像、基础摘要和物理资源，客户端不能提交执行脚本或产物地址。只纳入已审核数据；首次没有成功基线时统计全部合格训练内容。关闭、撤权和目录变化阻止后续新实验；成功不会自动注册或发布模型。

保存时使用与调度相同的任务适配器核验父版本，必须为已就绪且已通过资格检查的图片检测数据。历史视频版本或媒体类型未知的版本直接拒绝，不先保存为等待策略再由调度暂停。

扩展执行器在 runtime 中声明 `executor_protocol: 2` 和精确的 `supported_configs` 身份列表。缺少握手的旧执行器只能领取原六参数实验；定义摘要或适配器修订不匹配不能领取扩展实验。全局目录的 `parameter_schemas[].compatible` 使用相同能力判定，离线为 `null`。旧租约的心跳、结束和恢复不依赖当前创建目录。

自动策略可显式保存 `configuration`，固定同一参数定义与有效值，后续子实验按该快照解析。旧策略不自动采用扩展默认值。镜像、基础权重或参数定义发生变化时仍暂停策略，要求负责人检查并重新保存。

## 模型谱系与候选决策

以下路径以 `/api/v1/projects/{project_id}` 为前缀，仅项目管理者可读；API Key 同时需要 `annotations:read` 和 `datasets:read`，追加还需 `datasets:write`。

| 方法 | 路径                                     | 说明                                               |
| ---- | ---------------------------------------- | -------------------------------------------------- |
| GET  | `/model-lineage/{kind}/{node_id}`        | 单跳节点、直接来源／gap、可选关联列表              |
| GET  | `/trained-models/{version_id}/decisions` | 决策序号倒序，`limit=1..100`，`before` 游标        |
| POST | `/trained-models/{version_id}/decisions` | 追加 `request_id`、`disposition`、`note`，返回 201 |

节点类型为 `dataset/run/attempt/model/deployment/release/recipe/recipe_revision/evaluation`。不带 `relation` 时不展开后续记录；可用类型见响应 `sections`。关系页使用 `limit=1..100` 与 `cursor`，后续游标绑定当前项目、根节点和关系，不可跨关系复用。根节点不存在或属于其他项目返回 404；己方来源缺失、无效或归属不一致则只返回该边的 gap。查询不检查执行器或生成下载链接。

`model` 的 `predictions` 关系返回 `usage`，匹配候选权重 SHA、类别及绑定身份，同时核对预测复合主键与项目。`retained_count` 最多 1,000，`truncated=true` 表示还有记录；缺少候选身份时计数为 null 并返回 `unavailable_reason`。此范围不含旧格式和删除历史。

决策处置为 `active/research/declined/archived`；说明去除首尾空白后长度 1–2,000。相同模型内相同 UUID 与内容重试返回原记录；变更内容复用 UUID 返回 409。历史只追加，最新序号决定列表处置，不更新项目发布选择或代次。模型列表支持 `archive=exclude/include/only`，默认 `exclude` 仍保留当前发布；模型输出增加处置、决策序号、归档及当前发布标记。历史模型仍可按 ID 读取。

### 原生实例掩码与任务隔离

创建或预检实例分割数据时，显式提交 `task=segment`、`bridge=ultralytics-instance-rle-1`；省略 bridge 保留原多边形默认行为。新掩码契约为 schema 4、format `aap-instance-rle`、geometry `instance-mask`，完整身份和类别顺序进入摘要，禁止跨桥接父版本。

`GET /projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}/masks/{annotation_id}` 仅向当前项目负责人返回该冻结成员的 canonical RLE、来源 annotation 版本及像素摘要；不读取当前人工标注。

模型发布 POST body 和发布历史 GET query 的 `task` 默认 `detect`；分割显式传 `segment`。检测动态引用仍为 `project-release`，分割使用 `project-release:segment`，请求入队时解析并冻结。模型目录同时返回 `current_releases`，旧 `current_release` 保留检测默认语义。试用实例可通过 `GET /projects/{project_id}/trained-models/{version_id}/deployments/{deployment_id}/mask-content/{shape_index}` 读取；版本、部署、项目关系与当前管理权限全部核验。

自动策略 PUT body 的 `task` 与 GET／disable／check／triggers／DELETE query 同样默认检测。分割必须提供完整配置、同任务父版本及已合格基础模型，任务间修订和失败基线互不影响；项目级串行限制继续生效。

### 受控模型的训练和推理身份

`TrainedModelOut.framework_version` 与部署 `receipt.framework_version` 表示原始训练版本，保持不可变。新推理服务的安装／查询响应增加独立 `inference_runtime`，包含 `revision`、`framework`、`training_version`、`version`（实际推理版本）、`task`、`compatibility` 和 `qualification`（原生库版本及源码校验值）；部署 `trial` 同时保存该身份。

新冻结的 `model_ref` 和 backend 的 `frozen_model_ref` 携带这份 `inference_runtime`，参与引用及逐样本请求摘要。预测响应 `meta.trained_model` 仍为版本 ID、权重 SHA 和类别摘要三项，`meta.inference_runtime` 单独记录并与请求核对，预标来源保留两种版本。旧引用没有该字段时维持原摘要，只允许按原训练版本推理，不会自动改用新版环境。

共享样本评估接受已登记的 profile 修订 1 和 2，并核对各自运行时版本。未知修订返回 `502 evaluation_protocol_unqualified`；部署已记录的推理版本与实际 profile 不同返回 `409 deployment_runtime_changed`，需重新加载试用。重新提交就绪部署时，若实际推理身份变化会重新试用，成功后才更新部署身份。

数据集详情的冻结入口仍使用本接口。选择合同新增可选 `dataset_id`，必须已关联当前项目，服务端在同一快照查询中限定 DatasetItem；与 batch_id/task_ids 同时使用时取交集，显式选中范围外任务返回404。`GET /tasks` 同样支持 project_id 与 dataset_id 组合分页，避免浏览器为确认范围拉取全部任务。未提供 dataset_id 的旧快照合同保持原样；进入页面只预选范围，创建仍须通过既有预检摘要确认。
