# 0050 — ML Backend 服务池与真实请求路由（池 / 实例双 ID + 平滑加权轮询）

- **Status:** Accepted（v0.23.3 P0 范围；schema 与请求切换须经本文「冻结决策」逐 PR 落地）
- **Date:** 2026-07-20（提案 / P0 锁定）
- **Deciders:** core team
- **Supersedes:** —（在 [ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md) 的全局实例注册表之上加一层「逻辑服务池」，不改变 registry 行语义；与 [ADR-0049](./0049-cross-backend-gpu-memory-arbitration.md) 的 GPU 显存仲裁正交，互不替代）

## Context

ADR-0044 把「一个 ML backend 实例」上提为全局 `ml_backend_registry` 行（一行 = 一个唯一 URL + 能力快照 + 鉴权 + GPU claim）。项目层通过 `project_ml_backend` 显式启用某个实例；`Project.ml_backend_id` 指向项目主实例；`preannotate_pipeline[].ml_backend_id`、`Prediction.ml_backend_id`、`AsyncJob.payload.ml_backend_id` 与 `users.preferences.ai.*` 的多子键也都按 **物理实例 id** 分桶。

但「项目 / 用户想要的能力」和「平台具体派给哪个物理实例执行」从来没有被拆成两个步骤。一旦同一能力存在等价副本（同模型、同 schema 的第二个容器），现状没有机制表达「项目请求能力 → 平台在副本间选一个」。当前请求选择只有三类等价路径：

- `MLBackendService.get_project_backend`（`apps/api/app/services/ml_backend.py:601`）优先 `Project.ml_backend_id`，否则取第一个 connected interactive 实例；
- `get_tracker_backend_for_capabilities`（`ml_backend.py:618`）在 capable 列表里返回显式绑定或 `supporting[0]`；
- batch / pipeline / retry / secondary / tracker worker 在 job 启动时 `db.get(MLBackendRegistry, ml_backend_id)`，整批 / 整 stage / 整 job 复用同一行（`apps/api/app/workers/tasks.py:585,877`、`frame_preannotate.py:379`、`predictions_retry.py:181`、`services/video_tracking/runner.py:921`）。

这些路径既没有跨进程的原子选择，也没有权重、公平性、并发上限、traffic drain 或被动熔断。ADR-0049 明确**不做**同一 backend 多副本 / 负载均衡 / scale-out；它的职责是「在实例已经确定之后，按 `backend_registry_id + gpu_resource_id` 管理显存预算、request lease、卡级队列、驻留和驱逐」。所以负载均衡不能复用 GPU request lease。

如果不引入独立的服务池层，等价副本只能靠以下任一反模式承载，每个都会破坏既有不变量：

- 给 registry 行开「同一 URL 多份」——直接违反 ADR-0044 的「一行 = 一个唯一 URL」；
- 用 GPU 仲裁代选实例——把显存仲裁和路由并发控制耦死，CPU 实例、`gpu=off` 实例和 GPU `observe` 实例无法参与路由；
- 让第一方调用方各自 `supporting[0]` 或随机——API 与多个 Celery worker 之间没有一致选择，也没有 inflight / drain / 熔断。

候选方案：

| 选项                                                  | 主要卖点                                                                                                                                                                                           | 主要劣势                                                                                                                |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| **A. 新增服务池 + 路由 ledger（本 ADR）**             | 逻辑能力与物理实例解耦；项目启用与请求 lineage 以 pool id 为真值，现有 pipeline / 用户偏好合同继续使用 registry id 并在派发时解析所属 pool；跨进程原子选择、权重、并发、drain、熔断独立于 GPU 仲裁 | 新增两张表 + Redis ledger + 一套 API；第一方调用链需统一解析身份边界                                                    |
| B. 只把 GPU 仲裁改名复用                              | 不加新表                                                                                                                                                                                           | 违反 ADR-0049 不做负载均衡的边界；CPU / off / observe 实例无路由并发控制；route lease 与 GPU request lease 生命周期冲突 |
| C. 仅前端把若干 URL 分组                              | 零后端改动                                                                                                                                                                                         | 没有真实路由；选择仍按实例 id；副本扩容时项目配置和用户偏好会漂移                                                       |
| D. 多策略可插拔 router（随机 / least-request / 加权） | 灵活                                                                                                                                                                                               | 首版没有数据证明策略收益；插件框架是 speculative 复杂度，违反「一次只做一种生产策略」                                   |

## Decision

**采方案 A：在 `ml_backend_registry` 之上加一层「服务池」(`ml_backend_service_pools` + `ml_backend_pool_members`)，把「项目请求一个逻辑能力」与「平台选择一个物理实例」拆成两步；用独立的 Redis routing ledger 做跨进程原子选择。** 首版只实现一种生产策略：**平滑加权轮询（smooth weighted round robin, SWRR）+ per-instance 并发上限 + 被动熔断**。pool id 是逻辑请求身份，registry id 是物理执行身份，二者永不互换。

### 冻结决策

| ID  | 决策                                                                                                                                                                                                                                                                                                                                      |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | pool id 是逻辑请求身份；registry id 是物理执行身份。`Prediction.ml_backend_id` 永远表示实际执行的 selected registry instance；新增 `Prediction.ml_backend_pool_id` 表示 requested pool。                                                                                                                                                  |
| D2  | 一个 registry instance 同时最多属于一个 service pool（`ml_backend_pool_members.registry_id` 单列 unique）。                                                                                                                                                                                                                               |
| D3  | 一个 service pool 的全部 active 成员必须通过 routing capability fingerprint exact match；不匹配的成员不进入候选，不在运行时临时降级请求 schema。                                                                                                                                                                                          |
| D4  | 项目只能请求已启用的 pool；router 不能越过项目绑定选择实例（`project_ml_backend_pool` 是项目 × pool 的唯一启用真值）。                                                                                                                                                                                                                    |
| D5  | 只有 `traffic_state=active` 的成员可接收新 route lease；`draining` 保留既有 lease 不接收新 lease，`inflight=0` 后才能 disable / 移出 / 普通 unload。                                                                                                                                                                                      |
| D6  | route lease acquire / heartbeat / finish / cancel 使用 Redis 原子合同（Lua）；进程 crash 由 TTL 回收；`enforce` 下 Redis 不可用时 fail-closed。                                                                                                                                                                                           |
| D7  | routing ledger 与 GPU arbitration ledger 完全分离：route lease 不复用 GPU request lease；CPU / off / observe 实例也必须参与路由并发控制；traffic drain 与 GPU residency drain 不共享状态字段或 ACK。                                                                                                                                      |
| D8  | GPU dispatch 始终使用 selected registry id；GPU membership / token / lease / generation / fence 不池化。                                                                                                                                                                                                                                  |
| D9  | 不做透明跨实例 retry、hedged request 或 speculative execution；失败只影响后续选择，避免预测重复、状态会话破坏和非幂等副作用。                                                                                                                                                                                                             |
| D10 | 首版只实现 smooth weighted round robin；不预留多策略插件框架。least-request 等策略若需要，单独新增 ADR / feature。                                                                                                                                                                                                                        |
| D11 | 全部候选达到并发上限时返回结构化 503 `ml_backend_pool_saturated` + `Retry-After`；首版没有全局路由等待队列。                                                                                                                                                                                                                              |
| D12 | 多阶段聚合 Prediction 的 `ml_backend_id` 保存 root stage selected instance；每 stage / invocation 的双 ID lineage 必须存进 `PredictionMeta.extra.pipeline.selections[]`，不能把 root instance 冒充全部 shape 的生产者。                                                                                                                   |
| D13 | 没有真实指标时返回 unknown / null，不把缺失值解释成 0 或健康。                                                                                                                                                                                                                                                                            |
| D14 | 被动熔断只把连接拒绝、connect / read timeout、无 HTTP 响应和受控 gateway unavailable 计为 transport failure；业务 4xx、模型校验失败、GPU capacity 503、主动 cancel 不触发 ejection；熔断只作用于后续请求，不重放已失败的非幂等预测。                                                                                                      |
| D15 | 非空且 enabled 的 pool 必须有 `legacy_instance_id`，且它必须是本池成员；它只服务 `off` / `observe` 兼容 dispatch，不参与 `enforce` 优先级。空 pool 只能 disabled。                                                                                                                                                                        |
| D16 | pool / member / weight / traffic state 任何变更都单调增加 `routing_generation`；旧 generation 不得 acquire。                                                                                                                                                                                                                              |
| D17 | `off` → `observe` → `enforce` 是部署级单一开关（`ML_BACKEND_ROUTER_MODE`）；API / worker / beat 必须读取相同配置，部署时整体重建 / 重启，不能混跑不同 mode。                                                                                                                                                                              |
| D18 | migration 是 forward-only 安全：每个现有 registry 自动得到一个 singleton pool；多成员 pool 创建后 downgrade 不再无损，迁移必须 fail-closed 并提示 forward-only。                                                                                                                                                                          |
| D19 | 字段名或 schema 明确表示 backend / registry 的公共配置不得被迁移静默改写为 pool id。`preannotate_pipeline[].ml_backend_id`、`projects.default_variants` 的 key 以及 `users.preferences.ai` 的 backend 分桶字段保留 registry id；派发时通过 registry 的唯一成员关系取得 requested pool。项目启用/主绑定与新增请求 lineage 仍使用 pool id。 |

### 数据模型（摘要，详见计划 §5）

新增两张表，registry 行语义不变：

- `ml_backend_service_pools`：`id`（逻辑请求稳定身份）、`name`、`enabled`、`routing_policy`（首版仅 `smooth_weighted_round_robin`）、`legacy_instance_id`（nullable FK → registry，本池成员）、`routing_generation`（单调递增）、`capability_fingerprint`、`capability_snapshot`、时间戳。**不**保存 URL / auth / GPU claim / model residency / 实例 health。
- `ml_backend_pool_members`：`id`、`pool_id`、`registry_id`（单列 unique，保证一实例只属于一个 pool）、`traffic_state`（`active` / `draining` / `disabled`，DB CHECK）、`weight`（1..100，默认 1）、时间戳。`(pool_id, registry_id)` unique。FK 用 RESTRICT，删除 registry 前必须先 drain + inflight=0 + GPU retirement + 成员移除；若它是 `legacy_instance_id` 还必须先显式换人或把空 pool 原子置 disabled。

项目绑定迁移（`project_ml_backend` → `project_ml_backend_pool`）：

- `registry_id` → `pool_id`，唯一约束变为 `(project_id, pool_id)`。
- `Project.ml_backend_id` → `ml_backend_pool_id`，成为项目主服务池；项目启用和主池必须一致。
- 项目启用关联上的 `default_variants` 是 pool 级覆盖。`projects.default_variants`、`preannotate_pipeline[].ml_backend_id` 和用户 AI 偏好的既有公共字段保留 registry id，不做 UUID 语义偷换；它们的请求在派发边界解析所属 pool。

请求与结果溯源：

- `Prediction.ml_backend_id`：单阶段 = selected registry instance；多阶段聚合 = root stage selected instance（列语义明确不是「全部 shape producer」）。
- 新增 `Prediction.ml_backend_pool_id`（单阶段 = requested pool；多阶段 = root stage pool）。
- `PredictionMeta.extra.pipeline.selections[]`：每 stage / invocation 的 `{pool_id, instance_id, operation, shape / result}` 关联——多实例聚合的完整 lineage。
- `FailedPrediction` 保存失败所在 stage / pool + selected instance；选择前失败允许 instance id = null，不能一律写 root instance。
- `AsyncJob.payload / result`：新任务写 `ml_backend_pool_id` + 每阶段 pool id + selection 汇总；实例级失败记录 selected instance。
- 审计 / 结构化日志统一字段 `service_pool_id` / `backend_registry_id` / `route_lease_id` / `routing_outcome`。

### Capability 等价合同

服务池表示可互换的逻辑能力。成员加入前必须执行新鲜 `/setup` 探测并计算 canonical fingerprint（包含 protocol version、model ids、task、modality、infra、model / weights version、supported prompts / inputs / outputs / trackers、影响请求合法性的 parameter schema 字段、variant axes 与默认值、stateful / batchable / warmup 等路由能力；**排除** URL、实例名、auth、health 时间、GPU UUID、显存、residency、cache 等运行态或展示字段）。创建 pool 时以 seed instance 生成 pool snapshot / fingerprint；加成员必须 exact match，否则 409 `ml_backend_pool_capability_mismatch` + 结构化 diff。active 成员后续 health 刷新发现 fingerprint 漂移时原子改为 disabled 并记录诊断 / 审计。fingerprint canonicalization、排序、缺省值和 SHA 算法必须有 golden fixture，前后端不重复实现。

### 路由领域与依赖边界

新增 `app/services/ml_routing/`（`contracts.py` / `capability.py` / `policy.py` / `ledger.py` / `router.py` / `safety.py` / `diagnostics.py` / `metrics.py`）。被动熔断由 `ledger.py` 维护；`safety.py` 提供卸载、成员移除与 registry 删除共用的 quiescence 校验。边界：

- `MLBackendRouter` 返回 selected `MLBackendRegistry` + route lease，**不发 HTTP 请求**。
- 既有 `MLBackendClient`（`apps/api/app/services/ml_client.py:140`）仍只负责一个实例的 transport / auth / local semaphore / GPU dispatch；router 不反向导入它，避免新循环依赖。
- router 可以依赖 registry model、routing ledger、capability；**不得**导入 API router 或 worker。
- GPU arbitration 不导入 ml_routing；route selection 完成后单向调用实例 client。
- 破坏性实例操作必须复用 `safety.py`，在 router enforce、成员精确 draining、账本可读且清理过期 lease 后 exact inflight=0 时才放行；未知状态失败关闭。
- 禁止把全部实现塞回 `ml_backend.py` 或 `ml_client.py`，也不新增泛化 BaseRouter / repository 框架。

候选构建顺序固定：先验证项目已启用 pool 与 pool enabled → 再从 DB 读同一 `routing_generation` 下 active member（registry 必须 connected、health 未过期、capability fingerprint exact match）→ Redis acquire 再原子排除 circuit open 与达到 max concurrency 的成员 → 任何一步不确定都返回结构化 reason，不回落到名称 / URL / `legacy_instance_id` 猜测。

### Redis routing ledger（与 GPU ledger 隔离）

namespace 固定为 `ml-router:v1`（GPU 仲裁为 `gpu-arbiter:v1`，零交叉）：

```text
ml-router:v1:pool:{pool_id}:state
ml-router:v1:pool:{pool_id}:member:{instance_id}:leases
ml-router:v1:lease:{lease_id}
ml-router:v1:pool:{pool_id}:member:{instance_id}:circuit
ml-router:v1:pool:{pool_id}:metrics:{bucket}
```

acquire Lua 在一个原子步骤完成：校验 pool / candidate generation → 清理过期 route lease → 校验调用方传入的 active / fresh / capability-compatible candidates 并排除 circuit open 与达到 max concurrency 的实例 → 对剩余候选执行 SWRR → 写入 exact lease id / pool / instance / owner / operation / expiry → 更新 current weight、selection counter、运行时 bucket → 返回 selected instance 或结构化 rejection reason。heartbeat 只能延长 exact 未过期 lease；`finish(lease_id, outcome, duration)` 原子记录 metrics、更新 passive circuit 并释放；caller cancel 使用不计 transport failure 的 `cancel`。两者都 exact、幂等，重复终态返回同一稳定结果，不重复累计指标。key、KEYS / ARGV 顺序、TTL、返回码与 Lua SHA-256 在 P1 冻结 golden。

`metrics:{bucket}` 使用固定分钟桶和有限 histogram 边界，至少保存 selection / success / error / rejection / duration buckets；bucket 过期自动回收，**不**保存 request / lease 级高基数明细。route inflight / lease deadline 从 Redis snapshot 聚合（避免多进程 Gauge 各写各的）。

### 调度、熔断、draining

- **SWRR**：每轮 eligible 成员 `current_weight += configured_weight`；选 current weight 最大者；并列按稳定 instance UUID 排序；选中者 `current_weight -= eligible_total_weight`；达到 max concurrency 的成员先排除，不参与当轮权重推进；membership / weight generation 改变时旧 current weight 按新集合安全重置。
- **被动熔断**：连续 transport failure 达阈值后在 Redis 写 `open_until`，后续 selection 排除该实例；新鲜 health success 或 cooldown 后进入一次 half-open probe；成功关闭、失败重新 open；不自动重试当前失败请求。
- **Traffic draining**：`active --drain--> draining --inflight=0--> disabled`，`resume` 可从 draining 回 active，disabled 后 resume 需重新验证 health / capability。drain mutation 先持久化 membership state，再使新 acquire 不可选。`traffic_state` 是 configured state；runtime 另返回由 rollout mode 决定的 `effective_routing_state`：off / observe 下 drain 响应必须标 `effective=false`，shadow inflight 不能证明安全卸载；**只有 enforce 下 draining + 正式 route inflight=0 才形成 traffic quiescence proof**。traffic drain 完成不证明 GPU residency 已释放，卸载仍走现有 GPU / backend lifecycle proof。

### 路由选择作用域

同步 interactive / predict = 单 HTTP 业务请求（请求完成走 finish，主动取消走 cancel）；batch 单阶段 = 每个 task / execution unit（不把整批永久压在一个实例）；pipeline = 每个 stage invocation；video tracker / stateful session = 整个 job / session pin（heartbeat 保活，不能中途换实例）；retry failed prediction = 新的显式请求（可选不同实例，写新 selection 证据）；health / setup / warmup / reload / unload = 精确实例，**不**经过 pool router。

### Rollout 模式

| mode      | 实际实例                                                          | routing ledger                                       | 失败处理                              |
| --------- | ----------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------- |
| `off`     | 请求携带的 legacy instance；新 pool 请求使用 `legacy_instance_id` | 不 acquire                                           | 保持 v0.23.2 行为                     |
| `observe` | 请求携带的 legacy instance；新 pool 请求使用 `legacy_instance_id` | shadow namespace 计算 would-select、记录诊断，不门控 | ledger 失败不阻断实际请求             |
| `enforce` | router selected instance                                          | 正式 acquire / heartbeat / finish / cancel           | Redis / topology 不确定时 fail-closed |

新增环境变量（最终名称在 P0 按 typed settings 冻结）：`ML_BACKEND_ROUTER_MODE`、`ML_BACKEND_ROUTER_HEALTH_MAX_AGE_SECONDS`、`ML_BACKEND_ROUTER_LEASE_TTL_SECONDS`、`ML_BACKEND_ROUTER_HEARTBEAT_INTERVAL_SECONDS`、`ML_BACKEND_ROUTER_PASSIVE_FAILURE_THRESHOLD`、`ML_BACKEND_ROUTER_EJECT_SECONDS`。进入 enforce 前必须：migration / backfill 零 orphan、全部第一方调用方已传 pool id、observe 窗口 actual / would-select 一致且无未知映射、route lease cleanup / crash TTL / Redis 故障 / worker cancel 演练通过、至少一个真实双实例 pool 完成比例 / 饱和 / drain / ejection / GPU 交互验收。

### 第一方调用链切换（合同）

切换后调用形态统一：`resolve project + pool permission → router.acquire(pool_id, operation, owner) → selected registry instance → MLBackendClient(instance) → GPU dispatch + HTTP → record pool / instance / outcome → router.finish() / cancel() exactly once`。任何第一方生产路径直接 `db.get(MLBackendRegistry, requested_id)` 并发起推理都属于未迁移；实例 lifecycle / health / warmup / reload / unload 路径除外（见 P0 inventory）。

### API 合同（冻结）

Super Admin：`GET/POST/GET/PATCH/DELETE /admin/ml-integrations/service-pools[/:pool_id]`、`PUT/DELETE /admin/ml-integrations/service-pools/:pool_id/members/:registry_id`、`POST /admin/ml-integrations/service-pools/:pool_id/members/:registry_id/{drain,resume}`。Project Admin：`GET /projects/:project_id/ml-backends/pools/available`、`PUT /projects/:project_id/ml-backends/pools/:pool_id/enablement`。v0.23.4 读模型：`GET /admin/ml-integrations/{topology,runtime-snapshot}`（topology 双角色服务端裁剪，runtime snapshot 仅 Super Admin）。错误码：`ml_backend_pool_not_enabled` / `ml_backend_pool_unavailable` / `ml_backend_pool_saturated` / `ml_backend_router_unavailable` / `ml_backend_pool_capability_mismatch` / `ml_backend_member_draining` / `ml_backend_member_not_quiescent` / `ml_backend_legacy_instance_unmapped`（HTTP status、Retry-After、日志 reason、OpenAPI schema 一致）。

### 兼容窗口与 downgrade

- 新 API / 第一方 Web / worker 用 `ml_backend_pool_id`；旧请求中的 `ml_backend_id` 先尝试解析为 legacy registry id，再解析其唯一 pool；响应增加弃用提示，但本版本不删除字段。
- lifecycle API 的 `{registry_id}` 永远继续表示实例，不套用上述别名。
- 旧 `/admin/ml-integrations/all` 保持实例列表，供 v0.23.3 现有模型市场页面继续工作；完整池管理 UI 留给 v0.23.4。
- 所有 pool 都是 migration 生成的 singleton、且没有新 pool / 多成员 / pool-only 项目绑定时，可按保存映射逆迁移；一旦创建多成员 pool、变更 / 删除 legacy instance 或写入仅新 schema 可表达的 pool 绑定，downgrade 不再无损，迁移 fail-closed 并提示 forward-only。

### 实施纠偏

初始服务池迁移曾把 `preannotate_pipeline[].ml_backend_id`、`projects.default_variants` 的 backend-keyed 数据和 `users.preferences.ai` 的 backend 分桶字段误写为 pool id，与公共 API schema 及前端 registry 索引不兼容。纠正迁移 `0133_restore_registry_public_ids` 按 singleton 成员关系恢复这些字段的 registry id，并保留项目启用、项目主绑定与请求 lineage 的 pool id；这落实了 D19 的身份边界，不改变服务池路由决策。

## Consequences

正向：

- 「项目请求一个能力」与「平台选一个实例」首次成为两个明确步骤；项目启用和请求溯源使用 pool id，既有 pipeline / 用户偏好使用 registry id 并通过唯一成员关系进入同一路由边界，不破坏公共 schema 与前端索引。
- 跨进程原子选择 + 权重 + 并发上限 + drain + 被动熔断首次覆盖 API 与所有 Celery worker；CPU / off / observe 实例和无 GPU claim 实例也参与路由并发控制。
- routing ledger 与 GPU arbitration ledger 完全分离，二者可同时观测、独立回滚；GPU dispatch 仍用 selected registry id，ADR-0049 的 generation / fence / membership 不受影响。
- `Prediction` / `FailedPrediction` / `AsyncJob` / audit / logs / metrics 具备 pool + instance 双 ID，多阶段聚合的 stage-level lineage 完整，不再错误归因到 root instance。
- v0.23.4 模型市场只消费本文冻结的 topology / runtime contract，不复制 router 判定。

负向：

- 一次性成本高：两张新表 + Redis ledger + 一套 API + 八个错误码；第一方调用链（interactive / batch / pipeline / frame fan-out / tracker / retry / secondary）必须明确哪些边界接收 pool id、哪些现有公共字段仍接收 registry id，并使用机器可读的唯一成员映射进入路由。
- migration forward-only：创建多成员 pool 后无法无损 downgrade。
- 路由只覆盖第一方调用；平台外直连 predict / warmup / reload（ADR-0049 D13 已在 enforce 下用 admission token 限制）仍可能绕开 router，需运维侧网络与 token 双重约束。
- 首版无全局路由等待队列：全部候选达到并发上限直接 503，对突发负载没有缓冲；stateful tracker / session 必须 job-scope pin，不能在执行中换实例。
- 被动熔断只作用于后续请求；当前失败的非幂等预测不会被自动重试，需要用户显式 retry。

## Alternatives Considered（详）

**方案 B（GPU 仲裁代选实例）**：把「选哪个副本」塞进 ADR-0049 的 GPU ledger。否决——ADR-0049 明确「在实例已经确定之后」管显存；route lease 与 GPU request lease 生命周期不同（route lease 要覆盖 HTTP transport timeout，GPU request lease 只管显存准入），CPU 实例、`gpu=off`、GPU `observe` 实例都没有 GPU lease 可借，会被排除出路由。耦合后任一侧故障或回滚都会牵连另一侧。

**方案 C（前端 URL 分组）**：在模型市场页面把若干 backend URL 归成一组。否决——没有任何真实路由；后端选择仍按实例 id，副本扩容时 `Project.ml_backend_id`、`preannotate_pipeline[].ml_backend_id`、`users.preferences.ai.*` 全部漂移，prediction 溯源也无法区分 requested pool 与 selected instance。这与「真实请求负载均衡」的目标相悖。

**方案 D（多策略可插拔 router）**：首版就提供随机 / least-request / 加权等多个策略并留插件框架。否决——首版没有任何 latency / inflight 历史数据证明 least-request 比 SWRR 好；插件框架是 speculative 复杂度，违反「最小代码解决问题」。least-request 等若确实需要，单独新增 ADR / feature。

**「一实例可属于多 pool」子选项**：允许同一 registry 实例同时是多个 pool 的成员。否决——首版没有能力隔离证据，多 pool 共享实例会让 route inflight / circuit / drain 状态归属歧义；一实例一 pool 让 drain / 移除 / GPU retirement 的 proof 单值化。真有多能力实例，应注册成多行（ADR-0044 的「一行 = 一个唯一 URL」仍成立）。

**「透明跨实例 retry」子选项**：transport 失败后自动在另一副本重试。否决——预测非幂等（同一 prompt 在不同模型 / variant 下结果不同），重试会导致重复预测或状态会话损坏；GPU capacity 503 已由 ADR-0049 处理。失败只影响后续选择，由用户显式 retry。

## Notes

- 目标版本：**v0.23.3**，计划详见 [`docs/plans/archive/2026-07-20-v0.23.3-ml-backend-service-pool-load-balancing-foundation.md`../../plans/archive/2026-07-20-v0.23.3-ml-backend-service-pool-load-balancing-foundation.md)。本文是 P0 交付物；ADR 被接受之前可以完成 inventory / fixture / 实验脚本，但不得合并生产 schema 或请求切换（计划 §4）。
- 涉及代码（当前状态，待迁移）：
  - 数据模型：`apps/api/app/db/models/ml_backend_registry.py:18`（`MLBackendRegistry`）、`:84`（`ProjectMLBackend`）、`apps/api/app/db/models/project.py:43`（`Project.ml_backend_id`）、`apps/api/app/db/models/prediction.py:18`（`Prediction.ml_backend_id`，按月分区）、`:101`（`FailedPrediction.ml_backend_id`）、`apps/api/app/db/models/async_job.py:49`（`AsyncJob.payload / result`）、`apps/api/app/db/models/user.py:60`（`User.preferences`）。
  - 请求选择：`apps/api/app/services/ml_backend.py:131`（`get`）、`:582`（`get_interactive_backend`）、`:601`（`get_project_backend`）、`:618`（`get_tracker_backend_for_capabilities`）；`apps/api/app/services/ml_client.py:140`（`MLBackendClient`，被 12 个 prediction 站点 + 多个 lifecycle 站点构造）。
  - 第一方调用链（prediction，需 router）：`apps/api/app/api/v1/ml_backends.py:678,719,777,891`（interactive / frame 路由）、`apps/api/app/api/v1/projects.py:1560`（preannotate trigger）、`apps/api/app/api/v1/tasks/annotations.py:264`（secondary inference）、`apps/api/app/workers/tasks.py:585,866,877`（batch source / downstream）、`apps/api/app/workers/frame_preannotate.py:379`（frame fan-out）、`apps/api/app/workers/predictions_retry.py:181`（retry）、`apps/api/app/services/video_tracking/runner.py:921`（tracker pin）、`apps/api/app/services/video_tracking/adapters.py:161`（tracker per-window）、`apps/api/app/services/secondary_inference.py:217`。
  - lifecycle 例外（保实例，不经 router）：`/health` / `/setup` / `/capabilities` / `/unload` / `/reload` / `/warmup` / smoke-test / GPU arbitration / 项目克隆 capability 提取 / 周期 stats publisher。
  - JSONB 引用（7 类）：`projects.preannotate_pipeline[].ml_backend_id`、`projects.default_variants`（backend-keyed）、`users.preferences.ai.{params_by_backend, model_by_backend, interactive_backend_by_project, secondary_by_model}`、`AsyncJob.payload.ml_backend_id`。
  - 前端 / SDK：`apps/web/src/pages/AIPreAnnotate/**`、`apps/web/src/api/{adminMlIntegrations,ml-backends,projects,tasks,failed-predictions,mlCapabilities}.ts`、`apps/web/src/components/projects/CreateProjectWizard.tsx` + `steps/Step4Ai.tsx`、`packages/python-sdk/`。
- alembic：head 当前为 `0131`（`apps/api/alembic/versions/0131_gpu_collector_fence_delete_guard.py`），pool 迁移从 `0131` 分叉；`predictions` 按月分区，FK / 索引须在父表与分区上同步（ADR-0006 / ADR-0044 先例）。
- Redis namespace：routing ledger 用 `ml-router:v1`，GPU 仲裁用 `gpu-arbiter:v1`（`apps/api/app/services/gpu_arbitration/ledger/types.py:15`），零交叉。
- 相关 ADR：[ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md)（全局注册表）、[ADR-0049](./0049-cross-backend-gpu-memory-arbitration.md)（GPU 显存仲裁）、ADR-0006（predictions 分区）、[ADR-0043](./0043-staged-preannotation-pipeline.md)（多阶段 pipeline）。
- 后续演进触发条件：least-request / 自适应权重若需要，单独 ADR；池 merge / 跨实例 autoscale / 服务发现属于更后续 epic，不在本版本预留。
- v0.23.4 前置：本文冻结的 topology / runtime contract 是 v0.23.4「模型市场注册管理 / 运行时观测」UI 的硬前置；v0.23.4 不得复制 router 判定。
