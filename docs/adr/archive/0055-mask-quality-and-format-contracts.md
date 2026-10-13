# 0055 — Mask 质量闭环与格式适配采用版本化账本和显式损失合同

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** core team
- **Supersedes:** —（扩展 [ADR-0052](./0052-shared-raster-mask-and-image-geometry.md) 的共享 RLE 真值、[ADR-0053](./0053-native-mask-ai-candidate-lifecycle-and-video-correction.md) 的候选 / 局部决定语义和 [ADR-0054](./0054-raster-mask-large-canvas-memory-and-tiles.md) 的大画布资源边界，不改变既有 geometry）

## Context

图片和视频工作台已经把 instance Mask 收敛到内容寻址 COCO RLE，并具备 annotation version、
`MaskMutationService` 原子变更、Tracker staged candidate、人工反馈、异步 job 和多种导出。但这些能力还不能
形成可运营的质量与交换闭环：当前 annotation 只保留最新 geometry，重复扫描缺少稳定 issue 身份，批量修复
没有可验证的 dry-run / rollback 账本，Tracker 的已审区域不能跨 job 保护；格式分支也分散在导出打包、导入
服务、API 和前端常量中，尚无统一 preflight、损失报告或真实 consumer 通过门。

外部视频格式还带来一个不能靠字段重命名解决的语义冲突：YouTube-VOS 风格包只在部分帧提供标注，未标注帧
表示“未知 / 未采样”；现有 `video_track_mask` 只能表达 exact keyframe、nearest hold、outside 和 occluded。
把未知帧静默解释为 hold 或 outside 都会改变真值。为一个交换格式增加第三种插值状态，又会把格式侧不确定性
泄漏到工作台、Tracker、QC 和所有既有 resolver。

候选方案：

| 选项                                                                        | 主要卖点                                                                                      | 主要劣势                                                                                  |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **A. 版本化质量账本 + adapter / preflight + 显式有损 sparse gap（本 ADR）** | 当前真值不变；问题、修复和审阅都可追溯；格式损失在执行前可见；后续 adapter 共享安全与并发合同 | 增加 revision / run / issue / repair / review-scope 领域表和 consumer fixtures            |
| B. 把 QC 结果与历史 geometry 全塞进 Annotation JSONB                        | 表少、读取看似直接                                                                            | 热行持续膨胀；历史和当前写入争用；无法独立保留、分页、去重和 GC                           |
| C. 只复用 `annotation_feedbacks` 保存自动 QC                                | 不新增 issue 表                                                                               | 人工讨论与可重复计算结果生命周期不同；缺 run/config/snapshot 证据，stale 和 dedupe 不可靠 |
| D. 为 sparse gap 新增 geometry 状态                                         | 可无损表达 YouTube-VOS 未知帧                                                                 | 修改所有视频 resolver、渲染、Tracker、QC、AAP 和导出合同，收益只服务单一交换格式          |
| E. 每个格式继续在打包函数内加分支                                           | 局部实现快                                                                                    | 能力声明、预检、安全限制、缓存版本与 UI 持续漂移，无法证明无损                            |

## Decision

### D1. 历史 Mask 使用轻量 revision ledger，不复制像素正文

- 新增 `mask_annotation_revisions`，唯一键为 `(annotation_id, annotation_version)`；记录 project / task、
  geometry、geometry digest、source kind、可选 operation / actor、创建与到期时间。
- Raster Mask 从 version N 写到 N+1 前，必须在同一事务用 `INSERT ... ON CONFLICT DO NOTHING` 保存 N；
  删除或 deactivate 前保存最后一个活跃版本。创建 version 1 时不再复制一份 current。
- revision 只保存 immutable `coco_rle_ref`，不复制 RLE bytes。未过期 revision 和 repair rollback ref 都是
  Raster Mask GC live root。
- 默认每个 annotation 保留最近 20 个版本且至少 30 天；只要更新版本仍存在，其直接 predecessor 必须保留。
  基线已经过期时 compare 返回 `baseline_expired`，不得退化为当前版本、bbox 或 preview。
- revision 是历史证据，不是第二个可写 Annotation。恢复必须走新的 mutation / rollback 并产生新 version。

### D2. QC run 与 issue 分离，issue 身份绑定真值版本

- `mask_qc_runs` 是领域执行记录，与一个 `async_jobs(kind=mask_qc)` 一一对应；冻结 scope、QC config
  revision / digest、source snapshot digest、状态、进度和 summary。大 RLE 不写进 job payload。
- `mask_qc_issues` 保存 annotation/version、稳定 code、severity、状态、指标 / 阈值、帧范围以及可选精确
  region ref。精确 region 仍是内容寻址二值 RLE；归一化半开 bbox 只用于定位和预取。
- `dedupe_key = sha256(code + annotation/version + sorted related ids + frame range + region digest)`；
  `(project_id, dedupe_key)` 唯一。同一版本重跑只更新指标与 `last_seen_run_id`，不创建重复问题。
- issue 状态固定为 `open | resolved | wont_fix | stale`。只要 annotation current version 与 issue version
  不同，读取 API 必须立即给出 `effective_status=stale`，即使异步落库尚未完成。
- overlap 等多对象 issue 以稳定排序后的最小 annotation id 为 primary，成员全集写入 related ids。
  人工评论继续使用 `annotation_feedbacks`；QC issue 不复制评论线程。
- 相同 config digest + source snapshot digest + task scope 采用 single-flight。旧 run 可以完成审计写入，但只能
  产生 stale issue，不能覆盖新版本的 current summary。

### D3. Repair 使用 dry-run receipt、确定性分片和冲突安全 rollback

- repair 执行前必须返回冻结源版本、scope fingerprint、每项 changed pixels / mutation 数、稳定 skip code、
  plan digest 和 15 分钟 receipt。执行必须复核 receipt、plan、映射和源版本；漂移返回 409，不临场重算后继续。
- 每个 task 不超过 100 mutations 时复用 `MaskMutationService` 一次原子提交；更大范围固定按
  `task + 100 mutations` 分片。分片内全成或全败，跨 task 可恢复，重试只处理未提交分片。
- `mask_repair_batches` 保存每片 before refs、after versions、operation ids、状态和 7 天 rollback 到期时间；
  到期前 before refs 是 GC live root。
- rollback 仅在每个目标 current version 等于 repair 记录的 after version 时执行。任一冲突使该分片整体 409，
  不提供 force overwrite；成功 rollback 仍产生新 annotation version 和审计记录。
- SAM / Tracker 类修复只创建 staged candidate，不直接写真值；接受仍走候选版本、锁和幂等合同。

### D4. Review scope 是版本化保护账本，不是新的任务状态

- 新增 `mask_review_scopes`，记录 reviewer、source job / issue、源 annotation version、frame range、region
  digest、decision、result version 与审计时间。
- region accept 只在 immutable issue region 内应用 candidate XOR；region 外、窗口外、其它实例逐像素不变。
  reject 只消费对应 staged slice，其余候选继续保活。
- accept scope 绑定 mutation 后的 result version；只有 annotation current version 仍等于 result version 时，
  该 scope 才保护后续自动写入。之后的合法人工修改使旧 scope 保留审计价值，但不成为永久锁。
- manual keyframe、annotation lock、segment lock 和 current reviewed scope 必须在行锁后重查。普通 UI 不提供
  `override_manual` 或 `override_reviewed` 快捷绕过；特权覆盖是单独确认、单独授权、单独审计的操作。
- “接受但记录警告”仍把 task 写成既有 `completed`；warning issue ids、QC digest 和 note 写审计，不新增
  `completed_with_warning` 等伪状态，也不自动 resolve issue。

### D5. 格式能力由版本化 adapter registry 单一声明

- 每个 `MaskFormatAdapter` descriptor 至少声明 `format_id`、adapter version、manifest version、media types、
  import / export capabilities、loss class 和 option schema。API、worker、UI 可用格式列表和文档测试都从 registry
  派生，不能再各自维护静态集合。
- adapter 统一实现 `preflight_import` / `execute_import` 与 `preflight_export` / `write_export`。preflight 返回
  `MaskFormatPlan`：adapter / manifest version、逐 item `lossless | lossy | unsupported`、label / size / overlap
  冲突、ID / frame mapping、预估对象 / 文件 / bytes、loss / skip / warning codes 和 plan digest。
- import 只接受已 staged 的 object key + SHA-256；执行只能使用同一 staged digest、adapter version、mapping
  digest 和 plan digest。任一变化返回 409 并要求重新 preflight。
- import 以 task 为原子边界；大包跨 task 可恢复。export artifact cache key 必须包含 adapter version、manifest
  version 和 options digest；相同 key 的 miss 采用 single-flight 和 double-check，避免重复上传及唯一键竞争。
- archive adapter 必须共用 `safe_archive`，拒绝 zip-slip、绝对 / drive path、重复规范化路径、case-fold
  collision、symlink、zip bomb、manifest 悬空引用和 PNG magic / 位深 / 尺寸不一致；禁止直接 `extractall`。
- `lossless` 只有在像素、class、instance、frame 和 track identity 全部保持时成立。只保持像素但丢 track、
  occlusion 或 sparse-frame 语义仍是 `lossy`。

### D6. YouTube-VOS sparse gap 必须显式选择有损解释

- sparse 包中的未标注帧定义为 unknown / unsampled，既不等于 outside，也不等于 nearest hold。
- import preflight 必须要求调用者显式选择 `gap_policy=outside_gaps | nearest_hold`，没有默认值：
  `outside_gaps` 把未标注区间解释为不可见，`nearest_hold` 把最近精确 Mask 延续到缺口。
- 两种策略都必须把 `sparse_frames_collapsed` 标记为 item-level loss，并在 report 中列出受影响 track、frame
  range 和策略；UI 必须二次确认。adapter 不得根据相邻像素、IoU 或 ZIP 文件缺失自行猜测。
- export 到 sparse 目录时，manifest 必须列出实际写出的 annotated frames；若源轨迹还有未写出的可见状态，
  同样报告 `sparse_frames_collapsed`。
- 本阶段不为 unknown gap 修改 `video_track_mask`。将来只有在至少两个内部消费场景都需要 unknown time state，
  且能同步迁移 resolver、AAP、QC、Tracker 和工作台时，才另写 ADR 重新评估。

### D7. 不新增 geometry；所有外部格式先归一化到现有 instance Mask 真值

- 图片继续只使用 `raster_mask`，视频继续只使用 `video_track_mask`；COCO compressed RLE、Label Studio brush
  RLE、PNG、DAVIS、YouTube-VOS 和 MOTS 都是 adapter codec / package contract，不是新 geometry。
- import 成功后必须生成 canonical 内容寻址 COCO RLE ref，并通过现有 task-aware size / media / foreground
  校验。palette index、MOTS text row、BrushLabels RLE 和 polygon 都不能成为平行真值。
- 本 ADR 不引入 semantic / panoptic class map、16-bit semantic PNG、稀疏未知帧插值或通用 bitmap geometry。
  Indexed PNG 的 winner 只是一种显式有损导出策略，不改变平台允许 instance overlap 的事实。

### D8. Loss / skip code 是公共 ABI，真实 consumer 是格式完成门

- loss code 至少冻结为：`overlap_resolved`、`holes_polygonized`、`components_split`、
  `track_identity_lost`、`sparse_frames_collapsed`、`occlusion_lost`、`class_id_remapped`、
  `instance_id_remapped`、`instance_id_overflow`、`frame_base_changed`、`unknown_label`、
  `image_size_mismatch`、`unsupported_geometry`、`nonportable_media_reference`。
- repair / import 的稳定 skip code 至少冻结为：`task_not_found`、`annotation_not_found`、
  `annotation_locked`、`manual_keyframe_protected`、`reviewed_scope_protected`、`segment_lock_conflict`、
  `version_conflict`、`scope_stale`、`blocker_policy_conflict`、`unsupported_geometry`、`unknown_label`、
  `image_size_mismatch`、`instance_id_overflow`、`already_committed`、`not_selected`、
  `resource_budget_exceeded`。
- code 字符串不本地化、不复用为另一含义。UI 映射人类说明，但遇到未知 code 必须原样显示；增加 code 是
  加法兼容，删除或改义需要新 adapter / schema major。
- “格式完成”要求固定 golden 经过目标事实标准 consumer 解码，并逐像素比较；只校验 JSON shape、ZIP 文件名、
  自己的 encoder→decoder 或截图均不算完成。fixture 与通过门见
  [`docs/research/20-mask-qc-format-contracts.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/20-mask-qc-format-contracts.md)。

## Consequences

正向：

- QC 问题、compare、repair、rollback 和 region accept 都能追溯到不可变版本与 digest，晚到 job 不再冒充当前真值。
- 人工反馈、自动 issue、通用 async job 与领域账本职责清楚，能够独立分页、保留、去重和审计。
- 新格式共享同一 preflight、安全 archive、资源预算、缓存版本和报告合同；“支持”由真实 consumer 证据定义。
- YouTube-VOS sparse gap 的不可逆语义变化在执行前可见，同时避免为单一格式污染全部视频 geometry。

负向：

- 需要新增五类领域账本及其 retention / GC roots，migration、并发和清理测试规模增加。
- 部分外部格式即使像素可解码，也会因为 track、overlap、occlusion 或 sparse gap 丢失而显示 `lossy`，用户必须确认。
- adapter / manifest version 进入缓存和 receipt 后，serializer 变更会主动失效旧缓存并要求重新 preflight。
- 官方 consumer 依赖需要固定版本与可再现 fixture；格式实现不能只靠平台内部单测快速宣布完成。

## Alternatives Considered

**方案 B（历史与 QC 塞进 Annotation JSONB）**：revision、issue 和 current truth 更新频率、保留周期与查询方式
完全不同；热行膨胀还会让 compare / QC 写入与标注保存争锁。拒绝。

**方案 C（自动问题复用 annotation_feedbacks）**：人工 issue / comment 允许讨论、编辑和 resolve，而自动 QC 必须
按版本与 kernel 重算、去重和 stale。强行共表会让 run 证据和评论生命周期互相污染。拒绝；评论仍可锚定 QC region。

**方案 D（新增 unknown-gap geometry）**：可以忠实表达 sparse 数据集，但会改变 nearest-hold 的核心时间模型；当前
唯一需求是交换格式，显式有损 adapter 成本更低且不误导。拒绝。

**方案 E（继续格式分支）**：当前静态集合和 worker 分支已经使 API、前端、缓存和 consumer 测试分离；继续扩展会
让安全限制与 loss 语义不可统一。拒绝。

## Notes

- 研究与 fixture 合同：[`docs/research/20-mask-qc-format-contracts.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/20-mask-qc-format-contracts.md)。
- 实施计划：[`docs/plans/archive/2026-07-21-v0.23.11-mask-quality-review-format-ecosystem.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-21-v0.23.11-mask-quality-review-format-ecosystem.md)。
- 现有原子写入：`apps/api/app/services/mask_mutation.py`；现有 Tracker staged review：
  `apps/api/app/services/video_tracking/runner.py`。
- 现有格式基线：`apps/api/app/services/exporting/`、`apps/api/app/services/annotations_import.py`、
  `apps/api/app/services/predictions_import.py`。
