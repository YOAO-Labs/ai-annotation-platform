---
audience: [dev, ops]
type: reference
since: v0.9.0
status: stable
last_reviewed: 2026-10-21
---

# ML Backend

## 全局注册表

```http
GET    /api/v1/admin/ml-integrations/all                         # 全局列表
POST   /api/v1/admin/ml-integrations/registry                    # 新增（仅超级管理员）
PUT    /api/v1/admin/ml-integrations/registry/:registry_id       # 编辑（仅超级管理员）
DELETE /api/v1/admin/ml-integrations/registry/:registry_id       # 删除（仅超级管理员）
POST   /api/v1/admin/ml-integrations/registry/:registry_id/health # 健康检查
POST   /api/v1/admin/ml-integrations/probe                       # 注册前无副作用探测
```

一个物理 backend 对应一条全局 registry 记录；项目只引用它，不复制 URL、鉴权或能力快照。创建请求使用真实注册合同：

```http
POST /api/v1/admin/ml-integrations/registry
{
  "name": "sam-prod",
  "url": "http://172.17.0.1:8001",
  "is_interactive": true,
  "auth_method": "token",
  "auth_token": "...",
  "extra_params": { "max_concurrency": 4 },
  "gpu_resource_id": "gpu-node-a/GPU-1234abcd",
  "vram_budget_mb": 12288,
  "eviction_priority": 0
}
```

`gpu_resource_id` 与 `vram_budget_mb` 必须同时填写或同时为 `null`。`type`、默认 prompt、默认阈值和 `api_key` 不是注册字段；backend 类型及推理参数由 `/setup` 自描述，Bearer 凭据使用 `auth_method="token"` + `auth_token`。

URL 校验拒绝 host 精确为 `localhost / 127.0.0.1 / 0.0.0.0 / ::1`（[ADR 0015](../../dev/adr/archive/0015-ml-backend-url-validation)）。失败返回 422，并提示改用 Docker service DNS、bridge IP 或可路由域名。

## 修改 / 删除

编辑使用 `PUT /registry/:registry_id` 的部分字段请求体。受 GPU 管理的实例若已有运行时 epoch，修改 URL、鉴权、GPU claim 或 `extra_params` 前必须先完成 retirement。

删除不会把未知运行态当作空闲。若仍有运行中的预测任务、未完成 GPU retirement，或无法证明成员处于 `ML_BACKEND_ROUTER_MODE=enforce`、`draining` 且新鲜路由账本中的 exact `inflight=0`，接口返回 409/503 并保留记录。满足门禁后才解除服务池成员关系和项目主绑定；历史预测按外键策略继续保留可追溯性。

删除和成员移除会在写锁内重验安全检查时观察到的成员 ID、所属池、路由代次及接流状态。变化返回409，`detail.error_code=ml_backend_topology_changed`，须重新读取、drain 并重试。删除清理项目主绑定遇争用时返回409，`detail.reason=project_busy`，整个调用事务回滚。项目／模板配置等待池锁后若所选成员已离开该池，同样通过现有配置冲突返回409；不会继续启用旧池。

## 健康检查

注册前可用 `/probe` 测试远端 `/health` 与 `/setup`，不会写数据库。请求的 `url` 必须是有效 HTTP(S) 地址，携带合法 host 与端口，不得包含内嵌凭据（用户名/密码）、查询参数（`?`）、fragment（`#`）或控制字符，否则返回 422；网络错误使用安全错误分类，不回显目标 URL 或令牌。注册后的 `/registry/:registry_id/health` 会刷新连接状态、能力快照和 singleton 服务池能力指纹；能力合同漂移时，相关成员会停止接流等待重新校验。远端暂时不可达不会阻断创建，但 registry 会显示错误状态。

## 项目启用与主后端

每个项目从全局注册表勾选启用 backend，并设主后端。项目启用在取得项目与服务池锁后重新核验成员归属和池资格；等待期间池被停用或成员被移走，返回409并要求刷新后重试。

启用 / 覆盖（项目作用域）：

```http
GET /api/v1/projects/:id/ml-backends/available
PUT /api/v1/projects/:id/ml-backends/:registry_id/enablement
{ "enabled": true, "box_threshold": 0.3, "text_threshold": 0.25, "default_variants": {} }
```

- `available` 列出全部全局 backend + 本项目启用态 / 覆盖。
- `enablement` 切换启用并写项目级覆盖（`box_threshold` / `text_threshold` / `default_variants` 均可选）。

项目作用域端点同时执行角色门与项目范围门：

- list / get / setup / capabilities / interactive inference：调用者必须能看见该项目；读指定 backend 时还要求它已对本项目启用。
- create / update / delete(enablement disable) / health / capabilities refresh / warmup / predict-test：要求项目 owner（超级管理员仍按全局特权放行）。仅有 `project_admin` 角色但不是该项目 owner 不足以执行写操作。此外，`warmup` 要求目标 backend 已在当前项目中启用，否则在上游操作之前直接返回 404。
- reload / unload：**仅超级管理员**。二者改写的是「全局 backend 显存驻留 / 常驻变体」——同一物理 backend 被多个项目共用，项目 owner 若能触发就会驱逐 / 换掉其他项目正在用的权重。故这类破坏性驻留操作收口到平台管理员，与「运行时观测」面板（super_admin only）及 admin `observe/smoke-test` 运维基线一致。纳入服务池的实例执行 unload 前还必须处于 `enforce + draining`，且 Redis 路由账本能提供新鲜的 exact `inflight=0` 证明；任一值缺失或 Redis 失联都返回 409/503，不会将未知解释为空闲。构造性的 warmup 是项目自身预标 / 交互推理的前置（如预热加载类别表），刻意保留在项目 owner。
- 带 `task_id` 的测试 / 交互端点还会校验 task 确实属于 URL 中的 project；不属于时统一返回 404，避免跨项目 IDOR 与存在性泄露。

设主后端 / AI 开关（走项目本体）：

```http
PATCH /api/v1/projects/:id
{ "ml_backend_id": 3, "ai_enabled": true }
```

`ml_backend_id` 只能取**已启用**的 backend。未设主后端直接触发预标会得到明确错误：

```json
{ "detail": "Project has no ML backend bound. Configure in project settings." }
```

> 旧的项目作用域端点仍向后兼容：`POST /api/v1/projects/:id/ml-backends`（按 URL 复用或新建全局项 + 启用）、`DELETE`（停用）。主推路径是上面的启用清单。

## 项目服务池绑定

项目设置仍可通过 registry 兼容端点展示和选择物理 backend；存储层会把 registry 解析到它唯一所属的服务池。新调用方若直接管理逻辑路由边界，使用 pool 端点：

```http
GET /api/v1/projects/:id/ml-backends/pools/available
PUT /api/v1/projects/:id/ml-backends/pools/:pool_id/enablement
{ "enabled": true, "default_variants": {} }
```

项目启用、项目主绑定和请求 lineage 使用 pool id；预标注 pipeline、项目默认变体和用户 AI 偏好等既有公共字段继续使用 registry id。两类 UUID 不可互换，服务端只通过唯一成员关系做显式解析。

## 服务池管理与观测

```http
GET    /api/v1/admin/ml-integrations/service-pools
POST   /api/v1/admin/ml-integrations/service-pools
GET    /api/v1/admin/ml-integrations/service-pools/:pool_id
PATCH  /api/v1/admin/ml-integrations/service-pools/:pool_id
DELETE /api/v1/admin/ml-integrations/service-pools/:pool_id
PUT    /api/v1/admin/ml-integrations/service-pools/:pool_id/members/:registry_id
DELETE /api/v1/admin/ml-integrations/service-pools/:pool_id/members/:registry_id
POST   /api/v1/admin/ml-integrations/service-pools/:pool_id/members/:registry_id/drain
POST   /api/v1/admin/ml-integrations/service-pools/:pool_id/members/:registry_id/resume
GET    /api/v1/admin/ml-integrations/topology
GET    /api/v1/admin/ml-integrations/runtime-snapshot
```

服务池和成员 mutation 仅超级管理员可用。成员 PUT 是幂等 upsert：对已在本池的实例只更新权重，保留 traffic state；实例已属于其他池、能力指纹不一致或能力基线不可用时返回 409。移除成员与物理删除 registry 共用上述停流证明；删除服务池前必须先安全移除全部成员。

`topology` 对项目管理员和超级管理员开放，但在服务端裁剪项目管理员不可见的权重、接流状态和 GPU 标识；`runtime-snapshot` 仅超级管理员可用，并携带各数据来源的 `updated_at / stale / error` 与 partial 信封。

## 协议

ML Backend 必须实现：

| 端点            | 用途                                                                           |
| --------------- | ------------------------------------------------------------------------------ |
| `GET /health`   | 健康检查                                                                       |
| `POST /predict` | 推理（请求/响应见 [ML Backend 协议](../../dev/reference/ml-backend-protocol)） |

参考实现：`docs-site/dev/examples/echo-ml-backend/`。

## 能力目录（capabilities）

能力声明协议 v2（v0.14.9）下，平台把 backend `/setup` 派生成 model 粒度的能力目录，供模型市场与工作台多模型选择器消费：

```http
GET  /api/v1/projects/:id/ml-backends/:bid/capabilities
POST /api/v1/projects/:id/ml-backends/:bid/capabilities/refresh
```

- 服务端探 backend `/setup`（复用 30s setup 缓存链路）后调 `extract_capabilities` 派生快照，返回 `models[]` / `infra` / `modalities` 与扁平并集字段（所有 model 的 prompts / geometry 去重合并）。
- 老 backend（协议 v1，无 `models[]`）由平台合成隐式单 model（`id="default"`），返回结构一致，长度为 1。
- `refresh` 对该 backend 强制重探并刷新缓存。
- `/setup` 与 capabilities 允许能看见该项目的项目管理员、超级管理员、审核员、标注员读取；`refresh` 只允许项目 owner / 超级管理员。backend `/setup` 不可达时返回 502。
- 能力快照同时落库到 `ml_backend_registry.health_meta["capabilities"]`（健康检查时写入），无独立迁移。

字段语义与受控词表见 [ML Backend 协议 §4.1](../../dev/reference/ml-backend-protocol)。

## 交互式示例候选

图片通过 `POST /api/v1/projects/:id/ml-backends/:backend_id/interactive-annotating` 发起；视频当前帧使用同前缀的 `interactive-annotating-frame`，附带帧图和 `frame_index`。SAM3 的 `context.type=exemplar` 支持以下组合：

| `output` | `output_geometry` | `result` 候选                                 |
| -------- | ----------------- | --------------------------------------------- |
| `box`    | `polygon`         | `rectanglelabels`                             |
| `mask`   | `polygon`         | `polygonlabels`                               |
| `both`   | `polygon`         | 按实例交错的 `rectanglelabels, polygonlabels` |
| `mask`   | `mask`            | 原生 `mask`                                   |
| `both`   | `mask`            | 按实例交错的 `rectanglelabels, mask`          |

仅框召回不使用原生 Mask 合同。`both + mask` 的每对矩形和 Mask 必须具有相同标签与分数；矩形需为图像范围内的非空归一化框，空 Mask 不得留下孤立矩形。其他原生 Mask 请求不接受混入矩形。

原生候选的 ID 与采纳回执绑定其在完整 `result` 中的索引，例如第二个实例的 Mask 索引为 `3`。`accept_receipts` 只为 Mask 签发；客户端删除或采纳其他候选后，仍须使用原索引采纳 Mask，不得按过滤后的列表重新编号。框沿用普通标注创建路径，Mask 使用[原子采纳接口](./tasks-and-annotations#原生-ai-mask-候选采纳)。

## 审计

全局注册写入 `ml_registry.created / updated / deleted`，服务池及成员操作写入 `ml_service_pool.*`；项目兼容端点继续记录 `ml_backend.*`。全部事件进入 `audit_logs`。

## 相关

- [超管 - ML Backend 注册](../../user-guide/superadmin/ml-backend-registry)
- [容器网络与 loopback](../../dev/troubleshooting/container-networking)
- [预标注流水线](../../dev/concepts/prediction-pipeline)
