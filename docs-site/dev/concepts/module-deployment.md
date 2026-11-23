---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-11-23
---

# 后端模块解耦、插件化与独立部署

当前默认部署是模块化单体。API 与 worker 可以在同一进程、同一事务中调用领域 owner 的固定能力，不直接读写对方私表。只有资源隔离、安全隔离或扩缩需求明确时才拆进程；拆进程不改变数据所有权。设计依据见 [ADR-0186](/dev/adr/archive/0186-modular-monolith-and-on-demand-extraction)。

本页只记录当前源码的进程选择、凭据边界和未完成事项。部署命令见[运维手册](/ops/deploy/docker-compose#module-process-selection)，既有安装的切换顺序见[升级门禁](/ops/upgrade-guide#split-topology-upgrade)。历史验收不能代替本次部署的就绪、权限和恢复检查。

## 部署收敛：进程清单 {#deployment-convergence}

- **本机开发**：`docker-compose.yml` 启动 PostgreSQL、Redis、MinIO、Mailpit、Celery worker 和单实例 Beat。API／Web 在宿主运行。可选监控使用 `monitoring` profile。见[开发冷启动](/ops/deploy/development#development-cold-start)。
- **标准生产**：叠加 `docker-compose.prod.yml`，API／Web 进容器，不启动 Mailpit。见[生产冷启动](/ops/deploy/docker-compose#compose-cold-start)。
- **LAN 生产**：只用 `docker-compose.lan-prod.yml`。API、Web、网关、生产 Redis 和 worker 使用该项目；PostgreSQL、MinIO 位于栈外，`migrate` 一次性运行。见 [LAN 冷启动](/ops/deploy/lan-production#lan-cold-start)。
- **独立工作树**：`pnpm dev:worktree` 隔离数据库、Redis、桶和运行凭据。按需启动 API、Web 和 worker，不复用日常库、Beat 或角色。见[工作树环境](../how-to/worktree-environments)。

以所选环境文件、Compose 文件和 profile 运行 `docker compose ... config --services` 核对服务键，不用历史盘点数字或容器名判断当前装配。

默认 API 内嵌 Identity 签发、Notifications WebSocket、Workspace 六组命令／查询、Labeling 类目和 Assets Catalog。视频最终写入留在 tracker worker。图片和视频默认由 Celery 的独立窄角色执行；API 控制端不持媒体执行 DSN。Jobs 直接向 Celery 派发，`celery-worker-jobs-recovery` 用独立的 claim／publication 凭据从持久账本重派同一执行 ID。Audit 定时维护由 Beat 调度 `celery-worker-maintenance`，不要用一次性 Audit CLI 替代混合维护队列。

### 按需启用的进程

- **签发与邮件**：`docker-compose.identity.yml` 提供 `identity-mail-v2` 和仅在 `identity-issuer` profile 下启动的私有签发服务。只需邮件时不启远程签发。见[邮件部署](/ops/deploy/docker-compose#production-mail)。
- **通知**：默认由 API 即时投递，Beat 恢复 outbox。需要隔离 Redis 发布权限时才启用 `docker-compose.notifications-publisher.yml` 的 `notifications-publisher`。API 改为仅订阅，不重复恢复同一 outbox。见[通知投递](/api/guides/module-data#notifications-即时发布)。
- **推理控制**：`docker-compose.inference.yml` 的 `inference` 合并 Registry、探测、健康观察、路由、执行及 GPU 控制。各用途仍用独立 bearer、LOGIN 或签名身份。它不加载模型权重，显式配置的远端不可用时也不回退。见 [Inference 运行配置](/api/guides/inference-runtime#inference-compose)。
- **来源与导入**：`docker-compose.assets-source.yml` 的 `assets-source` 持有连接器凭据；`docker-compose.assets-import.yml` 的 `assets-import` 推进持久导入；`docker-compose.assets-cleanup.yml` 的 `assets-cleanup` 只按已保存的目标和版本回收。见[来源与导入指南](/api/guides/assets-source)。
- **媒体**：`docker-compose.assets-media.yml` 分别提供 `assets-media-control`、`assets-media-worker`（`assets-video-executor`）和 `assets-images-worker`（`assets-image-executor`）。`ASSETS_MEDIA_URL` 只选控制端。`ASSETS_VIDEO_EXECUTOR=split` 与 `ASSETS_IMAGE_EXECUTOR=split` 各停对应 Celery 领取，不必启动独立控制端。见[媒体部署](/api/guides/assets-media#部署与范围)。
- **Mask、探测与审计**：`docker-compose.assets-mask-content.yml` 的 `assets-mask-content` 与 `assets-mask-cleanup` 使用不同字节凭据；`docker-compose.labeling-mask-gc.yml` 的 `mask-gc` profile 只供手动历史回收。`docker-compose.modules.yml` 的 `media-probe` 不接触数据库或桶；`docker-compose.audit.yml` 的 `audit-maintenance` 只运行一次性命令。见 [Mask 协议](/api/guides/mask-content)、[探测协议](/api/guides/media-probe)和 [Audit 运行说明](/api/guides/audit-runtime)。
- **模型与训练**：Grounded-SAM-2、SAM3、YOLO、ONNXTools、RapidOCR 使用各自的 `docker-compose.ml-*.yml`，不持核心数据库凭据。`docker-compose.training.yml` 的 `training-executor` 使用 API token、已核验的权重、指定 GPU 和持久状态，不读取数据库或 broker。见[模型与训练部署](/ops/deploy/docker-compose#_8-5-gpu-节点部署)。
- **其他**：[站点 Agent](/ops/deploy/site-agents)只发起出站 HTTPS；LibreYOLO 使用独立插件制品与安装器，两者都不是平台 Compose 服务。`docker-compose.experiments.yml` 的 Go 月报仅供实验。

`docker-compose.hostvols.yml` 和 `docker-compose.ml.yml` 只覆盖卷；`docker-compose.inference-development.yml` 覆盖同一个 `inference`；`docker-compose.assets-native.yml` 覆盖已有 Source、媒体控制和图片消费者，不新增执行通道。旧 Workspace、Labeling、Catalog、Jobs relay 与分拆 Inference 服务已退役，不带旧选择器滚动升级。完整拆分档处置见[旧拓扑升级门禁](/ops/upgrade-guide#split-topology-upgrade)。

### 普通角色、Source 与 M2b 验收门槛 {#m2b-runtime-boundary}

- LAN 内嵌、LAN 远程 issuer 和工作树的普通 API／worker 均使用真实受限 LOGIN，只获固定 owner 能力和公开投影。迁移、DDL、维护、归档与回收各用自己的身份。验收私表、列、分区、物化视图、外表和序列时也检查有效所有权、PUBLIC 与成员关系。
- 已登记自定义 S3／SFTP 的连接器密钥和物理 I/O 归 Source。普通调用方使用窄 token 与已保存的位置、版本或回执；Source 不可用时不改读另一凭据、当前绑定或内建存储。[Source 部署](/ops/deploy/docker-compose#source-registered-io)列出授权和暂存配置。
- [M2b 原运行账本](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs/plans/evidence/1795366269_platform-pluginization/full-m2b-run-ledger.md)记录隔离冷启的真实业务、三类普通角色、新装与恢复、独立服务及自定义存储验收。不是日常迁移、生产放行或 GPU 容量证明。当前复验入口见[模块边界定向验收](/dev/testing#module-boundary-acceptance)。
- 复验须用新隔离环境走通登录、建项目、导入、预标注、标注、审核和持久通知恢复。分别冷启选定的独立服务，核对拒绝、重启和持久回执；未知确认先查原账本，不能当作回滚后重发。

## 插件与 SDK

内置模块可组合，不要求拆成独立服务。可安装扩展通过领域 owner 发布的版本化协议运行，只获声明的权限。平台负责验证结果、权限与提交。需要数据库原子性的跨 owner 操作继续使用调用方事务，不拆成多次 HTTP 提交。

### API SDK 与插件 SDK {#api-sdk-and-plugin-sdk}

`yoao-sdk`／`yoao` 面向平台 API 调用者与插件管理员。`yoao-inference-plugin-sdk`／`yoao-plugin` 面向推理插件作者。管理员安装插件和作者编写插件是两条入口。共享任务词表与生命周期证明由平台契约包维护，不把平台私有 API、数据库权限或模型运行时装入普通 SDK。官方插件源码归 [YOAO-Plugins](https://github.com/YOAO-Labs/YOAO-Plugins)，作者工具、推理运行时与 Mask 工具归 [Plugin-SDK](https://github.com/YOAO-Labs/Plugin-SDK)。

### 公开第一方制品与独立消费 {#plugin-candidate-artifacts}

用独立确认的 SHA-256 核验 manifest 和制品，再按[本地开发教程](/dev/tutorials/local-dev)或[推理插件接入教程](/dev/ml-backend/starter)安装。第一方 wheel 不包含第三方 wheelhouse、模型权重或 OCI 镜像。旧公开发布不能证明新候选已发布；取得镜像摘要也不等于权重获准或可用。[受控资产准备](/ops/deploy/docker-compose#_8-5-受控训练执行器-training-executor)由部署管理员负责。

### 通用推理插件准入与限定验收 {#inference-plugin-admission}

已有 HTTP 服务走[直连](/dev/ml-backend/starter#direct-registration)，平台管理的插件走[安装](/dev/ml-backend/starter#managed-installation)。安装、服务池启用、项目绑定、模型发布和物理设备资格分别验证。manifest、`/setup` 和实际资格必须对应同一部署；健康探测不证明 GPU、真实权重或业务预测通过。[历次准入范围](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L315)只适用于对应候选。

## 剩余工作台账

[平台插件化计划](https://github.com/YOAO-Labs/ai-annotation-platform/blob/dev/docs/plans/1795366269_platform-pluginization.md)维护里程碑与顺序。M2b 隔离装配已验收；M2c 日常切换仍须另行授权。统一内置装配、非推理扩展和跨语言替换不能由一个已验收插件代替。

- **Assets、Learning、Insights**：继续收口原件和 I/O、训练版本与评估／发布、跨域分析投影。固定子能力或执行器已接入，不代表完整领域迁移。
- **历史执行退役**：已支持的路径核对当前执行权；无法证明退役的旧作业继续拒绝项目删除或数据集解绑并保留输入。通用旧库排空、修复与对象存储灾难恢复未由本地试验放行。见[删除围栏](/api/guides/module-data#视频退役与删除围栏)和[协调恢复](/api/guides/inference-runtime#离线视频恢复-operator)。
- **运行与资格**：混合维护队列仍承担原职责。宿主机转发的历史异常不视为已修复；当前资格只覆盖原回执的源码、工件、设备、角色和网络。不得用健康检查或包安装代替业务权限、重启与恢复实测。

### 全目标完成门槛

1. 每个 HTTP、WebSocket、任务、CLI、定时作业和内部执行能力有唯一 owner。
2. 版本化协议用真实 fixture 固定权限、状态、错误、引用、幂等与兼容语义。
3. 边界不传递 ORM、会话或框架对象。实际拆出的进程有独立制品、配置和就绪检查，不继承其他模块的管理凭据。
4. 表、迁移、对象和 Redis 状态有唯一写入 owner。消费者只调用公开合同。
5. 强事务有同一提交方，或有验证过的协调、回执和重放；最终写入与撤权串行化。
6. 在隔离环境用真实进程验证重复、失联、在途恢复和回退，切换期间只有一个权威写入方。
7. 第二种实现可验证同一合同。切换实现语言须另行验证真实消费者及回退，单个 Go 实验不算通用证明。

## 历史验收索引

[拆分前的完整记录](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md)按固定提交保存原结果、失败、镜像和回执。下列入口保留旧深链，不是当前部署配方。

- <span id="identity-整模块独立装配" aria-hidden="true"></span>[Identity 整模块装配](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L696)
- <span id="identity-内嵌运行权限" aria-hidden="true"></span>[Identity 内嵌运行权限](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L928)
- <span id="identity-门控-e2e-夹具" aria-hidden="true"></span>[Identity E2E 夹具](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L971)
- <span id="identity-截图夹具" aria-hidden="true"></span>[Identity 历史截图夹具](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1001)
- <span id="identity-历史角色审计" aria-hidden="true"></span>[Identity 历史角色审计](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1028)
- <span id="identity-运维账号选择" aria-hidden="true"></span>[Identity 运维账号选择](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1058)
- <span id="媒体种子-identity-边界" aria-hidden="true"></span>[媒体种子 Identity 边界](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1089)
- <span id="主种子账号与演示成员" aria-hidden="true"></span>[主种子账号与演示成员](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1120)
- <span id="workspace-组合运行与恢复验收" aria-hidden="true"></span>[Workspace 组合运行与恢复](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L756)
- <span id="workspace-module-audit" aria-hidden="true"></span>[Workspace 整模块资格](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L772)
- <span id="workspace-daily-adoption" aria-hidden="true"></span>[Workspace 旧拆分拓扑的本机采用](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L878)
- <span id="workspace-default-private-storage" aria-hidden="true"></span>[Workspace 默认运行账号的存储围栏](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1731)
- <span id="workspace-媒体及截图种子" aria-hidden="true"></span>[Workspace 媒体及截图种子](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1148)
- <span id="workspace-开发点云种子" aria-hidden="true"></span>[Workspace 开发点云种子](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1222)
- <span id="workspace-冷运维导入验收" aria-hidden="true"></span>[Workspace 冷运维导入](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1268)
- <span id="指南素材命令与对象能力" aria-hidden="true"></span>[指南素材命令与对象能力](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1626)
- <span id="首个带状态的数据接口" aria-hidden="true"></span>[首个带状态的数据接口](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1678)
- <span id="独立连接器导入执行" aria-hidden="true"></span>[连接器导入执行](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1899)
- <span id="mask-内容字节服务" aria-hidden="true"></span>[Mask 内容字节服务](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L1963)
- <span id="runtime-host-forwarding" aria-hidden="true"></span>[宿主机转发故障与观测证据](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2272)
- <span id="legacy-video-postgres-fence" aria-hidden="true"></span>[旧视频 PostgreSQL 围栏观察](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L612)
- <span id="support-daily-and-jobs-facts" aria-hidden="true"></span>[Audit 日聚合与 Jobs 通知事实](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2313)
- <span id="notification-delivery-read-owner" aria-hidden="true"></span>[通知投递读取与当前授权](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2405)
- <span id="notification-publication-owner" aria-hidden="true"></span>[通知即时发布与模块组合](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2870)
- <span id="jobs-list-query-owner" aria-hidden="true"></span>[Jobs 列表查询](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2441)
- <span id="jobs-row-access" aria-hidden="true"></span>[Jobs 领域行访问与事务锁](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2477)
- <span id="jobs-lifecycle-queue" aria-hidden="true"></span>[Jobs 生命周期与旧队列交接](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2549)
- <span id="jobs-action-idempotency" aria-hidden="true"></span>[Jobs 动作幂等与点云读取](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/concepts/module-deployment.md?plain=1#L2826)
