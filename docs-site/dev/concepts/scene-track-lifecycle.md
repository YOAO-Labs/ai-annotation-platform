---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-08-25
---

# Scene Track 与轨迹生命周期

`SceneTrack` 是 3D Scene 中稳定的时序对象身份。它把“对象是谁、在哪些帧存在、哪些逐帧框属于它、一次修轨怎样安全撤销”从 Annotation 临时聚合提升为显式领域模型。

## 为什么不能只用 `Annotation.track_id`

`Annotation.track_id` 能把逐帧框串成一条链，但成员缺口有多种含义：对象确实缺席、尚未标注、插值结果未物化，或成员被停用。仅看框列表无法区分这些状态，也无法给整个对象提供类别、属性和并发 revision。

因此 3D Scene 使用四层权威关系：

| 层                    | 权威数据                                           | 典型消费者                     |
| --------------------- | -------------------------------------------------- | ------------------------------ |
| `SceneTrack`          | Scene 内身份、类别、轨迹级属性、revision、退役状态 | 时间轴、Data Manager、质量规则 |
| `SceneTrackInterval`  | 对象存在的闭区间，可有多段                         | 缺席/恢复/终止、导出           |
| `Annotation`          | 某帧几何、来源、成员属性、版本                     | 3D 画布、逐帧编辑              |
| `SceneTrackOperation` | 命令快照、前后状态、幂等结果和反向状态             | 历史、撤销、审计               |

`Annotation.scene_track_id` 是新 3D 成员关系的权威外键；冗余的 `Annotation.track_id` 保留给旧客户端、查询和交换格式。诊断器会检查两者以及 project、Scene、类别是否一致。

## 存在区间

存在区间使用闭区间 `[start_frame, end_frame]`。同一 Track 可有多个不重叠区间，例如 `[0, 12]` 与 `[18, 35]` 表示对象在 F13–F17 缺席后以同一身份恢复。

- PostgreSQL exclusion constraint 阻止同一 Track 的区间重叠。
- service 会合并相邻区间，避免同一存在段出现多种等价表示。
- `retired_at` 表示身份整体退役，不表示从某帧开始缺席。
- 活跃成员必须落在存在区间内；缺席区间内的既有成员会停用并保留为隐藏历史。

Track 同时记录存在模式：

- `inferred` 是成员包络，用于保守迁移和尚未明确编辑过生命周期的轨迹。传播或新成员可以扩展这个单一包络，成员缺口不会被猜成缺席。
- `explicit` 是用户命令或无损导入确认的控制面。新成员只能写入已声明区间；要跨过缺席或终止边界，必须先执行“恢复出现”。

生命周期命令会把 Track 切换为 `explicit`，撤销则恢复操作前的模式。这阻止后续自动传播无意中把已终止的轨迹延长回来。

历史数据回填只为合法链创建 `[min(member_frame), max(member_frame)]` 的 `legacy_envelope`。它不会把历史成员缺口猜成真实缺席；异常链保持未关联并由诊断端点报告。

## 时间角色与来源正交

`Annotation.source` 回答“结果从哪里来”，`Annotation.temporal_role` 回答“它在时间模型中扮演什么角色”。

| 时间角色   | 含义                                         |
| ---------- | -------------------------------------------- |
| `keyframe` | 用户创建、修改、确认或显式恢复得到的锚点     |
| `derived`  | 插值、传播、跟踪等可重算成员                 |
| `sample`   | 导入或历史数据无法可靠判断锚点语义的兼容成员 |

例如接受模型结果后可以同时是 `source=prediction_based` 与 `temporal_role=keyframe`。生命周期命令若会停用 `keyframe` 或 `sample`，必须在预览中列出影响并要求显式确认。

## 统一命令合同

拆分、合并、标记缺席、恢复出现和终止都使用同一流程：

1. `preview` 读取 Track、区间和所有成员版本，返回区间变化、受影响角色与 `snapshot_token`。
2. `execute` 按确定顺序锁定相同数据并重新计算快照；任何变化都返回 `track_snapshot_stale`，不产生部分写入。
3. 命令以 `(scene_id, actor_id, idempotency_key)` 幂等，重复请求返回第一次的同一结果。
4. journal 保存 before/after state、source/result revisions 和 inverse payload。
5. `revert` 只有在受影响 Track 和成员没有后续修改时执行；否则返回 `operation_revert_stale`。

撤销不会倒退 revision，也不会删除审计可见身份。拆分创建的新 Track 在撤销后进入退役状态；恢复命令新建的成员在撤销后保留为停用历史。

## 写路径与下游

以下路径必须同时维护 Scene Track：人工创建/修改/删除、AI 采纳、传播、插值、跨帧任务、标注导入、拆分与合并。`scene_track_domain.py` 集中处理解析、绑定、区间扩展、revision 和诊断，业务路径不应自行拼接 Track 记录。

- Scene 时间轴从存在区间判断 selected Track 是否出现；成员标记单独显示关键帧、派生帧和样本帧。
- Data Manager 使用 Track 类别和区间计算首尾帧、缺失帧与质量问题。
- AAP JSON 在顶层 `scene_tracks[]` 保存身份、`presence_mode`、轨迹属性和多段区间；逐 task Annotation 保存 `track_id` 与 `temporal_role`。
- 质量规则可以引用 Track revision 与命令 locator，但不能绕过预览直接改真值。

图片和 compact video track 继续使用现有存储合同，不会被自动迁入 Scene Track。跨 Scene 身份、多相机人工 2D 成员和完整 OpenLABEL 交换是建立在该领域之上的独立能力。

## 代码入口

| 位置                                                                   | 作用                            |
| ---------------------------------------------------------------------- | ------------------------------- |
| `apps/api/app/db/models/scene_track.py`                                | Track、区间和操作账本模型       |
| `apps/api/app/services/scene_track_domain.py`                          | 双写、revision、区间扩展与诊断  |
| `apps/api/app/services/scene_track_command.py`                         | 预览、执行、幂等 journal 与撤销 |
| `apps/api/app/api/v1/tasks/track_operations.py`                        | 详情、诊断和生命周期 API        |
| `apps/web/src/pages/Workbench/stages/three-d/TrackOperationsPanel.tsx` | 跨帧任务中心生命周期界面        |

架构决策与跨平台依据分别记录在仓库的 `docs/adr/archive/0069-scene-track-domain-and-lifecycle.md` 和 `docs/research/24-3d-temporal-object-lifecycle.md`。

## 相关文档

- [Scene 与帧索引](./scene-and-frame-index)
- [标注模块 · 跨帧 propagate 与插值](./annotation-module#跨帧-propagate-与插值3d-时序)
- [点云数据模型与多模态校准](../reference/point-cloud-data-model)
