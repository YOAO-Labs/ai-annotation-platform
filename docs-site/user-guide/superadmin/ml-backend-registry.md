---
audience: [super_admin]
type: how-to
since: v0.9.0
status: stable
last_reviewed: 2026-07-20
---

# ML Backend 注册（全局注册表）

ML Backend 是平台对接外部推理服务的契约层。注册表是**全局**的：一个物理 backend（一行 `ml_backends` 记录，URL 全局唯一）只注册一次，所有项目共享同一条记录。项目侧不再各自注册 backend，而是从全局注册表里**勾选启用**（详见 [启用 ML 后端](../projects/ml-backends)）。

## 入口

- 全局注册（新增 / 编辑 / 删除 / 健康检查，仅超管）：`/model-market` → **注册管理** tab
- 项目启用（勾选已注册 backend + 项目级覆盖，项目管理员 / 超级管理员）：项目设置 → **ML 模型** tab

## 表单字段

![注册表单全貌含 max_concurrency/extra_params](../images/superadmin/ml-backend/register-form.png)

| 字段         | 含义                                     | 约束                                                                                                                    |
| ------------ | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| 名称         | 显示名                                   | 无全局唯一性约束（`ml_backends` 表无 UNIQUE 索引）                                                                      |
| URL          | Backend HTTP 入口                        | **全局唯一**（同一 URL 只能注册一次）；**不能填 loopback**（详见下）                                                    |
| 交互式       | 是否支持工作台一键推理                   | 布尔开关，默认关                                                                                                        |
| API Key      | 可选，header `Authorization: Bearer ...` | —                                                                                                                       |
| GPU 物理资源 | 稳定 `gpu_resource_id`                   | 从运维显式配置的逐卡资源中选择；留空不等于已确认 CPU                                                                    |
| 显存预算     | `vram_budget_mb`                         | 与 GPU 资源成对填写，正整数 MiB，不得超过该卡 `allocatable_mb`                                                          |
| 驱逐优先级   | `eviction_priority`                      | 数值越大越难被驱逐，可为负数，不是请求队列优先级                                                                        |
| 最大并发     | `max_concurrency`                        | 1–32；始终作为单进程 / 事件循环的本地背压；`ML_BACKEND_ROUTER_MODE=enforce` 时还作为 Redis route lease 的跨进程实例上限 |
| 额外参数     | JSON 扩展字段                            | 不得与上述强类型字段冲突                                                                                                |

> **已删除的虚构字段**：`type`（类型选择器）、默认 prompt、默认阈值均**不是**注册表单的真实字段。backend 类型由 backend 自身通过 `/setup` 声明，不在注册时指定。`max_concurrency` 由表单单独填写后保存到 `extra_params` JSONB；其他扩展参数仍在高级 JSON 中编辑。

## URL 校验：拒绝 loopback

后端 Pydantic `field_validator` 会直接拒绝以下 host（`apps/api/app/schemas/ml_backend.py:8`）：

- `localhost`
- `127.0.0.1`（精确匹配，**不**拒绝整个 `127.x.x.x` 段）
- `0.0.0.0`
- `::1`

错误消息会指引你填正确的地址。背后原因：容器内 `localhost` 指向容器自身，不可能连到宿主机的 ML Backend。详见 [容器网络与 loopback](../../dev/troubleshooting/container-networking)。

**正确填法：**

| 场景                            | URL                                                                                          |
| ------------------------------- | -------------------------------------------------------------------------------------------- |
| Backend 在同一个 docker-compose | `http://grounded-sam2-backend:8001`                                                          |
| Backend 在宿主机 / 局域网       | `http://172.17.0.1:8001`（Linux 默认 bridge）/ `http://host.docker.internal:8001`（mac/win） |
| Backend 在另一台机器            | LAN IP / 域名                                                                                |

dev 环境 placeholder 已默认填 `172.17.0.1:8001`。

## 健康检查

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/superadmin/ml-backend/health-state-badges.png — connected/error/disconnected 三状态徽章对比 [manual] -->

注册后系统会自动调用 `GET <url>/health`。失败不阻断创建（避免临时网络问题让你卡住），但会在列表里显示红色徽章，状态值为 **`error`**（不是 `unreachable`；`state` 字段枚举：`disconnected` / `connected` / `error`，见 `apps/api/app/services/ml_backend.py:122`）。

**注册前连通性测试**：`POST /admin/ml-integrations/probe` 提供无 DB 副作用的探测（`/probe`），注册表单「测试连接」按钮即调用此端点。

## 全局注册 CRUD（超管）

注册管理 tab 对全局注册表做增删改查，对应后端端点：

```http
POST   /admin/ml-integrations/registry                  # 新增全局 backend
PUT    /admin/ml-integrations/registry/:registry_id     # 编辑
DELETE /admin/ml-integrations/registry/:registry_id     # 删除
POST   /admin/ml-integrations/registry/:registry_id/health   # 健康检查（探 /setup 派生能力）
GET    /admin/ml-integrations/all                        # 全局列表（新建项目向导复用）
```

每个全局 backend 注册后会自动得到一个 singleton 服务池（ADR-0050），池内含一个 active 成员指向该实例。off 模式下行为与单实例完全一致；当 `ML_BACKEND_ROUTER_MODE=observe|enforce` 时，路由器在该池内选择实例。详见 [ADR-0050 服务池与请求路由](../../dev/adr/archive/0050-ml-backend-service-pools-and-request-routing)。

## 服务池管理（超管）

<!-- TODO IMAGE_CHECKLIST: images/superadmin/model-market/registry-service-pools.png — 注册管理服务池主视图（结构化 tab + 展开成员 + 维护操作） [auto] -->

服务池是路由选择的逻辑边界，实例是可定位到物理 URL 的 registry 记录。一个池可含多个等价实例（同模型 / 同 schema，capability 指纹 exact match）。注册管理 tab 的「服务池」视图对池做增删改查 + drain / resume：

```http
GET    /admin/ml-integrations/service-pools                       # 列表（含成员）
POST   /admin/ml-integrations/service-pools                       # 新建池
GET    /admin/ml-integrations/service-pools/:pool_id              # 单池详情
PATCH  /admin/ml-integrations/service-pools/:pool_id              # 改名 / 启停
DELETE /admin/ml-integrations/service-pools/:pool_id              # 删除池
PUT    /admin/ml-integrations/service-pools/:pool_id/members/:registry_id   # 加入/改权重
DELETE /admin/ml-integrations/service-pools/:pool_id/members/:registry_id   # 移除成员
POST   /admin/ml-integrations/service-pools/:pool_id/members/:registry_id/drain    # active→draining
POST   /admin/ml-integrations/service-pools/:pool_id/members/:registry_id/resume   # draining→active
GET    /admin/ml-integrations/service-pools/:pool_id/members/:registry_id/capability-drift          # 预览能力差异
POST   /admin/ml-integrations/service-pools/:pool_id/members/:registry_id/capability-drift/accept   # 接受新基线
```

**drain / resume 语义**：

- **drain**：成员 `traffic_state` 从 `active` 切到 `draining`，停止接收**新** route lease；已在飞的请求继续完成。幂等。
- **resume**：`draining` 切回 `active`，恢复接流。幂等。`disabled` 成员不能直接 resume；这类状态需要先审核能力变更。
- drain / resume **只影响路由接流**，不触发模型权重卸载（那是 GPU 仲裁的 residency drain，独立）。
- 当 `ML_BACKEND_ROUTER_MODE != enforce` 时，drain 只是预配置，不会真正改变路由行为——运行时观测会标记为「未生效」。

**加入成员**：被加入的实例必须与池内现有成员 capability 指纹 exact match（同 `models[]` 模型目录、请求 schema、variant 组合和 tracker 等稳定能力，排除 URL/GPU/VRAM/residency 等运行态字段）。不匹配返回 `HTTP 409` 并附结构化 diff。对已在池内的成员再次 PUT 会更新权重，不会重置已有的 traffic state。同一实例不能同时属于两个服务池。

**审核能力变更**：健康刷新发现 `/setup` 的路由合同与服务池基线不一致时，平台会自动把成员设为 `disabled`；若它是 legacy 实例，还会关闭服务池。超管可在「服务池」展开成员或「实例」操作菜单中选择「审核能力变更」，查看旧/新指纹与 canonical 字段差异。确认时平台会重新执行健康与 `/setup` 探测，并校验候选指纹没有在审核期间再次变化；池内若仍有能力不一致的 active / draining 成员则拒绝。验证通过后，新基线、成员 `active` 状态、路由代际和可选的服务池启用在同一事务中提交。若实例已经回滚到原基线，则只恢复成员与服务池，不改写基线。该操作只接受明确审核过的合同，不会自动放行 capability superset。

**卸载 / 移除成员 / 删除**：纳管实例必须先进入 `draining`，并且只有在路由模式为 `enforce`、路由账本新鲜、清理过期 lease 后 exact `inflight=0` 时才会放行。账本失联、字段缺失或陈旧都会失败关闭，不会被当作零。删除服务池前必须逐一安全移除所有成员，空池才可删除。

## 项目启用

注册仅是创建可选项。真正生效需要项目把它启用：

1. 项目设置 → **ML 模型** → 点「管理 backend」在全局 backend 清单里**勾选启用**该 backend（推理参数运行时按 backend 自报的 `/setup.params` 调，不在此预设）
2. 同一页在 **项目主后端** 下拉里选一个**已启用**的 backend（设了主后端即视为启用 AI 预标注）
3. 保存 AI 设置

未设项目主后端直接跑预标会报错，并在前端给出配置引导。详见 [启用 ML 后端](../projects/ml-backends)。

## 引用全局注册项（不复制）

新建项目 wizard step 4 提供下拉选已注册的全局 backend；选中即为新项目**启用**该全局注册项（**引用同一全局 id，不再复制一份**）。这样所有项目共享同一物理 backend 记录，能力快照与健康状态一处维护、处处一致。

后端：`GET /admin/ml-integrations/all` 返回全局注册表列表。

## 删除

物理删除全局 backend 是**超管职责**（`DELETE /admin/ml-integrations/registry/:registry_id`）。删除前若有正在运行的预测 job、未完成 GPU retirement，或者路由安全门无法证明成员已停流且 inflight 归零，都会返回冲突或服务不可用错误并保留数据。条件全部满足后，删除会解除该实例的服务池成员关系；若它是 legacy 实例，所属池会停用并清除项目主绑定。历史预测仍按外键策略保留可追溯性。

项目管理员不能物理删除全局 backend，只能在项目设置里**取消勾选启用**让它对本项目停用（不影响其它项目）。

## 审计

以下事件写入 `audit_logs`：

- `ml_registry.created` / `ml_registry.updated` / `ml_registry.deleted`（全局注册 CRUD）
- `ml_service_pool.created` / `updated` / `deleted`（服务池 CRUD）
- `ml_service_pool.member_upserted` / `member_removed` / `member_drained` / `member_resumed`（成员与接流状态）
- `ml_service_pool.capability_drift_accepted`（审核并接受新的池能力基线）
- `ml_backend.created` / `updated` / `deleted` / `enablement`（项目兼容端点）
- `ml_backend.reloaded` / `ml_backend.unloaded` / `ml_backend.warmup`（生命周期动作）
- `ml_backend.smoke_tested`（模型市场试启动）

详见 [审计日志](./audit-logs)。

## 相关操作

- **观测（observe）**：`GET /admin/ml-integrations/observe` 直连 env 配的 ML Backend 容器，不需要项目注册即可看健康 / 变体目录。
- **试启动（smoke-test）**：`POST /admin/ml-integrations/observe/smoke-test`，只有新协议 residency 能严格证明所有 pool / builder / borrower 已空时才预热指定变体并自动还原；旧协议会同时保守检查图像池、视频池与活跃会话。
- **GPU 资源诊断**：超管可查看逐卡容量、静态预算、desired → effective mode 及配置 blocker。`observe` 仅记录影子决策，不会拒绝、排队或驱逐请求。
- **`max_concurrency`**：写入 `extra_params.max_concurrency`；本地 semaphore 始终生效，`ML_BACKEND_ROUTER_MODE=enforce` 时再由 Redis route lease 把 API 与多个 Celery worker 收口到同一实例上限。GPU 仲裁的逐资源 effective mode 管显存准入与驱逐，是另一条独立门禁。

## 相关

- [ADR 0015 — ML Backend URL 验证](../../dev/adr/archive/0015-ml-backend-url-validation)
- [模型市场](./model-market)
- [ML Backend 协议](../../dev/reference/ml-backend-protocol)
