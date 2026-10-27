---
title: 仓库地图：模块归属与调用链
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# 仓库地图：模块归属与调用链

正常月报由 Python `app/services/audit_summary.py` 本地归并。`apps/audit-summary` 与 `apps/api/experiments` 只保留 Go／HTTP 隔离实验，默认运行、生产部署和普通 CI 不依赖它。语言依据见 [ADR 0090](../adr/0090-module-languages-and-audit-summary-process)。

> 本文回答「一个行为改哪里、沿着哪些真实符号走」。只列当前实现与允许依赖方向，
> 不复制目录树、不复制代码、不维护第二套角色矩阵（角色语义见
> [可见性与权限](./visibility-and-permissions)）。

## 1. 顶层结构与依赖方向

| 区域            | 位置                                                                                        | 允许依赖                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 前端页面 / 装配 | `apps/web/src/pages/**`                                                                     | 领域 hooks → API client → 生成类型                                                                         |
| 前端领域状态    | `apps/web/src/pages/Workbench/state/**`、各页面 `*UrlState.ts`                              | 纯函数，不触网                                                                                             |
| 前端授权        | `apps/web/src/hooks/useProjectAccess.ts`、`src/components/routing/RequireProjectAccess.tsx` | 只消费后端返回的能力集                                                                                     |
| API 路由        | `apps/api/app/api/v1/**`                                                                    | 应用工作流或服务层；不做业务规则推导                                                                       |
| 应用工作流      | `apps/api/app/application/**`                                                               | 显式装配领域服务、协调用例事务与派发顺序；不持有领域状态机                                                 |
| 服务层          | `apps/api/app/services/**`                                                                  | 项目授权在 `project_access`，任务访问/写入准入在 `task_access`，聚合 SQL 在 `project_aggregates`           |
| Worker          | `apps/api/app/workers/**`                                                                   | 复用工作流与服务；终态信号仅适配事件、连接及调用方提交                                                     |
| ML 共享运行时   | `apps/_shared/backend_runtime`                                                              | 无状态叶子函数 + `ManagedLruPool` 池内核                                                                   |
| ML 协议         | `apps/_shared/protocol_v2`                                                                  | 五个 backend 共用的 schema 与受控词表                                                                      |
| 任务契约        | `apps/_shared/task_contract`                                                                | 纯 Python 统一视觉操作名（detect/segment/classify 等）与别名映射                                           |
| 训练参数契约    | `apps/_shared/training_contract`                                                            | 纯 Python 原生框架字段快照、catalog 依赖校验、预算与回流契约                                               |
| 光栅掩码工具    | `apps/_shared/mask_utils`                                                                   | RLE 编解码、多边形与掩码转换等无状态图像/视频共享工具                                                      |
| 只读文件原语    | `apps/_shared/filesystem_read`                                                              | 标准库 Linux `openat2` 只读目录/文件锚点校验与 `0600` 本地 `VerifiedStage`                                 |
| 站点 Agent 契约 | `apps/_shared/site_agent_contract`                                                          | 中心与跨网络只读站点 Agent 共用的命令、回执、摘要与协议模型                                                |
| 站点存储 Agent  | `apps/storage-agent`                                                                        | 独立出站 HTTPS 只读目录扫描与分块传输守护进程（零入站监听、本地 SQLite 状态）                              |
| 项目只读契约    | `apps/_shared/workspace_contract`                                                           | 纯 Project 配置快照、类别与属性投影；不依赖提供方 ORM、API 或运行时                                        |
| 项目成员命令    | `apps/workspace` (`aap-workspace`)                                                          | 成员、负责人转移与配置命令的便携 owner；限定本地验收已完成，普通读取与相邻写入仍待迁移                     |
| 独立素材目录    | `apps/assets-catalog`                                                                       | 七项目录与关联读取操作；受限 SQL LOGIN、只读 S3 身份与独立镜像                                             |
| 独立素材来源    | `apps/assets-source`                                                                        | 单文件上传/发布、来源删除、同步 ZIP/旧前缀扫描、六项连接管理操作与持久连接器导入；纯合同和独立 SQL/S3 身份 |
| 审计请求契约    | `apps/_shared/audit_contract`                                                               | 冻结请求上下文字段，不依赖 API 或 ORM                                                                      |
| 训练执行器      | `apps/training-executor`                                                                    | 独立拉取式执行器，文件锁独占 GPU，管理原生训练子进程与脱敏日志                                             |
| 素材探测服务    | `apps/media-probe`                                                                          | 独立元数据/帧时间表/packet 探测与 HTTP/NDJSON 合同；不取得数据库或对象存储访问权                           |
| 事务数据能力    | `apps/api/alembic/sql/0190*` 至 `0217*`                                                     | 授权、建题、删除、解绑、上传、目录、关联读取、空间状态、ZIP/扫描与连接管理；冻结 SQL/JSON 合同及同库事务   |
| ML backend      | `apps/{grounded-sam2,sam3,yolo,rapidocr,onnxtools}-backend`                                 | 各自的池包装 / 观测 / 生命周期                                                                             |

## 2. 授权真值（唯一来源）

- 平台身份与项目能力解析：`app/services/project_access.py`
  纯访问值、能力与标量角色规则位于独立共享契约包 `aap_workspace_contract/access.py`；Workspace 与 API 直接导入该所有者，解析和锁仍由本地 SQL 所有者持有。
  `ProjectWritePolicyResolver` 为只需访问决策的管理围栏、预测导入和上传交付纯值；审核、生命周期和单题/整批送审命令使用 actor UUID，再从同事务锁定账号投影 `identity_contracts.IdentityActorFacts`，每个送审分块重新生成。
  - `resolve_project_access()` / `resolve_project_access_by_id()`：请求期真值，
    每次重读账号与项目，未知的平台/项目角色 fail closed。
  - `is_privileged_for_project()`：纯函数，管理型访问 = 活跃超管，或项目负责人且平台角色为管理型。
  - `membership_role_compatible()`：账号平台角色与项目职责的合法组合。
- 任务资源访问与标注写入准入：`app/services/task_access.py`
  - `assert_task_visible()` / `visible_task_ids()`：单题与批量资源可见性。
  - `assert_task_editable()` / `assert_annotation_write_allowed()`：阶段锁与项目写能力。
  - `resolve_effective_task_assignee_id()` / `assert_effective_task_assignee()`：有效分派人。
  - `assert_review_adjustment_evidence()`：审核调整前的冻结非本人证据。
  - API 依赖、WebSocket 与写入所有者直接调用，不通过路由私有函数重新导出。
- 异步最终写入组合：`app/services/project_write_guard.py::assert_phase_write_allowed`；
  复用上述服务与当前项目授权，保留调用方的锁顺序与事务归属。
- 管理者写命令：`app/services/project_write_guard.py::lock_project_manager`；
  与 `resolve_project_access(lock_membership=True)` 复用账号→项目→成员当前权威锁，
  训练/模型/评估不再依赖训练数据集路由辅助函数。
- 推理候选最终写入：`app/services/labeling_write_scope.py` 冻结主体/目标/源版本事实，
  `labeling_prediction_contract.ApplyPredictionResultPort` 提供请求、精简回执与稳定错误，
  `PredictionService.apply_prediction_result` 在调用方原 Session 内持久化；
  `labeling_write_guard.py` 消费 `project_access.lock_current_project_access()` 的
  账号→项目→成员当前权威；候选/Meta/计数、标注采纳及批次推进由各自标注所有者写入，
  不把历史作业或 Scope DTO 当授权。
- 作业读取/取消/重试值接口：`app/application/async_job_contracts.py`；SQL 可见性实现为 `app/services/async_job_access.py::_can_access_job`，ORM 留在读取与命令适配器内；
  明细与取消重查当前项目、导出能力及审核任务范围，不把历史作业归属当作当前授权。
- 用户生命周期与责任交接：public REST routes／DTO 仍在 `app/api/v1/users.py` 与
  `app/schemas/user.py`。管理员 DELETE、暂停／恢复与 offboarding 各由独立 Identity command
  owner 组合；offboarding 的 HTTP composition/caller port/data 是
  `app/api/v1/offboarding_commands.py`、`app/services/identity_offboarding_commands.py` 和
  `app/services/identity_offboarding_data.py`。Portable contract/manager/Postgres adapter 在
  `apps/identity/src/aap_identity/{offboarding_contract,offboarding_manager,offboarding_postgres}.py`。
  旧 API lifecycle coordinator 已移除；`services/user_lifecycle.py` 仅保留
  `UserLifecycleService.lock_accounts`，现经 caller-bound Identity account lock adapter 获取只读事实并保留其显式 rollback 语义。Batch handoff/release helpers 已退役，Project owner
  transfer helper 继续供其他 workflows 使用。各命令的权限、回执和事务边界独立维护；offboarding
  已通过限定的本地资格，精确范围见 [ADR-0128](/dev/adr/0128-independent-identity-offboarding-commands)。
- Identity 运维账号 provisioning：`services/identity_provisioning_data.py` 把
  `AccountProvisioningData` 绑定到 caller `AsyncSession`；portable manager 只返回 UUID 或
  bootstrap 的 `None`，不把 User ORM/password hash 交给 CLI。Native 负责 per-email
  bootstrap defaults/fixed Audit 与 exact import-admin guard；outer bootstrap/import caller
  保留 commit/rollback。邀请账号仍由 Identity Invitation owner 创建；运维 provisioning
  不复用邀请写入路径。本切片已通过限定本地资格，见
  [ADR-0132](/dev/adr/0132-identity-operational-provisioning-and-authority-reads)。
- Identity authority facts 复用：`project_access` 的非锁定账号读取复用现有
  `IdentityAccountLockData.read_actor_status`／`read_actor_facts`；事务写路径复用已有 SHARE、
  UPDATE、KEY SHARE 函数，不新增 SQL 或传输协议。Project/member 刷新及 caller 原错误映射
  保持不变；详见 [ADR-0132](/dev/adr/0132-identity-operational-provisioning-and-authority-reads)。
- 同事务 Identity 账号锁：`apps/api/app/services/identity_account_lock_data.py` 调用
  `apps/identity/src/aap_identity/account_lock_contract.py` 定义的 SHARE/UPDATE/KEY SHARE 三项
  Native function；它使用 caller connection 并让锁持续到业务写入提交，只返回 id/name/email/role/is_active
  五字段事实。没有 lock HTTP endpoint、独立 DSN 或 User 原表授权；账号、Project、membership 与资源
  的顺序及各 caller 对 NOWAIT、SQLSTATE、busy 和 rollback 的映射仍由原 workflow 持有。跨帧写入每帧重取
  facts，非锁定 Workspace 查询和 bootstrap/import User writer 仍待处理；详见 [ADR-0131](/dev/adr/0131-identity-transactional-account-locks)。
- 批量 UserBrief enrichment：现有 `app/services/user_brief.py::resolve_briefs` 与
  `resolve_briefs_with_project_role` 为已授权的任务／项目／结果补充六字段用户摘要；
  调用者先检查自己的 rows，helper 不接收 actor，也不增加公共资料查询路由。Identity
  portable owner 位于 `apps/identity/src/aap_identity/{briefs_contract,briefs_manager,briefs_postgres}.py`；
  项目成员角色只通过 Workspace 的窄批量 capability 读取。该 owner 已通过 64 个 distinct
  selected cases（59 DB/process、5 pure/static），零跳过；旧行为、caller-transaction 和
  可选 private runtime 边界见
  [ADR-0129](/dev/adr/0129-identity-batched-user-brief-reads)。
- Projects 成员列表 `projects.list_members` 在 Workspace 完成授权并按
  `assigned_at DESC` 选择 membership rows 后，调用全局 `resolve_briefs` 一次补齐用户显示字段。
  Membership role 仍是项目职责，brief role 是平台角色；响应保持原 DTO，空列表不访问 Identity，
  缺失 profile 继续省略。该消费者另有 21 个限定案例（18 DB/process、3 pure/static）与 SDK 44
  项序列化检查通过，不计入前述 owner 的 64 例；验证边界见 ADR-0129。
- Project owner display enrichment：`projects._serialize_project` 与
  `_serialize_project_list` 只用全局 `resolve_briefs` 的 `name/avatar_ref` 填充可空 owner
  显示字段；Workspace 的 `owner_id` 和 ProjectOut schema 不变。list/query 先按既有 ACL、
  filters、order、count、pagination 选定页面，再对页面 distinct owner IDs 批量读取一次；
  空页无 owner I/O，single serializer 收到 `{}` 时不重复查询。创建、详情、patch、改名和
  转移对非空 owner ID 各读取一次；dataset-linked 项目路径仍保留 null 显示字段。59 个选定消费者案例
  （53 DB/process、6 pure/static）通过，更新后的 API image/contract checks 也已通过；见 ADR-0129。
- 分组目录与账号指派：`api/v1/group_commands.py` 收口 HTTP 请求与权限映射；
  `services/identity_group_commands.py` 选择本地事务、函数 DSN 或 Identity runtime，
  `services/identity_group_data.py` 只适配当前 SQL Session 上的本地 Group command data。
  可提取的 Python owner 位于 `apps/identity/src/aap_identity/{group_contract,group_manager,group_postgres}.py`，
  通过 Workspace 项目成员投影维护作用域并将 Group 状态、审计与冻结 receipt 同事务提交。
  该 owner 只覆盖目录／指派；invite group resolution 由下列 Identity invitation owner 通过私有 helper 内部调用，不授予 GROUP LOGIN。用户目录、平台角色管理与其余账号生命周期仍未整体迁移。
- 自助注销与到期处理：`api/v1/deactivation_commands.py` 用 `get_current_principal` 和
  `get_deactivation_commands`／`execute_deactivation` 绑定两个用户命令；
  `services/identity_deactivation_commands.py` 适配两个 versioned port，
  `identity_deactivation_data.py` 保留本地 SQL caller transaction。可移植 user/due owner
  位于 `apps/identity/src/aap_identity/{deactivation_contract,deactivation_manager,deactivation_postgres}.py`；
  `workers/deactivation.py` 保留原 task 名及 04:00 UTC 调度，
  通过 operation receipts 分页排空，不再直接扫描 User/Notification ORM。
- 注销通知投递：Identity 命令通过 Notifications owner 原子创建两种固定 event 与 delivery outbox；
  `apps/identity/src/aap_identity/notification_delivery_{contract,postgres}.py` 提供纯值领取端口，
  `aap_identity_runtime.notification_worker` 是无 HTTP 的独立 publisher，API 默认 queue
  的 `app.workers.notification_delivery.relay_identity_notifications` 每 10 秒运行并使用相同 executor 与 lease，
  不保留第二条即时发送路径。
- 在线状态扫描：`workers/presence.py` 解析阈值、计算 cutoff 并持有提交/计数/日志；
  `IdentityCommandService.mark_inactive_accounts_offline(cutoff)` 以单条批量更新写身份状态，
  保留 online 与缺失/严格早于 cutoff 的 `last_seen_at` 条件。
- 完整邀请生命周期：`api/v1/invitation_commands.py` 通过 Principal/value dependencies 装配 13 项 invitation contract；`services/identity_invitation_commands.py` 选择调用方事务或 Identity runtime，`identity_invitation_data.py` 只适配本地 SQL。可移植 owner 位于 `apps/identity/src/aap_identity/{invitation_contract,invitation_manager,invitation_postgres}.py`；User/Invitation/Group、Workspace membership、Audit 与 token proof 在一个调用方事务中组合，不水合 API ORM User/Project/ProjectMember/TaskBatch。
- Invitation MailV2：`services/identity_mail_v2_delivery.py` 在邀请 owner commit 后等待有界私有 pending/sent/failed 状态；`app.workers.identity_mail_v2_delivery.relay_invitation_mail` 每 10 秒在 default queue 执行相同 MailV2 executor。独立 sender 用 `apps/identity/src/aap_identity/{mail_v2_contract,mail_v2_postgres}.py`、`aap_identity_runtime.mail_config_v2` 和 `mail_worker_v2.py`，只有 MAILV2/MAIL_SETTINGS/key/SMTP 能力，不持 AUTH/Invitation/User 表权限。
- 自有 API key 管理：`services/api_key_contracts.ApiKeyManagementPort` 消费 `aap-identity-keys/v1` 值消息，`api_key_service.IdentityKeyManagement` 装配调用方事务、函数专用连接或独立运行时。`deps.get_current_principal` 提供准入和审计身份；管理路由不水合 User 或写 key ORM。Identity 命令所有者持有五项操作、当前凭据围栏、签发和冻结回执。
- 核心会话：`services/identity_session_commands.SessionCommandsPort` 消费 `aap-identity-sessions/v1`，装配同一 Identity 提供方的登录、刷新、登出和全端登出；API 保留限流和 Turnstile 验证，不水合 User 或写会话状态。提供方拥有密码验证、失败计数、当前凭据围栏、冻结签发回执和同事务 Audit 能力；三函数 LOGIN 与登录计数 Redis ACL 分离于其他能力。其他公开注册签发、用户目录／平台管理和完整账号生命周期仍待迁移。
- 类别重命名：Workspace Configuration 的 `rename_project_class` 组合便携绑定策略与
  Labeling 窄类名能力；账户、项目和成员围栏先于策略，实际改名再升级排他项目锁。
  两项写入同事务；同名请求在独立提供方持锁完成项目投影后返回，不写回执或自动重发。
  工具选择只收窄配置，存量标注的项目级名称迁移及原版本语义保留，原 API 写入副本已移除。
- 项目配置与模板命令：`apps/workspace` 的 portable manager 与 Native 能力持有配置编排、权限围栏及写入，
  `services/workspace_configuration.py::execute_configuration` 装配调用方事务、专用连接或 HTTP；
  原 API 配置 ORM 命令已移除，HTTP 保留提交与响应。
- 命名预标编排由 `aap_inference.pipeline_manager.PipelineManager` 与 `pipeline_policy` 持有；
  `services/inference_pipelines.py` 选择 API 借用事务、专用 DSN 或 HTTP owner，
  `inference_pipelines_data.py` 只适配固定 Native step。六个 CRUD/apply 公开路由与 DTO 保持原合同，
  结构校验仍使用 `aap_inference.pipeline.validate_saved_pipeline`；canonical `PipelineStage` /
  `PreannotateRequest` 位于 `aap_inference.preannotate`。Private Native RPC、Workspace 权威桥接和
  commit/status/projection 阶段见 [后端分层](./backend-layers#分层规则)。
- 指引图片：`services/guide_asset_uploads.py` 校验受管上传；
  `services/guide_asset_references.py` 调用 Workspace Configuration 的发布、解除引用、列表 CAS 与复制命令。
  Native `guide_assets_step` 持有当前权限、维护门、上传状态、引用与项目修订的同事务写入；
  项目删除单独退休引用，物理对象由清理流程处理。Workspace 不持有 S3 凭据。
- 全局 ML registry 删除：`application/ml_registry_deletion.py::delete_registry` 编排删除准入、路由静默、pool 解析与 registry 删除；成功后由 `services/project_workspace.py::clear_primary_backend_binding_for_pool` 在同一事务清空该 pool 的项目主绑定。workspace 持有主绑定 setter/clearer，`MLBackendService.delete` 不写 Project；HTTP 保留审计/提交，超管权限经 `identity_commands.IdentityCommandService.lock_platform_super_admin()` 锁定并刷新。
- 原标注评论：`application/annotation_comment.py::AnnotationCommentWorkflow` 组合
  `services/annotation_comment.py`、反馈镜像、审计和通知；当前权威与真实任务/标注/评论行
  在修改前重取并锁定。创建保留单事务及提交后发布，原评论与反馈镜像不做双向同步。
- 反馈命令：`application/annotation_feedback.py::AnnotationFeedbackWorkflow` 持有四类
  写入准入与事务组合，`FeedbackService` 写反馈行；实际项目/任务和刷新后的线程祖先
  决定权限与动作，不信任旧 `actions`、缓存正文支持或反规范化项目身份。
- 视频章节：`application/video_chapters.py::VideoChapterWorkflow` 组合当前权威与
  视频任务/素材/章节围栏，`services/video_chapter_service.py` 写素材级章节；
  `api/v1/videos.py` 保留审计、提交及响应 refresh，不改变协作分段账本。
- Mask 保留清理：`application/raster_mask_retention.py::RasterMaskRetentionWorkflow`
  组合四类领域过期命令与八类引用投影；`services/raster_mask_storage.py` 持有内容锁、
  reservation 两阶段提交和对象删除。`workers/cleanup.py` 只装配 job-local Session；
  dry-run 仍提交过期记录，原宽限、处理上限及存储失败语义见 [存储桶布局](../reference/storage-buckets#raster-mask-引用与清理)。
- 质检执行：`app/application/quality_execution.py` 将 `app/services/quality_job_lifecycle.py::QualityJobLifecycle` 绑定到通用 `async_job` 和终态通知；Mask QC / 点云质检执行分别由 `services/mask_qc/execution.py`、`services/point_cloud_quality/execution.py` 持有，视频轨迹质检仍由 `services/video_track_quality.py::execute_run` 持有。原三个 Celery task 名、单个 run ID 参数和 `media` 路由保留；worker 仅负责 Session 与应用调用，领域服务不反向导入 application / worker。
- 选定任务分派：`app/services/labeling_assignment.py::LabelingTaskAssignmentService`
  持有预览/确认与当前管理、接收人和编辑围栏；`schemas/labeling_assignment.py` 保留原公开模型。
  `data_management` 保留查询/保存视图及异步操作幂等助手，不直接写任务指派。
- 独立任务文件：`app/services/task_uploads.py` 组合当前管理权威、受管原件分配、验证与
  原生发布；`app/application/project_thumbnail_backfill.py` 持有项目缩略图回填的管理权
  重检并直接接受 Image owner 作业。原图地址用 `task_access.assert_task_visible` 后拒绝
  旧签名下载，不能只检查项目成员身份。
- 媒体物化：`apps/assets-media` 通过视频与图片的公开协议持有 codec、对象 I/O、作业与派生缓存；独立 worker 通过来源、代次和租约围栏发布结果，direct Task 的投影调用 Labeling 窄能力。旧结果 writer 已移除。
- 图片金字塔：`api/v1/tasks/image_pyramid.py` 保留 Task 可见性和 REST 缓存语义；`services/image_pyramid.py::AssetsImagePyramidApi` 将列表、manifest、asset URL 与重试组合到 `aap-assets-images/v1`，通过 `services/assets_images.py::ImageGateway` 调用本地原生能力或共享 `apps/assets-media` 控制服务。Assets 持有图片 source/job/receipt/generation 状态；Labeling 提供 direct Task 来源与 Task 缩略图投影，HTTP 不接收 ORM 作为跨模块值。图片 worker 使用独立 image LOGIN 与 lease 驱动执行；源/代次变化、取消和垃圾清理继续受 owner SQL 围栏。
- 失败预测管理：`app/services/prediction.py::assert_can_manage_failed_prediction`；
  `PredictionService` 持有 dismiss/restore、retry 结果计数与成功删除，路由保留当前
  项目管理准入及有状态变更时才记录审计和提交；worker 只持有推理/队列编排。
- 贡献与审核事实：`app/services/annotation_evidence.py` 持有轮次、贡献者快照、
  当前轮冻结证据与首次审核事实；提交审计缺失时只按既有规则回退保留行。
  API 和片段服务直接消费，不再经路由私有快照函数。
- 创建命令与收据：`services/annotation_create_contract.py::AnnotationCreatePort` 使用 UUID、原请求模型与纯审计来源，统一返回 `AnnotationOut`；`app/application/annotation_create.py` 的 SQL 实现绑定 HTTP 已锁定的 Task、当前 actor 和原 Session，编排创建、heartbeat、收据、审计与提交。`services/annotation.py::AnnotationService` 独占创建收据读写，同 actor/task/key 的规范化请求重放冻结响应，冲突仍返回 409 与原 operation ID。
- 聚合 SQL 的同一契约：`app/services/project_aggregates.py`
  - `valid_membership_conditions()`：成员行只在账号活跃、平台/项目角色均为现行值、
    且观察者只持观察者职责时生效。
  - `project_scope_clause()`：聚合查询的范围谓词；对停用或未知角色直接返回空集。
- 前端：`useProjectAccess` / `RequireProjectAccess` 只消费后端下发的能力集，
  不在前端重算角色矩阵。

## 3. 调用链：进入标注 → 提交 → 异步处理 → 通知

1. 路由进入：`apps/web/src/App.tsx` → 页面路由；项目内入口经
   `RequireProjectAccess` 调 `useProjectAccess` 取得能力集。
2. 后端鉴权：`app/deps.py::get_current_user` 先由现有 AUTH 完成 JWT/API-key 准入，再用其 actor UUID 和 admitted role 读取 Identity 当前主体上下文；缺失/停用/role drift 保持 401，owner 或响应错误保持 503。随后路由内的 `resolve_project_access()` 独立建立不可变 `ProjectAccess`，不会把 actor-context 当项目能力或写权限票据。该内部读取只替代准入后的 profile hydration，不替代项目/写入围栏，详见 [ADR-0130](/dev/adr/0130-identity-post-admission-actor-context)。
3. 取任务：`app/services/scheduler.py` 派题；任务锁语义在 `app/services/task_lock.py`。
   视频共享上下文和采样在 `app/services/video_context.py`；帧服务负责构建，
   片段、协作、导出、追踪与质检单向依赖该下层所有者。
   上下文只含媒体/任务标量与 `VideoMetadata`，回滚后仍可直接消费；片段提交/重开另收调用方已有的 Task，保留原锁和写入。质检及 Mask 关键帧验证显式复用已读媒体事实。
   Task 列表/详情、Data Manager、视频清单/时间表与转换的素材摘要使用 `assets_media_summary_contract.TaskMediaSummaryReader`；`assets_media_summary.SqlAlchemyTaskMediaSummaryReader` 持有单条/批量/帧数读取，HTTP helper 只组合 Task 回退。
   点云清单经 `app/application/point_cloud_manifest.py` 组合 `labeling_point_cloud_manifest_contract` 的链接引用与 `assets_point_cloud_manifest_contract` 的素材/标定/Scene/位姿事实；HTTP 保留当前可见性，SQL 适配器使用同一 Session，签名引用沿用原桶与时效。
4. 编辑与提交：Workbench 装配在
   `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` +
   `state/useWorkbenchShellModel.tsx`；任务切换准入在 `state/taskNavigation.ts`
   （latest-wins 调度、离开守卫、提交闸门、本地 URL 同步）；原生 Mask 变更工作流在
   `state/useMaskMutationWorkflows.ts`（错误策略纯模块 `state/maskMutationPolicy.ts`），
   视频 Mask 纠错在 `state/useVideoMaskCorrection.ts`，PVS 种子采集与传播在
   `state/useTrackerSeedCollection.ts`（种子分组纯模块 `state/trackerSeedPrompts.ts`），
   批量线后端选择在 `state/useBatchBackendSelection.ts`；后端 Mask 写入经
   `app/services/mask_mutation.py`（版本冲突与幂等键在此收口）。
   手工 PATCH/DELETE 经 `app/application/annotation_mutation.py` 绑定原 Session/Task/actor，消费 `annotation_mutation_contract` 的纯命令；原 Task-first 锁、Mask I/O 前后版本检查、属性 diff 审计、软删级联和 ETag/204 保持。批量编辑在同一装配文件绑定 Session/actor，经独立的 `AnnotationBulkUpdatePort` 保留 ID 选取、唯一 Task 锁与当前准入、整批写入/审计/提交顺序。
   人工候选接受/驳回由 `app/application/candidate_decision.py` 绑定同一 Session/Task/actor，消费纯 `CandidateDecisionPort`；原候选预读、最终写入围栏、视频范围、heartbeat 和提交顺序保留，接受只返回本次新建 DTO、驳回仍为 204。
   单框/整批跨帧传播和区间插值由 `app/application/annotation_cross_frame.py` 绑定原 Session/source Task/actor/access，消费纯 `AnnotationCrossFramePort`；后台逐帧作业另用只返回数量、从不提交的 `CrossFrameBatchStager`，保留原源版本、取消、逐目标回滚与提交。两者复用私有 SQL/位姿/几何步骤。
   素材关联/初始建题和入库后的追加建题经 `app/application/dataset_task_creation.py`；
   `services/dataset` 持有素材事实，`services/labeling_tasks` 持有建题、媒体关联与项目任务数增量，
   素材/帧传递使用 `services/dataset_task_facts`，纯帧分组与 Scene 规划由
   `apps/assets-source/src/aap_assets_source/frame_layout.py` 和 `scene_layout.py` 提供。
   `services/labeling_task_creation_contract` 与 `task_creation_contract` 定义初始/追加建题端口和严格值绑定；`LabelingTaskService` 消费冻结 SQL/JSON 能力，实际写入仅由 Labeling SQL 持有。初始操作保存许可、来源计划、游标和块回执；Jobs 客户端只调用其 owner 的生命周期能力。
   assets 关联结果与建题应用结果只返回 `link_id` 等标量，不向入口交付 `ProjectDataset` ORM。
   分块提交、追加 flush-only 和标定前置提交不同，不能合并成一种事务。
   单文件来源发布由公开 Source 工作流和原生命令持有，HTTP 保留认证/scope。旧无 intent 条目不补造上传者，完成元数据、Labeling append 与 Assets 回执共同提交。
   项目删除由 `application/project_deletion.py` 和严格 `services/project_deletion_contract.py` / `project_deletion_data.py` 绑定 Workspace 原生事务命令；各 owner 清理、学习保留、审计与删除回执共同提交，原始素材保留。Jobs 的 `retained_project_id` 固定删除后的访问范围，不从 payload 推断权限。
   `api/v1/datasets.py` 的 20 个 HTTP 路由（6 个读取，含 POST sniff；14 个写入）仍由同一 API 入口装配。项目解绑通过 Workspace 的 `DatasetUnlinkCommand` / `Receipt`，公开来源删除通过 Assets 的 `DatasetSourceDeletionCommand` / `Receipt` 调用原生协调命令。当前身份/权限、关联代次、Learning/Labeling 保留、Jobs 检查/取消、计数、Audit、owner 清理及不可变回执在同一调用方连接提交。`Idempotency-Key` 是可选 UUID，REST 回传它，SDK/Web 对同一操作复用原 key。
   项目解绑保留 Dataset、Item、原始文件和共享来源金字塔；来源删除保护关联证据，只排队 item-owned 金字塔。上传初始化仅保留 intent 并签发 staging PUT，完成时以 ETag 条件拷贝后原子登记 Item、追加 Task 和保存回执。独立 `apps/assets-cleanup` 用受限 SQL 能力和 prefix/key 受限 S3 身份轮询已提交责任；不依赖 API、ORM、Celery 或 Redis，也不在业务事务内删除对象。目录列表、详情、文件分页、创建/更新由独立 `apps/assets-catalog` 与同一原生 SQL 合同提供，创建/更新保留不可变回执，两项关联读取使用同一语句快照。同步 ZIP/旧前缀扫描由既有 `apps/assets-source` 工作流与 0215 Assets 原生合同执行；ZIP publication 保留不可变来源，Item、Labeling append、计数、Scene/frame 结果和派发意图在同一事务提交。连接器导入由独立进程消费持久清单/游标/租约，原子登记素材、任务与派发意图。视频和图片媒体有各自冻结协议、受限执行身份及持久工作/清理状态，但目录/来源以外的其他 CRUD、场景/标定、以及一般素材清理边界仍需单独评估；不能据此声称整个 Assets/Labeling 模块独立部署。
   生命周期、审核及单题/整批送审经 `application/task_lifecycle.py`、`task_review.py`、
   `task_submission.py`；状态/部分项目计数写入归 `LabelingTaskService`，证据归
   `annotation_evidence`，应用组合反馈、审计和通知。整批默认 200 题分块提交，
   不与单题事务或最终批次投影提交合并。
5. 领域执行派发：
   - 批量预标注：`application/preannotation_contract.BatchPredictionDispatcher` →
     `deps.get_batch_prediction_dispatcher` → `workers/batch_prediction_dispatch.py`；
     原 `workers.tasks.batch_predict` 参数/任务名保留，批量及分阶段状态机在
     `application/preannotation_execution.py`。帧元数据在同 Session 读取，ID/帧数事实
     经注入端口交给 `workers/frame_preannotate.py` 规划与派发 chord。
   - 质量：`app/services/quality_job_dispatch.py::QualityJobDispatcher` →
     `app/deps.py::get_quality_job_dispatcher` 装配
     `app/workers/quality_dispatch.py::CeleryQualityJobDispatcher`；
     覆盖三类质检、Mask 修复/回滚和格式导入。
     点云派发失败的运行终态归 `point_cloud_quality.service.record_dispatch_failure`；
     HTTP 保留通用作业失败命令及两者共同提交，原受理/派发窗口不变。
     Mask QC 与点云质检的人工 issue 决策分别归 `app/services/mask_qc/service.py::decide_issue`
     和 `app/services/point_cloud_quality/service.py::decide_issue`；两者保留冻结 Task
     身份与源新鲜度检查。API 仍解析当前角色/任务可见性并取得成员及 Task/issue 锁；
     点云审计、提交和响应 refresh 仍在原路由事务顺序内，issue 与源对象不转归反馈。
   - 媒体：`app/services/media_job_dispatch.py::MediaJobDispatcher` →
     `app/deps.py::get_media_job_dispatcher` 装配 `app/workers/media_dispatch.py`；
     数据集入库、视频缓存和资产重试共用该端口，连接器导入 worker 自行装配。
   - 训练数据集：`app/services/training_dataset_dispatch.py::TrainingDatasetDispatcher` →
     HTTP 的 `app/deps.py::get_training_dataset_dispatcher` 或周期策略 worker →
     `app/workers/training_dataset_dispatch.py::CeleryTrainingDatasetDispatcher`；
     领域保留 version/job/attempt 与派发失败竞态处理，适配器保留线程卸载。
     `/training-datasets` 创建命令由 `services/training_datasets.create_version` 持有，
     复用冻结选择和 `project_task_dataset.ensure_membership`；配方创建/追加修订的摘要重放、
     head CAS 与写入由 `services/training_recipes.write_revision` 持有；
     归档由同模块 `archive_recipe` 保持管理/行锁围栏及 head CAS，HTTP 保留提交和响应适配。
   - 原生训练预览过期：`app/api/v1/training_previews.py` 保留当前管理权、预览行锁、提交及 204 响应；`app/services/training_previews.py::expire` 设置过期时间并委托既有 `cancel`。排队记录即时终态取消，活动执行只请求停止并保留 CPU 租约；结果查询可显示 expired，周期 cleanup 的 PUT 宽限与物理删除仍由服务单独处理。
   - 执行取消：`app/services/job_execution_control.py::JobExecutionControl` →
     `app/deps.py::get_job_execution_control` 装配 `app/workers/job_execution_control.py`；
     只请求非强制撤销，不把请求回执当作已停止证明。
   - 失败预测重试：`app/services/prediction_retry.py::PredictionRetryDispatcher` →
     `app/deps.py::get_prediction_retry_dispatcher` 装配 `app/workers/prediction_retry_dispatch.py`；
     单项接收预选执行队列，批量失败项保留原默认路由，不用任务注册名充当接口。
   - 视频追踪：`app/services/video_tracking/dispatch.py::VideoTrackerJobDispatcher` →
     HTTP 的 `app/deps.py::get_video_tracker_job_dispatcher` 或 Mask 修复 worker 入口装配
     `app/workers/video_tracker_dispatch.py`；领域只收作业 UUID，不携带 Celery 注册名和队列。
6. 异步处理：`app/workers/signals.py` 将 `task_failure` / `task_revoked` 交给
   `app/application/job_terminal.py`，由工作流组合通用状态、领域回填及通知，
   信号适配器仍控制连接和提交。六类领域回填由各自服务的 `reconcile_job_terminal` 持有，
   经不可变 `app/services/async_job_terminal.py::JobTerminal` 传递事实；
   评估与主动学习走各自中断规则，不抹平 `partial`、`rollback_failed` 或非终态取消意图。
   HTTP 取消经 `app/application/job_cancellation.py` 组合准入和领域 `request_job_cancel`，
   保留 pending 立即取消、running 协作取消、评估取消意图与数据版本当前尝试隔离；
   事务仍由路由提交，列表/明细/取消的外部协议未变。
   训练数据构建取消经 `training_dataset_cancellation_contract.TrainingDatasetCancellationPort` 传 job/project/actor UUID，`training_datasets.SqlTrainingDatasetCancellation` 绑定原 Session/执行控制，保留 Version 锁、Job refresh、撤销和终态通知；不向领域接口传 Job/User ORM。
   通用行写入统一调用 `app/services/async_job.py`：`bind_execution_id` 绑定执行 ID，调用方保留预分配/回传 ID 的选用及既有提交时序；payload 浅合并、result 整体替换，
   `delete_terminal_jobs_before` 执行通用终态行删除；cleanup worker 保留 cutoff、终态集合、提交和删除 ID 计数。
   取消状态策略由执行器明确传入。跨帧保留逐帧进度提交及取消后的 100；
   连接器导入保留旧快照提交、逐文件事务和已经 cancelled 时的计数回填，不强制 100。
   `workers/predictions_retry.py` 继续使用仅替换结果、不改变生命周期的 `replace_result`。
   失败项重试经 `app/application/prediction_retry.py`，先提交占用再派发；
   发布异常仅释放派发调用尚未返回成功的项，保留已返回成功项的排队标记。该多阶段事务不与取消用例混为一谈。
7. 通知：`application/task_review.py` / `task_lifecycle.py` 保存审核/重开持久通知并 `commit()` 后调用
   `NotificationService.publish_committed()`；通用作业终态仍按
   `app/services/async_job_notify.py::notify_job_terminal` 的既有合同在调用方提交前写入和发布。
   列表/投递分别用 `_scope_conditions()` 与 `_allowed_delivery_indices()` 复用
   `valid_membership_conditions()`，投递路径额外要求账号活跃；不要混淆两种提交时序。
   讨论流的 `services/task_discussion.py` 与 `services/discussion_notifications.py` 直接调用
   `project_access.resolve_project_access_by_id` 和任务可见性所有者，不依赖 HTTP 装配；
   对实际接收人复核当前项目/任务资格，移除成员后不再成为受限讨论通知目的地。
8. 通知偏好 GET／PUT 经 `aap_notifications.preferences` 调用 `aap_notifications_v1.preference_command(jsonb)`；`NotificationService.notify` 先用同一 owner 预检静音，再经 `aap_notifications_v1.append_notification(jsonb)` 在原事务写入并合并返回行到 Session identity map。单条／批量已读经 `aap_notifications.read_state` 调用 `aap_notifications_v1.read_state_command(jsonb)`；0318 起，单条删除与清空已读经 `aap_notifications.delete` 调用 `aap_notifications_v1.delete_command(jsonb)`。这些命令保留 API 的认证、flush、提交和提交后同步职责，并按原 SQLAlchemy evaluator 同步 identity map；HTTP 入口仍持有提交权，通知按 `defer_publish` 合同发布到 `notify:{user_id}`。

## 4. ML backend 调用链

1. 协议：`apps/_shared/protocol_v2` 定义请求/响应 schema 与受控词表；
   `apps/web` 的生成类型与 API 快照由 CI 再生成，不手改。
2. 池内核：`apps/_shared/backend_runtime/src/aap_backend_runtime/managed_pool.py`
   提供 builder 单飞、borrower 串行、租约、quarantine 清理与强制回收；
   各 backend 通过 `logger_name` 保留自己的日志身份。
3. backend 本地实现：`model_pool.py` / `video_pool.py` / `pool_domain.py`
   是 per-model 包装，`gpu_lifecycle.py` 是 admission / generation / drain 状态机；
   这些**不**共享，因为池拓扑与 vendor 语义不同。
4. 平台侧消费：`apps/api/app/services/ml_client.py` 与
   `app/api/v1/ml_backends.py` 调 backend 的 HTTP 协议端点。
5. 共享预测准备：`app/services/prediction_context.py` 构建上下文与读取原图，
   `prediction_roi.py` 负责裁剪和回映，`prediction_progress.py` 发布现有项目/全局进度。
   HTTP、同步二次推理、批量及逐帧 worker 直接导入下层所有者；
   原执行编排与结果写入事务不变，没有增加语言替换所需的远程协议。
6. 预标批量清空：`app/application/bulk_clear_preannotate.py` 组合模式、当前账号/项目围栏、
   逐项结果及单次审计提交。标注软删和父预测解绑在 `services/annotation_cleanup.py`，
   分区预测与 Meta 删除在 `PredictionService.delete_for_tasks()`，
   预标作业删除在 `services/async_job.py`；批次/任务状态与计数仍归 `BatchService`。

## 5. 在线学习与模型训练调用链

1. 数据版本化：`dataset_task_facts.ProjectTaskSourceReader` 接收有界选择并交付不可变事实，`deps.get_labeling_task_source_reader` 在请求内绑定 `labeling_assets.SqlAlchemyProjectTaskSourceReader`；该适配器用单条 SQL 固定生产资格、目标工具标签及媒体/配置事实，其他工具只计数。`training_datasets.py` 消费事实构建训练身份；周期策略由 `application/learning_policy_execution.py` 在每个策略的原 Session 内绑定同一 reader。Celery export worker (`app/workers/training_dataset.py`) 下载校验原图并物化数据包，上传至 MinIO `training` 桶。
2. 执行调度与排队：`app/services/training_runs.py` / `training_studies.py` 管理训练实验与参数搜索计划，通过数据库资源锁和租约在多框架、多实验间排队。共同下层由 `training_adapters.py` 持有资源标识、配置目录与参数解析，`training_claims.py` 持有项目事务锁及策略/搜索的领取准入，不反向依赖编排服务。
3. 执行器通信：`apps/training-executor` 通过 `/api/v1/internal/training` 拉取实验与任务凭据，以 OS flock 独占 GPU 文件锁，拉取不可变训练包并守护训练子进程；阶段耗时、资源采样与脱敏日志通过 `/observations` 上报。
4. 产物与模型回流：`training_artifacts.py` 统一流式产物校验与有界 JSON 读取。负责人注册不可变 `TrainedModelVersion` 后，`application/trained_model_deployment.py` 协调 `trained_models` 的检查账本和 `trained_model_backend` 的认证安装/固定样本试用；checking 先提交，结果需重取当前管理权限并通过同一 `check_id` 才能发布。注册、检查、候选决策和 `ModelRelease` 发布共用 `training_claims.project_lock`；原发布代次 CAS 与冻结预测身份保持不变。
   冻结与验证由 `trained_model_reference_contract.FreezeModelReferencePort` 定义，`trained_models.bind_frozen_model_reference` 绑定原 SQL Session。API、推理路由、批量执行、评估与主动学习统一使用该数据契约；固定试用仅传普通事实，原 JSON 摘要与三字段模型 wire 不变。
5. 谱系与决策：`app/services/model_lineage.py` 提供单跳追溯，`model_decisions` 记录负责人处置（发布、归档、说明追加）。详见[在线学习与训练体系](./training-datasets)。
6. 评估与主动学习的共同下层：`sample_evaluation_sources.py` 负责原图条件读取及评估前后核验，通过 `labeling_evaluation_source_contract.CurrentTaskSourceReader` 获取当前标注切面；请求路由绑定 `labeling_evaluation_source.SqlAlchemyLabelingEvaluationSourceReader` 到原 Session。主动学习的 `LabelingActiveLearningSources` 单独交付候选分页/来源与编辑围栏，Assets 通过 `assets_active_learning_fence` 持有素材共享行锁；路由组合仍使用同一 Session。`active_learning_pool.py` 保留学习历史排除、媒体核验和 preview 摘要，`active_learning_references.py` 持有版本/评估引用及删除保护。主动学习编排、评估清理和项目/素材/任务删除入口使用同一引用所有者，保留原 409 与事务顺序，不通过高层编排互相导入。

## 5.5 数据集、目录来源与受控存储调用链

1. **存储位置、连接器与九用途绑定**：`app/services/storage_connection.py`（Fernet 加密 + `connector_guard.py` SSRF 白名单）与 `storage_location.py` 管理外部连接及四类存储位置（`filesystem` / `s3` / `sftp` / `agent`）；`dataset_binding_change.py` 与各用途 `*_storage.py` 管理九用途绑定切换与 27 家族保存目标（`storage_ref`）隔离。位置级访问准入与排空由 `storage_access.py` 和 `storage_maintenance.py` 维护。
2. **目录扫描、预检登记与增量同步**：`app/services/dataset_scans.py`、`dataset_registration.py`、`dataset_source_changes.py`、`dataset_source_revisions.py` 与 `dataset_source_sync.py` 编排目录枚举（底层调用 `apps/_shared/filesystem_read`、`sources/s3_reference.py`、`sources/sftp_reference.py` 或 `agent_storage.py`）、六类增量差异预检、内容修订与 Beat 周期同步。
3. **跨网络只读站点 Agent**：`apps/storage-agent` 轮询 `app/api/v1/site_agent_transport.py`（`app/services/agent_transport.py`），借由 `apps/_shared/site_agent_contract` 校验命令并上传 `256 KiB` 分块；显式同内容备用来源由 `dataset_reference_replicas.py` 调度。
4. **原件验证读取、缓存与保留回收**：`app/services/original_sources.py`、`managed_original_reader.py`、`original_binary.py` 与 `original_playback.py` 提供严格内容核验读取、认证下载与原生视频分片舞台；`original_cache.py` 管理按需完整原图缓存；`managed_original_creation.py`、`managed_original_retirement.py`、`managed_original_retention.py`、`managed_original_verification.py` 与 `managed_original_reclaim.py` 管理条件创建、删除保留账本、声明保留要求与准确版本回收。
5. **数据集工作台、质量检测与存储迁移**：`app/services/dataset_workbench.py` 提供六态观测聚合、组合筛选与关联项目可见任务统计；`dataset_quality.py` 与 `dataset_quality_review.py` 执行可恢复图片质量检测与固定范围批量复核；`storage_migration_*.py` 与 `storage_usage*.py` 提供配置源迁移清单、复制批次、维护窗口、已封存读取映射（`PhysicalReadPlan`）、九用途原子切换与六分类容量盘点。详见[存储位置、连接器与用途绑定](./storage-connections)。

## 6. 测试层级与运行入口

分层口径、运行命令与接线状态见[测试指南](/dev/testing)。速记：

- 后端纯规则测试不落库；事务/锁/通知顺序测试用独立连接。
- 前端页面测试优先在 MSW API 边界描述响应（`src/test/` 提供最小 fixture）；
  纯 URL/几何规则下沉到 `*UrlState.test.ts` / 几何单测。
- ML backend 与 `apps/_shared` 五个共享包（`backend_runtime` / `mask_utils` / `protocol_v2` / `task_contract` / `training_contract`）
  的 `tests/` 执行入口是可复用工作流 `.github/workflows/ml-cpu-test.yml` 与 `scripts/run-ml-cpu-tests.sh`。这些测试全部 CPU 可运行，不存在“硬件专属”测试。

## 7. 改动时的对齐参考

以下是现行实现的归属约定，不是新增门禁：

- 项目资源路由在 `resolve_project_access` 上做项目级授权；认证、健康检查与
  平台级管理入口各有自己的守卫（`get_current_user` / `require_roles` 等）。
- 带领域账本的异步作业在 `application/job_terminal.py` 显式绑定领域处理器；
  没有领域账本的作业（如 batch_predict）保持只有通用作业行，正常 worker 完成路径不变。
- 页面 URL 规则放 `*UrlState.ts` 并配纯函数测试；页面保留代表性集成用例。
- 共享包新增能力前，先确认消费 backend 是否同契约；per-model 语义留在 backend 本地。

## 公开契约与入口台账

此处是入口覆盖索引，不建立第二套路由、任务注册或数据真值。完整公开操作以 `apps/api/openapi.snapshot.json` 为准；`docs-site/api/guides/_routes.generated.md` 是源码装饰器索引，不能代替运行时注册表。数据写入见[所有权台账](./backend-layers#权威数据与写入所有权)，外部状态见[存储台账](../reference/storage-buckets#对象前缀所有者与删除边界)。

### 主 API 的 HTTP 覆盖

“公开”指被 OpenAPI 导出，不等于免鉴权或可从公网访问。按现有 tags/入口职责分组如下；这不是数据库所有者分组，例如推理结果入库和人工接受仍由 labeling 持有。

| 入口家族                       | paths | operations | 覆盖                                                                                   |
| ------------------------------ | ----: | ---------: | -------------------------------------------------------------------------------------- |
| identity                       |    52 |         61 | auth、邀请、me、users、groups、API keys、头像                                          |
| workspace                      |    31 |         41 | projects、项目模板、保存的流水线、指南资产                                             |
| assets                         |    40 |         51 | datasets、scenes、videos、files、存储连接器                                            |
| labeling                       |   123 |        138 | tasks、batches、annotations、history、comments、feedback                               |
| inference / tracking / quality |    82 |         94 | ML registry/pool、能力/集成、predictions、tracker、Mask/点云质检、格式与 alias 统计    |
| learning                       |    87 |        105 | dataset、run/study/executor/preview、recipe、evaluation、active-learning、model/policy |
| insights                       |    34 |         37 | Data Manager、dashboard、performance、task views、search、analytics                    |
| supporting                     |    36 |         41 | notifications、storage、settings、jobs、audit、bug reports、health                     |
| 合计                           |   485 |        568 | operationId 全部唯一；另含 57 个 tag、872 个 schema                                    |

源码路由索引只收集非空字面量路径，当前为 82 个模块、509 行；根路径操作、独立 executor/health 装配、隐藏路径的口径不同，不应为了对齐数字删改 API。以下入口单独核对：

| 边界               | 当前注册                                                    | 导出 / 准入                                                                                         |
| ------------------ | ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 应用隐藏 HTTP      | `GET /metrics`、`GET /api/v1/internal/metrics-targets`      | 不导出 OpenAPI                                                                                      |
| 受控 seed          | 21 个操作，19 POST / 2 GET，位于 `/api/v1/__test/seed/*`    | 仅非生产且 seed 开关开启时挂载，逐请求验证隔离测试数据库；不计入公开快照                            |
| 训练 executor HTTP | 10 POST，属于上表 learning 子集                             | 内部路径也导出 OpenAPI；使用 executor token，不能套普通用户授权；当前 Nginx edge 阻断 internal 前缀 |
| 框架文档           | `/openapi.json`、`/docs`、`/docs/oauth2-redirect`、`/redoc` | 框架默认入口，不计入业务操作                                                                        |

### WebSocket 入口

| path                                    | 数据 / 派送责任              |
| --------------------------------------- | ---------------------------- |
| `/ws/projects/{project_id}/preannotate` | 项目预标注进度与当前访问重查 |
| `/ws/batches/project/{project_id}`      | 项目批次进度                 |
| `/ws/prediction-jobs`                   | 预测作业进度                 |
| `/ws/video-tracker-jobs/{job_id}`       | 视频追踪作业进度与可见性     |
| `/ws/ml-backend-stats`                  | backend 运行观察             |
| `/ws/notifications`                     | 用户持久通知的实时派送       |

这些 socket 使用 Redis pub/sub，不形成第二套业务账本或持久重放协议。鉴权是 socket 特定合同：共享 JWT/API-key 解析与 notifications 的 JWT 握手不能被合并成一种可互换认证。

### Celery 执行入口

`workers/celery_app.py` 与各模块装饰器是注册名/签名真值；下表覆盖全部 first-party task-bearing 模块，不含 Celery 内建任务。Python 注册表导入核对不是 broker、消费者或真实模型资格。

| 家族               | task-bearing 模块（模块内 task 数）                                                                                                                                                                                                                                              | tasks | beat 项 |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----: | ------: |
| 推理 / 追踪 / 控制 | `tasks`(1)、`predictions_retry`(1)、`frame_preannotate`(4)、`video_tracker`(1)、`ml_health`(3)                                                                                                                                                                                   |    10 |       3 |
| 素材派生           | `media`(8)、`image_pyramid`(2)                                                                                                                                                                                                                                                   |    10 |       2 |
| 质检 / Mask 执行   | `video_track_quality`(1)、`mask_qc`(1)、`point_cloud_quality`(1)、`mask_repair`(2)、`mask_format_import`(1)                                                                                                                                                                      |     6 |       0 |
| 导入 / 建题 / 导出 | `dataset_import`(1)、`create_tasks`(1)、`cross_frame_job`(1)、`export`(1)、`training_dataset`(1)                                                                                                                                                                                 |     5 |       0 |
| 学习编排 / 保留    | `sample_evaluations`(2)、`active_learning`(2)、`learning_policy`(1)、`training_study`(1)、`training_preview_cleanup`(1)、`training_observability_cleanup`(1)                                                                                                                     |     8 |       6 |
| 支撑持久化 / 维护  | `cleanup`(4)、`audit`(1)、`audit_partition`(2)、`task_events`(1)、`presence`(1)、`deactivation`(1)、`notification_delivery`(1)、`identity_mail_v2_delivery`(1)、`analytics`(1)、`async_jobs_cleanup`(1)、`export_cleanup`(1)、`prediction_partition`(1)、`feedback_reconcile`(1) |    17 |      15 |
| 合计               | 36 个模块                                                                                                                                                                                                                                                                        |    56 |      26 |

include 另外包含 `signals`、`heartbeat`，后者是 worker bootstep，不是 task/beat。任务、结果与 accepted content 保持 JSON，调度使用 UTC。静态 exact-name 路由如下；未覆盖项走 default，项目/设备选择的动态队列不等同于该表，队列声明也不证明消费者正在运行。

| 队列            | exact-name 路由数 |
| --------------- | ----------------: |
| `default`       |                11 |
| `ml`            |                 3 |
| `media`         |                16 |
| `image-pyramid` |                 1 |
| `cleanup`       |                 8 |
| `gpu`           |                 1 |
| `export`        |                 2 |
| `ml.cpu`        |                 2 |
| `audit`         |                 2 |
| `maintenance`   |                 5 |
| `gpu.control`   |                 1 |
| 合计            |                52 |

注册名不是函数名的简单推导：质检保留 `app.workers.point_cloud_quality.run` 和 `app.workers.video_track_quality.run`。训练数据构建保留 version/job/attempt 与明确执行身份，质检传 run ID、修复/导入传 batch ID；不能在不迁移生产者和在途消息的情况下改名、重排参数或把它们宣称为跨语言公共协议。

### 运维入口与独立执行器

主 API 的 Python 可执行入口口径为 `apps/api/scripts/*.py` 与 `app/cli/**/*.py` 中的 `__main__`：29 个脚本和一个 application CLI。分类是审计索引，不表示可以安全地对任意数据库执行。

| 入口 / 分类                       | 所有权与提交责任                                                                                                                                                                                                                                                                                                                               |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bootstrap_admin`                 | `AccountProvisioningManager` 按 exact email 创建或跳过；Identity/Audit owners 原子写 User 与固定 Audit；CLI 保留提交和 boolean 返回                                                                                                                                                                                                            |
| `import_images`                   | Dataset/Item 命令归 assets；对象上传先于最终 SQL 提交                                                                                                                                                                                                                                                                                          |
| `import_nuscenes_scene`           | nuScenes 应用组合 provisioning UUID、assets、workspace、labeling；入口在同一 SQL 事务最终提交，失败时共同回滚                                                                                                                                                                                                                                  |
| `reset_datasets`                  | 应用组合 labeling 源引用清除与 assets 行删除；入口提交                                                                                                                                                                                                                                                                                         |
| `backfill_scenes`                 | 场景所有者命令，逐 dataset 提交/回滚                                                                                                                                                                                                                                                                                                           |
| `backfill_frame_poses`            | pose upsert 归 assets，CLI 最终提交                                                                                                                                                                                                                                                                                                            |
| `backfill_image_pyramids`         | 只读选择后派发素材派生，不直接改业务行                                                                                                                                                                                                                                                                                                         |
| `backfill_task_datasets`          | 应用组合 task-dataset 成员命令；apply 按批提交，其余模式 SQL read-only                                                                                                                                                                                                                                                                         |
| `app.cli.video.rebuild_timetable` | 应用组合素材时刻表/错误命令；逐 item 提交并在回滚后刷新后续身份                                                                                                                                                                                                                                                                                |
| seed / fixture / 恢复清理（15）   | `seed`、`seed_assets`、`seed_coco8`、`seed_large_images`、`seed_ocr`、`seed_pointcloud`、`seed_scale`、`seed_video`、`nuscenes_fixture`、`prepare_e2e_db`、`restore_screenshot_user_preferences`、`cleanup_screenshot_ocr_flow`、`screenshot_background_export_fixture`、`screenshot_job_recovery_fixture`、`screenshot_review_reject_fixture` |
| 诊断 / 契约 / 产物工具（6）       | `audit_project_roles`、`benchmark_image_pyramid`、`check_removed_service_modules`、`dump-openapi`、`freeze_gpu_ledger_lua`、`validate_gpu_arbitration`；诊断分类不保证只读，benchmark/资格工具可能创建夹具并执行负载                                                                                                                           |

`entrypoint.sh` 的迁移/进程启动、权限维护 SQL、诊断 SQL 和根目录的部署/worktree 管理器是运行时装配，不混入上述业务命令。训练执行器是 `python -m training_executor.worker` 的独立 HTTP 拉取式进程，不是 Celery task；CPU preview 与训练子进程监督共享其既有运行边界。Python SDK 的 `aap` CLI/TUI 是 HTTP/WS 消费者，不是额外的数据库写入口；此台账不代表 SDK 能力覆盖验收。

### 独立 ML 服务与契约真值

五个 ML FastAPI 应用不是主 API 的 mounts。以下仅是各 `main.py` 装饰器的源码声明计数，不代表已导入/启动模型或取得设备资格；其中 SAM3/Grounded SAM2 各含一个隐藏 metrics 声明，框架文档不计入。

| 独立应用                     | HTTP 声明数 |
| ---------------------------- | ----------: |
| `apps/yolo-backend`          |          17 |
| `apps/sam3-backend`          |          13 |
| `apps/grounded-sam2-backend` |          13 |
| `apps/rapidocr-backend`      |          10 |
| `apps/onnxtools-backend`     |          10 |
| 合计                         |          63 |

素材探测是另一个独立运行单元：`apps/media-probe` 提供三个版本化探测操作以及 health，
不计入上述 ML 声明数或主 API 路由数。它的非 root 镜像和锁文件只包含探测依赖；
主 API/worker 可按配置调用本地包或独立 HTTP 服务。协议见[素材探测](/api/guides/media-probe)，
全模块提取状态见[模块部署](/dev/concepts/module-deployment)。

| 合同                                                              | canonical source                                                                                              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| HTTP 请求 / 响应                                                  | `apps/api/app/schemas/*.py` 及路由内导出 DTO；`scripts/export_openapi.py` 导出主快照，docs 同步副本不另立真值 |
| 预标注请求 / stage                                                | `aap_inference.preannotate`，不是 projects 路由中的类型                                                       |
| 几何 / tool bindings / attributes / calibration                   | `schemas/_jsonb_types.py`                                                                                     |
| 视频媒体元信息                                                    | `schemas/task.py::VideoMetadata`                                                                              |
| 独立素材探测 wire                                                 | `apps/media-probe/src/aap_media_probe/protocol.py` 与独立应用导出；`apps/media-probe/openapi.json` 是生成快照 |
| 视觉任务词表与归一化                                              | `apps/_shared/task_contract`                                                                                  |
| ML wire / lifecycle / masks                                       | `apps/_shared/protocol_v2`                                                                                    |
| 冻结训练、原生执行、preview / observation / budget / model return | `apps/_shared/training_contract`                                                                              |

API-owned DTO 仍被多个领域消费，`schemas/project.py` 仍依赖服务侧 capability，部分本地 contracts 仍暴露 Session/ORM。它们是已知本地耦合，不因放进 schemas/contracts 文件就成为语言中立协议。提取条件与强事务限制以[后端分层](./backend-layers#跨语言提取前的条件)为准。
