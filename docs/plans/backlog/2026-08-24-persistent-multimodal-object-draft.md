# 持久化 2D / 3D 多模态对象计划草案

> Status: partially superseded research-draft
>
> Implementation authorization: no
>
> Version: unassigned；本草案不占版本号
>
> Finalization: 实施前必须执行 [`backlog/README.md`](README.md) 的“转定稿门”并先批准数据真值 ADR
>
> Superseded scope: 相机 bbox 成员、sensor context、标定 revision、投影残差与 KITTI lineage
> 已由 [多相机持久成员计划](../archive/2026-08-26-v0.24.15-persistent-multicamera-members.md) 实施。
> 本草案只保留相机 polygon / mask 等尚未排期的后续研究边界。

## 1. 推荐结论

推荐让同一 scene 对象的人工 3D cuboid、点掩码和各相机人工 2D 几何继续作为**各自可版本化的 Annotation 成员**，共同挂到 [`SceneTrack`](../archive/2026-08-25-v0.24.11-3d-track-domain-foundation.md)；现有 `track_id` 保留为稳定外部键和成员查询合同，另用显式 sensor context 记录成员属于哪一个 dataset item / camera role / calibration revision。

人工 3D 与人工 2D 成员是并列真值。3D 实时投影只是关联证据，绝不自动覆盖人工 2D；人工 2D 编辑也不在欠约束情况下自动改 3D。冲突通过残差、issue 和用户决定解决。

## 2. 历史基线与当前差异

以下条目记录草案形成时的历史基线，不能再作为当前实现说明。当前系统已经持久化相机 bbox
成员，并以 SceneTrack、sensor context、标定 revision 和投影残差维护 2D / 3D 真值关系；
后续转定稿只允许评估 polygon / mask 等未覆盖几何，不得重复建设 bbox 合同。

- ADR-0033 明确当前相机 overlay 是由 `box_3d` 实时投影的只读可视化，不持久化 2D 结果。
- 相机图种 3D 框当前只保存 `box_3d`，拖出的 2D 矩形是一次性提示。
- ADR-0045 已把 `track_id` 定为与 geometry 类型无关的跨帧对象身份；轨迹领域草案进一步让 SceneTrack 承担对象级权威状态，并保留该键作为兼容接口。
- task 与 dataset item 已有多关联和 camera role，calibration 存在于 sensor metadata；Annotation 本身尚未明确绑定某个相机成员。

## 3. 权威关系

```text
SceneTrack = Scene 内同一对象的权威身份与生命周期
track_id = 稳定外部键与 Annotation 成员查询键
  ├─ Annotation(box_3d, primary_lidar context, own revision)
  ├─ Annotation(point_mask_3d, primary_lidar context, own revision)
  ├─ Annotation(bbox / raster mask, camera_front context, own revision)
  ├─ Annotation(bbox / raster mask, camera_left context, own revision)
  └─ derived projection evidence(calibration revision, ephemeral/cacheable)
```

推荐不新建第二个 `object_id` 与 `track_id` 或 SceneTrack 竞争。多模态成员直接关联 SceneTrack，`track_id` 继续冗余在 Annotation 上以兼容现有查询与导出。若实施时发现现有作用域无法表达同帧非时序对象，转定稿 ADR 必须先调整合同，而不是静默增加另一层字符串关联。

sensor context 推荐使用受约束的关系记录，而不是把 `camera_role`、dataset item id 和 calibration digest 随意塞进 attributes。具体表 / 列由 ADR 根据届时 Annotation 模型决定，但必须保证：annotation member 唯一、sensor item 属于同一 task / scene、role 合法、calibration revision 可审计。

## 4. 编辑与冲突合同

- 从相机图创建持久 2D 成员时，用户明确选择“只作为 3D 种子”或“保留为对象成员”；默认保持当前快速种子，避免无意增加真值。
- 修改 3D 后实时显示新投影和人工 2D 的残差，不自动改人工 2D。
- 修改人工 2D 后只更新该 member；可以请求“用此证据调整 3D”的候选操作，接受前不写 3D。
- calibration revision 改变后重新计算派生投影，人工 member 坐标不变并标记 relationship stale，等待复核。
- 删除一个 member 不删除整个对象；删除最后一个 member 时由 SceneTrack lifecycle command 决定保留、终止或退役，不能从成员计数隐式推导。
- 跨帧传播必须显式说明传播 3D、某相机 2D 或全部成员，不能只凭共享 `track_id` 批量覆盖。

## 5. 范围

- 多模态对象 ADR：身份作用域、member 真值、sensor context、revision、冲突和删除语义。
- 相机 bbox 作为第一种持久 2D member；mask / polygon 在合同稳定后分切片加入。
- 3D 工作台对象定义 / 成员列表、显隐、选择、来源和残差提示。
- 创建、编辑、删除、标定更新和导出的 lineage / audit。
- 质量流程中的投影残差规则，以及 COCO / KITTI / 原生格式对成员与派生结果的明确选择。

## 6. 非范围

- 不把所有 2D 投影结果批量落库，不为每帧每相机制造冗余 annotation。
- 不用单目 bbox 自动求解唯一 3D 尺寸和深度。
- 不让最后写入者无条件覆盖另一模态人工真值。
- 不在第一版持久化任意 camera polygon、brush、关键点和 3D mesh 的笛卡尔积。
- 不把多模态对象扩展成通用 ontology / entity graph 或具身 episode 模型。

## 7. 推荐实现切片（转定稿后执行）

1. **ADR 与只读聚合**：以 SceneTrack 为对象身份，冻结 member / sensor context，先把现有 3D member 和派生投影聚合成对象视图，不新增 2D 写入。
2. **单相机 bbox member**：显式保留、独立编辑、revision / audit、与 3D 残差，不做反向自动求解。
3. **多相机与标定更新**：成员可见性、occlusion、relationship stale 和批量复核。
4. **跨帧与导出**：明确 scope，补原生 / COCO / KITTI lineage；通过后再评估 mask / polygon。

## 8. 验收方向

- 同一 track 的 3D 与多个相机 member 可独立编辑、撤销和审计，任何一次修改只改变目标 member。
- UI 能区分人工 2D、人工 3D、模型候选和派生投影，不能用同一种实线让用户误判真值。
- calibration 更新不篡改人工坐标；旧关系明确 stale，新投影可复算并定位差异。
- API 拒绝跨 task / scene、非法 camera role、错误 dataset item 或不可见 member 的关联。
- 删除 / 恢复 member、track propagation、export 和 concurrent version conflict 都有集成测试。
- 没有持久 2D member 的项目继续保持当前实时投影和快速种框，不产生迁移负担。

## 9. 风险与回滚

- 最大风险是真值不清：若 UI 不区分 member 与 projection，用户会把派生框当人工标注。视觉与数据来源必须同时表达。
- SceneTrack 同时承接时序和跨模态对象身份，需要严格作用域、member 唯一性与传感器关系约束；ADR 和约束优先于 UI。
- 回滚可停止新 member 创建并把已存在 member 只读展示 / 原生导出；不能删除人工 2D 真值。派生投影继续按旧路径工作。

## 10. 转定稿专项检查

- 重新审计 Annotation schema、SceneTrack / `track_id` 约束、task-dataset-item link、camera calibration revision 与所有导出器。
- 先写 ADR 比较“Annotation + SceneTrack 外键 + sensor context”“独立 multimodal member 表”“SceneTrack member 关系表”至少三种方案，并以真值所有权、约束完整性和可迁移性作选择。
- 用真实多相机任务演练创建、标定更新、成员冲突、跨帧传播和删除，冻结每一步真值所有权。
- 精确列出模型 / 迁移 / API / UI / projection / quality / export / tests / docs 的文件与兼容策略，确认大变更面后再批准。
