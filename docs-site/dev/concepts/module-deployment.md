---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-25
---

# 所有业务模块的独立部署与语言替换

当前后端还不能把所有逻辑模块单独部署并直接替换实现语言。此前完成的所有权梳理、Python 进程内端口和局部调用迁移，为逐模块独立运行建立了边界，但多数端口仍绑定主 API 的 `AsyncSession`、ORM 和装配图。本页记录完整目标、当前证据与缺口。实现状态按证据逐模块更新，不使用百分比推算。

## 范围和判断口径

目标范围包括 `identity`、`workspace`、`assets`、`labeling`、`inference`、`learning`、`insights`，以及 `jobs`、`audit`、`notifications` 三项支撑能力。运行装配、迁移、队列、对象存储和运维入口也必须明确归属，才算模块可以独立构建、部署和维护。

路由 tag、Python package、Celery queue 或一个独立 worker 都不等于完整业务模块。一个模块只有同时拥有稳定的语言无关公开协议、独立运行工件、明确数据和凭据范围、完整消费者迁移及故障恢复证据，才算具备独立部署能力。所有公开交互还需证明不依赖提供方的语言运行时；实际选中的语言替换实现须通过相同合同与真实消费者验收。共享 PostgreSQL 实例可以保留，但不能让模块绕过其他所有者的契约读写其表。

## 当前模块状态

当前入口清单覆盖主 API 的 485 paths、568 operations，另有 WebSocket、Celery、训练 executor、CLI 和诊断工具。逻辑所有权台账覆盖 136 张 ORM 表；受理输入、批量清单、视频帧计划与跟踪输入另有十二张 Assets 账本及四张 Labeling 受理／证据表，由 native SQL 迁移持有。路由数和表数用于定位现有边界，不代表模块已有稳定 wire schema。具体入口以[仓库地图](./repository-map#公开契约与入口台账)为准，表和事务以[后端分层](./backend-layers#权威数据与写入所有权)为准。

| 模块          | 已有证据                                                                                                                                                                                                                                                                                          | 独立部署与替换仍缺少的能力                                                                                                                                                                                             |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| identity      | 独立 owner 包、版本化协议、能力凭据和公开 API／运维消费者已迁移；当前检查点通过 268 个去重定向案例及完整冷装配。源码已合入主工作树，日常库升级至 0306，既有内嵌 API 的健康与热重载通过。                                                                                                          | 本地功能资格和主工作树源码采用已完成；日常独立 owner 拓扑、另一语言完整提供方及生产容量尚未获得资格。                                                                                                                  |
| workspace     | 31 个路径、41 个操作、5 张所有者表。项目配置、模板、成员与项目删除已有领域 owner；项目删除和数据集解绑使用 Workspace 当前权限合同及同库事务协调。                                                                                                                                                 | Workspace 尚非独立服务。项目转移、邀请、成员生命周期仍涉及多 owner 当前授权、写入与审计；完整入口、迁移和独立凭据仍缺。                                                                                                |
| assets        | 40 个路径、51 个操作、34 张所有者表。视频探测有独立工件与 Node 互操作验收；目录、关联读取及 Scene/位姿/标定由既有 Catalog 工件提供；来源控制、六项连接管理操作、同步 ZIP/扫描、清理和持久连接器导入使用现有 Assets Source 运行时；本地图片与 nuScenes importer 使用单独的六项 operator SQL 能力。 | 视频与图片执行及引用已通过受限账号、独立进程及容器联调；Workbench 单图及普通图片批量输入清单已完成本地联调；一般 Blob、其余推理输入和运维写入仍待提取，全模块部署与语言替换尚未完成。                                  |
| labeling      | 123 个路径、138 个操作、36 张所有者表，覆盖任务、批次、标注、候选、审核、反馈、轨迹、Mask 和质检。多项写入端口及解绑主来源选择、保留检查、Task 清理和投影已有 owner 合同。                                                                                                                        | 许多操作仍绑定请求方 SQL Session。完整写入集合仍须与账号、成员、Task 和编辑锁的当前权威串行化；跨模块事务 owner 和远程撤权/重放协议仍待完成。                                                                          |
| inference     | 82 个路径、94 个操作与 tracking/quality 路由合计；9 张推理控制面所有者表。五个 ML backend 已是独立 HTTP 服务，已有 JSON/Pydantic 协议包。                                                                                                                                                         | ML backend 是计算子能力，不是平台 inference 模块。Registry、pool、GPU 仲裁、路由、pipeline 和作业账本仍在 API/Redis/Celery 内。需公开平台控制协议，约束 GPU 租约、取消和迟到结果，并解除对 Python 协议包的运行时依赖。 |
| learning      | 87 个路径、105 个操作、35 张所有者表。训练 executor 有独立 HTTP 拉取进程和执行权交接验收；项目删除与 Dataset/Item 删除、解绑已有保留检查能力。                                                                                                                                                    | executor 只是执行子能力；版本、训练、评估与发布账本仍由 API 持有，并依赖 Labeling/Assets 冻结来源、Workspace 权威和 Inference 安装/试用。                                                                              |
| insights      | 34 个路径、37 个操作；`project_task_views` 是其 1 张权威表，另有跨域列表、Dashboard、分析视图及物化统计。只读查询和可重建投影适合作为低事务风险候选。                                                                                                                                             | 查询仍跨 owner 表并共享授权判断；保存视图写入与运行时都在 API。需由各 owner 提供批量只读合同或可重建投影，定义新鲜度、授权和数据删除语义，避免逐行 RPC。                                                               |
| jobs          | `async_jobs` 为 1 张通用作业表；建题有创建/取消/恢复合同，项目删除保留原 Project scope，数据集解绑按 Dataset/Project/association 代次取消匹配的活动建题作业。                                                                                                                                     | Celery 注册名/参数仍是 Python 内部协议；通用状态和领域终态需独立作业协议、队列交接、取消确认及消费者重启恢复。                                                                                                         |
| audit         | `audit_logs` 为 1 张所有者表；月度汇总算法已移到本地 Python 函数；项目删除与数据集解绑会在各自同一事务中由 Audit owner 记录事件。                                                                                                                                                                 | 其他业务审计仍需与业务写入同事务提交；分区归档涉及对象存储。Go 月报仅是架构实验，不是生产模块或目标完成证明。                                                                                                          |
| notifications | 完整 Python owner、固定数据合同、独立 publisher／socket 及既有 API／worker 绑定已实现；采用候选通过四镜像、真实组合和完整 PG 恢复的限定本地资格，见[模块装配](#notifications-primary-adoption-candidate)。                                                                                        | 主工作树源码采用、日常库升级及独立拓扑未切换；历史 0313 资产迁移须与暂缓功能合并协调。Node 消费者已验证，完整替代语言 owner 和生产容量未验证。                                                                         |
| ops           | 主 API 的 29 个运维脚本、一个 application CLI、迁移入口和 Compose 运行单元已有清单；生产 API 与队列 worker 使用同一 API Dockerfile。                                                                                                                                                              | 需要逐模块独立工件、启动/健康/迁移职责、最小凭据、日志/指标与升级回退流程。现有队列拆分和运维清单不提供模块级运行时或权限边界。                                                                                        |

这些模块的公开 HTTP 仍由主 API 聚合。现有端口、owner 表台账和架构守卫证明的是代码内部依赖方向及选定写入职责，不证明独立二进制、独立部署、最小凭据或跨语言兼容。下述建题数据接口也只覆盖选定的事务操作，不改变模块整体未完成的判断。

## Notifications 整模块独立装配 {#notifications-primary-adoption-candidate}

当前采用候选的 Python owner、数据合同、即时／持久 publisher、socket、共享 Identity
准入、分离凭据、原 API／worker 入口与 SDK 原生 JSONB 载荷已具备。以主工作树
`5421fa1af` 为基线的业务实现自 `e19247554` 起未改，新增内容仅为既有冷验收接线、
测试依赖与证据。主工作树尚未合入，日常库及运行拓扑未切换。

源码 `913d3fc5c` 的一个冷组合案例和 `0325 → 0189 → 0325` 完整往返通过，覆盖真实
REST、原 WS 地址桥接、Node HTTP 字节保留和越界抑制、偏好／已读／删除同步、当前
撤权、终态提交前发布及回滚、普通失败最佳努力，以及恢复后持久 outbox 接管和新事件。
四个镜像均按候选实际输入构建并核对非 editable 安装；3,374 项运行输入未变，全部
自有资源和存储身份清理成功。最终启动器准备后重新应用并验证普通角色的私表、事实
视图及私有 helper 拒绝。已通过的包 224 项、准入 48 项和 SDK／CLI／OpenAPI 50 项在
核对未变源码及原结果哈希后复用，没有重复执行。

规范收据 `.worktree/notifications-candidate-cold-qualification.json` 的 SHA-256 为
`4057783eab9b2713a345ef6f3b060b7336e7a622e369bc318ac3200573e9460f`。
原两个准备阶段失败保留，成功记录不覆盖旧状态。Node 是实际消费者，未作为另一语言
owner；Go 仍仅实验，未取得其他平台或生产容量资格。

候选完整保留 `0307 → 0325` 的十九项历史迁移与五十七份冻结文件，未引入 0326 Jobs
列表能力。历史含 Workspace、Audit 和视频输入保留，0313 会改变资产垃圾领取围栏，
应与此前暂缓的训练／资产功能合并协调，不能当成纯 Notifications 升级直接应用日常库。
本地模块及采用候选资格已完成；主线合回、数据库采用与日常拓扑仍分别记录。

## Identity 整模块独立装配

`apps/identity`、共享邮件传输包及 `docker-compose.identity.yml` 复用已验收的独立实现。API 保留原 HTTP、事务和撤权语义；旧 Workspace 模板与编排通过已有 Identity 组织 ID 能力读取归属，普通 API 为两张公开关系和六项函数，无私表／列授权。

本检查点源于已接受的 Identity 源码及其线性迁移前置，未带入后续 Workspace 实现。252 个去重 API 案例和另一个受限 HTTP／Node 案例通过。主库的只读快照已在独占 PostgreSQL 中验证 `0234→0306` 升级及原快照恢复；原库只有一行业务数据，另在副本添加两名旧账号和两条恢复令牌验证密码、JWT 准入及预期的令牌哈希转换。

当前源码检查点已通过匹配冷工件和完整独立装配的真实消费者验收：一次组合案例含 66 次公开请求，以及 Socket、Node、邮件、通知、停服、重启和完整数据库恢复。另有 268 个去重定向案例通过。规范证据为 `.worktree/identity-adoption-qualification.json`，SHA-256 为 `7c7a3739e540e6f1de389f3b5667c8afc573865e0c6c37ceac3486a769aa19d3`。

已验收源码检查点 `d2ea90f3f` 已合入主工作树。采用前保留了权限为 0600 的数据库备份和集群角色备份，并在独占 PostgreSQL 验证数据恢复；日常库实际从 `0234` 升至 `0306`，171 张原有业务关系及唯一业务行保持。原 LaunchAgent 重新启动既有内嵌 API，DB／Redis／MinIO、注册策略读取和未认证拒绝通过，公开 OpenAPI 与快照一致；触碰源码后新子进程完成启动。采用记录为 `.worktree/identity-primary-adoption-delivery.json`，SHA-256 为 `5bc31b3fa2b4d3e5f95001f6d19707a73ec1acce250d3a5724bfc245fd5d1cd2`。日常独立 owner 拓扑未切换，Celery 仍未启动。Go 保持实验性质，其他模块的独立部署资格不随迁移前置安装而成立。

## 已存在的运行边界

生产 Compose 将 API 和按队列拆开的 Celery workers 都构建自 `infra/docker/Dockerfile.api`。worker 使用主 API 代码与共享数据库配置，并持有执行所需的 Redis/对象存储能力。队列隔离可以限制并发和资源，但没有形成业务模块的工件、数据权限或协议边界。

ML backends 和 training executor 已有独立运行单元。ML 服务只证明模型推理接口可独立运行；training executor 通过内部 HTTP 领取任务，证明训练子进程可独立执行和移交。这些服务都不足以代表对应的 inference 或 learning 业务模块已经完成。

Go 月报容器是独立的多语言架构实验。其部署或大请求容量结果不作为本目标的完成门槛；正常月报仍由 Python API 提供。Go 不进入默认或真实环境路径。

## 首个真实独立服务切片

assets 的视频探测能力已完成首片本地验收：`apps/media-probe` 持有视频元数据、完整帧时间表和分块 packet 样本的唯一探测实现、协议、独立锁文件和非 root 镜像。它只运行 `ffprobe` 和纯数据解析，不读数据库或对象存储。`app.services.media_probe` 是调用方的本地/远程装配适配器，所有实际消费者继续通过同一公开客户端获取完整结果。

服务接收不带对象键或 URL 的视频字节流，不持有业务数据库或对象存储凭据。视频元数据用有类型的 JSON 返回；帧与 packet 记录用 NDJSON 流返回，并以包含记录数和 SHA-256 的完成行结束。缺少完成行、数量不符或摘要错误时，消费者放弃结果；真实数据库验收确认随后提交也不会部分替换原有帧索引。调用端继续负责授权、读取源对象、写 assets 表和现有事务提交。字段、错误和摘要规则见[独立素材探测协议](/api/guides/media-probe)。

消费者包括 `apps/api/app/workers/media.py`、`apps/api/app/application/video_timetable_rebuild.py`、`apps/api/scripts/seed_video.py` 和测试用 `apps/api/app/api/v1/_test_seed_webcodecs.py`。本地装配复用包内同一实现；`MEDIA_PROBE_URL` 显式选择远程服务后不会因故障切回本地。独立容器与主 API 镜像均通过构建和真实导入检查；独立容器未安装 SQLAlchemy、Celery、Boto3 或 Torch。部署与配置入口见仓库 `DEV.md` 和 `docker-compose.modules.yml`。

完整时间表及 packet 样本会随输入规模增长。`ffprobe -show_frames -of json` 仍把探测输出整体读入内存，调用方也保留完整结果后再落库。NDJSON 避免新增一次整体 JSON 编码/解码副本，但不会消除原算法的线性内存需求；默认 1 GiB 文件上限、30 秒探测超时和容器预算不构成长视频或生产容量证明。每个远程操作还会上传一次源文件。

同一个真实 API 客户端先调用独立 Python 服务，再切到无 Python 依赖的 Node 测试实现，三个操作结果一致；另有真实 B 帧媒体、舍入边界、摘要、重启重放、上传限制和网络引用拒绝验收。独立容器的停服拒绝及重启重放也通过。Node 只作为第二语言验收夹具，不是生产语言选型；这些证据仅覆盖探测子能力，assets 全模块及其他模块继续按下方门槛推进。

## 首个带状态的数据接口

已有数据集的追加建题使用独立安装的版本化 PostgreSQL 能力：Workspace 提供项目引用锁，Assets 提供当前关联与素材最小快照，Labeling 的 `aap_labeling_v1.append_dataset_items` 写 Task、计数投影和冻结操作回执。调用方使用 UUID、UUID 数组与 JSON，通过标准数据库协议在同一连接的外层事务中组合，无需传递 Python Session 或 ORM 对象。函数、冻结 JSON Schema 与 Alembic 使用的 SQL 安装工件位于 `apps/api/alembic/sql/`；职责与安全约束见 [ADR-0092](/dev/adr/0092-transactional-module-data-capabilities)。

原上传完成、ZIP、扫描和连接器消费同一操作。重复的操作 ID 重放原结果，换请求复用 ID 被拒绝；旧追加入口每次生成新 ID，重复调用仍返回零。同步追加、关联创建、解绑和单素材删除按父行优先锁序协调，任务与计数不会因这些路径交错而部分提交。回执不因素材或项目删除而级联消失，当前没有自动回执清理。

当前独立角色验收包含 13 个模块专属 `NOLOGIN` 函数 owner 和 5 个受限 `LOGIN` 调用角色。迁移期主 API 仍持有原来的宽权限角色；受限角色能力不能被表述为整个 API 已隔离。项目引用锁只保证引用存续；初始操作接受另调用 Identity 当前账号和 Workspace 当前管理权合同，项目授权锁阻止负责人或模式更新穿过接受事务。

此前针对 0190–0195 追加与初始建题切片的隔离验收记录为 209 项业务、协议、消费者及角色用例通过，零跳过，覆盖真实独立登录、拒绝私有表访问与角色提升、原生 `psql` 调用、提交失败、丢响应重放、追加/解绑锁序、5,000 条素材追加及 33,000 条初始建题。使用受限账号的真实 Celery 在发起者停用后完成已接受作业，重复投递不重建任务；独立执行进程强制退出后可续跑，解绑和取消阻止后续块。迁移回退、当时冻结 SQL 的独立安装和外层回滚、再正常升级均通过；临时消费者、容器与隔离资源已清理。该历史记录不覆盖项目删除切片（0196–0200），也不是生产容量或数据库内部 SQL 工作量证明。项目删除切片的本轮验收见下节。

该边界仍依赖共享 PostgreSQL 连接与事务，不能直接拆成多个数据库。初次关联的分块建题已使用额外的持久操作、来源计划、游标和回执；当前许可在接受时固定，执行阶段按原关联和作业状态围栏。作业创建/领取/进度/完成/取消由 Jobs 能力持有，重复关联可找回活跃作业重新派发。账号停用或负责人转移保留已接受执行权，解绑或显式取消停止后续块；已提交任务保留。项目删除和数据集解绑现有 Workspace 主协调合同，分别见下两节；Dataset/Item 来源删除与上传切片已迁移，其他写入仍需提取，完整模块运行工件和凭据边界尚未完成。

参数、冻结结果、错误及恢复语义见[同库事务数据接口](/api/guides/module-data)。

## Workspace 项目删除事务切片

Workspace 的版本化 `delete_project` 命令在调用方外层事务中组合 Identity 当前活跃 actor、Workspace 当前管理权和项目行锁、Learning 保留检查、各数据 owner 的清理、Audit 项目删除记录及 Workspace 持久回执。通过保留检查后，Assets、Inference、Insights、Export、Diagnostics、Jobs、Learning 和 Labeling 分别清理自己的 SQL 行；Workspace 最后移除成员和模板来源引用并删除项目。任何 SQL 错误或最终提交失败都会使 owner 清理、审计和回执共同回滚。成功仍返回原 HTTP 204；原生调用方可用预先保存的 operation ID 在不确定提交后重放，重放要求原 actor 仍然活跃。

项目删除不清除源对象存储字节，不发布或撤销 broker 消息。Jobs owner 在 SQL 中取消活动建题作业、解除 live Project 外键，并通过不可重绑的无外键保留范围让作业与关联通知继续按原项目授权。未版本化作业、scope 丢失或与冻结 payload 不符时失败关闭；不能把旧作业改成全局作业，也不从 payload 回填权限。该事务仍依赖共享 PostgreSQL，不代表 Workspace 或参与 owner 已具备独立运行时。

本地独立 PostgreSQL 的整合回归为 432 项通过、零跳过，使用 11 个 NOLOGIN owner 和受限 LOGIN，覆盖各 owner 清理、学习保留、当前权限、锁争用、回滚/提交失败、审计和回执、旧孤立作业终态及作业/通知授权。真实 LOGIN 调用删除后，由无 Python 依赖的原生 `psql` 重放同一冻结回执；既有建题的真实 Celery 与独立 peer 交接也继续通过。全部冻结 SQL 独立安装/回滚、迁移回退/升级通过；自有容器、数据库、Redis 和隔离桶已清理。此结果不代表生产容量或完整模块运行资格。

完整实现与限制见[ADR-0094](/dev/adr/0094-transactional-project-deletion)。账号/成员生命周期、其他 Assets 操作及其他模块操作、完整模块运行工件和凭据仍是全目标的未完成工作；全目标门槛见下文。

## Workspace 数据集解绑事务切片

Workspace 通过版本化 `preview_dataset_unlink` 提供只读数量预览，并以 `unlink_dataset(operation_id, actor_id, dataset_id, project_id, cleanup_bucket, audit_context)` 协调实际解绑。新命令锁定活跃身份和操作 ID，先锁 Dataset，再重验当前 Workspace 管理权并以 NOWAIT 锁 Project，随后锁定原 association generation。Labeling 按 batch UUID 再 Task UUID 顺序锁定并验证来源/批次快照；Learning 检查所有 round/project 的 pool member 与 candidate 引用，Labeling 再保护人工反馈和跨 Task annotation lineage。

来源选择优先 `Task.dataset_item_id`；仅旧点云 Task 缺少直接来源时回退到 `primary_lidar` 链接，忽略 camera-only 和无来源 Task。通过保留检查后，Diagnostics 解除被删 Task 的 bug-report 引用，Assets 为精确的 Task-owned pyramid 记录 outbox，Jobs 只取消匹配原 association generation 的活动版本化建题作业，Labeling 删除对应 Task 子行并重算计数，Assets 删除锁定的 association generation。Audit 记录 `dataset.unlink`，Workspace 保存不可变回执；所有 SQL、审计和回执共用同一外层事务。

事务内不调用 S3 或 broker。独立 `apps/assets-cleanup` 用受限 LOGIN、专用 S3 身份和非 root 镜像轮询 outbox，替代旧清理 Celery task。它按精确 UUID/prefix/bucket 删除，等待捕获的 building lease，到期后才可领取和确认；失败记录不阻塞后续页，重启后可重试。REST 可选 UUID `Idempotency-Key` 在成功时回传，SDK/Web 的同次重试复用原 key；响应体仍为五字段，API key 写入需 `datasets:write` scope。

本节只记录 Dataset→Project 解绑与清理回执边界。Assets 公开来源删除与暂存上传的后续切片见下节。实现决策见 [ADR-0095](/dev/adr/0095-transactional-dataset-unlink)。本阶段独立 PostgreSQL 整合回归 516 项通过、零跳过，覆盖两个受限 LOGIN、原生 psql、回滚/提交失败、幂等重放和真实 Celery/MinIO 清理恢复；迁移往返及冻结 SQL 独立安装/回滚通过，隔离资源已清理。角色验收不等于默认 Compose 已部署受限清理 worker，也不提供完整模块或生产容量资格。该解绑切片验收时主 checkout 应用至 0205，API 热重载与 DB/Redis/MinIO 检查通过；日常 Celery 未启动，相关健康检查维持 503。

## Assets 来源删除、暂存上传与独立清理

Assets 的来源删除命令在当前素材库管理员与来源锁内组合 Learning/Labeling 保留检查、Jobs 导入检查及精确关联代次取消、批次来源归属解除、item-owned pyramid outbox、metadata 删除、Audit 和不可变 Assets 回执。主来源、所有相机/点云链接、标注、轨迹、质检、视频执行及主动学习引用都会保留；JSON-only 导入/质检接受也取得同一来源围栏。全局素材库政策保持不变，不新增 created_by 或项目负责人限制。

普通上传初始化只存 intent，签发 import staging PUT；完成时以同一 ETag 条件拷贝到本次 publication UUID 路径，再原子登记 Item、通过 Labeling 追加任务并冻结结果。已完成重试不重新拷贝。旧无 intent 记录不补造上传者，缺失/重复不删旧行或文件。原始字节始终保留；未完成 intent 和提交不确定时的未引用拷贝目前也保留，不能把 staging 生命周期当作所有原始拷贝的回收证明。

`apps/assets-cleanup` 是独立 Python 执行工件，只有 asyncpg/boto3 与纯协议依赖；没有 API/ORM/Celery/Redis 依赖。数据库角色精确执行六项能力：原存储 outbox 的 bucket/keyset pending、完成确认，以及图片垃圾、受理输入垃圾各自的领取和完成确认。S3 可以在 media-cache 的 `image-pyramids/*`、`image-assets/*` 列举与删除；版本化输入桶仅授予版本状态探测及 `accepted-inputs/*` 的版本列举、指定版本删除和 multipart 清理，不允许读取或写入内容。调用方提交 outbox 后无需 broker 唤醒。独立 Compose、secret 文件、健康/就绪及回退职责见 `DEV.md` 和 [ADR-0096](/dev/adr/0096-assets-source-deletion-and-cleanup)。

本地隔离 PostgreSQL、受限 LOGIN、真实 MinIO 与独立容器的有效结果共覆盖 630 个唯一用例，无业务跳过；先前失败/夹具缺失的用例以定向补验结果替换，不把多次执行重复相加。覆盖两种生产者、当前权限、引用保留、强事务、重放、UTF-8 key 预算、条件拷贝、lease 延迟、迟到对象、确认丢失与重启恢复。独立包 29 项单元用例及 writer deadline 2 项通过；自有数据库、Redis、桶、存储身份、临时容器和镜像均已清理。

这仍只覆盖所选 Assets 操作与派生清理进程，完整 Assets 的独立部署、完整语言替换及其他模块门槛保持未完成。

## Assets 目录独立运行

目录的列表、详情、文件分页及公开创建/更新由独立 `apps/assets-catalog` 工件提供，公开 API 保留认证与 scope 检查；原生 SQL 再按当前账号及全局素材库管理政策检查写入。每个写命令把最终结果与操作 UUID 保存到 Assets 的不可变回执，提交后的响应丢失、进程重启或实现切换均可用原键确认。省略/清空坐标系字段的区别与未知元数据保留不变。

目录服务仅有七项 SQL 执行能力和 datasets/cache 范围的只读对象身份，不能直接读取其他 owner 表，也不需要 API、ORM、Celery、Redis 或桶管理。空目录标记没有应用消费者，普通创建改为纯 SQL。网关远程模式故障时明确失败，切回本地也消费同一组原生合同和回执，避免另建一条业务写入实现。

该工件仍不覆盖导入、来源发布和媒体派生等完整 Assets 入口；独立目录不能替代全模块的部署、凭据和语言替换验收。协议、预算与切换次序见[目录协议](/api/guides/assets-catalog)和 [ADR-0097](/dev/adr/0097-independent-assets-catalog)。

本地整合与定向补验去重后覆盖 610 个数据库、合同和真实消费者用例，零有效业务跳过；迁移往返、原生冻结 SQL、受限角色、真实 S3/独立容器、提交后丢响应、重启和本地切换均通过。独立包、客户端、TypeScript、架构检查、文档和 API 镜像构建/冷导入也已验证。隔离测试资源按归属清理，这些结果不提供生产容量或全模块资格。

## Assets 与 Workspace 关联读取

两个公开关联列表已迁移到独立目录工件。Dataset→Project 保留原素材库全局可读政策，Project→Dataset 仍按当前项目可见性授权；两者的 API key scope 差异保持。Workspace 通过纯 `aap-workspace-contract` 配置快照和版本化 SQL 投影提供数据，Labeling 提供持久计数及直接来源聚合，Assets 统一返回版本化关联结果。

所有数据与授权使用同一 PostgreSQL 语句快照；读取不持有阻止解绑或撤权的行锁。并发提交后的变化在下一条 READ COMMITTED 语句生效。旧 ProjectOut 默认值、类别/属性派生、项目侧直接来源计数和 UTC 时间字符串保持，完整合同见[目录协议](/api/guides/assets-catalog)及 [ADR-0098](/dev/adr/0098-association-read-projections)。这仍是目录子能力，不构成完整 Assets 或 Workspace 独立部署。

本轮关联读取整合验收 660 项通过、零业务跳过，覆盖受限 SQL 角色、真实独立容器与 API、响应默认值、scope 差异、故障重启和并发快照。完整迁移四阶段、冻结 SQL 安装/回滚、独立包、SDK、TypeScript、架构、文档及 API 镜像冷导入均通过；测试资源已按归属清理。结果只提供本地切片资格。

## Assets 来源控制与连接管理

`apps/assets-source` 的既有 HTTP runtime 执行单文件上传初始化、发布完成、Dataset/Item 删除、同步 ZIP、旧前缀扫描，以及列表、读取、创建、更新、删除和测试六项连接管理操作。连接管理仍通过 API 原有认证入口；内部 Source 接口接收 API 提供的 actor UUID，并由 Identity 与 Assets 原生能力重新验证活动状态、连接范围和当前权限。`ASSETS_SOURCE_URL` 为空时使用本地 Source 工作流；配置远程 URL 后，远程故障不会回退到本地实现。Source 容器不是默认或生产 Compose 的一部分，按独立工件部署。

Source LOGIN 精确执行 26 项 Assets 能力：14 项既有来源/入库函数、9 项连接管理函数和 3 项图片来源能力（已接受来源解析、受理和旧数据集上传事实读取），不授予业务关系、序列或其他 owner schema 的直接权限。Catalog LOGIN 仍为 29 项，导入执行器 LOGIN 为 9 项；两个旧媒体投递 claim/ack 能力只供 API maintenance relay。

Assets Media 共用控制 LOGIN 执行 49 项能力（视频 18 项、图片 14 项、图片输入 4 项、图片批量清单 4 项、视频输入 9 项），视频 worker 执行 20 项（既有视频 10 项、视频输入执行 9 项和回收扫描 1 项）；图片执行 worker 使用独立 LOGIN，精确执行 6 项图片及 4 项输入能力。Assets cleanup LOGIN 执行 8 项能力，分别处理通用存储 outbox、图片垃圾、图片输入垃圾和视频输入垃圾。以上受限角色均不获得业务表、列、序列、外部 owner schema 或 DDL 权限。视频输入的原生状态、HTTP、独立执行、证据与回收已完成本地数据库和真实对象存储联调，Node 消费者验证全部十个 HTTP 操作。逐帧业务的受理、覆盖、推理、失败重试及候选采纳已接入固定输入；API/Celery、Media 和 Cleanup 的本地容器链路、固定负载与两类在途交接均已有本地证据；全模块目标继续推进。

API 的 `accepted_video_inputs` 适配器复用上述十项 HTTP 控制合同；本地模式装配同一控制器和执行器。清单捕获仅调用固定 SQL 能力，与原始 Job 共用提交事务；准备和精确版本校验在独立事务及对象 I/O 中执行。Labeling 的来源围栏和 Annotation 证据 pin 绑定原写入事务，不通过 HTTP。逐帧预标、覆盖、失败重试和候选采纳已使用该边界；完整 Assets、Labeling、Inference 和 Jobs 仍有其余入口待提取。

本地容器验收从 API 的真实 HTTP 受理开始，经过两个 Celery Worker、独立 Media 控制与视频执行进程，完成两段各 12 帧视频的固定版本读取、24 次模型 HTTP 调用和 24 条带帧证据的预测写入；重复段没有新增推理或预测。Media Worker 收到停止请求后约 0.17 秒退出，退出码为 0。清理容器另验证精确尝试前缀、未完成 multipart 和迟到版本回收。

API 和 Celery 使用普通应用数据库角色；Media 使用各自受限 SQL/S3 身份。主 API 启动仍检查十个桶，验收仅为这十个临时桶提供元数据与生命周期身份，并验证它不能读写源对象；Celery 不持有效直接存储凭据。容器不挂源码，主链路按 CPU、内存和 256 MiB 临时盘限制运行。模型是读取真实帧的确定性 HTTP 桩；24 帧从受理到完成耗时约 147 秒，包含准备、队列和重试等待。这是有限正确性样本，未提供 GPU、长视频或生产并发容量结论。

扩展负载固定为三个各 120 帧的 1280×720 视频，API 受理到完成为 256.459 秒，实际模型调用和预测均为 360 次，重放没有新增调用或写入；每帧只产生一个执行 attempt。HTTP 桩的读取与解码 p50/p95 为 4.023/5.076 毫秒，帧生成 attempt 为 409.585/783.868 毫秒。Media Worker 峰值约 392 MB，API 约 528 MB，两台 Celery 分别约 446/407 MB。模型响应仍在处理时，持有该分段的 Celery 容器在 3.64 秒内优雅退出，退出码为 0；另一台完成全部分段。进度采样的实际中位间隔约 2.223 秒，包含控制检查开销；缺少作业 started_at，未推算单独的排队与执行时间。900 秒仅是本次验收等待预算，模型为确定性桩，结果不包含 GPU 推理或生产 SLO。

Media 接管另使用 20 秒、600 帧、约 32.4 MB 的视频：暂停实际执行者 A，仅在隔离数据库中推进同一输入和 attempt 的租约到期时间，B 于约 1.89 秒领取第二代租约，约 3.56 秒完成封存；恢复 A 后原 token 被拒绝，B 的计划及精确帧版本和 SHA 保持不变。此场景覆盖挂起后的租约接管；Celery 优雅退出用例不提供强制杀进程后的任务重投结论。

连接凭据只以密文写入；列表与普通读取返回公开配置和 `secret_set`，捕获能力只向受信任的 Source 工作流返回加密凭据。创建、更新、删除和测试结果与 Audit 及 `storage_connection_operations` 不可变回执同事务提交。该回执表没有实时连接外键；删除连接不会级联删除已接受的导入快照。创建者对 owner 连接保留管理权限，哪怕之后平台角色降低；全局连接仍由超级管理员管理。新连接写入忽略旧 `project_id`，旧非全局连接在公开响应中映射为 owner 并保留其项目 ID。

连接器主机策略由 Settings 所有。`system_settings.connector_host_allowlist` 覆盖 Source 的 `ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST` JSON 字符串数组回退值。DNS 和连接探测在数据库行锁外完成；最终应用只在需要主机策略的写入时短暂共享锁定 Settings 表，复核策略快照后与 Audit、回执同事务提交。简单改名或空 patch 不访问策略，也不需要 Fernet key。Source 的 `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY` / `_FILE` 是可选启动配置；创建或解密加密凭据需要提供与本地 API/导入执行器相同的 key。`ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE` 可补充受信任主机文件，未知 SFTP 主机仍被拒绝。

连接管理的限域本地资格覆盖 13 个受限角色原生/HTTP 用例，包括真实 S3/SFTP、并发、重放、重启与切换。临近响应上限的列表测试读取 1,033 条记录、合计 42,139,197 字节，分 7 个内部页完成，单页最大 15,206,907 字节；在 0.5 CPU、512 MiB Source 容器中用时 6.238 秒，峰值内存为 149,778,432 字节。迁移四阶段验证通过。这些是隔离本地证据，不代表 CI、生产容量或完整 Assets 独立部署资格。

旧上传完成不再通过 API 的私有 ORM 修改来源；0213 原子组合元数据、Labeling 建题、计数和 Assets 自己的不可变回执。ZIP 以 `dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}` 写入不可变 publication，metadata 保存逻辑路径；扫描仅读取精确 Dataset name 前缀，不接管 modern publication 或未登记拷贝。ZIP/扫描 Item、计数、Labeling append、ZIP Scene/frame 更新、最终回执和媒体 outbox 在同一个外层事务提交。原始对象不会被覆盖或删除，完成回执可按原 actor/Dataset/操作类型/请求身份重放。Assets 当前共 34 张表，全仓 ORM 所有权台账为 136 张表；视频媒体有四张作业、回执、暂存和垃圾清理表，图片媒体有三张作业、不可变回执和垃圾清理表。这些边界不单独证明完整 Assets 模块的独立部署资格。完整协议、预算、部署顺序与回退见[来源控制](/api/guides/assets-source)、[同库数据接口](/api/guides/module-data)及 [ADR-0101](/dev/adr/0101-assets-synchronous-zip-and-scan)。

## 全目标完成门槛

全目标完成时，每个表列模块都要能提供以下证据：

1. **完整的公开操作清单。** HTTP、WebSocket、Celery、CLI、定时任务及内部执行器操作均归属唯一 owner，调用方迁移范围可追踪。
2. **冻结的语言无关协议。** 命令、查询、批量读取、schema 版本、状态、错误码、权限上下文、Blob 引用、幂等/attempt/generation 及兼容策略均有真实 fixture 和契约用例。
3. **可独立构建与部署。** 有单独工件、依赖、配置、健康/就绪状态、扩缩和运维流程；不导入主 API/Celery 装配，也不继承其他模块的数据库或存储管理凭据。
4. **唯一的数据写入所有者。** 表/字段、schema migration、对象前缀和 Redis 等外部状态都有权限边界。消费者只通过 owner 合同访问数据，不直接查写其他模块的私有 ORM/SQL。
5. **事务和撤权语义成立。** 每条跨模块强事务有单一事务 owner，或有经验证的可靠协调、收据和重放方案。权限撤销与最终写入保持当前权威串行化；对象存储、数据库和 broker 之间的失败窗口有明确定义。
6. **故障与切换通过。** 重复、丢响应、断网、迟到响应、取消、进程重启、在途执行、执行权交接、失败恢复和回退都通过隔离数据及真实运行进程验证。切换期间同一数据只有一个权威 writer。
7. **语言替换有证据。** 每个模块的完整公开合同都有独立运行时的互操作及领域 fixture 验收，消费方只使用本语言的公开客户端/数据绑定，不依赖提供方的私有 Python 实现、ORM、对象或异常。实际选择更换语言的实现通过相同合同、领域行为、真实消费者及切换/回退验收。语言按功能选择，不要求为每个模块永久维护两个业务实现；Go 月报实验不能替其他模块满足这些门槛。

同一进程中的 Python `Protocol`、DTO 序列化测试、目录分层、Celery task registry、单独启动的 health endpoint 或仅能读写共享数据库，都不能单独证明模块达到门槛。

## 实施顺序

详细依赖和结果写在仓库 `docs/plans/1792160873_backend-module-decoupling.md` 中。当前依赖顺序为：

1. 保持已通过的 media probe 独立运行、消费者和第二语言合同门禁；新增媒体类型或预算时补齐对应实际验收。
2. 逐项提取 assets 操作，定好 Blob 和引用协议；追加/初始建题、项目删除、Dataset→Project 解绑和连接管理已有选定事务合同，继续提取其余 CRUD、导入和派生写入边界。
3. 建立 identity/workspace 的远程身份、当前撤权和项目能力合同，再处理会依赖这些许可的写命令。
4. 定义 jobs、audit、notifications 与领域提交之间的可靠交接，覆盖 attempt/终态、审计原子性和持久通知投递。
5. 按事务边界提取 inference、learning 与 labeling。先固定 GPU/执行租约、训练来源快照、模型身份、审核写入及在途作业，再迁移生产者/消费者。
6. 提取 insights 的批量只读输入或可重建投影，固定授权、新鲜度、删除和大结果传输语义。
7. 为每个逻辑模块单独完成部署、合同互操作、领域行为、故障和回退验收；对选定的语言替换执行同合同第二实现验收，汇总全部公开入口和数据权限后才关闭总目标。

顺序可以根据依赖审查调整，但不能因一个子能力通过就把完整目标标记完成。

## 独立连接器导入执行

`aap_assets_source_import` 消费已接受的导入操作，以七项 execute-only Assets 能力和专用来源发布 S3 身份运行，不依赖 API/Celery/Redis 包。API 的接受/取消适配器使用版本化命令；Jobs、Settings 和 Notifications 的跨 owner 数据只经各自能力传递。

冻结清单、租约接替、条件源读取与不可变发布路径，使导入在进程中断后继续已提交进度。Item、Labeling append、计数、游标和派发 outbox 原子提交；显式取消与当前作业状态阻止迟到 I/O 重新建题。后续媒体/通知仍由现有执行方接手，outbox 只保证可恢复派发，不宣称计算完成或 broker 恰好一次。

迁移拒绝仍在活动的旧导入，需先由原运行时排空或取消，不能补造其执行权。完整边界与保留限制见 [ADR-0100](/dev/adr/0100-durable-connector-import) 和[来源控制](/api/guides/assets-source)。ZIP 与扫描已归入既有 Source 工件；其余 Assets 媒体/Blob、运维流程和其他模块的完整操作、凭据与故障切换仍未全部提取，全模块独立部署目标尚未完成。

## 可信本地导入器写入

本地图片和 nuScenes 导入脚本通过 Assets 的六项 operator SQL 能力登记数据，不经过 `apps/assets-source` HTTP runtime，也不读取 `ASSETS_SOURCE_URL` 或在远程错误后回退。它们使用调用方 SQL Session；nuScenes 的 Dataset/Item、Labeling、Workspace 和空间事实仍在原有外层事务中提交。S3 上传或 nuScenes 对象覆盖无法随数据库回滚，原始字节保留。独立 `asset_operator_client` 只可执行这六项函数，不具有表权限或其他 owner schema 权限。owner UUID 保持归属标记，图片 owner 选择和 nuScenes Catalog 资格/锁顺序沿用现有行为。

0218 新增的 `dataset_operator_operations` 以不可变回执记录操作且没有 live Dataset/Item 外键。追加、更新最多 500 项一批；读取使用有序 Item UUID 游标和跨页 snapshot digest，单页受 15 MiB 记录 JSON、16 MiB 响应和 500 项限制，单次请求受 8 MiB 限制。更新在 Assets 校验 opaque source snapshot 后只合并请求字段。部署 grants 和完整字段语义见[同库数据接口](/api/guides/module-data#可信导入器数据集与素材登记)、仓库 `DEV.md` 和 [ADR-0104](/dev/adr/0104-operator-source-registration)。这是现有共享数据库上的可信本地写入边界，不代表独立 Assets 或 CLI 部署资格。

## 同步 ZIP 与旧桶扫描

公开 ZIP 和扫描入口已使用既有 Assets Source HTTP 工件及同库原生合同；本地装配复用同一工作流。ZIP 保留活跃账号与 `datasets:write` 权限，扫描保留管理员限制。旧 ORM 导入循环已移除，ZIP 使用不可变 publication key，扫描只分页读取当前 Dataset 的旧名称前缀。素材、任务追加、计数、ZIP 场景与帧分配、完成回执和媒体派发意图在同一事务提交。完整行为和恢复规则见[来源控制](/api/guides/assets-source)与 [ADR-0101](/dev/adr/0101-assets-synchronous-zip-and-scan)。

连接管理与来源控制共用既有 Source runtime，但这项边界更新不改变 ZIP/扫描的同步响应和对象预算，也不构成当前连接操作的真实环境或生产容量资格声明。

## 视频媒体控制与执行

`apps/assets-media` 提供独立控制和轮询执行进程，冻结 `aap-assets-media/v1` 数据及 HTTP 工件。共用控制 LOGIN 精确执行视频 18 项、图片 14 项及受理输入 4 项能力，视频执行 LOGIN 保留 10 项；视频的四张账本表记录逻辑作业、不可变操作回执、时间表暂存与可重试垃圾责任。来源、作业、租约与时间表依赖在发布时核验，执行进程不导入 API、ORM、Celery 或 Redis。

视频 REST、摘要、Data Manager 聚合、帧预标注、导出引用、seed、时间表工具及旧 Celery 注册入口已接入 owner。显式远程配置不会回退；本地绑定使用同一套实现。受限 SQL 账号、独立 HTTP/S3/FFmpeg 进程及两个带资源限制的容器已通过本地联调，覆盖租约交接、来源变化、缓存丢失与迟到结果围栏。完整字段、预算及恢复规则见[视频媒体接口](/api/guides/assets-media)。

容器验收使用两秒、20 帧的小视频；控制与执行的观测内存峰值分别为 95,670,272 和 88,903,680 字节。它验证当前本地预算下的部署和真实读写，未建立长视频、生产并发或 GPU 容量。精确帧按源解码序号选取，当前从头解码的成本仍需单独测量。

逐帧和专用跟踪作业分别使用自己的受理输入协议；其他推理消费方仍须按实际输入迁移。一般 Blob、章节和运维 reset 继续推进；这些缺口及其他模块没有因媒体子能力提取而完成。Go 仍是单独实验。

## 图片媒体控制与执行

图片缩略图、尺寸、金字塔、回填、签名引用和保留清理由 `aap-assets-images/v1` 合同连接。现有 Media 控制工件承载图片路由，独立 image-worker 轮询已接受作业；原 API ORM 图片执行实现已移除。本地 Celery 适配器使用同一套 owner 和任务级数据库会话，连续执行时不复用已关闭事件循环的连接。图片执行账号精确执行六项图片及四项受理输入能力，源对象只读，派生结果写入独立 attempt 前缀，删除由 Cleanup 持有。

本地联调覆盖实际 S3 条件读取、Pillow/blurhash 与 libvips 输出、来源替换、租约交接、解绑取消、迟到对象清理、冷安装和受限容器。图片容器在 2 CPU/2 GiB 下完成缩略图和 1024×1024 金字塔，观测内存峰值为 128,532,480 字节；该小样本不代表生产容量。工作台验证必需金字塔缺失时原图请求为零，尺寸探测成功后自动恢复小图，损坏图片保留明确失败状态。完整协议、迁移排空和回退保护见[媒体接口](/api/guides/assets-media)、[图片运维](/ops/runbooks/image-pyramid)及 [ADR-0106](/dev/adr/0106-independent-image-media-owner)。

## 已接受的图片输入与批量清单

`aap-assets-inputs/v1` 在既有媒体工件中提供十二项 native 能力及五项私有 HTTP 操作，固定持久 Job、Task 来源绑定、Assets 代次／快照和条件对象身份。副本进入独立版本化输入桶；整图 URL 与 ROI 使用相同 VersionId、SHA 和大小，缺失或损坏时明确失败。Workbench 单张图片的根推理、ROI、失败项恢复及候选证据已完成本地联调并合入主开发环境。

受限 SQL/S3 身份下的独立控制、图片执行、清理进程与清理容器已有本地证据；Node 消费五项 HTTP 操作，真实模型桩读取版本化原图和 ROI。原 Job 删除、原发起者停用后，当前有权限的用户仍可采纳受保留的候选并登记 Annotation pin。浏览器验收覆盖同身份请求重放、新运行及任务切换，响应使用受控拦截，不作为真实推理证据。

普通图片批量预标（含 Scene 模式中的单图 Task）使用 `aap-assets-input-manifest/v1`，在提交 Job 的事务中固定完整成员和逻辑来源，队列只携带清单身份及摘要。每页最多 100 项且受 2 MiB 字节预算约束，全项目／批次不增加总量上限，显式勾选保持 200 项限制。输入准备完成后封存逐题结果；执行验证原图后才清理该题旧预测，整图、ROI 和失败重试使用相同 Input。固定失败成员保留旧预测；缺少原始字节的失败项不再回退到当前来源。场景执行单位、点云多相机、文字与跟踪仍需各自固定输入；这项扩展不增加新的媒体协议或服务。

本地验收的 41 项受限 SQL/S3、迁移、API 及进程用例全部通过，包含安装后独立控制／图片执行进程、Node 读取 1503 个成员、丢响应重放、来源替换、部分失败和原 Job 删除后的恢复。另有 290 项后端回归通过；3 项需要显式独立 Celery 环境的既有帧验收未在本轮重跑。原发起者硬删除后保留证据，最终写入仍核对当前权限、来源及实际 Input 所属作业。清单后的页面故障会明确结束失败作业并记录进度，完整作业租约／进程接管协议继续提取。

Scene 执行单位和 Learning 首次来源组合尚未迁移。素材转换、Mask 修复批处理及部分视频 Mask 路径还存在持业务锁的对象 I/O，完整 Labeling、Inference 和 Jobs 独立协议也未完成。已有冻结训练包和评估样本继续使用 Learning 自己的保留副本。一般 Blob 生命周期和其余模块继续推进。Go 保持独立实验，没有默认接入或生产部署。

输入保留、当前权限、锁边界和后续完整迁移门槛见[已接受作业的媒体输入](./accepted-media-inputs)。

## 专用视频跟踪输入

跟踪、范围传播和 Mask 修正由 Labeling 的规范 `video_tracker_jobs` 提供执行权威；`async_jobs` 继续只承担汇总。两张 native Labeling 表保存受理快照和原片级 Annotation 证据，复用 Assets 视频输入、时间表与 attempt 账本。视频逐帧的原准入仍只支持 batch prediction／frame，不能用它代替专用跟踪授权。

Media 控制增加 `input`、`prepare`、`reference` 三项私有 HTTP 操作，不增加服务或配置。返回值提供固定执行事实、实测视频和计划摘要，模型窗口使用同一保留原片的版本化链接。Worker 通过独立的五项 tracker 租约操作复用现有原片物化器；四类工作公平轮询，tracker 与批量帧分别清理，避免未准备输入占满另一类领取或回收页。

准备未完成时保持 queued，在推理前延后同一作业；后续调整项目采样不改变旧网格。暂存前检查当前发起者、规范作业和来源，锁繁忙时保留同一推理结果；最终采纳则检查实际当前审核者、Task、分段、标注版本与保留输入，并在同一事务登记证据。普通跟踪取消可保留已完成部分供审核，修正取消继续丢弃结果。派生标注继承 pin，原规范作业删除不会解除已存在的原片保留关系。

升级须先处理缺少来源快照的旧活动／待审作业，不通过运行时回填历史证明。控制、执行、最终写入及 GC 的 native 授权各有精确 allowlist；共享主 API 的宽数据库角色和完整 Mask 业务协议仍是后续边界，不能据此认定 Labeling 或 Inference 全模块已完成。协议和切换说明见[媒体接口](/api/guides/assets-media#视频跟踪的原片输入)与 [ADR-0110](/dev/adr/0110-retained-tracker-video-inputs)。

## Mask 内容字节服务

`apps/assets-mask-content` 提供 `aap-assets-mask-content/v1` 与独立运行工件。内容进程接受 `put/get`，清理进程提供兼容的 `list/delete` 及持久回收的 `scan/execute/status`；两者使用各自的 token 与 S3 身份，固定到既有 Mask 桶的内容与日志前缀，不持有数据库、Celery 或 Redis 凭据。API 的 Mask 内容入口、导入、AI 采纳、修复、QC、追踪及导出继续使用既有包装函数，字节操作统一通过 owner。

内容地址、未压缩 SHA/长度以及旧 JSON/gzip 引用保持兼容。写入使用条件创建并验证已存在内容，读取复核引用和实际字节。显式远程配置不会回退到 API 的对象存储身份；未配置的开发模式装配同一实现。

Labeling 继续拥有任务权限、业务尺寸与帧校验、上传配额／预约、引用连接及完整回收根集。业务回收统一使用持久每键代次、删除意图与精确终态回执，不借用业务事务跨网络删除；近期同内容重传会续期预约并受生产宽限期保护。字节服务只比较对象事实，不读取业务表。当前逻辑键的删除与历史 S3 版本的物理回收仍是不同责任。

Python 实现复用现有编解码与 S3 栈，Node 测试提供方验证实际 Python 客户端能够通过相同 wire 合同更换实现语言；该 peer 是合同夹具，不代表第二语言持久运行时的生产资格。完整 Labeling 预约、写入与 GC 协调协议、一般 Blob 和其余模块仍须推进。协议与配置见 [Mask 字节接口](/api/guides/mask-content)及 [ADR-0111](/dev/adr/0111-independent-mask-content)。

## Mask 持久回收协调

`apps/labeling-mask-gc` 拥有独立 JSON 合同、函数专用 PostgreSQL 适配器、协调器和私有 HTTP/轮询入口。Labeling 原生 authority 保存每键代次、删除意图、生成内容宽限期、引用根和扫描游标；cleanup 登录仅能执行六项 GC 能力，四项业务生产能力不向它开放。字节进程通过独立日志前缀保存一次性删除请求和不可变终态回执，不读取业务表。

Celery 消费者通过 `services/mask_gc_collection.py` 选择本地协调器或 `LABELING_MASK_GC_URL` 独立 HTTP 服务，显式远程失败不回退。领域过期命令先提交，指标读取事务随后释放；原生 GC 操作每次使用新事务。只读预览调用 `inspect_mask_gc_page`，不建立意图、日志或破坏性扫描游标；物理续扫位置和扫描／删除预算分别返回。

本地验收覆盖受限账号、旧凭据和过期代次拒绝、失败前缀后的扫描推进，以及真实 S3 丢回包、HTTP 超时后迟到完成、字节进程重启与同键重传保护。没有终态回执时持续隔离该键，不按 HEAD 不存在或租约过期解除。当前切片的独立进程、跨语言消费和运行镜像按隔离本地门槛验证；完整 Labeling 与其他模块的独立部署、凭据收口及生产容量仍未完成。设计见 [ADR-0112](/dev/adr/0112-durable-mask-gc-fences)。

两类非 root、只读镜像的固定样本覆盖 1001 个保留对象及 7 个孤儿对象：预览按 1000 与 8 项续扫，删除预算为 3 时实际分三轮删除 3、3、1 个，引用内容全部保留。该样本的协调部分耗时约 3.42 秒，不含启动和播种；字节与协调容器峰值分别约 85 MiB、64 MiB。冷升级样本验证已有引用根回填、只读预览、拒绝遗失持久状态的降级，以及预览函数回退后的向前恢复；日常开发库未用于该演练。

日常开发环境已完成受控升级，使用当前内嵌协调器及持久结果账本；升级前的实际备份也在隔离库恢复并迁移成功。API 的包路径、只读预览、热重载及公开接口快照均已核对。八类 worker／beat 镜像已重建，消费者保持关闭；其 503 健康状态不能表述为正在执行的 worker 已就绪。日常库的核心业务表为空，有内容的保护与容量结论仍来自上述隔离样本。主 API 继续使用既有宽权限开发角色，完整模块的最小凭据边界尚未闭合。

## 普通批量结果检查点

`apps/preannotation-results` 提供 `aap-preannotation-results/v1`、独立包和函数专用 PostgreSQL CLI。Jobs 的两张 native 表持有执行租约、固定成员、覆盖标记和汇总；Labeling 的三张表按模型响应保存分块原始／准备结果、稳定 Prediction 身份和精确回执。最终 Prediction、Meta、回执、引用根释放和成员游标在同一事务提交，跨进程恢复不重做已完成推理、覆盖或计数。

独立执行角色仅能调用十二项 Jobs 和五项 Labeling 能力（含重试读取与派发），不能直接读取私有表或完成业务写入。`PREANNOTATION_RESULTS_DATABASE_URL` 或 `_FILE` 可选择该连接；默认仍由现有应用连接装配。原始字节完整后，即使 Mask 根登记遇到围栏，也必须继续恢复同一份结果。每条传输最多 2 MiB、单块最多 512 KiB，不对整题结果增加 16 MiB 或结果数量限制。

普通批量与重试的实际消费者、受限身份、提交回包丢失及真实 Celery 中断重投已有隔离本地证据；独立 wheel 和受限容器已核验，完整 Labeling、Inference 或 Jobs 服务仍有未迁移操作。失败预测重试已接入相同结果账本及单独 outbox；运行中跟踪输出增加持久根，结束时同事务转交待审根。日常 API 与回收消费者装配已切换，完整模块的独立部署及生产容量继续推进。协议见[同库数据接口](/api/guides/module-data#普通批量结果检查点)与 [ADR-0113](/dev/adr/0113-durable-preannotation-results)。

## 来源删除的辅助引用

Assets 的删除事务通过所属模块能力检查引用：相机链接、场景轨迹／质量历史、视频章节、媒体执行和保留输入会阻止来源删除；单条素材也检查所在场景。Assets 仅向 Labeling 提供素材到场景的投影，不开放来源表；原任务、标注和主动学习证据继续保留。仍有引用时返回 409 `source_in_use`，要求先在对应项目或功能中解除引用，旧 FK 的级联不会代为移除这些记录。迁移通过新增合同安装该规则；现有公开删除和幂等回执接口保持不变。

取消 tracker、终止轨迹和软删除相机标注会保留历史引用；部分记录没有独立解除命令，因此来源仍被保留。相机链接、轨迹与视频历史的独立生命周期仍是所属 owner 的后续工作。运维脚本 `reset_datasets.py` 直接组合全库来源脱离与表删除，不经过普通来源删除守卫，仍可能触发辅助引用级联；该重置入口未获得上述保留保证或模块最小凭据资格。
