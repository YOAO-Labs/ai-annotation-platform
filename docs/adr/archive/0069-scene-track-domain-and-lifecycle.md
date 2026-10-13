# 0069 — Scene Track 作为 3D 时序对象生命周期权威模型

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** core team
- **Extends:** ADR-0035、ADR-0045、ADR-0047

## Context

ADR-0045 把 `Annotation.track_id` 提升为几何类型无关的跨帧身份键，解决了 `group_id` 与 geometry 内
`track_id` 分裂的问题。它仍把轨迹表示成按字符串临时聚合的一组逐帧 Annotation，无法持久化以下状态：

- 对象暂时缺席后恢复形成的多个存在区间。
- 人工关键帧、系统派生成员与无法判断角色的存量成员。
- 轨迹级类别、属性和并发 revision。
- split、merge、缺席、恢复、终止和撤销的一致命令历史。
- 质量 Issue 与多模态成员可以稳定引用的对象实体。

如果直接把“终止轨迹”实现成停用尾段 Annotation，成员缺口会同时表示缺席、漏标、未物化和删除，后续
3D Quality、导出、多模态成员与撤销会各自推断生命周期。

## Decision

建立 Scene 级轨迹领域，由 `SceneTrack`、`SceneTrackInterval`、Annotation member 和
`SceneTrackOperation` 组成：

```text
SceneTrack(project, scene, track_id, class, revision)
  ├─ SceneTrackInterval [0..N]
  └─ Annotation [0..N] via scene_track_id
       └─ track_id + temporal_role + per-frame geometry/version

SceneTrackOperation
  └─ preview snapshot → atomic mutation → reversible journal
```

### 身份与成员

- `scene_tracks.id` 是服务端和关系表使用的权威实体键。
- `(project_id, scene_id, track_id)` 唯一；`track_id` 继续作为稳定外部键和旧客户端查询合同。
- `annotations.scene_track_id` 是新 3D Scene 成员关系的权威外键，删除 Track 时 `SET NULL`，避免级联删除
  Annotation 真值。
- 新 3D Scene `box_3d` 成员必须同时写 `scene_track_id` 与匹配的 `track_id`。图片、compact video track
  和迁移 verifier 拒绝的异常链可以保持 `scene_track_id=NULL`。
- Track 是身份、类别、轨迹级属性和 revision 的权威来源；Annotation 是逐帧 geometry、来源、成员属性和
  version 的权威来源。

### 存在区间

- Track 以 `presence_mode=inferred|explicit` 区分保守的成员包络与已确认的生命周期控制面。
- `inferred` 始终是单一包络，新成员可扩展边界；成员缺口不表示缺席。
- lifecycle 命令和携带 `scene_tracks[]` 的导入把模式切换为 `explicit`；此后缺席帧写入必须先执行 resume，不能由传播静默延长。
- 区间使用闭区间 `[start_frame, end_frame]`，允许 `end_frame=NULL` 表示开放尾段。
- 同一 Track 的区间不得重叠或相邻；相邻区间必须归一化合并。
- PostgreSQL exclusion constraint 阻止重叠，service 在同一事务内锁 Track 并处理相邻区间归一化。
- Annotation 缺口不自动表示缺席。历史合法链只回填一个
  `[min(member_frame), max(member_frame)]` 的 `legacy_envelope`。

### 时间角色

`annotations.temporal_role` 与 `source` 正交：

- `keyframe`：用户明确创建、修改或确认的锚点。
- `derived`：插值、传播或跟踪生成的可重算成员。
- `sample`：存量或导入数据无法可靠判断关键帧语义。

迁移只把 `source=interpolated` 回填为 `derived`，其它存量成员回填为 `sample`。不能因为
`source=manual` 就推断它是关键帧。

### 命令和撤销

- split、merge、mark_absent、resume、terminate 与 revert 共用 Scene 级命令合同。
- 每个命令先预览并冻结 Track revision、interval version 与 Annotation version，再按确定顺序加锁执行。
- `scene_track_operations` 保存幂等键、请求摘要、执行前后状态、inverse payload、结果与撤销关联。
- 撤销本身是新 operation。目标 operation 后相关 Track 或成员发生变化时返回 stale，不做部分恢复。
- `AnnotationOperation` 保持 Task 级 Mask/转换账本，不扩充为 Scene Track journal。

### 迁移与发布

- 先进行预检和保守回填，再进入全写路径双写与影子读对账。
- 异常链保留旧查询路径，但禁止新生命周期命令；迁移不能猜测修复类别漂移、重复帧或跨 Scene 污染。
- Scene 时间轴、Data Manager、可信导出和后续 3D Quality 必须改读存在区间后，才能开放终止命令。
- 普通用户入口可以通过功能开关回滚；已提交 Track operation 和 interval 只读保留，不能删表回滚真值。

## Rejected alternatives

### 继续只按 `Annotation.track_id` 临时聚合

迁移面较小，但无法表达存在区间、轨迹 revision、可逆命令或稳定外键。每个下游会重复推断相同状态，拒绝。

### 把逐帧 Annotation 全部改成稀疏 path

关键帧模型更紧凑，但会同时重写权限、锁、历史、Data Manager、导出和协作合同。保留 Annotation 物化读模型，
以 SceneTrack 作为控制面，可以渐进迁移，拒绝全量替换。

### 复用 `AnnotationOperation`

现有表以 Task 为作用域，kind 与 lineage 严格面向 Mask/转换。Scene 命令跨多个 Task，强行复用会破坏作用域、
幂等唯一键和撤销语义，拒绝。

## Consequences

正向：

- 缺席、恢复、终止、拆分、合并和撤销使用同一生命周期模型。
- 质量、导出、多模态成员和时间轴能够引用稳定 Track revision 与存在区间。
- `track_id` 兼容接口保留，旧客户端与已有导出可以渐进迁移。

负向：

- 新增三张表、两个 Annotation 列以及 PostgreSQL `btree_gist` 扩展。
- 所有能创建或改变 3D Track 成员的写路径都必须双写；漏掉任何路径都会形成漂移。
- 删除或移动 Scene、项目数据时需要额外验证成员外键与审计保留策略。

## Verification

- 迁移前后 Annotation 数、geometry hash、`track_id`、source、active 与 version 一致。
- 随机命令序列持续满足区间有序、不重叠、不相邻、revision 单调和精确撤销。
- 影子读对账在 dev nuScenes mini 合法链上无差异，异常链只产生稳定诊断 code。
- 浏览器覆盖出现、缺席、恢复、终止、撤销、拆分和合并，并在刷新后保持一致。

## References

- 实施计划：`docs/plans/archive/2026-08-25-v0.24.11-3d-track-domain-foundation.md`
- 调研：`docs/research/24-3d-temporal-object-lifecycle.md`
- [ADR-0035](0035-scene-and-frame-foundation.md)
- [ADR-0045](0045-track-id-as-annotation-column.md)
- [ADR-0047](0047-data-manager-entity-read-model.md)
