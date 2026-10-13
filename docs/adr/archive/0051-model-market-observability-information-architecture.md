# 0051 — 模型市场可观测性信息架构（四状态轴 + 诊断去重 + 卸载安全门）

- **Status:** Accepted（v0.23.4 P0–P4 范围；四状态轴、诊断去重键、卸载安全门为冻结决策）
- **Date:** 2026-07-20（提案 / P0 锁定，P4 落地）
- **Deciders:** core team
- **Supersedes:** —（在 [ADR-0050](./0050-ml-backend-service-pools-and-request-routing.md) 的服务池 / 实例 / GPU 三层之上定义观测面 IA；不改路由核心或 GPU 仲裁）

## Context

ADR-0050 把「服务池 / 实例 / GPU 物理资源 / 模型驻留池」拆成四层稳定 ID。模型市场的「注册管理」与「运行时观测」两块页面却仍按 v0.19.0 的扁平实例视图组织：

- `RegisteredBackendsTab.tsx` 把 URL / 来源 / 类型 / 并发 / GPU 预算 / 完整诊断 / 状态 / 时间 / 三个操作同时塞进 `min-w-[980px]` 的一行，桌面横向滚动，窄屏无主次折叠。
- `RuntimeObservePanel.tsx` 同时请求 `/all` + `/overview` + `/observe`，再在浏览器按 URL join；每个实例渲染一张大卡，健康 / GPU / compute / cache / 模型版本 / residency / 项目 / 操作 / 变体全部混在同一层，十几个实例形成连续卡片墙。
- 缺失值回落成 `connected` 缓存冒充「在线」；两个刷新按钮分别刷新「注册状态」和「实时指标」但不解释数据源、快照时间与陈旧程度。
- 「卸载」是直接实例动作，缺少「停止接流 → 等待活动请求归零 → 卸载」的安全顺序。
- 同一 GPU 超售问题在资源卡、实例 GPU 配置、全局诊断里重复出现三四次。

ADR-0050 发布的 `topology` / `runtime-snapshot` 读模型在 v0.23.3 首版是 `-> dict` 无类型返回，OpenAPI 序列化为 `{type: object, additionalProperties: true}`，前端 generated TS 是 `unknown`；也没有 freshness 信封、派生 `routable_instances` / `status` / `routing_reason_codes`，且把 `routing_policy` + `weight` 暴露给了 Project Admin（违反本计划 §5）。

候选方案：

| 选项                                                         | 主要卖点                                                                                                                                          | 主要劣势                                                                             |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| **A. 四状态轴 + 诊断去重 + 卸载安全门 + 合同补齐（本 ADR）** | 把「连通 / 路由 / 容量 / 驻留」拆成四条独立轴；同一诊断只渲染一次；卸载走 drain→quiescent→unload 门控；后端只读字段补齐让 generated TS 真正 typed | 新增一层 view-model + 5+5 组件；需要后端 response_model 补齐（属 §1 允许的只读修正） |
| B. 单「在线」徽标 + 卡片墙换皮                               | 改动最小                                                                                                                                          | 治标不治本：状态语义继续混在一起；URL join 仍漂移；安全卸载仍可绕过                  |
| C. 引入图表库 / 状态管理框架 / SSE                           | 实时趋势、动态拓扑                                                                                                                                | 违反 §4.2「不引入新图表库 / 状态管理框架 / 实时协议」；首版不需要                    |

## Decision

### D1. 四条独立状态轴（不合成单一「在线」徽标）

主视图按以下四轴分别判定，每条轴的来源不可互推：

| 状态轴    | 枚举                                                                                                       | 数据来源                                                          | 禁止推断                                                                                                      |
| --------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| 连通/健康 | `healthy / degraded / offline / unknown`                                                                   | runtime-snapshot `registry_state` + `health_fresh`                | `connected` 缓存不冒充实时 healthy                                                                            |
| 路由      | configured `active / draining / disabled` + effective `routable / draining / bypassed / blocked / unknown` | topology `traffic_state` + runtime `circuit_open` + `router_mode` | health 200 或 configured draining ≠ 已停流；`router_mode != enforce` 时 draining 只能显示 shadow              |
| 容量      | `idle / serving / saturated / unknown`                                                                     | runtime `route_inflight`                                          | GPU lease/queue/semaphore ≠ 路由 inflight；`limit=null`（v0.23.4 合同无 max_concurrency）时不可推算饱和百分比 |
| 驻留      | `empty / loading / resident / draining / unloading / unknown`                                              | `/observe` residency（InstanceDetailSheet 透出）                  | CPU compute ≠ GPU 已释放                                                                                      |

派生规则集中在 `runtimeTopology.ts:deriveMemberRouting` / `deriveMemberCapacity`（纯函数，无业务真值猜测）。

### D2. unknown / stale / partial 语义

- `unknown`：字段 `null` 或来源未提供 → 渲染「未知」/「暂无路由指标」，**禁止**回落为 `0` / `healthy` / `idle`。
- `stale`：来源 `updated_at` 超阈值或 `SourceFreshness.stale=true` → 保留上次成功值 + stale 标记 + 时间，**不沿用**实时状态色。
- `partial`：任一来源 stale/error → 聚合行显示「N/M 来源新鲜」+ partial_reason，**不抹掉**其它可信来源。

### D3. metrics-driven 字段恒为 null（不 wire Prometheus）

`last_selected_at` / `selection_count_window` / `rejection_count_window` / `p95_ms` / `error_rate` 在 Pydantic schema 中保留为 `None` 默认，前端统一渲染「暂无路由指标」。理由：v0.23.4 计划 §4.2 明确禁止 wiring 共享路由计数器，v0.23.3 Prometheus 模块未 wire。字段在 schema 中保留（而非删除）是为了 v0.23.4 不需要再次重构合同。

### D4. 诊断去重合同

稳定去重键 = `code + subject_type + subject_id`。同一键在问题中心只渲染一次主记录；`affected_service_pool_ids[]` / `affected_instance_ids[]` / `affected_gpu_resource_ids[]` 完整列出受影响对象；受影响行只显示计数 + 跳转，不复制全文。严重度 `info / warning / critical / blocker`，`ok` 不是问题记录。实现在 `runtimeTopology.ts:collectDiagnostics`。

### D5. 卸载安全门（drain → quiescent → unload）

`evaluateUnloadGate(member, routerMode, ledgerFresh)` 在 `runtimeTopology.ts` 中实现，阻塞条件：

1. 成员未精确进入 `draining`（`active` / `disabled` 都不能作为停流证明）；
2. `route_inflight > 0`（必须等待归零）；
3. `route_inflight` 缺失或路由账本 stale（无法确认 inflight 已归零）；
4. `router_mode != enforce`（drain 仅预配置未实际停流）。

任一阻塞 → 卸载按钮 disabled + tooltip 列出 reasons；强制卸载（若合同支持）是独立高风险 AlertDialog，不是默认路径。

### D6. 后端只读字段补齐（v0.23.4 内部吸收）

v0.23.3 发布的 `topology` / `runtime-snapshot` 是 `-> dict` 无类型返回。v0.23.4 P1 在 **不改路由核心** 的前提下补齐：

- 新增 `apps/api/app/schemas/ml_routing.py`：`TopologyResponse` / `RuntimeSnapshotResponse` 等 Pydantic 模型；
- `diagnostics.py:build_topology` / `build_runtime_snapshot` 返回 typed 模型；
- topology 增加 `routable_instances` / `status` / `status_reason_codes` 派生字段；
- runtime-snapshot 增加 `observed_at` / `partial` / `partial_reason` / `sources[]` freshness 信封；
- Project Admin 投影收紧：`routing_policy="unknown"`、member `weight` / `state` / `last_checked_at` / `gpu_resource_id` → `None`（服务端裁剪，不是前端隐藏）；
- 路由加 `response_model=TopologyResponse` / `RuntimeSnapshotResponse`，OpenAPI snapshot 生成真实 `$ref` schema。

属 §1 允许的「针对展示缺口的只读字段修正」。

### D7. 前端分层

```
apps/web/src/pages/ModelMarket/
├── runtimeTopology.ts                  # 纯 view-model（无 React/Query 依赖）
├── registry/                           # 注册管理 4 视图 + 问题中心
│   ├── ServicePoolsSection.tsx
│   ├── BackendInstancesSection.tsx
│   ├── GPUResourcesSection.tsx
│   ├── ProjectBindingsSection.tsx
│   └── IssueCenter.tsx
└── runtime/                            # 运行时观测 + 共享组件
    ├── StateTokens.ts                  # 四轴 label + variant + icon 集中映射
    ├── RuntimeStatusBadge.tsx          # 单轴徽标（icon+text，非纯色）
    ├── FreshnessIndicator.tsx          # 来源新鲜度 chip
    ├── TrafficDistributionBar.tsx      # 紧凑分段条 + 暂无指标 sentinel
    ├── DiagnosticBadge.tsx             # 严重度徽标 + 影响对象计数
    ├── ServicePoolRuntimeTable.tsx     # 池→成员树表
    ├── BackendInstanceRow.tsx
    ├── InstanceDetailSheet.tsx         # 详情下沉到 Sheet
    ├── EnvOnlyContainerCard.tsx        # 未纳管容器独立归组
    ├── LifecycleActions.tsx            # drain/resume/unload 门控
    └── parseResidency.ts               # 纯函数 residency 解析
```

`RegisteredBackendsTab.tsx` 与 `RuntimeObservePanel.tsx` 仅做编排，不再承载详情渲染。

### D8. 角色裁剪是服务端责任

Project Admin 通过 `topology` 端点拿到裁剪后的响应（`routing_policy="unknown"`、敏感字段 `None`），前端只是不渲染已被服务端置空的字段。**不依赖前端隐藏作为唯一门控**（§5 硬要求）。`runtime-snapshot` / `/observe` / `/gpu-resources` / `/overview` + 所有 mutation 端点对 Project Admin 返回 403。

## Consequences

正向：

- 超管能在不阅读原始 UUID 与诊断长文的前提下回答：哪些池可用 / 流量如何分布 / 故障属于哪条轴 / 一个问题影响哪些对象 / 能否安全维护实例。
- 同一诊断主文案只出现一次（`code + subject_type + subject_id` 去重）。
- 缺失 / 陈旧路由指标不显示为 0 或 healthy（D2 + D3）。
- 普通卸载不能在实例仍 routable / inflight 非零 / 数据 stale 时执行（D5）。
- generated TS 类型真实 typed（D6），不再 `unknown`。

负向：

- metrics-driven 字段在 v0.23.4 恒为 null，流量分布列首版显示「暂无路由指标」——需等后续版本 wire 共享计数器（§4.2 已禁止本期 wire）。
- gpu / residency 来源在 runtime-snapshot 中标 stale + `not_bundled_in_v0_23_3`，需通过 `/observe` 在 InstanceDetailSheet 透出——多一个数据源。
- 四状态轴比单「在线」徽标视觉信息密度更高，需要用户学习成本（由 icon + 文本 + tooltip 缓解）。

## Alternatives Considered

**方案 B（单「在线」徽标 + 卡片墙换皮）**：改动最小，但状态语义继续混在一起，URL join 仍漂移，安全卸载仍可绕过。不解决 §2.2 / §2.3 的任何结构性问题。

**方案 C（引入图表库 / 状态管理框架 / SSE）**：实时趋势与动态拓扑吸引人，但违反 §4.2「不引入新图表库 / 状态管理框架 / 实时协议」；首版用紧凑分段条 + 表格已能满足「十几个实例可横向比较」的需求。

## Notes

- 实现代码位置：
  - 后端合同：`apps/api/app/schemas/ml_routing.py`、`apps/api/app/services/ml_routing/diagnostics.py`、`apps/api/app/api/v1/admin_ml_integrations.py:1854-1889`
  - 前端 view-model：`apps/web/src/pages/ModelMarket/runtimeTopology.ts`
  - 注册管理：`apps/web/src/pages/ModelMarket/RegisteredBackendsTab.tsx` + `registry/`
  - 运行时观测：`apps/web/src/pages/ModelMarket/RuntimeObservePanel.tsx` + `runtime/`
- 相关 ADR：[ADR-0050](./0050-ml-backend-service-pools-and-request-routing.md)（服务池与路由核心）、[ADR-0049](./0049-cross-backend-gpu-memory-arbitration.md)（GPU 仲裁，正交）、[ADR-0044](./0044-global-ml-backend-registry-and-project-enablement.md)（全局注册表，底层）
- 后续可能演进：当共享路由计数器落地后，D3 的 metrics 字段从 null 切为真值，前端只需移除「暂无路由指标」sentinel，合同无需重构。
