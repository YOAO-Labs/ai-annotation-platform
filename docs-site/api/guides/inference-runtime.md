---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-27
---

# Inference 私有运行配置

Inference 独立进程承接模型 HTTP、不可变推理结果、流水线／ROI／上下文算法、后端控制、健康观察、Registry 环境同步、持久路由与 GPU 权威。公开 `/api/v1` 路由、认证、项目权限与最终 Labeling 写入仍由原调用方持有；独立进程不接管 Celery broker 或业务终态。

## 选择一个数据 owner

API／普通 worker 的模型执行、后端控制、健康观察、Workspace 探测、Registry 环境同步和路由使用明确的 HTTP owner。没有 owner 配置或显式提供方失败时返回 unavailable，不回退到本地私表、旧模型客户端、另一实例或 CPU。

命名 `ProjectPipeline` 仍可在 URL 留空时借用原请求事务；项目内嵌的 `preannotate_pipeline` 继续由 Workspace 管理。普通 API 不再使用 Inference 私有 DSN selector。模型实际端点的密封捕获、拓扑／项目绑定、健康写回和 seed 使用原事务中的固定 Native 能力，不把请求事务改成网络事务。

| 能力           | API／worker selector                                                            | 私有端点                                   |
| -------------- | ------------------------------------------------------------------------------- | ------------------------------------------ |
| 命名流水线     | `INFERENCE_PIPELINES_URL`、`INFERENCE_PIPELINES_TOKEN(_FILE)`                   | `/v1/pipelines/execute`、`query`、`status` |
| Workspace 探测 | `INFERENCE_BACKEND_PROBE_URL`、`INFERENCE_BACKEND_PROBE_TOKEN(_FILE)`           | `/v1/backends/probe`                       |
| 健康观察       | `INFERENCE_HEALTH_OBSERVATION_URL`、`INFERENCE_HEALTH_OBSERVATION_TOKEN(_FILE)` | `/v1/backends/health-observation`          |
| 环境同步       | `INFERENCE_REGISTRY_INGRESS_URL`、`INFERENCE_REGISTRY_INGRESS_TOKEN(_FILE)`     | `/v1/registry/env-sync`、`env-sync-status` |
| 路由           | `INFERENCE_ROUTING_URL`、`INFERENCE_ROUTING_TOKEN(_FILE)`                       | `/v1/routes/*`                             |
| 模型执行       | `INFERENCE_MODEL_EXECUTION_URL`、`INFERENCE_MODEL_EXECUTION_TOKEN(_FILE)`       | `/v1/model-execution/execute`              |
| 后端控制       | `INFERENCE_BACKEND_CONTROL_URL`、`INFERENCE_BACKEND_CONTROL_TOKEN(_FILE)`       | `/v1/backends/control`                     |
| GPU            | `INFERENCE_GPU_URL`、`INFERENCE_GPU_TOKEN(_FILE)`                               | `/internal/inference/gpu/v1/*`             |

同一 secret 的直接值和 `_FILE` 互斥。各能力 bearer 必须不同，不能用 routing token 调用执行、控制或 GPU 端点。API 只获得所需 HTTP bearer，不能挂载 owner DSN、后端存量 token 或 GPU 签名私钥。

### 使用专用 DSN

专用 DSN 仅供 Inference owner 进程读取。先应用当前迁移，再以迁移账号运行 `infra/docker/module-data-grants.sql` 与 `infra/docker/lan-production-grants.sql`，最后安装各函数专用 LOGIN 的授权。迁移账号不得成为长期运行凭据。

既有 pipeline、probe、health-observation、health-data、registry-ingress、routing 使用各自 `inference-*-grants.sql`。`infra/docker/inference-owner-grants.sql` 为以下闭合 profile 分别安装 LOGIN：

- 端点打开：`setup`、`health_observation`、`catalog_fallback`、`stats`、`backend_read`、`lifecycle`、`execution`；各账号只能执行 policy 与对应 opener。
- GPU 自有捕获／打开：`gpu_health_observation`、`gpu_control`；仅持有 policy、capture 和该用途的 opener。
- 聚合统计：`backend_stats`；仅在 owner 内捕获统计分组和端点，公开结果不含 auth 分组键。
- GPU 私有权威：`gpu_authority`，使用 `inference_gpu_authority_client`；policy／step 两函数，与普通 caller 的有限 GPU 管理能力分离。
- GPU 回收：`gpu_collector`，使用 `inference_gpu_collector_client`；单独 policy／step，两函数 LOGIN 与权威账号不同。

授权脚本要求显式数据库名、迁移角色、Inference owner、client role 和 profile；拒绝超级权限、继承／成员旁路、表／序列访问、对象所有权和无关函数执行。不可把一组专用 LOGIN 合并成宽权限账号来通过 readiness。

### 使用私有 HTTP runtime

```sh
uv run --project apps/inference --locked --extra runtime python -m aap_inference
```

默认监听 `127.0.0.1:8768`。完整部署复用 `docker-compose.inference.yml`，将 `.env.example` 中 `INFERENCE_RUNTIME_*_FILE` 指向宿主 secret 文件；Compose 只读挂载到 owner 容器。`INFERENCE_GPU_REDIS_URL(_FILE)` 指向权威账本使用的 Redis。GPU 签名文件仅挂载给 Inference，不能挂载给 API／普通 worker／模型 backend。

完整服务配置八项 `INFERENCE_REQUIRED_CAPABILITIES`：`pipelines,backend_probe,health_observation,registry_ingress,routing,execution,backend_control,gpu`。`GET /health` 必须验证全部必需能力；某一健康能力不能掩盖其他必需能力未就绪。直接启动子能力进程时可明确选择其 required 列表。

容器以 UID/GID 10008、只读根文件系统、32 MiB `/tmp`、丢弃 capabilities、`no-new-privileges` 运行。宿主发布仅限 loopback。`INFERENCE_MAX_CONCURRENCY` 默认8，满载立即拒绝，不建立无界等待队列。Compose 的 CPU／内存值是运行配置，不代表容量或 SLO 证明。

保存流水线的 metadata 请求／响应上限为16 MiB；probe、观察、Registry、routing、control 和 GPU 分别使用自身合同边界。模型执行不套用保存流水线的 metadata 上限；读取可能较大的执行 body 前先认证，实际 backend 的响应上限与期限仍按调用方配置执行。模型请求只发送一次，不在不确定响应后自动重试。

## 权限、事务与恢复

普通 API 先执行既有 Identity／Workspace 授权，固定逻辑任务、请求、项目和 owner。`model_execution_port(db, *, bound_route=None)` 选择实际 registry／pool／member，使用原调用方事务捕获密封 endpoint，并附上精确路由 scope 和 attempt 身份。公开 Registry 投影只返回 `credential_configured`，不返回存量 auth token。

路由准入只传 pool 或 direct registry 其中之一；已选实例通过 `required_instance_id` 限定，不能把 registry 与 pool 当作两个并列 selector 发送。

Inference 在 owner 内打开密封 endpoint；对写操作，在真实 HTTP 前用新鲜的 owner-local issuer 建立短路由 subject。捕获时尚未提交的端点值、原 credential rotation 语义和实际所选实例保持一致；不能仅按 registry ID 在另一个快照回读后端密钥来替代原请求快照。

只读 health、setup、catalog、statistics、receipt／versions 不创建预测 attempt。模型写入与 unload／reload／warmup 则保持各自 scope/control 生命周期、begin／settle 和 GPU one-shot grant 的真实响应／未发送／不确定结果。过期租约不等于 backend 已停止；未确认 scope 必须保留。

GPU rollout 首次启用先建立 settled `off` 状态，再在同一事务进入 `promoting`；不能绕过原状态迁移围栏。取消意图的恢复与提交保持精确的 membership、generation、control／runtime epoch 和 token horizon 检查；端点 URL、认证方式或凭据轮换都会使旧意图失效。旧协议留下的无法验证指纹必须失败关闭，不能据此重新打开 resident 状态。

不可变 `FrozenModelOperation`／`ModelOperationResult` 携带确切 task IDs、阶段图、输入、实际实例 provenance、原模型 metadata 和 transport observation。Metrics 在调用方按真实 transport observation 记录一次，不用 RPC wall time 替代模型耗时。结构化错误保持 status、detail、Retry-After、GPU error code 和实际路由来源。

Assets 提供输入与 crop 发布；Jobs 持有领取／派发／代次；Labeling 持有原始／准备结果、最终候选和业务终态。Inference 不替这些 owner 提交。最终写回仍复核原账号、项目、任务／批次、源版本、取消和结果围栏。帧级多阶段仍不支持，不因换用远程执行而扩大能力。

命名流水线的公开接口保持：

- `GET`／`POST /api/v1/project-pipelines`
- `GET`／`PUT`／`DELETE /api/v1/project-pipelines/{pipeline_id}`
- `POST /api/v1/projects/{project_id}/pipelines/apply`

借用模式保留 caller commit；HTTP 模式由选定 owner 提交。含糊确认只查询同命令 status，不重发写命令；unknown 不是回滚证明。原 list／apply 的可见性、stage 校验、Workspace 最终围栏、行投影和 Identity 显示名读取顺序保持。

## Workspace 的后端能力探测

Workspace 只传 registry UUID、不透明 `probe_subject`、timeout 和 `CapabilityLimits`。Inference 打开端点并实际请求 `/setup`，返回绑定后的 modalities，不把 URL、auth 或 extra_params 交给 Workspace。内部 owner／权限失败返回503；真实 backend setup 失败保持 null modalities 的既有 fail-open 行为。

独立 Workspace 使用 `WORKSPACE_INFERENCE_PROBE_URL`、`WORKSPACE_INFERENCE_PROBE_TOKEN(_FILE)` 与 `WORKSPACE_INFERENCE_PROBE_TIMEOUT_SECONDS`。API 的同名 Inference client 也必须选择 HTTP。独立 probe 进程使用 `INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)` 与 `INFERENCE_BACKEND_PROBE_CONTROL_TOKEN(_FILE)`。

## 健康观察与调用方写回

健康检查的实际 `/health` 和条件 `/setup` 在 Inference owner 内运行，密封证据绑定原 caller transaction、registry、request、challenge 和观察窗口。普通 API 不打开 endpoint 或接收存量 bearer。

能力提取、数据库时间、端点身份／成员代次复核、registry→resource→promotion barrier→membership 锁序、健康写入和延迟 pool reconcile 保留在调用方原 UOW；不得以密封证据替代最终当前权威。较慢的观察不能覆盖较新的结果，endpoint／membership 变化会丢弃旧观察。清理旧 capability、challenge 回显和有限错误阶段保持原语义。

### 同事务的健康数据能力

`aap_inference_health_data_v1` 是固定 Native 数据边界，`HealthDataPostgresData.bind(connection)` 只绑定当前活动事务；提交／回滚后丢弃 binding。`InferenceHealthData` 是 API 原 Session 的适配器，不是第二套健康算法。GPU owner 的持久健康刷新也复用同一 `BackendHealthCoordinator` 和专用数据 LOGIN，不通过布尔“健康”替代真实持久观察。

## Registry 环境同步

启动环境同步通过 `INFERENCE_REGISTRY_INGRESS_URL` 与独立 bearer 提交完整命令。owner 先实际探测 `/setup`，再在一个事务内协调 Registry、singleton pool 和观察回执；空 URL 列表仅断开 environment-managed 项，manual 优先级保持。调用方不能提交伪造的 probe 结果。

模糊确认查询同 request ID／command hash 的 status，不重发写命令。截图 seed 使用同事务、凭据不可见的固定 Native 入口；项目选择和绑定继续由 Workspace 持有。

## 持久路由执行权

`/v1/routes/command`、`status`、`runtime` 与 `quiescence` 共用独立 routing bearer。Native 持有 scope、attempt、control、回执和真实 backend continuity；Redis circuit 只提供诊断提示，不替代持久准入。公开读数与 quiescence 是严格绑定 registry 的 owner 观察，不能由普通调用方的宽数据库连接伪造。

### 单个执行范围的退役

`POST /v1/routes/retire` 与 `/v1/routes/retirement-status` 复用 routing bearer。先关闭准确 scope 的新准入，再依据原 backend 范围内的真实零工作证明收敛；其他 scope 不被全局停止。外部 future 必须登记到 backend scope，未登记工作不在该证明内。

### 视频执行代次与路由绑定

`video_acquire` 将 Jobs 当前执行证明、稳定 run、递增 generation、worker、lease token 与路由绑定放入同一 Native 事务。重派发继续原 run 和窗口，过时 attempt、取消后新增模型调用及迟到写入受到原 owner 围栏约束。已有 tracker scope 在全部窗口中复用，不能偷偷获取第二个替代路由。

### 离线视频恢复 operator

`aap-video-restore` 使用 `--database-url-file`、`--inspect`／`--command-file`／`--status`，只执行专用 `video_restore_step(text,text)`。恢复绑定备份摘要、外部停机／凭据围栏、实际 controller incarnation 和固定 control IDs，原子轮换恢复后的执行权。未知／pending 按原命令查询与恢复，不自动回滚或重跑模型。

摘要不是外部进程停止、S3 身份撤销或对象存储恢复的证明。先满足[备份恢复前提](/ops/deploy/docker-compose#_5-0-恢复前提与执行权)，再按[协调恢复决策](/dev/adr/0160-coordinated-video-restore)操作。

## 当前边界

实现、当前非editable工件、真实 API／worker、完整 PostgreSQL 恢复及本机采用分别记录在[唯一部署台账](/dev/concepts/module-deployment)。已有操作级证据不自动提升为完整模块或生产资格。物理 GPU／NVIDIA 容量、其他部署拓扑、替代语言提供方、对象存储灾难恢复及暂缓 Learning／Assets 扩展须单列验证。
