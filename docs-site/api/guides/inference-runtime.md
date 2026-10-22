---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-21
---

# Inference 私有运行配置

本页说明保存的命名预标编排如何选择数据 owner，以及如何在本地启用可选的 Inference HTTP runtime。同一进程可独立承载命名编排协议、Workspace 后端能力探测，或同时启用两者；不执行模型推理或 Celery 预测任务。它描述 API 与 owner 之间的私有边界，不新增终端用户 API。

命名 `ProjectPipeline` 与 Project 内嵌的旧 `preannotate_pipeline` 是两个不同能力：前者由 Inference 承担；后者继续由 Workspace 管理。六个公开命名编排路由与 DTO 保持不变：

- `GET` / `POST /api/v1/project-pipelines`
- `GET` / `PUT` / `DELETE /api/v1/project-pipelines/{pipeline_id}`
- `POST /api/v1/projects/{project_id}/pipelines/apply`

## 选择一个数据 owner

API 默认借用当前请求的 SQLAlchemy Session 和事务。不设置下表中的 selector 即保持该模式；现有 API 启动不依赖独立 Inference 进程。

| 模式          | API 配置                                           | 事务归属                     |
| ------------- | -------------------------------------------------- | ---------------------------- |
| 借用 API 事务 | URL、DSN 和 DSN 文件 selector 留空                 | API 在成功命令后提交一次     |
| 专用 DSN      | 只设 `INFERENCE_PIPELINES_DATABASE_URL` 或 `_FILE` | Inference 专用数据库连接提交 |
| 私有 HTTP     | 设 `INFERENCE_PIPELINES_URL` 和控制 token          | Inference runtime 提交       |

URL 与 DSN 不能同时选择。直接值和它的 `_FILE` 形式也不能同时设置。显式选择的 owner 配置错误或不可用时 API 返回服务不可用，不回退到借用 Session 或另一种远程模式。

### 使用专用 DSN

API 进程直接连接当前 PostgreSQL，但使用专为命名编排创建的 `inference_pipelines_client` LOGIN。凭据应通过 secret file 提供：

```dotenv
INFERENCE_PIPELINES_URL=
INFERENCE_PIPELINES_DATABASE_URL=
INFERENCE_PIPELINES_DATABASE_URL_FILE=/absolute/path/to/inference-pipelines-client-dsn
INFERENCE_PIPELINES_TIMEOUT_SECONDS=30
```

该账号只能执行私有 `aap_inference_pipelines_v1` schema 中的 `pipeline_policy()` 与 `pipeline_step(jsonb,text,jsonb,text[],text[])`。先应用 migration 0271 和 `infra/docker/inference-pipelines-grants.sql`；不要给它 `project_pipelines`、回执表或其他 owner 的原表权限。若 API 运行于宿主机，`_FILE` 必须指向宿主机 API 进程可读的 secret 文件。

### 使用私有 HTTP runtime

可选 Compose 服务在容器内监听 8768，并将宿主机端口限制映射到 `127.0.0.1:8768`。以下例子假设 API 进程运行在宿主机上：

```dotenv
# API 进程的环境
INFERENCE_PIPELINES_URL=http://127.0.0.1:8768
INFERENCE_PIPELINES_TOKEN_FILE=/absolute/path/to/inference-control-token
INFERENCE_PIPELINES_DATABASE_URL=
INFERENCE_PIPELINES_DATABASE_URL_FILE=
INFERENCE_PIPELINES_TIMEOUT_SECONDS=30

# Compose 从宿主机读取这两个文件，并只读挂载给 inference 容器
INFERENCE_RUNTIME_PIPELINES_DATABASE_URL_FILE=/absolute/path/to/inference-pipelines-client-dsn
INFERENCE_RUNTIME_CONTROL_TOKEN_FILE=/absolute/path/to/inference-control-token
```

使用仓库现有 Compose 文件启动可选服务：

```sh
docker compose -f docker-compose.yml -f docker-compose.inference.yml up -d --build inference
```

Compose 将 DSN 和 bearer 分别挂载到容器内的 `/run/secrets/inference_pipelines_database_url` 与 `/run/secrets/inference_control_token`，再设置 `INFERENCE_PIPELINES_DATABASE_URL_FILE` 和 `INFERENCE_CONTROL_TOKEN_FILE`。不要把 secret 内容写进 `.env`、镜像或命令行参数。API 使用的 token 文件和 runtime 控制 token 文件必须提供相同的 bearer 值。

如 API 也运行在 Docker 网络中，应让 API 与该服务加入同一网络，并把 URL 改为 `http://inference:8768`；同时将控制 token 文件单独挂载给 API。上面的 Compose override 只为 `inference` 服务挂载这两个 secret。

`apps/inference/Dockerfile` 以 UID/GID 10008 运行。Compose 服务使用只读根文件系统、32 MiB `/tmp`、丢弃 Linux capabilities、`no-new-privileges`、64 PID 上限、1 CPU 和 256 MiB 内存限制。容器内绑定 `0.0.0.0:8768` 仅供 Compose 网络访问，宿主机发布仍限制在 loopback。runtime 默认 active-request 上限为 8，可由 `INFERENCE_MAX_CONCURRENCY` 调整；超过上限的请求立即收到 busy/unavailable 响应，不进入等待队列。请求与响应 body 上限各为 16 MiB。

`GET /health` 会验证 runtime 配置、专用数据库连接、函数权限与 Native policy；它不是公开业务端点。私有 bearer 保护 `POST /v1/pipelines/execute`、`/query` 和 `/status`。终端用户仍只访问上表列出的 `/api/v1` 路由。runtime 直接启动时默认绑定 `INFERENCE_HOST=127.0.0.1`、`INFERENCE_PORT=8768`；Compose 显式将容器绑定改为 `0.0.0.0`，再通过宿主机 loopback 发布。

## 权限、事务与恢复

API 先用现有 Identity 认证构造 actor ID 与严格字符串 role 快照；合法平台 role 包括 `super_admin`、`project_admin`、`employee` 与 `viewer`。普通 list/CRUD 保留该初始快照，不在 Inference 层增加账号活跃状态检查。Project 可见性依旧在其原有读取阶段调用 Workspace Project Query。

Apply 的公开 `require_project_owner` 检查仍在读取源编排之前执行。之后 Inference 按原顺序读取来源、校验 stages、查询 backend registry/pool/project enablement，再通过 Workspace 的固定能力按账号、项目、必要时成员的顺序重新读取当前权威并持锁到事务结束。Native 的每个阶段仍是单独语句，保留 READ COMMITTED 快照；Workspace facts 与最终 pipeline 写入共享一个 PostgreSQL 事务。

借用模式中，manager 返回 `caller_pending`；API 随即提交一次，再按完整原命令回执读取新的流水线行投影，并通过 Identity UserBrief 取创建者显示名。专用 DSN 和 HTTP owner 只有在自己的提交完成后返回 `committed`，API 不重复提交。apply 响应仍返回 `created_by_name: null`。回执绑定投影本身不要求外层事务已经提交；如果在调用方外层事务或 savepoint 中读取，它只证明当前事务可见的结果，不代表数据库已提交。

HTTP 执行收到含糊确认时，`PipelineHttpClient` 最多发送一次同命令 status 查询，不会重新发送写命令。status 只把其他已提交事务的回执报告为 `committed`；当前创建事务中的未提交回执为 `unknown`，这不等于确认回滚。未知结果返回 unavailable。公开重试会生成新操作 UUID，本 API 没有新增公开幂等键。

Pipeline 行 codec 会保留 `stages` 的 JSONB `null` 与标量，不提前改写历史值。非空 scalar 在 apply 的既有 `validate_saved_pipeline` 阶段失败；JSONB `null` 会通过该函数的旧空值快捷路径，随后在 backend 阶段迭代失败。list/detail 的 `ProjectPipelineOut.stages` 仍在 public response 阶段要求对象数组；mutation 的新行投影与 Identity 名称补充发生在提交后，所以 response validation failure 仍可能出现在提交之后。此处不把坏值提前清洗，也不声称预处理与旧失败时点等价。

## 当前边界

本页描述的代码与 opt-in 配置已经存在。借用事务、专用 DSN、HTTP 的冻结 API 回放、真实权限锁冲突、丢响应恢复、重启、非空备份恢复、独立安装包、受限容器和完整 migration 均已通过限定本地验收。Compose 的 CPU/内存限制是配置值，不代表生产容量或 SLO 验收。生产配置没有在本页范围内变更。该切片只覆盖保存的命名编排，不表示完整 Inference 模块已经可以任意更换语言；已有 Go 月报实验仍是独立尝试。

架构决策见 [ADR-0146](/dev/adr/0146-inference-named-pipeline-owner)。

## Workspace 的后端能力探测

`POST /v1/backends/probe` 接收 `aap-inference-backend-probe/v1` 请求：
`request_id`、`registry_id`、不透明 `probe_subject`、`timeout_seconds` 和两项
`CapabilityLimits`。成功回复只含绑定信息及 `modalities`；后端 URL、auth token 和
extra_params 不进入 Workspace。JSON Schema 随 `aap-inference-contract` 工件发布。

API 的 `INFERENCE_BACKEND_PROBE_URL` 与 `INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)`
互斥，均留空时借用 caller connection；HTTP 使用
`INFERENCE_BACKEND_PROBE_TOKEN(_FILE)`，默认超时30秒。
独立 Workspace 设置 `WORKSPACE_INFERENCE_PROBE_URL`、
`WORKSPACE_INFERENCE_PROBE_TOKEN(_FILE)` 和 `WORKSPACE_INFERENCE_PROBE_TIMEOUT_SECONDS`。
任何显式提供方失败都不选择另一个提供方。缺少内部能力返回503；实际后端 setup 失败保持
旧的 fail-open 行为，返回 null modalities；旧 JSON 数值物化失败仍导致配置事务回滚和500。

Inference 可以只设置 `INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)` 与
`INFERENCE_BACKEND_PROBE_CONTROL_TOKEN(_FILE)` 启动 probe-only 进程。
专用 `inference_backend_probe_client` 只有私有 policy/open 两项函数权限；迁移后依次应用
`module-data-grants.sql` 和 `inference-backend-probe-grants.sql`。它不能读 registry、
数据库私钥表或其他 owner 原表。Probe bearer 与 pipeline bearer 必须不同，不能互相调用。

```dotenv
# API on host
INFERENCE_BACKEND_PROBE_URL=http://127.0.0.1:8768
INFERENCE_BACKEND_PROBE_TOKEN_FILE=/absolute/path/to/probe-token
# Compose secret-file selectors
INFERENCE_RUNTIME_BACKEND_PROBE_DATABASE_URL_FILE=/absolute/path/to/probe-client-dsn
INFERENCE_RUNTIME_BACKEND_PROBE_CONTROL_TOKEN_FILE=/absolute/path/to/probe-token
# Independent Workspace on the same Compose network
WORKSPACE_INFERENCE_PROBE_URL=http://inference-probe:8768
WORKSPACE_RUNTIME_INFERENCE_PROBE_TOKEN_FILE=/absolute/path/to/probe-token
```

```sh
docker compose -f docker-compose.inference-probe.yml -f docker-compose.workspace-configuration.yml -f docker-compose.workspace-inference-probe.yml up -d --build inference-probe workspace-configuration
```

Workspace 还需要原有配置 DSN 和 control token 文件。Probe 示例与 pipeline 示例都发布宿主
8768，分别启动时只能选择其一；需要双能力时在同一 Inference 服务中显式提供两组凭据。
沿用 UID10008、只读根、1 CPU／256 MiB 等限制，它们是开发配置，不是生产容量结论。

Subject 由调用方事务内的固定 Native 函数加密生成，绑定数据库、registry 和仍在进行的
完整事务 ID，保留未提交后端及轮换前快照。它不新增业务提交或外部 I/O 前的 registry
行锁；事务结束后不能重新打开。请求上限16 MiB、回复上限4 KiB；压缩回复被拒绝，客户端
单次请求、不跟随重定向、不重试。此大小边界只适用于探测元数据，不适用于预测数据，
也不证明无限大历史 extra_params 的兼容性。详见
[ADR-0149](/dev/adr/0149-credential-owning-workspace-backend-probe)。

## 健康观察与调用方写回

既有项目、管理员、周期 worker 和 GPU 刷新的健康检查，把实际 `/health` 与条件
`/setup` 交给同一个 Inference 观察 owner。默认嵌入调用方，或显式选择专用 DSN／HTTP；
配置及受限 Compose 示例见仓库 `DEV.md` 的 Inference 健康观察一节。
API 的 `INFERENCE_HEALTH_OBSERVATION_DATABASE_URL(_FILE)` 与
`INFERENCE_HEALTH_OBSERVATION_URL` 互斥，HTTP bearer 为
`INFERENCE_HEALTH_OBSERVATION_TOKEN(_FILE)`。整个调用默认90秒，使用
`INFERENCE_HEALTH_OBSERVATION_TIMEOUT_SECONDS` 调整；每次真实后端请求仍受
`ML_HEALTH_TIMEOUT` 控制。显式选择失败不回退，调用方收到503并保留原事务回滚边界。

私有 `POST /v1/backends/health-observation` 只接受 registry/request UUID 和加密快照；
响应只有同一身份、快照摘要及密封证据，没有可伪造的 healthy/capabilities 布尔值或凭据。
快照保留初次已加载对象的端点和认证值，关闭自动刷新时不强制落库或重新读取。
专用只读 LOGIN 仅可调用 policy/open/seal；证据按数据库、原事务、请求和快照绑定，
另一个仍在运行的事务也不能消费它。提交、回滚、错配或修改证据均拒绝。

收到证据后，原调用方才继续能力提取与数据库时钟，再构造生命周期证明、锁定当前
Registry/GPU membership、写回和对账；观察服务没有健康业务写权限，也不会代替调用方提交。
嵌入模式的宽 API 数据库角色仍是过渡安排。本协议仅收口外部观察，完整健康写服务、
路由停流连续性及整个 Inference 模块尚未完成；详情见 [ADR-0152](/dev/adr/0152-credential-owning-health-observations)。

### 同事务的健康数据能力

`aap_inference_health_data_v1` 提供11项固定数据函数，后端状态写回与能力对账都不自行提交。API 保留原 Session 的对象身份、自动刷新及延迟 flush；独立安装包也可使用 `await HealthDataPostgresData.bind(connection)` 绑定已开启事务的 asyncpg 连接，再由调用方提交或回滚。提交或回滚后须丢弃绑定；原绑定在事务结束或更换后拒绝继续调用。

迁移后依次应用 `module-data-grants.sql` 与 `inference-health-data-grants.sql`，为 `inference_health_data_client` 授予封闭的11函数。此角色属于可信健康协调器，没有 Registry／Pool 原表权限；不能用它接收任意公开 HTTP 的健康结果。观察角色与签发／验证角色保持独立。完整远程协调、其他控制面写入与全模块替换仍在推进，见 [ADR-0153](/dev/adr/0153-caller-owned-native-health-data)。

## Registry 环境同步

启动时的环境后端同步按完整命令选择 owner。默认借用原 Session；
`INFERENCE_REGISTRY_INGRESS_DATABASE_URL(_FILE)` 使用独立受限数据库账号，
`INFERENCE_REGISTRY_INGRESS_URL` 使用独立 HTTP 进程，二者互斥。
HTTP bearer 使用 `INFERENCE_REGISTRY_INGRESS_TOKEN(_FILE)`，整体预算默认90秒，
由 `INFERENCE_REGISTRY_INGRESS_TIMEOUT_SECONDS` 控制；单次后端探测仍由
`ML_HEALTH_TIMEOUT` 控制。显式配置失败不会回退到本地写入。

独立进程使用相同 DSN/token selector，运行 `python -m aap_inference`；
受限容器示例见 `docker-compose.inference-registry.yml`。其 LOGIN 为
`inference_registry_ingress_client`，仅能执行11项固定 Native 函数。
先应用迁移及 module-data grants，再应用 `infra/docker/inference-registry-ingress-grants.sql`。

私有路由 `/v1/registry/env-sync` 接收受信任的运维 URL 配置和能力限制，
由 owner 实际执行 setup 探测；不接收调用方提供的健康结果。空 URL 列表会将全部
环境来源后端置为 disconnected，手工注册项继续优先，历史溯源不删除。
命令与响应各限24 MiB，bearer 与其他 Inference 能力分开。

回执区分 caller_pending、committed 和 unknown。客户端在响应不确定时只查询
`/v1/registry/env-sync-status` 一次，不重发写入；unknown 不能作为已回滚的依据。
截图 seed 复用相同 ingress 策略，但写入仍属于原调用方事务，项目选择与绑定继续由
Workspace 消费者负责。完整 Registry/Pool 管理、健康远程协调、路由排空及租约连续性
仍未闭合，参见 [ADR-0154](/dev/adr/0154-registry-ingress-and-environment-sync-owner)。

## 持久路由执行权

此能力正在本地联合验收，尚不代表完整 Inference 模块完成独立部署。
原有 `/api/v1` 预测和管理接口保持不变；私有 owner 增加以下能力：

| 路径                      | 用途                                                  |
| ------------------------- | ----------------------------------------------------- |
| `POST /v1/routes/command` | 执行准入、结算、心跳、关闭、管理占用和后端绑定意图    |
| `POST /v1/routes/status`  | 查询一次命令的持久结果，恢复丢失的回包                |
| `POST /v1/routes/runtime` | 批量读取最多 512 个实例的运行计数和控制状态；不写回执 |

API 通过 `INFERENCE_ROUTING_URL` 或 `INFERENCE_ROUTING_DATABASE_URL(_FILE)` 选择提供方，
两者互斥。未指定时，使用 API engine 上独立的执行控制事务，不提交当前业务事务。
HTTP 采用 `INFERENCE_ROUTING_TOKEN(_FILE)` 和 `INFERENCE_ROUTING_TIMEOUT_SECONDS`；
显式提供方失败不回退，也不会自动重发推理。

独立运行时使用 `INFERENCE_ROUTING_DATABASE_URL(_FILE)` 与独立
`INFERENCE_ROUTING_TOKEN(_FILE)`，并配置 `INFERENCE_ROUTING_HEALTH_MAX_AGE_SECONDS`
和 `INFERENCE_ROUTING_LEASE_TTL_SECONDS`。应用当前迁移后，依次应用
`module-data-grants.sql` 和 `inference-routing-grants.sql`，将
`inference_routing_client` 限定为 core policy、core step 与定向退役 step 三项固定函数；
不要为此角色授予原表或临时对象权限。

后端进程配置 `AAP_ROUTE_CONTROL_TOKEN(_FILE)`，其值与 Registry 中保存的 token 一致。
可用 `AAP_ROUTE_REGISTRY_ID` 固定身份；一个物理端点对应一个控制器进程。
每次启动都等待实际认证绑定，拒绝无凭据和旧代次计算，不提供旧流量启动绕过。
未配置控制凭据的普通旧推理可以运行，但无法确认管理连续性。

首次绑定由运维向私有 command 端点提交以下意图。`request_id` 标识这一次提交，
`control_id` 标识一次排空与绑定操作；均由调用方生成 UUID：

```json
{
  "schema": "aap-inference-routing/v1",
  "request_id": "<request-uuid>",
  "operation": "reconcile",
  "body": {
    "registry_id": "<registry-uuid>",
    "control_id": "<control-uuid>"
  }
}
```

提供方实际联系后端，先关闭旧准入并等待工作结束，再绑定新 epoch 和进程身份；
调用方不能提交声称已空闲的回执。排空未完成时，用新的 `request_id` 查询相同
`control_id` 的后续进展。旧请求的回执保持原结果，未知提交最多查一次状态，不能据此
重发写入。控制失败或超时的恢复仍在联合验收，不能手动清空在途记录来解除阻塞。

配置、解绑和删除继续由既有授权入口负责；Actor、Task、标注版本、业务审计和最终提交
仍属于原业务事务。接口和当前验证边界见 [ADR-0155](/dev/adr/0155-durable-inference-routing-authority)。

GPU 驻留驱逐使用独立的执行 scope，与既有 GPU 签名凭据共同授权卸载；不要求先在
服务池界面手动停流。成员激活、rollout 和取消排空沿用当前环境的连接来源。
卸载回包丢失后仍复查健康状态，只有新鲜且匹配的实际卸载证据才能回收预算；
检查失败或无法证明已卸载时保留未决状态。
发送前被路由拒绝的普通 GPU 请求可撤回自己的租约／冷加载预留；驱逐已经推进代次后
不能据此回退或清除预算，持续故障后的完整恢复仍在实施。日常及生产环境尚未切换。

### 单个执行范围的退役

独立 owner 的内部接口沿用 `INFERENCE_ROUTING_TOKEN(_FILE)`、请求字节上限和显式
连接来源，不增加部署 selector：

| 接口                                | 行为                                                        |
| ----------------------------------- | ----------------------------------------------------------- |
| `POST /v1/routes/retire`            | 创建或继续同一个 scope 的持久退役，联系实际后端确认工作结束 |
| `POST /v1/routes/retirement-status` | 只读取已持久化阶段，不发送后端控制或推理请求                |

请求包含 `schema: "aap-inference-scope-retirement/v1"`、固定的 `retirement_id`、
`scope_id` 和原 scope 的私有 `capability`。调用方保存退役 ID 后再发起命令；同一 ID
不能改用于另一个 scope，capability 不应写入日志、事件或业务响应。

退役先关闭旧准入，再在后端屏蔽旧 scope 的延迟 token，等待该范围的真实请求及已登记
后台工作结束。其他 scope 不必一起排空。只有与原后端身份匹配的证明才能进入
`completed`；`draining` 或 `state: "unknown"` 均不能当作旧工作已结束。退役不代表
丢失的推理结果成功，也不能据此重发模型写请求。

该能力已通过限定本地验收，覆盖实际 TCP、三种 API 连接方式、独立安装进程的停止／
重启与克隆恢复隔离；视频 runner 已消费精确退休证明恢复封存窗口。边界见
[ADR-0156](/dev/adr/0156-scoped-inference-retirement)。

### 视频执行代次与路由绑定

视频 runner 在业务写入前领取 Jobs 私有执行权，固定逻辑 `run_id`，用递增代次、worker
身份和私有 token 区分重派发。Inference 通过同一个内部 command 接口处理 `video_acquire`，
在同一数据库事务内保存请求身份、预留 scope 并绑定当前执行；公开 HTTP、SDK 和 Job prompt
不携带这些凭据。原有两项 core RPC 与19项 core 动作不变，路由 LOGIN 明确增加第四项固定
`video_execution_scope_step(text,text)` 能力，继续没有原表权限。

已被接替的执行不能取得新 scope、续租或重放 attempt 换取新准入。当前执行取消后可继续
续租并完成部分候选处理，但不能再发起推理；短暂锁竞争会重试。API 当前 runner 的启动、
进度、候选、Mask 根和失败写入使用原业务事务中的执行权检查，不能把另一个已提交事务的
guard 结果当作写许可。后台任务汇总使用稳定身份，仅投影锁定的主作业终态。

租约到期不证明旧后端工作结束。存在绑定 scope 时，runner 暂缓需要恢复的执行；只有精确
退役证明才能允许后续路由重绑定。冻结计划、窗口检查点和 worker 最终 Labeling writer
已有持久协议与限定本地验收；人工审核及中间写入仍未完整迁移。协议与边界见
[ADR-0157](/dev/adr/0157-video-execution-claims-and-route-binding)、
[ADR-0158](/dev/adr/0158-video-window-journals-and-replay) 与
[ADR-0159](/dev/adr/0159-independent-labeling-video-writer)。

### 离线视频恢复 operator

恢复交接使用 `aap-video-restore/v1` 与独立 `video_restore_operator_client` LOGIN，
只调用 `aap_inference_routing_v1.video_restore_step(text,text)`。它不是路由 listener 的
HTTP 功能，也不授予普通 routing／Jobs／API 角色。先应用迁移与 module-data owner 授权，
再单独应用 `infra/docker/video-restore-grants.sql`；不能把日常 API 宽权限连接交给 operator。

operator 显式读取权限仅限当前用户的数据库连接文件，通过 `--inspect` 获取非敏感源／
目标 realm 与密钥指纹。提交文件包含这些值、固定 restore UUID、备份文件 SHA-256 和
外部围栏记录的 SHA-256；连接串、控制 token、签名密钥与执行证明不放入命令行或输出。
`fence_sha256` 是记录引用，不是自动验证源环境已停写的凭据。

准备恢复后，普通 Jobs／Inference 权威保持关闭。Native 事务屏障先排空既有写事务；
该能力要求 READ COMMITTED，避免较旧的重复读快照绕过围栏。operator 读取实际后台的
当前 epoch，再按相同 incarnation 与 predecessor CAS 执行全局 drain/bind。活跃请求
未退出或结果不明时，保持准备态并重复同一命令；完成前再次核对每个后台的当前绑定。
源进程、普通 API ORM、数据库与对象存储凭据的外部围栏仍由操作人员负责。

最终事务轮换权威、使旧证明失效、重新包裹相同路由命令并保存追加的退休证据。
普通 claim 随后产生新代次，恢复继续使用原 run、冻结计划、输入身份与窗口字节；
后台 incarnation 改变、输入原始版本缺失或未知控制过渡仍拒绝同作业续跑。
限定本地验收已通过独立安装 CLI、真实 CPU 后台、数据库副本及实际 runner 的中断续跑，
包括旧请求未退出时保持准备态、后台 incarnation 变化拒绝、事务故障回滚与重复命令。
验收复用同一 S3 原始版本；完整对象存储恢复、GPU／生产容量与全部模块资格仍需单独验证。
决策与故障语义见 [ADR-0160](/dev/adr/0160-coordinated-video-restore) 和
[备份恢复前提](/ops/deploy/docker-compose#_5-0-恢复前提与执行权)。
