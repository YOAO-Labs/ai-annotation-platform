---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-11-23
---

# Inference 私有运行配置

单个 Inference 凭据进程承接模型 HTTP、不可变推理结果、流水线／ROI／上下文算法、后端控制、健康观察、Registry 环境同步、Workspace 探测、持久路由与 GPU 权威。公开 `/api/v1` 路由、认证、项目权限与最终 Labeling 写入仍由原调用方持有；Inference 不接管 Celery broker 或业务终态。

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

部署时让所有启用的 URL selector 指向同一个 Inference origin，但每个端点继续使用自己的 bearer；直接值和 `_FILE` 互斥。不能用 routing token 调用执行、控制或 GPU 端点，也不能在三个合并的端点间互换 bearer。API 只获得所需 HTTP bearer，不能挂载 owner DSN、后端存量 token 或 GPU 签名私钥。

### 使用专用 DSN

专用 DSN 仅供 Inference owner 进程读取。先应用当前迁移，再以迁移账号运行 `infra/docker/module-data-grants.sql` 与 `infra/docker/lan-production-grants.sql`，最后安装各函数专用 LOGIN 的授权。迁移账号不得成为长期运行凭据。

既有 pipeline、probe、health-observation、health-data、registry-ingress、routing 使用各自 `inference-*-grants.sql`。`infra/docker/inference-owner-grants.sql` 为以下闭合 profile 分别安装 LOGIN：

- 端点打开：`setup`、`health_observation`、`catalog_fallback`、`stats`、`backend_read`、`lifecycle`、`execution`；各账号只能执行 policy 与对应 opener。
- GPU 自有捕获／打开：`gpu_health_observation`、`gpu_control`；仅持有 policy、capture 和该用途的 opener。
- 聚合统计：`backend_stats`；仅在 owner 内捕获统计分组和端点，公开结果不含 auth 分组键。
- GPU 私有权威：`gpu_authority`；policy／step 两函数，与普通 caller 的有限 GPU 管理能力分离。生产 owner 明确选择 `yoao_inference_gpu_authority_client`；独立工作树夹具使用自身的专用 LOGIN，不接管共享角色。
- GPU 回收：`gpu_collector`，使用 `yoao_inference_gpu_collector_client`；单独 policy／step，两函数 LOGIN 与权威账号不同。

授权脚本要求显式数据库名、迁移角色、Inference owner、client role 和 profile；拒绝超级权限、继承／成员旁路、表／序列访问、对象所有权和无关函数执行。不可把一组专用 LOGIN 合并成宽权限账号来通过 readiness。

### 启动保留的私有 HTTP runtime {#inference-compose}

```sh
docker compose --env-file /path/to/inference.env -f docker-compose.inference.yml \
  up -d --build --wait inference
```

只启动 `inference` 一个服务。让该容器通过私网访问已迁移、已授权的 PostgreSQL 和 Redis，消费者也须能访问同一 owner。已与此服务共享网络的容器使用 `http://inference:8768`，在宿主运行的消费者使用 `http://127.0.0.1:8768`。不通过公网网关转发私有 Inference 路由。已有本机开发使用的 `docker-compose.inference-development.yml` 仅指定完整 owner 的镜像和外部网络，不会再启动一组凭据进程。

若使用已核验的开发镜像，在 `0600` 私有 env 文件设置 `AAP_INFERENCE_IMAGE` 和 `AAP_INFERENCE_DEVELOPMENT_NETWORK`，不重新构建：

```bash
docker compose --env-file /path/to/inference.env -p aap-inference-development \
  -f docker-compose.inference.yml -f docker-compose.inference-development.yml \
  up -d --no-build --wait inference
```

在仅给 owner 使用的 env 文件里，把 `.env.example` 中 28 个 `INFERENCE_RUNTIME_*_FILE` 指向实际 secret 文件：十八个专用 PostgreSQL LOGIN 的 DSN、八个不同的 HTTP bearer、一个 GPU 账本 Redis 身份、一个 Ed25519 私钥环。Compose 以只读方式挂载这些文件，针对 UID/GID 10008 设置最小读取权限；不要把它们放进源码、镜像或 API 的 env 文件。`GPU_LIFECYCLE_ACTIVE_SIGNING_KID` 对应私钥环中的 key ID；collector DSN 使用完整 `postgresql+asyncpg://` URL。文件与授权映射见 [`docker-compose.inference.yml`](https://github.com/YOAO-Labs/ai-annotation-platform/blob/main/docker-compose.inference.yml) 和[专用 DSN](#使用专用-dsn)。

`GET /health` 要求 `pipelines,backend_probe,health_observation,registry_ingress,routing,execution,backend_control,gpu` 八项全部就绪。容器使用只读根文件系统、32 MiB 不允许设备节点的 `/tmp`、丢弃 capabilities 和 `no-new-privileges`，宿主发布仅限 loopback。`INFERENCE_MAX_CONCURRENCY` 默认 8，满载立即拒绝，不建立无界等待队列。

这个镜像是凭据控制面，不挂载 GPU 设备或权重。新建、未管理 GPU 的 CPU-only 环境设 `GPU_ARBITER_MODE=off`、`GPU_ARBITER_ROLLOUT_ENABLED=false`、`GPU_ARBITER_RESOURCES_JSON={}`；保留完整 listener 的专用 authority／collector DSN、Redis 与签名配置。不是用空文件或 API 账号代替它们，也不要将已经在用的 GPU 策略切到 `off` 迁就部署。实际插件／模型容器继续独立运行、授权和绑定设备。

从旧 `inference-registry`、`inference-health`、`inference-probe` 安装切换时，先核对私网地址、端口、数据库 grants 与各调用方 bearer；删除三份退役的按操作 Compose 文件引用，保留对应的 secret 配对。维护窗口内按现有准入／排空约束结束旧服务，启动 `inference` 并验证 `/health` 后，再刷新 API／worker 使用统一的私网 origin。没有新 schema、HTTP 路径、环境变量名、bearer 匹配规则或数据库回填；未确认的 Registry 写入继续用原请求身份查回执，不能重新提交。

`uv run --project apps/inference --locked --extra runtime python -m yoao_inference` 保留给契约和独立包验收；单独选择部分能力并不等于当前 Compose 部署。

保存流水线的 metadata 请求／响应上限为16 MiB；probe、观察、Registry、routing、control 和 GPU 分别使用自身合同边界。模型执行不套用保存流水线的 metadata 上限；读取可能较大的执行 body 前先认证，实际 backend 的响应上限与期限仍按调用方配置执行。模型请求只发送一次，不在不确定响应后自动重试。

### 冷启动验收 {#inference-owner-cold}

在已提交的隔离 checkout 上运行。Linux 测试宿主须能访问 Docker 桥接网关，Compose 须支持 `!override`，Docker 须支持 `network connect --gw-priority`。通用前置条件见[模块边界定向验收](/dev/testing#module-boundary-acceptance)。

```sh
(
  set -eu
  identity=$(openssl rand -hex 12)
  revision=$(git rev-parse HEAD)
  image="aap-inference-acceptance:$identity"
  docker build -f apps/inference/Dockerfile \
    --label org.aap.acceptance=module-data-boundary \
    --label "org.aap.acceptance-id=$identity" \
    --label "org.aap.source-revision=$revision" -t "$image" .
  trap 'docker image rm "$image"' EXIT
  AAP_INFERENCE_OWNER_IMAGE="$image" apps/api/.venv/bin/python \
    scripts/verify_module_data_boundary.py --output .worktree/inference-owner.json \
    --test-file tests/test_inference_backend_probe_containers.py \
    --test-file tests/test_inference_health_observation_containers.py \
    --test-file tests/test_inference_registry_ingress_containers.py
)
```

三项消费者都启动同一保留的 Compose 服务形态；验收只覆盖镜像身份、私网和临时 loopback 映射，不减少挂载的能力。使用真实受限 LOGIN 与 Redis 检查 readiness，并验证错误的 bearer／SQL 身份不能借用同一进程的其他能力。验收还覆盖真实应用的认证探测及 503 后无新项目／回执、健康观察的调用方提交及停机失败，以及 Registry 提交确认丢失后仅查回执、重启后重读同一回执。实际镜像 ID、Compose 摘要和运行限制保存在 `.worktree/aap-inference-*-runtime.json`，另有脱敏日志和验证器的 JUnit 输出。这不验证物理 GPU、模型容量或 Docker Desktop 转发，也不触碰日常服务。

## 权限、事务与恢复

普通 API 先执行既有 Identity／Workspace 授权，固定逻辑任务、请求、项目和 owner。`model_execution_port(db, *, bound_route=None)` 选择实际 registry／pool／member，使用原调用方事务捕获密封 endpoint，并附上精确路由 scope 和 attempt 身份。公开 Registry 投影只返回 `credential_configured`，不返回存量 auth token。

路由准入只传 pool 或 direct registry 其中之一；已选实例通过 `required_instance_id` 限定，不能把 registry 与 pool 当作两个并列 selector 发送。

Inference 在 owner 内打开密封 endpoint；对写操作，在真实 HTTP 前用新鲜的 owner-local issuer 建立短路由 subject。捕获时尚未提交的端点值、原 credential rotation 语义和实际所选实例保持一致；不能仅按 registry ID 在另一个快照回读后端密钥来替代原请求快照。

只读 health、setup、catalog、statistics、receipt／versions 不创建预测 attempt。模型写入与 unload／reload／warmup 则保持各自 scope/control 生命周期、begin／settle 和 GPU one-shot grant 的真实响应／未发送／不确定结果。过期租约不等于 backend 已停止；未确认 scope 必须保留。

GPU rollout 首次启用先建立 settled `off` 状态，再在同一事务进入 `promoting`；不能绕过原状态迁移围栏。取消意图的恢复与提交保持精确的 membership、generation、control／runtime epoch 和 token horizon 检查；端点 URL、认证方式或凭据轮换都会使旧意图失效。旧协议留下的无法验证指纹必须失败关闭，不能据此重新打开 resident 状态。

不可变 `FrozenModelOperation`／`ModelOperationResult` 携带确切 task IDs、阶段图、输入、实际实例 provenance、原模型 metadata 和 transport observation。Metrics 在调用方按真实 transport observation 记录一次，不用 RPC wall time 替代模型耗时。结构化错误保持 status、detail、Retry-After、GPU error code 和实际路由来源。

追踪结果在进程内保持不可变；只在写入窗口 continuation、Mask 内容／引用和实时 frame 事件的 JSON 边界转换为普通对象。写入争用重试复用已接受的模型结果，不再次推理；窗口种子、检查点、取消状态和预览几何保持相同内容。

Assets 提供输入与 crop 发布；Jobs 持有领取／派发／代次；Labeling 持有原始／准备结果、最终候选和业务终态。Inference 不替这些 owner 提交。最终写回仍复核原账号、项目、任务／批次、源版本、取消和结果围栏。帧级多阶段仍不支持，不因换用远程执行而扩大能力。

命名流水线的公开接口保持：

- `GET`／`POST /api/v1/project-pipelines`
- `GET`／`PUT`／`DELETE /api/v1/project-pipelines/{pipeline_id}`
- `POST /api/v1/projects/{project_id}/pipelines/apply`

借用模式保留 caller commit；HTTP 模式由选定 owner 提交。含糊确认只查询同命令 status，不重发写命令；unknown 不是回滚证明。原 list／apply 的可见性、stage 校验、Workspace 最终围栏、行投影和 Identity 显示名读取顺序保持。

`PUT /api/v1/project-pipelines/{pipeline_id}` 只更新显式提供的字段。省略 `stages` 时保留原阶段；替换阶段必须提交数组并通过既有校验。显式 `stages: null` 返回 422，原编排及更新时间保持不变；独立 Inference owner 也拒绝该输入，不会先写入无效值再报错。项目／组织授权和模型服务引用范围仍按当前事实校验。

## Workspace 的后端能力探测

Workspace 只传 registry UUID、不透明 `probe_subject`、timeout 和 `CapabilityLimits`。Inference 打开端点并实际请求 `/setup`，返回绑定后的 modalities，不把 URL、auth 或 extra_params 交给 Workspace。内部 owner／权限失败返回503；真实 backend setup 失败保持 null modalities 的既有 fail-open 行为。

Workspace 配置 owner 运行在 API 内；需要探测时，API 使用 `INFERENCE_BACKEND_PROBE_URL`、`INFERENCE_BACKEND_PROBE_TOKEN(_FILE)` 和 `INFERENCE_BACKEND_PROBE_TIMEOUT_SECONDS` 调用 Inference。缺少配置或探测 owner 不可用时不回退本地。`INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)` 与 `INFERENCE_BACKEND_PROBE_CONTROL_TOKEN(_FILE)` 仅由 Inference 进程使用；不要把该 DSN 或后端凭据挂给 API。

## 健康观察与调用方写回

健康检查的实际 `/health` 和条件 `/setup` 在 Inference owner 内运行，密封证据绑定原 caller transaction、registry、request、challenge 和观察窗口。普通 API 不打开 endpoint 或接收存量 bearer。

能力提取、数据库时间、端点身份／成员代次复核、registry→resource→promotion barrier→membership 锁序、健康写入和延迟 pool reconcile 保留在调用方原 UOW；不得以密封证据替代最终当前权威。较慢的观察不能覆盖较新的结果，endpoint／membership 变化会丢弃旧观察。清理旧 capability、challenge 回显和有限错误阶段保持原语义。

### 同事务的健康数据能力

`yoao_inference_health_data_v1` 是固定 Native 数据边界，`HealthDataPostgresData.bind(connection, expected_role=...)` 只绑定当前活动事务，并核验可信 owner 指定的精确 session／current LOGIN；提交／回滚后丢弃 binding。生产 GPU owner 选择 `yoao_inference_health_data_client`，工作树夹具使用自己持有的专用 LOGIN，两者均须通过相同的函数白名单、权限拒绝和 policy 核验；LOGIN 身份不能由 HTTP 请求或连接的自报身份决定。`InferenceHealthData` 是 API 原 Session 的适配器，不是第二套健康算法，GPU owner 的持久健康刷新也复用同一 `BackendHealthCoordinator` 和专用数据 LOGIN。

## Registry 环境同步

启动环境同步通过 `INFERENCE_REGISTRY_INGRESS_URL` 与独立 bearer 提交完整命令。owner 先实际探测 `/setup`，再在一个事务内协调 Registry、singleton pool 和观察回执；空 URL 列表仅断开 environment-managed 项，manual 优先级保持。调用方不能提交伪造的 probe 结果。

模糊确认查询同 request ID／command hash 的 status，不重发写命令。截图 seed 使用同事务、凭据不可见的固定 Native 入口；项目选择和绑定继续由 Workspace 持有。

## 持久路由执行权

`/v1/routes/command`、`status`、`runtime` 与 `quiescence` 共用独立 routing bearer。Native 持有 scope、attempt、control、回执和真实 backend continuity；Redis circuit 只提供诊断提示，不替代持久准入。公开读数与 quiescence 是严格绑定 registry 的 owner 观察，不能由普通调用方的宽数据库连接伪造。

### 为直连实例建立路由连续性 {#route-continuity}

默认服务池路由模式 `off` 支持手动 Token 的直连服务。部署已选 `enforce` 时，未绑定的新实例即使健康、全局池与项目均已启用，首次预测仍可能返回 503；路由 `begin` 回执为 `continuity_unverified`，此时不会发送 `/predict`。不要用健康检查、切换模式或手写数据库状态代替绑定。

1. 先确认目标为刚注册的准确实例，无在途或未确认的工作；已有业务的实例先停止接流并核对持久账本。后端必须实现当前 `/v1/yoao-routing/control` 的认证 drain／bind。SDK 示例由服务运营者配置 `AAP_ROUTE_REGISTRY_ID` 和 `ROUTE_CONTROL_TOKEN_FILE`，后者对应注册的服务 Token。它不是平台的 `INFERENCE_ROUTING_TOKEN`。只实现旧 `/v1/aap-routing/control` 的 wheel 即使包名与版本相同也不兼容；请核对平台契约 manifest，不设双名回退。
2. **平台运维方**通过私网 routing owner 执行一次 `reconcile`。保存实例 ID、唯一操作 UUID 和命令内容，以便在网络不确定时查回执。下面在装有 `yoao-inference-contract` 的运维环境执行；`ROUTING_CONTROL_ID` 在发命令前产生并保存，切勿每次运行随机生成。`ROUTING_ACTION=bind` 仅用于首次绑定；`ROUTING_ACTION=status` 查询原命令。向后端传递绑定材料的链路必须可信，跨主机应使用受保护的传输。不要把 owner bearer 交给服务作者。

   ```python
   import asyncio
   import os
   from pathlib import Path
   from uuid import UUID

   from yoao_inference_contract.routing import (
       make_routing_command, make_routing_runtime_request, make_routing_status_request,
   )
   from yoao_inference_contract.routing_client import RoutingHttpClient

   registry = UUID(os.environ["REGISTRY_ID"])
   operation = UUID(os.environ["ROUTING_CONTROL_ID"])
   client = RoutingHttpClient(
       os.environ["INFERENCE_ROUTING_URL"],
       Path(os.environ["INFERENCE_ROUTING_TOKEN_FILE"]).read_text().strip(),
   )
   command = make_routing_command(
       "reconcile", {"registry_id": str(registry), "control_id": str(operation)}, request_id=operation,
   )
   async def inspect():
       action = os.environ["ROUTING_ACTION"]
       if action == "bind":
           reply = await client.execute(command)
       elif action == "status":
           reply = await client.status(make_routing_status_request(command))
       else:
           raise ValueError("ROUTING_ACTION must be bind or status")
       view = (await client.runtime(make_routing_runtime_request([registry]))).registries[0]
       print(reply.state, getattr(reply.result, "phase", None), getattr(reply.result, "reason", None), view.continuity)

   asyncio.run(inspect())
   ```

3. 只有原实例、原操作的回执为 `committed` 且 `phase=completed`，并且该实例运行读数为 `continuity=ready`，才恢复接流并用真实项目任务验证。`draining`、拒绝和未知状态都不是完成。安装档在激活时由安装 owner 发起此绑定，不能对其另建手动操作替代安装证明。

### 单个执行范围的退役

`POST /v1/routes/retire` 与 `/v1/routes/retirement-status` 复用 routing bearer。先关闭准确 scope 的新准入，再依据原 backend 范围内的真实零工作证明收敛；其他 scope 不被全局停止。外部 future 必须登记到 backend scope，未登记工作不在该证明内。

### 视频执行代次与路由绑定

`video_acquire` 将 Jobs 当前执行证明、稳定 run、递增 generation、worker、lease token 与路由绑定放入同一 Native 事务。重派发继续原 run 和窗口，过时 attempt、取消后新增模型调用及迟到写入受到原 owner 围栏约束。已有 tracker scope 在全部窗口中复用，不能偷偷获取第二个替代路由。

### 离线视频恢复 operator

`yoao-video-restore` 使用 `--database-url-file`、`--inspect`／`--command-file`／`--status`，只执行专用 `video_restore_step(text,text)`。恢复绑定备份摘要、外部停机／凭据围栏、实际 controller incarnation 和固定 control IDs，原子轮换恢复后的执行权。未知／pending 按原命令查询与恢复，不自动回滚或重跑模型。

应用迁移及 module-data owner 授权后，按 `infra/docker/video-restore-grants.sql` 安装 `yoao_video_restore_operator_client`，变量 `video_restore_operator_role` 指向该专用 LOGIN。其 DSN 只写入操作用户可读的 `0600` 文件。

```bash
yoao-video-restore --database-url-file /secure/restore-dsn --inspect
yoao-video-restore --database-url-file /secure/restore-dsn --command-file /secure/restore-command.json
yoao-video-restore --database-url-file /secure/restore-dsn --status "$RESTORE_ID"
```

命令文件固定同一恢复 UUID、备份和外部围栏 SHA-256，以及 inspect 返回的目标身份。退出码为零仍可能是 `pending`；核对实际 controller epoch／incarnation 与完成回执后，才能恢复普通 claim。

摘要不是外部进程停止、S3 身份撤销或对象存储恢复的证明。先满足[备份恢复前提](/ops/deploy/docker-compose#_5-0-恢复前提与执行权)，再按[协调恢复决策](/dev/adr/0160-coordinated-video-restore)操作。

## 当前边界

实现、当前非editable工件、真实 API／worker、完整 PostgreSQL 恢复及本机采用分别记录在[唯一部署台账](/dev/concepts/module-deployment)。已有操作级证据不自动提升为完整模块或生产资格。物理 GPU／NVIDIA 容量、其他部署拓扑、替代语言提供方、对象存储灾难恢复及暂缓 Learning／Assets 扩展须单列验证。
