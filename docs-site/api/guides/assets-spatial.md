---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-20
---

# 场景、位姿与标定

场景、素材帧序号、位姿与标定由 Assets 持有，产品在调用方会话使用既有 [Catalog 原生能力](./assets-catalog)。公开 API 保留认证、scope、权限政策和原有响应；任务、相机链接、标注与轨迹的组合查询仍由 Labeling 持有。事务说明见[同库数据接口](./module-data)。

## 公开接口

| 操作                          | 行为                                                                                                        |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Scene 列表、详情和 trajectory | 保留原响应；不存在的 Dataset 列表为空，不存在的 Scene 返回 404                                              |
| Scene PATCH                   | 省略字段或 `null` 不修改，`source_metadata: {}` 清空来源元数据；同一 Dataset 重名返回 409，其他字段也不提交 |
| Dataset 场景回填              | 支持 `single`、`per_subdirectory`、`auto` 和 dry-run；已有 Scene 或分配保持幂等                             |
| 坐标系推断                    | 保留链接相机优先、前相机优先、投票及稳定排序                                                                |
| 任务相机标定历史与 PATCH      | 保留当前可见性、管理与跨项目共享检查；更新必须携带期望 revision 和 digest                                   |

Scene PATCH、场景回填和标定 PATCH 可附带 UUID `Idempotency-Key`。响应中断时，用相同键和相同请求确认结果；新操作使用新键。复用键但修改目标或内容返回冲突，不会自动重试写请求。标定版本冲突时先读取当前版本，再决定是否重新编辑。

## 内部合同

协议为 `aap-assets-spatial/v1`。完整字段、错误码和二十二项原生签名位于：

- `apps/assets-catalog/src/yoao_assets_catalog/spatial_contract.schema.json`
- `apps/api/alembic/sql/0216_assets_spatial.schema.json`

两份 schema 由同一 wire model 生成。内部命令只接受既有服务 Bearer 凭据；普通公开请求不能调用无 actor 的运维写入。消费者不传角色、权限布尔值、任意存储 origin 或 bucket。

| 范围                       | 原生能力                                                                                                                                                                                                                              |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 当前身份、Scene 和素材事实 | `guard_spatial_actor`、`list_spatial_scenes`、`get_spatial_scene`、`read_spatial_layout`、`read_spatial_items`                                                                                                                        |
| 位姿和标定读取             | `read_spatial_poses`、按精确帧集合读取的 `read_spatial_pose_pairs`、`read_spatial_calibrations`、`lock_spatial_calibration_facts`、`read_spatial_camera_target`                                                                       |
| Scene 与布局写入           | `mutate_spatial_scene`、`read_spatial_layout_receipt`、`apply_spatial_layout`、`create_spatial_scene`、`assign_spatial_items`、`set_spatial_scene_source`、`upsert_spatial_poses`、运维 Dataset 坐标系设置 `set_spatial_dataset_axis` |
| 标定写入                   | `update_spatial_calibration`、`record_spatial_calibration_provenance`                                                                                                                                                                 |
| 已受理点云建题准备         | `read_pointcloud_preparation`、`apply_pointcloud_preparation`                                                                                                                                                                         |

读取按游标分页，每页最多 500 条。每页携带原范围、原游标、下一游标、完整来源快照摘要、总数及完成标志。消费方必须确认范围和快照一致、记录有序且不重复、游标持续前进、完成总数相符，才交付完整结果。公开 REST 继续返回原整体结构，不按新上限截断长轨迹或列表。单条记录和每次内部 JSON 请求/响应仍受既有字节预算保护。

快照摘要由原生所有者计算并视为不透明值。标定 digest 则保留历史算法：先经 `SensorCalibration` 归一化，使用排序、紧凑 JSON 编码并计算 SHA-256；不得改用 PostgreSQL JSONB 文本或消费者自行实现的新规则。持久修订的历史摘要保持不透明，因为 JSONB 会归一化负零，读回值重算可能改变版本身份。没有持久修订的合法旧标定仍以虚拟 revision 1 返回，`created_at` 为 null。

## 提交和撤销

Scene、布局、位姿和标定写入与不可变操作回执共同提交。标定更新还在同一事务内追加历史修订、合并当前元数据并记录审计；任一步失败均回滚。分配 Item 时重新检查目标 Dataset 与 Scene 归属，来源或布局快照变化返回冲突。

相机成员标注仍由 Labeling 检查当前标注权限、任务可见性和编辑状态。读取并锁定标定事实时只复核当前账号、相机链接和来源快照，不要求素材标定的管理权限；跨项目共享素材也可以用于成员标注。直接修改素材标定仍检查管理权限和共享限制。

布局重放通过 `read_spatial_layout_receipt` 先检查当前管理权限并读取原结果，命中后不再根据后来变化的素材重新规划。布局和 Item 分页只携带本页素材引用的 Scene；完整 Scene 清单使用独立的 Scene 分页读取。

后台点云准备沿用首次受理的执行权，逐次检查关联代次、操作和 Job 状态。原发起者停用或失去项目管理权不撤回已受理操作；解绑或显式取消会阻止后续未提交结果。读取标定文件时使用固定 datasets 桶、有限字节读取及 S3 条件版本校验，完成外部读取后才获取最终 Source 写锁并重新检查状态。人工修订不会被导入结果覆盖。

关系型过滤、计数与排序使用 Assets 发布的 `spatial_item_references` 和 `spatial_scene_references` 两个只读投影，避免复制完整素材元数据。校准缺失统计保留 JSONB 键存在判断，不把非法但存在的旧值改成缺失。

正常读写和点云来源准备使用同一个内嵌 owner。普通点云建题先提交已受理操作，然后在既有准备事务中读取来源并发布；nuScenes 原子导入仍使用调用方同一 SQL 连接组合准备、建题和 provenance。导出按当前块的精确 Scene/帧对一次读取位姿，使用同一原生快照；缺失位姿保持缺失，不重复加载完整轨迹。

本地和远程单页响应均受 16 MiB 预算约束，完整轨迹等聚合结果可跨页超过该值。内部同事务导入直接传递原生计划，不套用 HTTP 的 1 MiB 请求体上限。标定提交返回更新后的来源快照；校验绑定同一素材身份，允许修订号、摘要和更新时间变化。

nuScenes 导入的标定 provenance 只填充缺失摘要，保留已有摘要和其他元数据。它与后续导出漂移检查使用同一标定身份，不提供任意元数据修改入口。

## 部署与回退

按正常迁移和普通运行 LOGIN 授权顺序刷新 API，不再启动目录监听器。独立 Catalog 工件仍精确校验 29 项能力及专属只读 S3 身份，仅用于独立资格验证。

标定读取使用固定 datasets 桶和条件读，既不从业务请求选取存储 origin，也不持有跨 I/O 的空间写锁。独立工件可分别配置 `ASSETS_CATALOG_S3_ENDPOINT`（内部读取）和 `ASSETS_CATALOG_S3_PUBLIC_ENDPOINT`（浏览器签名）。

回退应用时保留原生 SQL 与操作回执；回退迁移前停止相关写入，确认不再需要已保存的重放依据。
