---
audience: [ops, dev]
type: reference
since: v0.8.7
status: stable
last_reviewed: 2026-07-22
---

# 可观测性 / 运维监控

> 适用读者：负责上线运维 / SRE 视角的工程师；需要在 production 看 panel 排查抖动的开发者。

FastAPI `/metrics` 暴露 Prometheus metrics。超管也可以直接打开 `/admin/health` 查看同一批基础探测的实时摘要；该页面适合快速确认 DB / Redis / MinIO / Celery 是否可用，长期趋势和告警仍以 Prometheus / Grafana 为准。

| Metric                                        | 类型      | Labels                                       | 用途                                                |
| --------------------------------------------- | --------- | -------------------------------------------- | --------------------------------------------------- |
| `http_requests_total`                         | Counter   | `method`, `path`, `status_code`              | 按路由模板统计请求 QPS / 错误率                     |
| `http_request_duration_seconds`               | Histogram | `method`, `path`                             | 请求延迟分位（p50 / p95 / p99）                     |
| `ml_backend_request_duration_seconds`         | Histogram | `backend_id`, `outcome`                      | ML Backend 调用延迟                                 |
| `celery_queue_length`                         | Gauge     | `queue`                                      | 各队列堆积（default / ml / media / audit / events） |
| `celery_worker_heartbeat_seconds`             | Gauge     | `worker`                                     | worker 上次心跳距今秒数                             |
| `raster_mask_content_operations_total`        | Counter   | `operation`, `outcome`                       | Mask 内容加载、存储与校验结果                       |
| `raster_mask_content_errors_total`            | Counter   | `operation`, `reason`                        | Mask 内容错误的固定原因分类                         |
| `raster_mask_active_geometries`               | Gauge     | `kind`                                       | 活跃图片 Mask 标注与含 Mask 预测数量                |
| `mask_ai_operations_total`                    | Counter   | 受控操作、提示、输出、候选、决定、降级与结果 | Mask AI 成功、失败与冲突率                          |
| `mask_ai_phase_duration_seconds`              | Histogram | `operation`, `phase`, `outcome`              | Mask AI 各阶段耗时                                  |
| `mask_ai_correction_jobs`                     | Gauge     | `status`                                     | 视频 Mask 纠错 job 库存                             |
| `mask_ai_correction_oldest_age_seconds`       | Gauge     | `status`                                     | 最老活跃纠错 job 年龄                               |
| `mask_ai_staged_mask_references`              | Gauge     | `job_kind`                                   | Tracker staged Mask 引用库存                        |
| `mask_ai_accept_decisions`                    | Gauge     | `state`                                      | 原生 Mask 接受幂等快照库存                          |
| `mask_ai_oldest_expired_decision_age_seconds` | Gauge     | —                                            | 最老过期幂等快照积压年龄                            |

定义在 `apps/api/app/observability/metrics.py` 与 `apps/api/app/main.py:108`。

---

## Raster Mask 内容指标

`raster_mask_content_operations_total` 的 `operation` 固定为 `load`、`store`、`verify`，
`outcome` 固定为 `success`、`error`。计数发生在最外层异步内容边界：例如 verify 内部读取对象，
只增加一次 `operation="verify"`，不会同时增加嵌套的 load；gzip 存储回落到普通 JSON 也只增加一次 store。

错误计数的 `reason` 只使用以下固定集合：

- `missing_object`、`digest_mismatch`、`size_mismatch`、`run_mismatch`、`byte_mismatch`；
- `invalid_encoding`、`invalid_payload`、`storage_unavailable`、`unknown`。

这些指标不携带 task id、annotation id、prediction id 或 object key。定位具体对象时，先按
`operation` / `reason` 查看错误速率，再结合相同时间窗口的请求日志或 worker 日志排查，避免把业务标识
引入 Prometheus label。

`raster_mask_active_geometries{kind="annotation"}` 统计仍活跃且顶层几何为 `raster_mask` 的标注；
`kind="prediction"` 统计结果中包含 raster geometry 的 Prediction 行。API 健康巡检与对象 GC 每次运行
都会从 PostgreSQL 精确刷新这两个序列。查询或指标客户端失败时保留上次值，不把未知写成零，也不会改变
GC 的引用集合或中断清理任务。

常用查询：

```promql
sum by (operation, reason) (rate(raster_mask_content_errors_total[5m]))
```

```promql
raster_mask_active_geometries
```

监控栈预置三条 Raster Mask 告警：

- `RasterMaskContentCorruption`：对象缺失、摘要、尺寸、runs、bytes 或 payload 校验错误持续 5 分钟，视为严重内容完整性问题；
- `RasterMaskStorageUnavailable`：对象存储错误持续 5 分钟，检查 MinIO 健康、网络和 bucket 权限；
- `RasterMaskInventoryMetricMissing`：annotation / prediction 任一固定库存序列缺失 15 分钟，检查健康巡检、GC 与 scrape。

告警 label 不含业务标识。收到告警后，先在 `Anno Overview` 按
`operation / reason` 确定错误类型和时间窗，再检索同窗口 API / worker 日志中的 request id 与 task id。

---

## Mask AI 指标与隐私边界

`mask_ai_operations_total` 只接受固定低基数词表：

- `operation`：`single_frame`、`refine`、`correction`；
- `prompt_family`：`point`、`bbox`、`mask`、`scribble`、`text`、`exemplar`、
  `correction_frame`、`unknown`；
- `output_geometry`：`mask`、`polygon`、`bbox`、`unknown`；
- `candidate_count`：`0`、`1`、`2_3`、`4_10`、`11_plus`；
- `decision`：`none`、`accept`、`reject`、`cancel`；
- `fallback_reason`：`none`、`mask_prompt_unsupported`、`unknown`；
- `outcome`：`success`、`error`、`conflict`。

任何未知调用值都会归一化，不能把 task、annotation、backend、object key、候选摘要或用户提示正文
带入 label。HTTP 指标的 `path` 同样只使用 FastAPI 路由模板；未匹配的 `/api/` 请求统一归入
`/api/unmatched`，不会为 UUID 路径创建时序。

阶段耗时的定义如下：

| `phase`     | 边界                                              |
| ----------- | ------------------------------------------------- |
| `upload`    | 视频当前帧完成有界读取后，上传临时帧到对象存储    |
| `inference` | 平台调用实际路由到的 ML Backend                   |
| `decode`    | 校验并归一化 backend 响应、RLE 和诊断             |
| `encode`    | 生成平台响应、候选签名与短期会话令牌              |
| `commit`    | 接受候选、局部决定或纠错 job 状态写入数据库并提交 |

库存 Gauge 在 API `/metrics` scrape 时从 PostgreSQL 刷新，并为所有固定 status / kind / state
显式输出零值。查询失败时保留上次成功值并让 scrape 继续返回；排查时不要把陈旧值理解为实时零。
`mask_ai_correction_jobs` 区分 `queued`、`running`、`pending_review`、`partially_reviewed`、
`accepted`、`discarded`、`failed`、`cancelled`；待审 staged candidate 超过 24 小时会由每日 GC
释放引用，待审 job 转为 `discarded`，已取消 job 保持 `cancelled`。

平台与 SAM backend 的普通日志不写完整图片、RLE counts、scribble 点集、文本提示或 logits；
`mask_input_next` 是加密且鉴权的五分钟令牌，只存在于当前浏览器会话和请求响应，不持久化。

预置告警覆盖：操作失败率、冲突率、纠错排队 / 运行超时、待审积压、过期幂等快照积压和固定
库存序列缺失。`Anno Overview` 展示平台操作、阶段 p95、纠错状态 / 年龄、staged 引用和决定库存；
`ML Backends` 展示 backend 侧 Mask AI 请求速率与推理 p95。

---

## 1. 本地启动监控栈

本地监控栈通过 docker-compose `monitoring` profile 启动（默认不启动，避免 dev 多吃约 200 MB）：

```bash
docker compose --profile monitoring up -d prometheus grafana
```

- Prometheus → http://localhost:9090
- Grafana → http://localhost:3001（admin / admin，dev 默认）

Grafana 启动时自动 provision：

- Datasource `Prometheus`（`infra/grafana/provisioning/datasources/prometheus.yaml`）
- Dashboard 文件夹 `Anno`（`infra/grafana/provisioning/dashboards/default.yaml`）
- Dashboard JSON `Anno Overview`（`infra/grafana/dashboards/anno-overview.json`）

打开 Grafana → Dashboards → Anno → Anno Overview，可查看 HTTP rate / HTTP p95 /
ML p50/p95/p99 / Celery queue / Celery worker heartbeat、Raster Mask 内容指标，以及 Mask AI 操作、
阶段延迟、纠错 job、staged 引用与决定库存。

> Linux 上 `host.docker.internal` 默认未解析；docker-compose.yml 已显式 `extra_hosts: host.docker.internal:host-gateway`。如果 Docker 版本太旧不支持，把 `infra/prometheus/prometheus.yml` 中的 target 改成宿主机 LAN IP。

---

## 2. Production 部署

production 不建议把 prometheus / grafana 跟应用塞同一 docker-compose（资源 / 升级耦合）。建议：

- 用现有运维栈的 prometheus / grafana 实例
- scrape config 把 `apps/api/openapi.snapshot.json` 那类配置参考 `infra/prometheus/prometheus.yml`，target 改成 internal API 域名 / SRV
- Grafana 一次性 import `infra/grafana/dashboards/anno-overview.json`（点 `+` → Import → Upload JSON）
- 后续 dashboard 升级 = git pull + 再 import 一次（Grafana 选 Replace 即可）

---

## 3. ML Backend GPU 指标接入（自动发现，v0.11.19）

ML backend（grounded-sam2 / sam3 / 后续接入的任意 backend）的 `/metrics` 由 Prometheus 的 `ml-backends` job 自动抓取，**无需手改 `prometheus.yml`**：

- 该 job 用 `http_sd_config` 定期拉 anno-api 的 `/api/v1/internal/metrics-targets`，端点从 `ml_backend_registry`（`state != disconnected`）生成 target 列表并按 host:port 去重。**新 backend 在超管「模型市场」注册即被纳入抓取**，与 PerfHud 共用同一真相源。响应即 Prometheus http_sd 原生格式（`include_in_schema=False`，不入 OpenAPI；实现 `apps/api/app/api/v1/internal.py`）：

  ```json
  [
    {
      "targets": ["grounded-sam2-backend:8080"],
      "labels": {
        "service": "grounded-sam2",
        "backend_id": "1",
        "project_id": "1"
      }
    }
  ]
  ```

  鉴权可选：`METRICS_SD_TOKEN` 非空时端点校验 `Authorization: Bearer <env>`，否则免鉴权（与 `/metrics` 同走 nginx `/internal` 网段隔离）。

- 指标统一为**裸名 + `service` label**（不带 backend 前缀），同语义指标靠 label 区分 backend：

| Metric                                                                  | 类型              | 用途                                                      |
| ----------------------------------------------------------------------- | ----------------- | --------------------------------------------------------- |
| `gpu_utilization_percent`                                               | Gauge             | GPU SM 利用率                                             |
| `gpu_memory_used_mb` / `gpu_memory_total_mb`                            | Gauge             | 显存（占用率 = used/total）                               |
| `gpu_temperature_celsius` / `gpu_power_watts`                           | Gauge             | 温度 / 功耗                                               |
| `inference_latency_seconds`                                             | Histogram         | 推理延迟（P50/P95 用 `histogram_quantile`）               |
| `embedding_cache_hits_total` / `_misses_total` / `embedding_cache_size` | Counter/Gauge     | embedding cache 命中与容量                                |
| `container_cpu_percent` / `container_memory_percent`                    | Gauge             | 容器 CPU / 内存                                           |
| `video_tracker_*`（仅 grounded-sam2）                                   | Counter/Histogram | 视频追踪帧数 / 延迟                                       |
| `mask_ai_backend_inference_total`                                       | Counter           | 视频追踪 / 纠错调用、候选桶、显式降级与结果               |
| `mask_ai_backend_inference_seconds`                                     | Histogram         | grounded-SAM2、SAM3 multiplex / PVS 的追踪 / 纠错推理延迟 |

> backend 在独立 GPU 机、prometheus 不在同网时，把 `ml-backends` job 的 `http_sd_configs` 注释掉，改用同 job 里注释好的 static 兜底（target 填 prometheus 视角可达地址）。
>
> **dev 前提**：`pnpm dev:api` 默认 `uvicorn --port 8000`（绑 `127.0.0.1`），prometheus 容器经 host-gateway 抓不到——http_sd 与**既有 `anno-api` job 同此前提**。要在 dev 抓到，用 `uvicorn app.main:app --reload --port 8000 --host 0.0.0.0` 起 api；或按上一条用 static 兜底，target 写 bridge gateway `172.17.0.1:<host 映射端口>`（dev 下 backend 的 service DNS 未必解析）。
>
> 与超管 PerfHud 的关系：两套通道、同一数据源（`/metrics` 与 `/health` 共用同一次 NVML/psutil 采样）。PerfHud 实时一眼看（无历史），Prometheus 留 14d 时序做趋势/告警。Grafana 的 `ML Backends` dashboard（`infra/grafana/dashboards/ml-backends.json`，v0.11.20，provisioning 自动加载）据此渲染。

---

## 4. 告警

**ML backend GPU 告警已落地**（v0.11.21）：`infra/prometheus/alerts.yml` 提供 `MLBackendDown`（`up==0` 持续 5m）/ `GPUMemoryHigh`（显存 >90% 持续 10m）/ `InferenceLatencyHigh`（P95 >10s 持续 10m）三条规则，随 `monitoring` profile 的 `alertmanager`（9093）经 SMTP 发邮件 —— dev 投递到 mailpit（见 `infra/alertmanager/alertmanager.yml`），生产换真实 SMTP。`MLBackendDown` 只覆盖 `ml-backends` job 的 target（由 http_sd 从 `state != disconnected` 的 backend 生成），主动 disconnect 的 backend 不会误报。排查见 [ML Backend 不可用 runbook](/ops/runbooks/ml-backend-down)。

其余 HTTP / Celery / Sentry 相关不在仓库内强制产出（不同团队偏好不一），下面是建议规则：

| 告警                               | 触发表达式                                                                                                                                   | 严重度   |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| API p99 > 1s 持续 5min             | `histogram_quantile(0.99, sum by (le)(rate(http_request_duration_seconds_bucket[5m]))) > 1`                                                  | warning  |
| ML Backend 失败率 > 10%            | `sum(rate(ml_backend_request_duration_seconds_count{outcome="error"}[5m])) / sum(rate(ml_backend_request_duration_seconds_count[5m])) > 0.1` | critical |
| Celery queue 堆积 > 200 持续 10min | `celery_queue_length > 200`                                                                                                                  | warning  |
| Worker 离线 > 2min                 | `celery_worker_heartbeat_seconds > 120`                                                                                                      | critical |
| Sentry DSN 未配置 production       | (启动日志 WARN，配合 deploy.md checklist)                                                                                                    | one-shot |

---

## 5. ML Backend pool 观测口径 (v0.14.14)

各 backend 的 `/health.pool` 统一为 `PoolStatus`（协议 §4.3），运维 / 模型市场都从同一结构读：

```jsonc
{
  "pool": {
    "cap": 4,
    "current_size": 2,
    "loaded_keys": [
      {
        "key": "yolov11/s/detection", // backend-defined opaque string
        "loaded_at": "2026-06-08T03:11:22Z",
        "last_used_at": "2026-06-08T03:15:00Z",
        "hit_count": 12, // 累计命中次数 (不含 warmup)
      },
    ],
    "last_evict": {
      "key": "yolov8/x/detection",
      "at": "2026-06-08T03:14:00Z",
      "reason": "lru", // 受控: lru | manual | idle_timeout
    },
  },
}
```

**key 命名约定**（backend 自由选择，平台只做相等匹配）：

| backend               | key 形式                                                                 |
| --------------------- | ------------------------------------------------------------------------ |
| yolo-backend          | `{series}/{size}/{task}`，如 `yolov11/s/detection`                       |
| grounded-sam2-backend | `sam={sam_variant}/dino={dino_variant}`；video 池另用 `video:sam=…` 区分 |
| sam3-backend          | 模型变体字符串如 `sam3`；`cap` 永远 `1`                                  |

**`/predict` 响应观测三件套**（协议 §4.2）：

| 字段                                      | 用途                                                                                      |
| ----------------------------------------- | ----------------------------------------------------------------------------------------- |
| `cache_hit: bool \| null`                 | 本次推理是否命中 pool 内权重；`false` 表本次触发加载（冷启动 / pool evict 后 / 服务重启） |
| `model_load_ms: int \| null`              | 本次 disk→GPU 加载毫秒（`cache_hit=True` 时通常 `0`）                                     |
| `pool_state: {current_size, cap} \| null` | 轻量 pool 快照（按需开启，常态 `null` 避免响应体膨胀）                                    |

**前端冷启动 UX**：响应回来后调 `recordPredictCacheHit(backendId, variants, cache_hit)` 写入本会话 Map，下次同 variant 调用前查 `isVariantHot()` 决定按钮文案。pool LRU evict 后误判一次不可避免（前端 Map 还以为热的），但下次响应里 `cache_hit=false` 会自我修正。v0.14.13 的 `sessionStorage` 猜测保留作老 backend (未上报 `cache_hit`) 的 fallback，v0.14.15 删除。

**显式预热（`POST /warmup`）**：运维 / 用户可在不消耗推理算力的前提下把指定 variant 权重加载到 pool。响应 `evicted` 字段回填本次因 cap 上限淘汰的 key 名，供前端 toast 提示。例：

```bash
# 三 backend 的 warmup body 各自定义, 响应统一为 WarmupResponse:
curl -X POST localhost:8003/warmup \
  -d '{"task":"detection","variants":{"series":"yolov11","size":"s"}}' | jq
# {"ok":true,"model_load_ms":4500,"cache_hit":false,"evicted":null}
```

平台代理 `POST /api/v1/projects/{pid}/ml-backends/{bid}/warmup` 把 body 原样转发；upstream 4xx 透传，5xx 502 兜底。

> **术语区分**：本节 §5 描述的 `pool` 是**实例内部的模型驻留池**（单 backend 的 `/health.pool`、cap、loaded_keys、LRU evict）。ADR-0050 引入的**服务池** (`ml_backend_service_pools`) 是不同的概念——它是跨实例的**请求路由逻辑边界**，决定一个请求由哪个物理实例执行。两者的观测口径完全独立：模型驻留池走 backend `/health`，服务池路由状态走下面的 §5b。

---

## 5b. 服务池路由观测口径

服务池 / 实例 / 路由状态由两个权威读模型提供（`apps/api/app/services/ml_routing/diagnostics.py`）：

| 端点                                          | 角色                        | 用途                                                                                              |
| --------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------- |
| `GET /admin/ml-integrations/topology`         | PROJECT_ADMIN + SUPER_ADMIN | 服务池 + 成员实例拓扑；角色裁剪（Project Admin 看 `routing_policy="unknown"` 与 `null` 敏感字段） |
| `GET /admin/ml-integrations/runtime-snapshot` | SUPER_ADMIN only            | router mode + per-pool/member `route_inflight` + `circuit_open` + health + freshness 信封         |

**runtime-snapshot 的 freshness 信封**：每个异步来源（`topology` / `router_ledger` / `health` / `gpu` / `residency`）独立带 `updated_at` / `stale` / `error`。单个来源失败不会抹掉其它可信数据，`partial=true` + `partial_reason` 列出哪些来源 stale。`gpu` 与 `residency` 在 runtime-snapshot 中恒为 stale + `not_bundled_in_v0_23_3`——它们需通过 `/observe` 在模型市场 Instance Detail Sheet 单独取。

**metrics 字段当前为 null**：`last_selected_at` / `selection_count_window` / `rejection_count_window` / `p95_ms` / `error_rate` 在 schema 中保留但恒为 `null`。Prometheus 模块 `apps/api/app/services/ml_routing/metrics.py` 已声明 `ml_backend_router_selections_total` / `_rejections_total` / `_ejections_total` / `_routed_request_duration_seconds` / `_inflight`，但当前未 wire（避免引入跨进程共享计数器的复杂度）。模型市场对这些字段统一渲染「暂无路由指标」，不会回落为 `0`。

**route ledger 故障排查**：路由账本走 Redis namespace `ml-router:v1`（独立于 GPU 仲裁 `gpu-arbiter:v1`）。`ML_BACKEND_ROUTER_MODE=off` 时 ledger 不读取；`observe|enforce` 下若 Redis 不可达，`router_ledger` 来源切 stale + `error="redis_unavailable"`，`enforce` 模式 acquire 直接 fail-closed 返回 `ml_backend_router_unavailable` (503)。

**drain / resume 状态机**：成员 `traffic_state` 为 `active` / `draining` / `disabled`。`drain` 只停止接**新** lease，不影响在飞请求或模型权重卸载（后者是 GPU 仲裁的 residency drain，独立）。卸载、移除成员和物理删除 registry 共用同一安全门：router 必须处于 `enforce`，成员必须精确为 `draining`，Redis 账本可读并先清理过期 lease，随后新鲜快照中的 exact `inflight=0` 才构成 quiescent 证明。状态不满足返回 409；账本不可用、缺失或不可信返回 503，不能把未知当作零。

详见 [ADR-0050](../../dev/adr/archive/0050-ml-backend-service-pools-and-request-routing)（路由核心）与 [ADR-0051](../../dev/adr/archive/0051-model-market-observability-information-architecture)（观测面 IA）。

---

## GPU 显存影子决策日志

当全局与资源级 mode 合成为 `observe` 时，API 和各 Celery 派发进程会在发送
predict、交互预测、warmup 或 reload 之前输出 `gpu_arbiter_shadow_decision`。主要字段包括：

- `decision=would-admit|would-evict|would-reject`、`reason`、`operation`；
- 完整 `resource_id`、`allocatable_mb`、`committed_before_mb`、`projected_mb` 和 `shortfall_mb`；
- 仅通过新鲜 residency、managed generation / identity 与空闲安全检查的 `candidates`；
- 保守计费的 `uncertain_backend_ids`、`authoritative=false` 和
  `candidate_order_authoritative=false`。

候选列表只是当前快照中的可行集，不是最终 LRU 顺序。此模式不会调用驱逐、拒绝或排队；
手工 legacy unload 另记 `gpu_arbiter_shadow_unload`，并固定
`releases_allocation=false`。未注册 smoke-test 的直连加载会记
`gpu_arbiter_shadow_unregistered_dispatch`，用来识别无法绑定物理资源的非受管旁路；
未注册 unload 另记 `gpu_arbiter_shadow_unregistered_unload`，不带 `would-*` 决策。

旁路查询超过短超时或数据库不可用时会记 `gpu_arbiter_shadow_observer_failed` 并放行业务请求。
`off` 模式不解析资源配置、不查询影子快照也不产生上述日志。这些日志由各派发进程分别输出，不应把单个 API
进程的内存或日志片段解读为跨进程全局账本。

### GPU 资源账本与修复状态

`GET /api/v1/admin/ml-integrations/gpu-resources` 会同时返回 `rollout_enabled` 和逐资源
`rollout`：PostgreSQL 持久状态、保守 effective/target mode、exact transition/last transition、
revision 与 blocker。`runtime_ready` 只在 desired、持久 `enforcing` 与 Redis ready 同时成立时
才为真。持久状态仍为 `promoting/enforcing/demoting/blocked` 却关闭 release latch，会报
`gpu_rollout_active_while_disabled` blocker，必须先完成安全 demotion。

每个资源的 `runtime` 包含 Redis 账本状态、ready、revision、
证据 deadline、已承诺预算、durable pending/active/retiring 数、allocation 状态数、lease、卡级/backend 级
队列以及 transition。`durable_domain_matches=false` 表示 PostgreSQL 封闭成员域与 Redis 不一致，不能仅凭
Redis 快照继续工作。`prepared` 表示 proof reset 已冻结并可在下一轮恢复；`corrupt/unavailable` 都按
fail-closed 处理。prepared、disabled 或读取失败时，无法原子证明的 child/queue/transition 计数返回 `null`，
不会用 `0` 误报为空。

每轮完整 backend 健康扫描后会输出逐资源 `gpu_arbiter_resource_repair` 结构化日志，包含 action、status、
reason、revision、committed、rollout 结果、GC collection 结果和耗时。release latch 开启后，
desired mode 为 `enforce` 的资源以及任意持久状态尚未收敛为 `off` 的资源都运行该控制面；
beat 消息过期、长于任务 hard limit 的防重入锁和 50 秒批次总时限共同阻止慢任务跨分钟堆积；多卡最多四路
并行，并按波次数量均分 45 秒工作预算，确保固定排序后的每张卡都能在本轮获得执行机会。稳定 ready 且不含
retiring 的卡只读
返回，缺失/损坏/过期账本走严格 proof reset。退役 GC 的常见阻塞包括等待 token horizon、
缺少 challenge 回显、residency 未严格 unloaded、仍有 lease/queue/transition，或 registry 已删除而无法重新
探活。每个 tombstone 由不可复用 `retirement_id` 标识，completion receipt 独立保留七天，并以自身收集结果域
承受数据库 sibling 域的后续演进；proof reset 轮换 incarnation 后则必须用新鲜证明重新收集。冻结的退役 health
只用于诊断，不能授权删除 tombstone。

promotion 结果中的 `controls` 会依次显示 reset 与 enforce-mode 的 `issued/pending/acknowledged`；
`transition_pending=true` 表示正常等待 post-horizon health，不是失败。demotion 先记录
`demotion_waiting_for_runtime_quiescence`，等 lease、两级 queue 和 transition 都清空后才出现
`mode_legacy`；只有全部 Backend 变为 `acknowledged` 才完成 off。

release latch 关闭时，周期 worker 不创建仲裁 Redis client。开启后，纯 `off/observe`
且持久 rollout 已为 `off` 的资源仍返回 `runtime.status=disabled`；若存在未收敛的持久过渡态，
管理查询会保守读取 Redis runtime 供排障。observe 的 shadow 日志仍按上一节工作。worker 结果通过
Celery 结果与进程日志暴露，当前不应把 worker 进程内的普通
Prometheus Gauge 当作 API `/metrics` 可见的跨进程指标。

---

## 6. 关键文件索引

| 主题                              | 路径                                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------------ |
| Metrics 定义                      | `apps/api/app/observability/metrics.py`                                              |
| Raster Mask 活跃量巡检            | `apps/api/app/observability/raster_mask.py`                                          |
| ML backend 指标埋点               | `apps/grounded-sam2-backend/observability.py` · `apps/sam3-backend/observability.py` |
| ML backend http_sd 端点           | `apps/api/app/api/v1/` → `GET /api/v1/internal/metrics-targets`                      |
| ML backend 告警规则               | `infra/prometheus/alerts.yml`                                                        |
| Alertmanager 配置                 | `infra/alertmanager/alertmanager.yml`                                                |
| FastAPI `/metrics` 挂载           | `apps/api/app/main.py:108-130`                                                       |
| Sentry 初始化                     | `apps/api/app/main.py:22-45`                                                         |
| Grafana dashboard JSON            | `infra/grafana/dashboards/anno-overview.json`                                        |
| Grafana provisioning              | `infra/grafana/provisioning/`                                                        |
| Prometheus scrape                 | `infra/prometheus/prometheus.yml`                                                    |
| docker-compose monitoring profile | `docker-compose.yml`                                                                 |
| 超管系统健康聚合端点              | `apps/api/app/api/v1/admin_system_health.py`                                         |
| GPU/ML backend 实时浮窗 (PerfHud) | [concepts/perfhud](/dev/concepts/perfhud)                                            |
