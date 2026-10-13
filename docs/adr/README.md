# Architecture Decision Records

本目录记录关键架构决策，采用 [Michael Nygard 模板](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)。

## 何时写一份 ADR

满足以下任一即应写：

- 选了一个会影响后续 6 个月以上代码结构的方案
- 在两个以上方案间纠结，最终选了不那么显然的一个
- 引入了新的核心库 / 框架
- 改了某个跨多模块的约定（命名、层次、契约）

## 何时**不**写

- 单个 bug 修复 → CHANGELOG 即可
- 临时方案、技术债 → 在代码里加 TODO + issue 链接
- 可被 git log 解释清楚的小重构

## 命名

`NNNN-short-kebab-title.md`，编号自增，**不**复用、**不**删除。

如果决策被推翻，新建一份 ADR，把旧的状态改为 `Superseded by ADR-XXXX`，但**保留旧文档**。

## 模板

见 [`TEMPLATE.md`](TEMPLATE.md)。复制为 `NNNN-short-kebab-title.md`，填入元数据与各章节即可。

规范化要点（所有 ADR 应满足）：

- 标题：`# NNNN — 简短中文标题`（em-dash，不写 `ADR-NNNN:` 前缀）
- 元数据块（紧跟标题，列表形式）：`Status` / `Date` / `Deciders` / `Supersedes` 四项必填
- 章节顺序：`Context` → `Decision` → `Consequences`（正向 / 负向）→ `Alternatives Considered`（可选）→ `Notes`（可选）
- 引用代码：`path/to/file.py:NN`，便于跳转

## 索引

> Accepted 状态的 ADR 归档到 [`archive/`](archive/) 子目录（不进 docs-site sidebar，但编号连续性仍受 `check-adr.mjs` 校验）。当前主目录只保留 Proposed / 待决 ADR。

### 待决（Proposed / Draft）

- [0009](0009-task-events-table-and-partition.md) — task_events 表与按月分区方案
- [0025](0025-webhook-event-envelope-versioning.md) — Webhook 事件信封与版本化（草案，未实现）
- [0034](0034-lidar-axis-convention.md) — 点云数据集 lidar 坐标系约定：dataset 级声明 + 加载侧归一化
- [0075](0075-platform-identity-vs-project-authority.md) — 平台身份与项目职责分离：员工按项目成员角色授权（本地实现中，未部署）

### 已采纳（Accepted，归档）

- [0001](archive/0001-record-architecture-decisions.md) — Record architecture decisions
- [0002](archive/0002-backend-stack-fastapi-sqlalchemy-alembic.md) — 后端选型：FastAPI + SQLAlchemy 2.0 async + Alembic
- [0003](archive/0003-openapi-client-codegen.md) — 前端 OpenAPI 客户端生成方案：@hey-api/openapi-ts
- [0004](archive/0004-canvas-stack-konva.md) — 标注画布引擎：Konva（4 Layer 架构）
- [0005](archive/0005-task-lock-and-review-matrix.md) — 任务锁（5min TTL）与审核流转角色矩阵
- [0006](archive/0006-predictions-partition-by-month.md) — predictions 表按月 RANGE 分区
- [0007](archive/0007-audit-log-partitioning.md) — 审计日志月分区
- [0008](archive/0008-batch-admin-locked-status.md) — 批次 admin-locked 字段（与状态机正交）
- [0010](archive/0010-security-headers-middleware.md) — Production Security Headers Middleware
- [0011](archive/0011-websocket-token-reauth.md) — WebSocket 鉴权过期重连
- [0012](archive/0012-sam-backend-as-independent-gpu-service.md) — SAM 系列 backend 独立 GPU 服务化
- [0013](archive/0013-mask-to-polygon-server-side.md) — mask→polygon 转换在 ML backend 端做
- [0014](archive/0014-prediction-jobs-table.md) — Prediction Jobs 历史表与 Worker 三时点写入
- [0015](archive/0015-ml-backend-url-validation.md) — ML Backend URL 验证：拒绝 loopback
- [0016](archive/0016-docs-ia-redesign.md) — 文档 IA 重构：Diátaxis 框架 + audience 元数据
- [0017](archive/0017-workbench-shell-mode-and-stage-adapters.md) — 工作台 Shell 采用 Mode Hooks 与 Stage Adapters
- [0018](archive/0018-video-frame-service-segments-and-tracker-boundary.md) — 视频帧服务 Segment 与 Tracker 边界
- [0019](archive/0019-prompt-first-tooldock-1n-arch.md) — ToolDock prompt-first 1:n 架构
- [0020](archive/0020-ml-backend-capability-negotiation.md) — ML Backend capability 协商
- [0021](archive/0021-polygon-lod-and-spatial-index.md) — 多边形 LOD 与空间索引
- [0022](archive/0022-mask-editor-tool-architecture.md) — Mask 编辑器工具架构
- [0023](archive/0023-project-template-vs-clone.md) — 项目模板 vs 克隆策略
- [0024](archive/0024-aap-json-format.md) — AAP JSON v1.0 平台原生格式
- [0026](archive/0026-tool-unit-class-and-attribute-binding.md) — 类别与属性按工具单位 (tool_unit) 强隔离绑定
- [0027](archive/0027-annotation-feedback-unified-table.md) — AnnotationFeedback 统一反馈表(三段式迁移)
- [0028](archive/0028-annotations-import-semantics.md) — 标注导入语义
- [0029](archive/0029-task-dataset-item-multi-link.md) — 点云任务-数据项多文件关联中间表（保留 2D 1:1）
- [0030](archive/0030-sensor-calibration-in-dataset-item-metadata.md) — 相机标定存进 DatasetItem.metadata\_（不加列）
- [0031](archive/0031-dual-canvas-konva-three.md) — 双画布架构：Konva 2D / Three.js 3D 双栈并存
- [0032](archive/0032-3d-box-editing-main-view-gizmo.md) — 3D 框编辑交互形态：主视图 gizmo + 数值面板（推迟三正交视图）
- [0033](archive/0033-3d-to-2d-projection-overlay.md) — 3D→2D 投影联动：实时纯函数投影 + canvas overlay（不预存）
- [0035](archive/0035-scene-and-frame-foundation.md) — Scene + frame_index 跨 task 时序帧序列地基
- [0036](archive/0036-ml-backend-capability-protocol-v2-multi-model.md) — ML Backend 能力声明协议 v2（多模型目录 + infra）
- [0037](archive/0037-protocol-capability-catalog-decoupling.md) — 协议能力目录与 backend 注册解耦
- [0038](archive/0038-defer-ml-backend-base-class.md) — ML backend 基类抽象推迟到 N≥4
- [0039](archive/0039-protocol-field-name-unification.md) — Protocol field name unification with model_variants
- [0040](archive/0040-shared-annotation-visual-spec-not-stack-merge.md) — 标注视觉:统一参数规格(共享 annotationVisual.ts),不合并图片/视频渲染栈
- [0041](archive/0041-video-canvas-unify-to-konva.md) — 视频渲染栈统一到 Konva(帧合成 / 坐标模型 / 测试基建)
- [0042](archive/0042-tailwind-shadcn-design-system.md) — 前端样式体系迁移到 Tailwind v4 + shadcn/ui
- [0043](archive/0043-staged-preannotation-pipeline.md) — 多阶段预标注编排（路径 B：平台层跨 backend pipeline）
- [0044](archive/0044-global-ml-backend-registry-and-project-enablement.md) — ML Backend 全局注册表 + 项目级启用（解耦能力声明与项目绑定）
- [0045](archive/0045-track-id-as-annotation-column.md) — 跨帧对象标识 track_id 提升为 annotation 表列（统一 group_id 高位段与 geometry 内 track_id）
- [0046](archive/0046-project-pipeline-library-and-scopes.md) — 全局 Pipeline 库：持久化命名编排模板 + 三档作用域（private/organization/public，apply copy-on-write）
- [0047](archive/0047-data-manager-entity-read-model.md) — Data Manager 按 task / object / track 分 grain 查询，不复制权威写模型
- [0048](archive/0048-video-raster-mask-content-addressed-rle.md) — 视频栅格 mask 使用内容寻址 RLE 对象，不内联进 annotation JSONB
- [0049](archive/0049-cross-backend-gpu-memory-arbitration.md) — 按物理 GPU 资源进行跨 Backend 显存预算准入与驱逐
- [0050](archive/0050-ml-backend-service-pools-and-request-routing.md) — ML Backend 服务池与真实请求路由（池 / 实例双 ID + 平滑加权轮询，独立于 GPU 仲裁）
- [0051](archive/0051-model-market-observability-information-architecture.md) — 模型市场可观测性 IA（四状态轴 + 诊断去重 + 卸载安全门 + 只读字段补齐）
- [0052](archive/0052-shared-raster-mask-and-image-geometry.md) — 图片 / 视频共享内容寻址 RLE，引入图片 `raster_mask` 与静态读取合同
- [0053](archive/0053-native-mask-ai-candidate-lifecycle-and-video-correction.md) — 原生 Mask AI 瞬态候选、原子接受、视频局部纠错与可回收生命周期
- [0054](archive/0054-raster-mask-large-canvas-memory-and-tiles.md) — 图片大画布采用稀疏 tile、XOR history 与固定 Worker pool，视频 / AI 保持 4096 边界
- [0055](archive/0055-mask-quality-and-format-contracts.md) — Mask 质量闭环采用版本化账本，格式适配使用 preflight、显式损失与真实 consumer 门
- [0056](archive/0056-raster-mask-persistent-client-compute-session.md) — Raster Mask 大 ROI 使用持久客户端计算会话与可回滚 WebGPU 候选后端
- [0057](archive/0057-raster-mask-webgpu-packed-xor-contract.md) — Raster Mask WebGPU 使用 packed source 与 core XOR result，保留惰性 CPU fallback
- [0058](archive/0058-raster-mask-immutable-packed-base-cache.md) — Raster Mask Worker 使用有界 immutable packed base cache 与 word-span ROI assemble
- [0059](archive/0059-raster-mask-dense-word-scatter-over-sparse-compaction.md) — Raster Mask 保留 dense word-scatter，不采用 atomic sparse compaction
- [0060](archive/0060-default-enable-capability-gated-client-acceleration.md) — 默认启用按客户端能力安全回退的 WebCodecs 与 Raster Mask WebGPU
- [0061](archive/0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit.md) — Raster Mask 大 ROI 使用 packed CPU fallback、双预算与独立 WebGPU 熔断
- [0062](archive/0062-immutable-image-pyramid-assets.md) — 超大图采用不可变代次金字塔与批量鉴权交付
- [0063](archive/0063-konva-viewport-image-tiles.md) — 超大图客户端采用视口 LOD Tile 与解码字节 LRU
- [0064](archive/0064-task-scoped-raster-resource-coordination.md) — 图片工作台采用任务级栅格资源协调器
- [0065](archive/0065-keep-one-pass-raster-mask-webgpu-kernel.md) — Raster Mask WebGPU 保留 one-pass kernel，不采用可分离候选
- [0066](archive/0066-video-segment-fragments-and-canonical-tracks.md) — 视频协同以 Segment Fragment 为写入真相，完整轨迹按边界决策派生
- [0067](archive/0067-audit-bi-daily-materialized-view.md) — 审计 BI 采用日粒度物化与热数据补尾
- [0068](archive/0068-experimental-webgpu-pointcloud-renderer.md) — 3D 点云渲染器采用可回退的实验性 WebGPU 后端
- [0069](archive/0069-scene-track-domain-and-lifecycle.md) — Scene Track 作为 3D 时序对象生命周期、存在区间与可逆命令的权威模型
- [0070](archive/0070-single-context-pointcloud-four-view-rendering.md) — 点云四视图采用单 context 与事件驱动渲染
- [0071](archive/0071-persistent-multicamera-annotation-members.md) — 多相机人工标注作为 SceneTrack 的持久成员
- [0072](archive/0072-workbench-dockable-layout.md) — 工作台采用受控 Dockview 布局与单一快照写入者
- [0073](archive/0073-shared-surface-for-3d-docking.md) — 3D 自由布局采用工作区共享渲染 surface 与整组相机呈现
- [0074](archive/0074-atomic-annotation-slice-restore.md) — 图片对象切割采用受限操作账本与原子恢复
