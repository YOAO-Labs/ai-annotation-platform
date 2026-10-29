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

- [0180](0180-installable-inference-plugins.md) — 可安装推理插件与组合式检测开发包（实施与 GPU 验收中）
- [0166](0166-workspace-locked-authority-snapshots.md) — Workspace 锁定权限读取与会话快照（限定本地定向验收通过）
- [0167](0167-workspace-unlocked-authority-snapshots.md) — Workspace 无锁权限读取与调用方快照（限定本地定向验收通过）
- [0168](0168-workspace-exclusive-project-snapshots.md) — Workspace 独占 Project 快照（限定本地定向验收通过）
- [0173](0173-workspace-actor-scope-relations.md) — Workspace actor 范围与列表读取（限定本地验收通过）
- [0174](0174-workspace-nonlocking-member-facts.md) — Workspace 非锁定成员事实与 ORM 名册（限定本地验收通过）
- [0175](0175-workspace-project-fact-relations.md) — Workspace Project 集合与原生事实（限定本地验收通过）
- [0176](0176-labeling-lifecycle-counter-deltas.md) — Labeling 生命周期与审核原子计数（限定本地验收通过）
- [0177](0177-workspace-caller-transaction-dataset-unlink.md) — Workspace 数据集解绑的可移植事务绑定（限定本地验收通过）
- [0178](0178-video-retirement-deletion-fences.md) — 视频退役后的删除与解绑围栏（限定本地验收通过）
- [0179](0179-workspace-consumer-credential-closure.md) — Workspace 普通消费者凭据边界（限定本地验收通过）
- [0172](0172-project-set-locks-and-labeling-counters.md) — Workspace 项目集合锁与 Labeling 计数持久化（限定本地验收通过）
- [0171](0171-workspace-actorless-business-snapshots.md) — Workspace 无 actor 的业务快照（限定本地定向验收通过）
- [0170](0170-workspace-transactional-member-locks.md) — Workspace 同事务成员锁（限定本地定向验收通过）
- [0169](0169-workspace-task-training-project-snapshots.md) — Task／Training 的 Workspace 项目快照（限定本地定向验收通过）
- [0165](0165-workspace-caller-transaction-project-deletion.md) — Workspace 项目删除的调用方事务边界（调用边界定向通过，视频前置未闭合）
- [0164](0164-workspace-transactional-project-maintenance.md) — Workspace 相邻项目写入的同事务能力（限定本地定向验收通过）
- [0163](0163-identity-registration-policy-owner.md) — Identity 注册状态与部署默认值的统一读取（限定本地定向验收通过）
- [0162](0162-identity-read-relations-and-consumer-grants.md) — Identity 集合读取关系与独立 API 消费者权限（限定本地定向验收通过）
- [0157](0157-video-execution-claims-and-route-binding.md) — 视频执行代次、原子路由绑定与当前写入检查（完整恢复继续实施）
- [0156](0156-scoped-inference-retirement.md) — 推理执行范围的定向退役与作业交接边界（定向退役通过限定本地验收）
- [0009](0009-task-events-table-and-partition.md) — task_events 表与按月分区方案
- [0025](0025-webhook-event-envelope-versioning.md) — Webhook 事件信封与版本化（草案，未实现）
- [0034](0034-lidar-axis-convention.md) — 点云数据集 lidar 坐标系约定：dataset 级声明 + 加载侧归一化
- [0075](0075-platform-identity-vs-project-authority.md) — 平台身份与项目职责分离：员工按项目成员角色授权（本地实现中，未部署）
- [0131](0131-identity-transactional-account-locks.md) — Identity 同事务账号锁与只读事实（本地实现中，资格验证待完成）
- [0132](0132-identity-operational-provisioning-and-authority-reads.md) — Identity 运维账号 provisioning 与 authority facts 复用（已通过限定本地资格）
- [0133](0133-workspace-membership-lifecycle-owner.md) — Workspace 项目成员生命周期命令 owner（成员命令本地限定验收通过）
- [0134](0134-workspace-project-owner-transfer.md) — Workspace 项目负责人转移命令与本地传输提供方实验（限定本地验收通过）
- [0135](0135-workspace-configuration-values-and-dependencies.md) — Workspace 配置值、纯策略与跨模块依赖（配置命令 owner 仍在实施）
- [0136](0136-workspace-configuration-command-owner.md) — Workspace 配置命令协议、分阶段写入与事务边界（限定本地验收通过）
- [0137](0137-workspace-template-query-owner.md) — Workspace 模板读取与提交后响应投影（限定本地验收通过）
- [0138](0138-workspace-project-read-foundation.md) — Project 读取的类型化外域依赖与 Workspace 权限值（基础实现已验证，查询 owner 接入中）
- [0139](0139-workspace-project-query-owner.md) — Project 查询协议、原始值编码与分阶段读取策略（选定查询能力已完成限定本地验收）
- [0140](0140-workspace-directory-query-owner.md) — 成员目录与提及候选的独立读取协议及成员时间戳权限边界
- [0141](0141-workspace-project-access-query.md) — Project 访问上下文的两轮当前权威读取、成员版本与共享权限策略

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
- [0076](archive/0076-controlled-trained-model-identity.md) — 受控模型、部署与发布分离，预测冻结权重及类别来源
- [0077](archive/0077-native-augmentation-preview-cpu-ownership.md) — 原生增强预览采用独立且有界的 CPU 所有权，确认退出后发布并保留过期清理墓碑
- [0078](archive/0078-attempt-observability-and-redacted-logs.md) — 执行观测独立于训练所有权，日志保存前脱敏并通过不可变历史副本下载
- [0079](archive/0079-frozen-sample-evaluation.md) — 冻结逐样本证据、后端请求锁／收据与当前源核对
- [0080](0080-model-lineage-and-decisions.md) — 模型谱系按需读取，候选决策独立追加并保留当前发布
- [0081](archive/0081-controlled-training-initialization.md) — 项目权重初始化保留固定来源并创建全新训练状态
- [0082](archive/0082-budgeted-training-studies.md) — 参数搜索复用普通训练并以进程回执结算预算
- [0083](archive/0083-human-reviewed-active-learning.md) — 人工主动选样保留独立对照、显式预算与不可变决策
- [0084](archive/0084-qualified-libreyolo-model-return.md) — LibreYOLO 回流复用受控模型并独立核验原生推理
- [0085](0085-qualified-polygon-instance-training.md) — 简单多边形实例训练复用原生执行器并独立冻结任务身份

- [0086](0086-canonical-task-data-contracts.md) — 任务名称共享并与格式、媒体及冻结身份分离
- [0087](archive/0087-project-task-dataset-streams.md) — 项目按任务派生版本流，管理关联与冻结身份分离
- [0088](archive/0088-service-owned-task-admission.md) — 任务可见性与写入准入由服务层统一持有
- [0089](archive/0089-injected-quality-job-dispatch.md) — 质量执行通过领域端口注入 Celery 派发适配器
- [0090](0090-module-languages-and-audit-summary-process.md) — 按模块选择语言，以只读审计月报验证 Go 进程边界
- [0091](0091-independent-media-probe-contract.md) — 独立素材探测协议与逐模块提取
- [0092](0092-transactional-module-data-capabilities.md) — 同库事务中的版本化模块数据接口
- [0093](0093-durable-task-creation-authority.md) — 已接受建题操作的执行权与分块围栏
- [0094](0094-transactional-project-deletion.md) — Transactional project deletion across module owners
- [0095](0095-transactional-dataset-unlink.md) — Transactional Dataset unlink across module owners
- [0096](0096-assets-source-deletion-and-cleanup.md) — Assets source deletion, staged publication and independent cleanup
- [0097](0097-independent-assets-catalog.md) — Independent Assets catalog and durable create/update replay
- [0098](0098-association-read-projections.md) — Same-snapshot owner projections for independent association reads
- [0099](0099-independent-assets-source-controls.md) — Independent source publication, legacy completion and deletion
- [0100](0100-durable-connector-import.md) — Durable connector import execution and publication
- [0101](0101-assets-synchronous-zip-and-scan.md) — Synchronous ZIP and legacy bucket scan in Assets Source

- [0102](0102-assets-spatial-state.md) — Assets spatial state and transactional native composition
- [0103](0103-assets-connection-administration.md) — Assets storage connection administration and replay
- [0104](0104-operator-source-registration.md) — Operator source registration through native Assets capabilities

- [ADR-0105: Durable video media execution and source-bound publication](0105-independent-video-media-owner.md)
- [ADR-0106: Independent image materialization and source-bound delivery](0106-independent-image-media-owner.md)
- [ADR-0107: Versioned media inputs for accepted inference](0107-versioned-accepted-media-inputs.md)
- [ADR-0108: Durable bulk image input manifests](0108-bulk-image-input-manifests.md)
- [ADR-0109: Retained video inputs and source-bound frame plans](0109-retained-video-inputs-and-frame-plans.md)
- [0110 — 跟踪作业固定原始视频输入](0110-retained-tracker-video-inputs.md)
- [0111 — 独立 Mask 内容字节边界](0111-independent-mask-content.md)
- [0112 — Durable Mask deletion fences and terminal receipts](0112-durable-mask-gc-fences.md)
- [0113 — Durable ordinary preannotation results and execution handoff](0113-durable-preannotation-results.md)
- [0114 — Durable failed-prediction retries and running tracker roots](0114-durable-prediction-retries.md)
- [0115 — Identity 准入与持久会话撤权](0115-identity-admission-and-durable-revocation.md)
- [0116 — API key 命令与可重放签发](0116-independent-identity-key-commands.md)
- [0117 — 核心会话命令与原子审计](0117-independent-identity-session-commands.md)
- [0118 — 自助账户命令与在线状态所有权](0118-independent-identity-account-commands.md)
- [0119 — Identity 注册恢复与凭据邮件独立所有权](0119-independent-identity-recovery-and-mail.md)
- [0120 — Identity 分组目录与指派独立所有权](0120-independent-identity-group-catalog-and-assignment.md)
- [0121 — Identity 自助注销与通知投递所有权](0121-identity-self-deactivation-and-notification-delivery.md)
- [0122 — Identity 邀请生命周期与 durable MailV2 投递所有权](0122-independent-identity-invitations-and-mail-v2.md)
- [0123 — Identity 用户目录读取所有权与可见性合同](0123-independent-identity-directory-reads.md)
- [0124 — Identity 管理员临时密码重置所有权](0124-independent-identity-admin-password-reset.md)
- [0125 — Identity 平台角色预览与变更所有权](0125-independent-identity-platform-role-commands.md)
- [0126 — Identity 管理员停用与恢复账号所有权](0126-independent-identity-suspension-commands.md)
- [0127 — Identity 管理员删除账号所有权](0127-independent-identity-deletion-commands.md)
- [0128 — Identity 离职预览与提交所有权（限定本地资格）](0128-independent-identity-offboarding-commands.md)
- [0129 — Identity 批量 UserBrief 读取（限定本地资格）](0129-identity-batched-user-brief-reads.md)
- [0130 — Identity 认证后的主体上下文读取（限定本地资格）](0130-identity-post-admission-actor-context.md)
- [0142 — Labeling Class Usage Query Owner](0142-labeling-class-usage-query-owner.md)
- [0143 — Workspace Project Class Rename Owner](0143-workspace-project-class-rename-owner.md)
- [0144 — Workspace Guide Assets and Assets Object Capability](0144-workspace-guide-assets-owner.md)
- [0145 — Workspace Project Access Facts for Independent Owners](0145-workspace-project-access-facts.md)
- [0146 — Independent Inference Named-Pipeline Owner](0146-inference-named-pipeline-owner.md)
- [0147 — Inference Pool Policy and Workspace Backend Bindings](0147-inference-pool-policy-and-workspace-bindings.md)
- [0148 — Inference Backend Transport and Deletion Dependencies](0148-inference-backend-transport-and-deletion-dependencies.md)
- [0149 — Credential-owning Workspace Backend Probe](0149-credential-owning-workspace-backend-probe.md)
- [0150 — Portable Backend Health Coordination](0150-portable-backend-health-coordination.md)
- [0151 — Inference Topology Locking](0151-inference-topology-locking.md)
- [0152 — Credential-owning Health Observations](0152-credential-owning-health-observations.md)
- [0153 — Caller-owned Native Health Data](0153-caller-owned-native-health-data.md)
- [0154 — Registry Ingress and Environment Synchronization Owner](0154-registry-ingress-and-environment-sync-owner.md)
- [0155 — Durable Inference Routing Authority](0155-durable-inference-routing-authority.md)
- [0156 — Scoped Inference retirement](0156-scoped-inference-retirement.md)
- [0157 — 视频执行代次与原子路由绑定](0157-video-execution-claims-and-route-binding.md)
- [0158 — 视频窗口账本与会话重放](0158-video-window-journals-and-replay.md)
- [0159 — Labeling 视频 worker 最终写入 owner](0159-independent-labeling-video-writer.md)

- [0088](archive/0088-model-catalog-and-frozen-pipelines.md) — 模型目录独立投影，管线按阶段冻结并完整重试

- [0089](archive/0089-task-scoped-native-instance-learning.md) — 实例分割采用原生掩码训练与按任务隔离的在线学习

- [0090](archive/0090-versioned-yolo-identities-and-content-assets.md) — YOLO 家族按冻结运行时核验，权重按内容寻址并独立准备节点

- [0091](archive/0091-dataset-content-and-location-identity.md) — 数据集内容身份与固定托管位置分离，旧键兼容回填不改业务快照

- [0092](archive/0092-controlled-storage-registry.md) — 存储位置在受控部署根内动态注册并按节点验证

- [0093](archive/0093-resumable-directory-inventory.md) — 目录观测按完整目录提交并以执行代次恢复

- [0094](archive/0094-verified-request-content-snapshots.md) — 可变原图通过有界请求快照核验后交付

- [0095](0095-immutable-directory-reference-tasks.md) — 目录样本固定内容身份并在任务锁内复核写入
- [0096](0096-durable-directory-registration.md) — 目录批量登记使用独立的两阶段持久账本
- [0097](0097-directory-ownership-previews-and-lifecycle.md) — 目录原图所有权、鉴权派生预览与来源生命周期分离
- [0098](0098-strict-external-s3-references.md) — 外部 S3 引用保存精确定位并逐次验证条件读取
- [0099](0099-qualified-sftp-references.md) — SFTP 持续引用以部署核验的只读 jail 和固定主机公钥为准入边界
- [0100](0100-preflight-owned-source-differences.md) — 增量差异固定已接纳基线，接纳范围贯穿恢复
- [0101](0101-periodic-source-coordination.md) — 周期来源协调沿用领域运行和人工接纳
- [0102](0102-explicit-directory-content-revisions.md) — 内容修订追加业务身份并推进来源路径头
- [0103](0103-explicit-same-content-revalidation.md) — 同内容重新核验追加读取凭据并保留业务身份
- [0104](0104-explicit-source-relocations.md) — 来源内移动通过明确接纳的新定位保留原业务身份

- [0105](0105-opt-in-pure-addition-sync.md) — 周期计划按明确策略自动接纳纯新增轮次

- [0106](0106-reviewed-source-selection-changes.md) — 扫描选择规则通过影响预览变更，保留旧业务读取
- [0107](0107-scoped-native-video-stages.md) — 原生视频通过路径 Cookie 读取有界完整核验暂存
