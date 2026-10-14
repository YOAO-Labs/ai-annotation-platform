# 高清母版录制 backlog

新增选题见[近期能力高清母版计划](../../docs/plans/1789230200_new-capability-marketing-masters.md)：
选题的注册状态以 `marketing-assets.ts` 为准，是否已录制和通过质量验收以归档 manifest 为准，不再将整批统一视作待录。
下方五批保留已录制历史，“已录”不代表当前源码复核或云端归档已经完成。
不依赖 ML 的素材已通过正式 Mac 原生路径重录。屏幕采集源为 2880×1620，母版按既定编码规范归一化为 3840×2160 / 60fps；这不代表物理原生 4K 采集或渲染器性能认证。有效独立帧率、硬件合成与来源记录门槛保持不变；ML 相关素材保留原记录，本轮不重新认证。

真正开始实现某项时，先把规格写入
`apps/web/e2e/screenshots/_helpers/marketing-assets.ts`，再增加一个只表达该目标的独立 flow；
不得用无意义停顿、慢放或混入其他功能来满足时长。

每项完成后需要同时满足：4K UHD / 60fps 门禁通过、运行 manifest 含完整分镜与来源提交、
文档派生 MP4 已生成、对应文档引用已更新、媒体审计不报告损坏或过期。

## 第一批：当前题 AI 完整链路

| 优先级 | 资产 ID                         | 完整链路与验收重点                                                            | 建议时长 | 文档落点                                | 状态 |
| ------ | ------------------------------- | ----------------------------------------------------------------------------- | -------- | --------------------------------------- | ---- |
| P0     | `current-task-image-inference`  | 打开当前题 AI → 选择项目编排 → 运行 → 查看候选几何与置信度 → 采纳 → 标注落库  | 15–24 秒 | `ai/current-task-inference`             | 已录 |
| P0     | `current-frame-video-inference` | 定位有目标帧 → 运行当前帧推理 → 查看帧级候选 → 采纳 → 切换相邻帧确认作用范围  | 16–26 秒 | `ai/current-task-inference`、视频工作台 | 已录 |
| P0     | `secondary-inference-attribute` | 选中已有目标 → 发起二次推理 → 返回属性/文本 → 人工修正 → 保存到同一标注       | 14–24 秒 | `ai/current-task-inference`             | 已录 |
| P0     | `candidate-review-lifecycle`    | 连续审阅至少三个候选 → 分别采纳、驳回和跳过 → 焦点自动前进 → 展示最终候选计数 | 16–28 秒 | `ai/candidate-review`                   | 已录 |

## 第二批：视频 AI 追踪与修正

| 优先级 | 资产 ID                           | 完整链路与验收重点                                                              | 建议时长 | 文档落点                    | 状态 |
| ------ | --------------------------------- | ------------------------------------------------------------------------------- | -------- | --------------------------- | ---- |
| P0     | `video-tracker-text-discovery`    | 输入明确目标文本 → 发起无源发现 → 跨帧查看候选 → 采纳为新轨迹                   | 18–30 秒 | `workbench/video-propagate` | 已录 |
| P0     | `video-tracker-combo-discovery`   | 输入文本 → 运行“发现 + 逐对象记忆”组合模型 → 跨窗口核对稳定身份 → 筛选并采纳    | 30–50 秒 | `workbench/video-propagate` | 已录 |
| P0     | `video-mask-correction-propagate` | 查看错误边界 → 在后续帧添加正负笔迹修正 → 从修正帧重新传播 → 核对并保存更新轨迹 | 20–36 秒 | `workbench/mask-brush`      | 已录 |
| P0     | `video-track-batch-propagate`     | 多选两条真实轨迹 → 一次批量延展 → 跨帧复核双目标 → 保留人工种子并回填原轨迹     | 18–45 秒 | `workbench/video-propagate` | 已录 |

## 第三批：项目编排、运维与大数据能力

| 优先级 | 资产 ID                         | 完整链路与验收重点                                                           | 建议时长 | 文档落点                            | 状态 |
| ------ | ------------------------------- | ---------------------------------------------------------------------------- | -------- | ----------------------------------- | ---- |
| P1     | `pipeline-template-create`      | 新建编排 → 添加两个有因果关系的阶段 → 配置模型 → 校验并保存                  | 18–30 秒 | 项目 AI 编排                        | 已录 |
| P1     | `pipeline-apply-project`        | 选择已保存编排 → 绑定项目 → 发起预标 → 查看阶段进度与最终候选                | 18–32 秒 | `projects/ai-preannotate`           | 已录 |
| P1     | `jobs-retry-recovery`           | 打开失败作业 → 查看错误摘要 → 重试 → 新作业完成 → 进入工作台审阅结果         | 16–45 秒 | 工作流失败恢复                      | 已录 |
| P1     | `pointcloud-camera-seed-3d-box` | 在相机视图给出二维提示 → 视锥选点拟合真实 3D 框 → 三视图核对 → 相机重投影    | 16–36 秒 | `workbench/pointcloud-projection`   | 已录 |
| P1     | `pointcloud-crossframe-track`   | 已复核 3D 框 → 延续两个相邻帧 → 中间帧修正 → 核对同一 `track_id` 与邻帧参考  | 22–40 秒 | `workbench/pointcloud-crossframe`   | 已录 |
| P1     | `large-image-pyramid-recovery`  | 缩放至高清切片 → 模拟单切片失败 → 自动重试恢复 → 继续平移并保持细节          | 16–28 秒 | `workbench/index`                   | 已录 |
| P1     | `model-market-runtime-pool`     | 打开服务池 → 展开实例 → 查看路由状态、并发与资源观测 → 切换实例详情          | 15–26 秒 | 超级管理员模型市场                  | 已录 |
| P1     | `project-ml-routing`            | 启用 YOLO 并设为批量主后端 → 当前题 AI 命中 YOLO → Smart Point 自动命中 SAM3 | 18–32 秒 | 项目后端配置、当前题 AI             | 已录 |
| P1     | `background-export-download`    | 发起大批量导出 → 查看后台任务进度 → 完成后下载 → 展示导出格式与文件          | 16–30 秒 | `datasets/prediction-import-export` | 已录 |

## 第四批：项目管理与团队配置

| 优先级 | 资产 ID                                | 完整链路与验收重点                                                           | 建议时长 | 文档落点                    | 状态 |
| ------ | -------------------------------------- | ---------------------------------------------------------------------------- | -------- | --------------------------- | ---- |
| P1     | `project-create-existing-resources`    | 新建图片项目 → 配置车辆类别 → 关联已有数据集 → 选择标注员和审核员 → 核对摘要 | 14–32 秒 | `projects/index`            | 已录 |
| P1     | `model-market-runtime-partial-failure` | 4/5 来源新鲜 → 展开部分失败告警 → 比较 fresh / stale 来源 → 核对超时与退避   | 9–20 秒  | `superadmin/model-market`   | 已录 |
| P1     | `large-image-mask-limit`               | 缩放 Cosmic Cliffs → 落库矢量框 → 悬停 Mask 入口核对尺寸上限                 | 10–24 秒 | `workbench/mask-brush`      | 已录 |
| P1     | `model-market-gpu-resource-overview`   | 切换 GPU 资源 → 对比静态 / 运行时预算 → 展开 blocker 卡与受影响实例          | 10–22 秒 | `superadmin/model-market`   | 已录 |
| P1     | `platform-overview`                    | 平台 KPI / 分布 → 注册趋势 → ML 成本 → 审计活动 → 全平台项目                 | 16–30 秒 | `superadmin/index`          | 已录 |
| P1     | `project-actions-menu`                 | 项目行 → 更多操作 → 导出标注 / 复制配置 / 导入预测三个入口                   | 9–20 秒  | `projects/index`            | 已录 |
| P1     | `jobs-bell-active`                     | 顶栏角标 → 预标 / 导入进度与取消 → 已完成导出摘要与下载                      | 10–22 秒 | `projects/ai-preannotate`   | 已录 |
| P1     | `video-tracker-job-states`             | 四状态同屏 → 项目 / 待审筛选 → 核对帧范围与方向 → 返回视频工作台             | 12–26 秒 | `workbench/video-propagate` | 已录 |

## 第五批：现有文档动态能力补录

| 优先级 | 资产 ID                                | 完整链路与验收重点                                                      | 建议时长 | 文档落点                    | 状态 |
| ------ | -------------------------------------- | ----------------------------------------------------------------------- | -------- | --------------------------- | ---- |
| P1     | `model-market-video-pool`              | 视频服务池 → SAM 3 实例 → 独立池容量 / 会话 / GPU → 视频权重与预热入口  | 12–28 秒 | `superadmin/model-market`   | 已录 |
| P1     | `video-timeline-prediction-navigation` | 展开预测密度轨 → 连续跳转预测帧 → 核对画布候选与时间轴位置              | 12–24 秒 | `workbench/video-playback`  | 已录 |
| P1     | `video-propagate-track-vs-copy`        | 同一真实轨迹分别展示 AI 延展与纯几何复制 → 对比作业、结果与适用边界     | 18–36 秒 | `workbench/video-propagate` | 已录 |
| P1     | `pointcloud-billboard-label`           | 启用 3D 标签内容 → 绕框旋转视角 → 核对文字持续正对相机并保留类别 / 属性 | 14–26 秒 | `workbench/3d-box`          | 已录 |
| P1     | `storage-connector-create-test`        | 新建脱敏 S3 / OSS 连接器 → 测试连接 → 展示成功状态与样本计数 → 精确清理 | 14–28 秒 | `datasets/index`            | 已录 |

## 录制模板预览

以下 GIF 从已审阅的非 ML 原始采集源派生，供维护者检查模板动作与结果。它们不是高清母版，也不替换使用手册中的 MP4 或恢复已退休的 GIF 手册引用。原始采集源与高清母版保存在私有 `.artifacts/marketing/` 归档，文件哈希、动作窗口与编码参数见生成来源清单。

| 模板动作            | 预览                                                                              |
| ------------------- | --------------------------------------------------------------------------------- |
| 矩形绘制与提交      | ![矩形绘制模板](../user-guide/images/bbox/draw-in-progress.gif)                   |
| Mask 笔刷绘制与提交 | ![Mask 笔刷模板](../user-guide/images/mask-brush/draw-in-progress.gif)            |
| 多边形闭合与提交    | ![多边形绘制模板](../user-guide/images/polygon/draw-in-progress.gif)              |
| 折线绘制与提交      | ![折线绘制模板](../user-guide/images/polyline/draw-in-progress.gif)               |
| 批次批量操作        | ![批次操作模板](../user-guide/images/projects/batch-bulk-actions.gif)             |
| 质检退回与队列更新  | ![质检退回模板](../user-guide/images/review/reject-flow.gif)                      |
| 章节笔刷创建        | ![章节创建模板](../user-guide/images/video-timeline/brush-create-chapter.gif)     |
| 章节边界调整        | ![章节调整模板](../user-guide/images/video-timeline/chapter-resize-hover.gif)     |
| 时间轴水平缩放      | ![时间轴缩放模板](../user-guide/images/video-timeline/horizontal-zoom.gif)        |
| 快捷键帮助          | ![快捷键模板](../user-guide/images/workbench/hotkey-cheatsheet.gif)               |
| 旋转矩形            | ![旋转矩形模板](../user-guide/images/workbench/rotated-bbox.gif)                  |
| 视频相邻帧携带      | ![相邻帧携带模板](../user-guide/images/workbench/video-track-carryover-ghost.gif) |
| 视频帧绘制与提交    | ![视频绘制模板](../user-guide/images/workbench/video-track-trajectory.gif)        |

## 版本与复核

- 录制来源版本、dirty 状态、种子修订和媒体哈希由 manifest 自动记录，不在表格中手填。
- 人工复核只通过 `pnpm docs:media:approve -- --asset <仓库相对路径>` 写入
  `media-reviews.json`；全部素材都已逐项目视检查后可改用 `--all`，命令要求工作区干净。
- 每周巡检只生成报告，不自动重录或覆盖母版；Tier A 缺口、源码变更或超过复核周期后进入下一轮计划。
