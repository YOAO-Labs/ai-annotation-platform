# Mask 质检与格式生态合同基线

> 调研日期：2026-07-22
>
> 用途：冻结 Mask QC golden、格式事实标准、consumer fixture、稳定 loss / skip code 和实施风险。本文描述
> M0 时点的仓库事实与后续通过门，不把计划能力误写成当前能力。

## 0. 结论

1. 平台已经有可复用的 canonical instance Mask 真值：图片 `raster_mask`、视频 `video_track_mask` 均引用
   内容寻址的 uncompressed COCO RLE；尺寸、runs、bytes、gzip 和 task media 均有边界。
2. 当前只有 AAP image Mask 做到了平台 importer round-trip；COCO image / COCO Frames / DAVIS 已有导出，
   但 consumer 证据与双向覆盖不完整。Label Studio BrushLabels、binary / indexed PNG 通用包、DAVIS import、
   YouTube-VOS 和 MOTS 尚未实现。
3. 当前没有 revision ledger、QC run / issue、repair / rollback 或 reviewed-scope 领域账本；不能从最新
   Annotation、人工 feedback 或通用 async job 反推这些语义。
4. 格式能力分散在后端集合、打包分支、API 描述和前端类型中。必须先建立 adapter registry、preflight receipt
   和共享 archive guard，再扩展格式；否则每个新格式都会复制安全、幂等和 loss 判断。
5. “可导出”不等于“格式完成”。完成门是目标事实标准 consumer 可读，且 canonical→artifact→consumer 后按
   像素、class、instance、frame 和 track identity 比较；无法保持的维度必须报告稳定 loss code。

相关架构决策：[ADR-0055](../adr/archive/0055-mask-quality-and-format-contracts.md)。

## 1. 仓库基线

### 1.1 真值、版本与原子变更

| 能力               | 当前证据                                         | 可复用合同                                                                                                               | 缺口                                                         |
| ------------------ | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ |
| Canonical RLE      | `apps/api/app/utils/raster_mask_rle.py`          | COCO column-major uncompressed counts；图片 8192 / 67,108,864 pixels，视频 4096 / 16,777,216 pixels，最多 1,000,000 runs | production 尚无 compressed COCO counts codec                 |
| 内容寻址           | `apps/api/app/services/raster_mask_storage.py`   | canonical JSON SHA-256、identity / gzip 对象、ref 全字段复核、task-aware media / size 校验、advisory lock                | revision、repair before ref、QC region ref 尚未加入 GC roots |
| 压缩防护           | `apps/api/app/utils/raster_mask_gzip.py`         | 8 MiB compressed、4 MiB uncompressed、20× expansion，有界解压并拒绝尾随 / 拼接 member                                    | 只服务平台 RLE 对象，不等于 COCO compressed counts           |
| Annotation version | `apps/api/app/db/models/annotation.py`           | current row 有递增 `version`                                                                                             | 没有历史 geometry，上一版本 compare 无可靠来源               |
| 原子 mutation      | `apps/api/app/services/mask_mutation.py`         | task→RLE/upload→annotation 锁序、scope fingerprint、expected versions、idempotency key、operation lineage                | 尚无 repair plan、分片 ledger、rollback retention            |
| Tracker review     | `apps/api/app/services/video_tracking/runner.py` | staged result、job revision、source version、实例 / 帧窗口 accept / reject、manual / segment lock                        | 没有 issue region selector和跨 job reviewed scope            |

RLE 对象是唯一像素真值。bbox、overview、PNG、palette index、polygon 和 QC metric 都是派生表示，不能写成
与 `coco_rle_ref` 并列的权威来源。

### 1.2 QC 与审阅

`annotation_feedbacks`（`apps/api/app/db/models/annotation_feedback.py`）已经提供 project / task / annotation /
pixel anchor、issue / comment / reject / bug、thread 与 resolve，适合人工讨论；但它没有 kernel run、config digest、
annotation version、dedupe key 或 stale 规则，不能承担自动 QC issue。

`async_jobs`（`apps/api/app/db/models/async_job.py`）提供通用 pending / running / completed / failed / cancelled 和
进度通知；当前 kind 不含 Mask QC / repair / format import。它适合作为执行壳，不适合取代长期可查询的领域 run、
issue 或 rollback ledger。

当前必须补齐的独立账本：

| 账本                        | 身份与不可变量                                                | 保留 / 冲突语义                                                 |
| --------------------------- | ------------------------------------------------------------- | --------------------------------------------------------------- |
| `mask_annotation_revisions` | annotation id + version + geometry digest                     | 20 versions 且至少 30 天；ref 是 GC root；过期 compare 明确失败 |
| `mask_qc_runs`              | scope + config digest + source snapshot digest                | 与 async job 1:1；相同快照 single-flight                        |
| `mask_qc_issues`            | version-bound dedupe key + optional exact region digest       | version 漂移立即 effective stale；重跑 upsert，不复制           |
| `mask_repair_batches`       | plan / receipt + before refs + after versions + operation ids | task / 100 mutation 原子分片；7 天可冲突安全 rollback           |
| `mask_review_scopes`        | reviewer + source + frame / region digest + result version    | 仅 current==result version 时保护；之后保留审计但不永久锁       |

### 1.3 当前导入 / 导出框架

- `apps/api/app/services/exporting/packaging.py` 用 `IMAGE_EXPORT_TARGETS` / `VIDEO_EXPORT_FORMATS` 静态集合和
  `build_export_zip` 分支选择格式；`apps/web/src/api/projects.ts` 与 Dashboard 导出 UI 另有类型 / 选项。
- `apps/api/app/workers/export.py` 已有专用 export queue、磁盘 tempfile ZIP、对象存储上传和 finally cleanup；
  `apps/api/app/services/exporting/cache.py` 的 key 只有 scope、targets、options 和 annotation 更新指纹，尚未包含
  adapter / manifest version，也没有同 key cache miss single-flight。
- `ExportService.iter_export_chunks` 可供 per-file 格式按 task 分块；COCO / AAP 单文档仍会整体 materialize。
- annotation AAP import（`apps/api/app/services/annotations_import.py`）有 dry-run、task matching、mask object
  验证和 append / imported-subset overwrite，但入口同步读取完整文件，结果只有 imported / skipped / errors。
- COCO prediction import（`apps/api/app/services/predictions_import.py`）可接 polygon 和 uncompressed RLE；
  compressed string counts 明确拒绝。`pycocotools` 当前只在 test extra，不是 production codec。
- dataset ZIP 的 `_normalize_zip_relpath` 能拒绝 zip-slip、绝对 / drive / hidden path，并限制 200 MiB 原包、
  5,000 entries、100 MiB 单 entry；它是 router 私有 helper，且缺总展开 bytes、压缩比、规范化重复、
  case-fold collision、symlink 和 manifest 引用闭包。

## 2. QC golden 与资源基准合同

M1 / M2 实现前固定下列输入与预期；fixture 的 RLE digest 和预期整数指标必须入库，不能只存 PNG 截图。

| Fixture                                      | 覆盖语义                                  | 必须断言                                                       |
| -------------------------------------------- | ----------------------------------------- | -------------------------------------------------------------- |
| `empty` / `near-empty-15` / `near-empty-16`  | 空与阈值边界                              | area 整数、规则在阈值两侧稳定                                  |
| `diagonal-touch`                             | 前景 8-connectivity / 背景 4-connectivity | 前景 component 合并，背景 hole 不产生双重连通解释              |
| `donut-with-island`                          | hole、component、bbox                     | hole / component 数、最小面积、精确 region ref                 |
| `narrow-bridge-w2`                           | erosion bridge                            | 只报告连接两侧的 removed region，不把整条细长目标当 bridge     |
| `noisy-boundary`                             | close→open XOR / 4-neighbor boundary      | numerator、denominator、ratio 均固定                           |
| `same-class-overlap` / `cross-class-overlap` | pair ordering、intersection               | primary 是最小 annotation id，交集像素与 region digest 稳定    |
| `tile-seam-512`                              | 跨 tile component / hole union            | 与 dense 小图 oracle 完全一致，只产生一个 issue                |
| `held-outside-occluded`                      | 视频 resolver                             | held→held 相同 ref 不充当稳定证据；outside 跳过；occluded 单列 |
| `flicker-middle` / `flicker-edge`            | 短 visible / absent run                   | 中间夹住的短 run 报告，首尾截断不报同一规则                    |
| `drift-three`                                | manual anchor 与连续阈值                  | anchor frame、连续帧数、source / confidence lineage 稳定       |
| `revision-race`                              | 晚到 run                                  | 旧 run 只能写 stale，current summary 不被覆盖                  |

视频详细 resolver 冻结五态 `exact | held | absent | outside | occluded`。现有
`outside[].source=prediction` 表示模型明确判空，映射为 `absent`；`source=manual` 映射为
`outside`。旧兼容 resolver 对这两态都返回 `None`，不为此扩展 geometry schema。

性能 fixture 固定三档：1080p × 20 Mask 单帧、300 frames × 20 tracks、8192² sparse Mask。分别验证 kernel
p95、取消检查点、held / outside 正确性、tile + halo materialization、Worker RSS 回稳；8K 路径禁止创建全帧
RGBA 或 dense alpha。任何 benchmark 生成的大包、trace 和临时目录在记录聚合数据后删除。

## 3. 官方 / 事实标准格式合同

### 3.1 图片格式

| 格式                     | 外部合同与本平台映射                                                                                                                                     | M0 当前状态                                                                   | 冻结的损失边界                                                                                |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| AAP JSON                 | `mask_objects{sha256: coco_rle}` + `coco_rle_ref`；import 后仍是 canonical ref                                                                           | image Mask export / import round-trip 已有                                    | embedded object 可无损；ref-only artifact 是 `nonportable_media_reference`                    |
| COCO instance            | `segmentation` 接 polygon、uncompressed RLE 或 compressed RLE；Mask export 用 RLE、`iscrowd=1`                                                           | image export 为 uncompressed counts；prediction import 不收 compressed counts | 像素 / class 可无损；compressed codec 未完成前不得宣称完整 import                             |
| Label Studio BrushLabels | task `data` + annotations / predictions `result`；result 必须含 `from_name`、`to_name`、`type=brushlabels`、原图尺寸、`value.format=rle` 和 LS brush RLE | 仅 tool binding 把 `brushlabels` 归类为 region，无 codec / adapter            | LS RLE 与 COCO RLE 是独立 codec；字段或 decoder 缺一即 unsupported                            |
| Binary PNG per instance  | 平台 manifest v1；`masks/<item>/<instance>.png`，8-bit `L`，0 背景 / 255 前景                                                                            | 无通用 adapter；Pillow 已是 production dependency                             | 每实例分文件可保留 overlap；manifest 缺 class / annotation / media mapping 时 unsupported     |
| Indexed PNG instance map | 平台 manifest v1；每 item 一张 8-bit `P`，0 背景、1–255 instance，index 是 identity                                                                      | DAVIS writer 可复用 palette / pixel index；无图片 adapter                     | overlap 默认 error；winner 产生 `overlap_resolved`；超过 255 必须拒绝或显式分片，不能回绕     |
| YOLO Seg                 | 每行 `class x1 y1 ...`，归一化 polygon                                                                                                                   | 已有 polygon import / export 路径，不是 Mask 无损 round-trip                  | hole、multi component、1–2 px 结构与曲线边界至少触发 `holes_polygonized` / `components_split` |

官方依据：

- [COCO API](https://github.com/cocodataset/cocoapi) 的 `annToRLE` / `annToMask` 接受 polygon、uncompressed RLE
  与 RLE；consumer 固定用官方 `pycocotools`。
- [Label Studio BrushLabels](https://labelstud.io/tags/brushlabels) 明确 `original_width`、`original_height`、
  `value.format="rle"` 和 `value.rle`；[pre-annotations](https://labelstud.io/guide/predictions.html) 要求 task
  `data`、`predictions[].result`、`from_name` / `to_name` 与 labeling config 匹配。decoder 取
  [Label Studio SDK converter](https://github.com/HumanSignal/label-studio-sdk/tree/master/src/label_studio_sdk/converter)
  的固定版本，不以平台自写 decoder 作为唯一 oracle。
- [Ultralytics segmentation dataset](https://docs.ultralytics.com/datasets/segment/) 只表达 polygon 点序列，
  因而不能承诺任意 Raster Mask 逐像素无损。

### 3.2 视频格式

| 格式             | 外部合同与本平台映射                                                                             | M0 当前状态                                                       | 冻结的损失边界                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| AAP JSON         | keyframe、outside、occluded、source、track id 与 embedded RLE                                    | video Mask export 已有；完整双向 golden 待补                      | embedded object 可保持平台全部时序语义                                                        |
| COCO Frames RLE  | 每个展开帧是 COCO image + instance annotation；track extension 必须显式                          | polygon / uncompressed Mask RLE export 已有；无 import            | 每帧像素可无损；缺 track extension 必须 `track_identity_lost`，禁止按 category / IoU 猜轨     |
| DAVIS            | `Annotations/<resolution>/<sequence>/<frame>.png` palette index；0 背景，同一 object id 跨帧稳定 | export 已有标准 palette、稳定 1–254 id、z-order winner；无 import | overlap winner 报 `overlap_resolved`；255 保留 void，不能作为普通 instance                    |
| YouTube-VOS 风格 | sparse annotation PNG + `meta.json` object / category / annotated frames                         | 无 adapter                                                        | 未标注帧是 unknown；必须选 `outside_gaps` 或 `nearest_hold`，两者均 `sparse_frames_collapsed` |
| MOTS             | sequence 根目录 `<sequence>.txt`；每行 `frame id class_id height width compressed_coco_rle`      | 现有 `build_mot_gt` 是 bbox MOT，不是 MOTS；无 codec / adapter    | frame base、class map、track id map 必须显式；不从组合 id 推断任意项目类别                    |
| Video JSON       | 平台内部 ref-only JSON                                                                           | export only                                                       | 固定 `nonportable_media_reference`，不宣称可移植 import                                       |

官方依据：

- [DAVIS dataset reader](https://interactive.davischallenge.org/docs/dataset.davis/) 把 annotation 读成
  `frames × H × W`，像素值是 object index，0 是背景；palette 只用于显示。
- [YouTube-VOS](https://youtube-vos.org/dataset/vos/) 明确对象可能从中间帧出现，且数据使用稀疏采样标注；
  文件缺失不能被当作对象不存在。平台额外 manifest 固定 annotated frame 列表与 gap policy。
- [MOTChallenge MOTS instructions](https://motchallenge.net/instructions/) 固定六字段文本行，RLE 是 COCO
  compressed string，可由 cocotools 与 height / width 解码；普通 MOT 十字段 bbox 行不能复用为 MOTS。

## 4. Consumer fixture 矩阵

所有 fixture 使用同一组 canonical oracle：空背景、单像素、donut、三 component、两个重叠实例、255 / 256
instance、非方形尺寸、视频 exact / held / outside / occluded / sparse gap。比较在解码后的 row-major binary /
index buffer 上完成；编码器输出字节不要求相同，语义必须相同。

| Adapter                  | Import fixture                    | Export consumer                         | 必须比较                                               | 完成门                                                    |
| ------------------------ | --------------------------------- | --------------------------------------- | ------------------------------------------------------ | --------------------------------------------------------- |
| AAP image / video        | AAP importer                      | AAP importer + shared golden            | digest、pixel、class、instance、frame / track state    | 双向，无未报告损失                                        |
| COCO image               | polygon、uncompressed、compressed | `pycocotools.COCO.annToMask`            | pixel、category、image size、bbox / area 派生值        | 三种 segmentation 都可读；compressed 可 production import |
| Label Studio BrushLabels | 官方 task / prediction sample     | 固定版 LS SDK brush decoder             | RGBA / alpha→binary pixel、label、from / to name、尺寸 | 不调用平台 decoder 作为 oracle                            |
| Binary PNG               | manifest + L PNG                  | Pillow `Image.open(...).convert("L")`   | 像素只含 0 / 255、instance 文件映射、SHA-256           | overlap fixture 保持两实例独立                            |
| Indexed PNG              | manifest + P PNG                  | Pillow palette decode                   | index buffer、id map、winner 覆盖像素数                | 255 通过；256 稳定拒绝 / 分片，不回绕                     |
| YOLO Seg                 | 官方目录 / txt parser             | 独立 polygon rasterizer                 | class、polygon raster 后 pixel 与 loss report          | donut / multi / thin 均有预期 loss，不标 lossless         |
| COCO Frames              | frame doc import                  | `pycocotools.COCO.annToMask`            | 每帧 pixel、source frame mapping、track extension      | 无 extension 时逐帧实例 + `track_identity_lost`           |
| DAVIS                    | palette PNG / ImageSets           | Pillow + DAVIS directory reader         | index buffer、跨帧 id、0 / 255 语义、frame filename    | import→export 后 winner buffer 一致                       |
| YouTube-VOS              | sparse PNG + meta                 | 独立 directory / metadata reader        | annotated frames、object id / category、gap report     | 两种 policy 分别 golden，均报告 sparse loss               |
| MOTS                     | txt + compressed RLE              | `pycocotools.mask.decode` + line parser | pixel、frame base、class / track mapping、尺寸         | 多 sequence、id collision、compressed string 均通过       |

当前已有的直接证据：

- `apps/api/tests/test_raster_mask_portability.py`：AAP image Mask object 去重与 import round-trip。
- `apps/api/tests/test_export_video.py`：COCO Frames 可由 `pycocotools` 建索引并解码 polygon；Mask RLE 仍需
  加入同一 consumer pixel golden，不能只断言结构。
- `apps/api/tests/test_export_davis.py`、`apps/api/tests/test_export_packaging.py`：Pillow 解码 palette index、
  overlap winner 与目录结构；尚缺 DAVIS import round-trip。

## 5. 稳定 loss / skip code

### 5.1 Loss codes

loss 表示 item 可以执行，但输出不能保持 canonical 的全部语义。`unsupported` 不是 loss，不能执行。

| Code                          | 稳定含义                                                              |
| ----------------------------- | --------------------------------------------------------------------- |
| `overlap_resolved`            | 多实例重叠按显式 winner 策略被覆盖；报告每实例 lost pixels            |
| `holes_polygonized`           | hole 经过不支持 hole 的 polygon 表示而丢失 / 填充                     |
| `components_split`            | 一个 instance 的多连通 component 被拆成多个外部对象 / 行              |
| `track_identity_lost`         | 帧像素仍在，但跨帧同一对象身份不可恢复                                |
| `sparse_frames_collapsed`     | unknown / 未采样帧被解释为 outside / hold，或 dense state 未写出      |
| `occlusion_lost`              | 目标像素 / track 保留，但 occluded 状态无法表达                       |
| `class_id_remapped`           | class identity 保持，但外部数值 id 改写；mapping 必须随 artifact 保存 |
| `instance_id_remapped`        | instance / track identity 保持，但外部数值 id 改写                    |
| `instance_id_overflow`        | 目标格式 id 容量不足；若无显式分片则 item unsupported                 |
| `frame_base_changed`          | 0 / 1 based 或采样网格编号改变；source↔output map 必须保存            |
| `unknown_label`               | 外部 label 无项目映射；未映射时不能静默落 `__unknown`                 |
| `image_size_mismatch`         | manifest / annotation 与媒体尺寸冲突；默认 unsupported                |
| `unsupported_geometry`        | item 含 adapter 不能表达的 geometry；不能静默跳过                     |
| `nonportable_media_reference` | artifact 依赖平台 object key / 临时 URL，不能作为独立备份             |

### 5.2 Skip codes

skip 表示本次 repair / import 明确没有对该 item 产生 mutation，不能计入 success：

```text
task_not_found, annotation_not_found, annotation_locked,
manual_keyframe_protected, reviewed_scope_protected, segment_lock_conflict,
version_conflict, scope_stale, blocker_policy_conflict,
unsupported_geometry, unknown_label, image_size_mismatch, instance_id_overflow,
already_committed, not_selected, resource_budget_exceeded
```

- `already_committed` 只用于可恢复重试中确认同 digest 分片已经成功，不等于重复写入。
- `not_selected` 只用于 preflight plan 中存在、执行请求明确未选择的 item，不能掩盖解析失败。
- `resource_budget_exceeded` 必须附 budget 名称、limit 与 observed / estimated；archive 安全拒绝和 digest 漂移是
  整体执行错误，不降级为可忽略 skip。
- error / reason code 与 HTTP 语义另行分层，例如 receipt / staged digest / adapter version / plan digest 漂移
  返回 409；`qc_overlap_pair_budget_exceeded` 在 decode 前拒绝；temp / archive quota 失败必须清理临时目录。

code 字符串属于 API / artifact ABI：不本地化、不随文案修改、不复用旧 code 表达新含义。UI 必须给已知 code
映射说明，同时对未知 code 原样显示。每个 report 同时保留 `code`、`message` 和结构化 detail。

## 6. Manifest 与 plan 最小字段

平台拥有的 PNG / sparse directory manifest v1 至少包含：

```text
format_id, manifest_version, adapter_version,
media_path, width, height, source_frame_index?, output_frame_index?, frame_base?, padding?,
class_id, class_name, instance_id, track_id?, pixel_id?,
source_annotation_id, source_annotation_version, content_sha256,
overlap_policy?, gap_policy?, id_mapping?, frame_mapping?, loss_report
```

`MaskFormatPlan` 至少包含 adapter / manifest version、staged object digest、mapping / options digest、逐 item loss
class、unknown labels、dimension / overlap conflicts、ID / frame mapping、estimated objects / files / bytes、loss / skip /
warning codes 和 canonical plan digest。execute 复核全部 digest；不能在确认后偷偷采用新 mapping 或 winner。

## 7. 并发、幂等、安全与版本风险

| 风险                       | 当前暴露面                            | 冻结缓解                                                                              |
| -------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------- |
| 重复 QC issue              | run 没有版本身份                      | version-bound dedupe key + upsert + stale effective status                            |
| 晚到 job 覆盖新真值        | worker 执行时状态已变化               | source snapshot digest；旧结果只写 stale                                              |
| repair 预览与执行不一致    | dry-run 后 annotation / config 漂移   | 15 分钟 receipt + plan / scope / version digest；锁后重查                             |
| rollback 覆盖人工新改      | 只持 before ref 不够                  | current==after version 才按分片 rollback，否则 409                                    |
| 并发 export 重复上传       | cache lookup / record 间有窗口        | adapter-version key + advisory single-flight + double-check                           |
| append import 重复对象     | 当前 AAP 无 external id upsert        | staged digest + plan item identity + per-task idempotency；重试只做未提交             |
| 假取消                     | 同步 import / 大循环不检查 cancel     | 独立 format import worker；固定 item / frame 检查点                                   |
| ZIP traversal / bomb       | dataset helper 只覆盖部分检查         | shared safe archive + expanded bytes / ratio / duplicate / symlink / manifest closure |
| PNG 解压峰值               | 只信文件大小                          | magic、IHDR 位深 / 尺寸、pixel budget，scanline / tile decode                         |
| 缓存跨 serializer 版本命中 | key 无 adapter / manifest version     | 两个版本与 options digest 都入 key                                                    |
| 对象先写、DB 后失败        | 可能产生 orphan RLE / artifact        | 事务报告 + staged retention + 现有宽限 GC；失败 / cancel 清 temp                      |
| 8K 全图 materialize        | DAVIS / PNG 逐对象 bytearray 容易放大 | scanline / tile compositor，禁止全帧 RGBA，固定 temp / RSS 退出门                     |

## 8. M0 之后的顺序约束

1. 先实现 revision / QC kernel 与 issue version 语义，再开放 compare、repair 和 reviewed scope。
2. 先实现 registry、preflight receipt、safe archive、worker / quota / cache version，再接新 adapter。
3. 图片格式先补 COCO compressed consumer，再做 Label Studio 与 PNG；视频先补 COCO Frames / DAVIS 双向，
   最后接 YouTube-VOS 和 MOTS。
4. 任一 adapter 只有在本矩阵目标 consumer 逐像素通过、loss / skip 可见、取消 / 重试 / 清理通过后，才出现在
   后端项目级可用格式列表；前端不得用编译期常量提前开放。

## 9. 参考资料

- [ADR-0052：共享栅格 Mask 与图片 geometry](../adr/archive/0052-shared-raster-mask-and-image-geometry.md)
- [ADR-0053：原生 Mask AI 候选生命周期与视频局部纠错](../adr/archive/0053-native-mask-ai-candidate-lifecycle-and-video-correction.md)
- [ADR-0054：Raster Mask 大画布资源预算](../adr/archive/0054-raster-mask-large-canvas-memory-and-tiles.md)
- [COCO API](https://github.com/cocodataset/cocoapi)
- [Label Studio BrushLabels](https://labelstud.io/tags/brushlabels)
- [DAVIS dataset reader](https://interactive.davischallenge.org/docs/dataset.davis/)
- [YouTube-VOS dataset](https://youtube-vos.org/dataset/vos/)
- [MOTChallenge MOTS format](https://motchallenge.net/instructions/)
