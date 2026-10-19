---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# PerfHud — GPU / ML Backend 实时监控浮窗

> 参考 [ComfyUI-Crystools](https://github.com/crystian/ComfyUI-Crystools) 的“后端 `pynvml`/`psutil` 推送 → 前端趋势图”架构，而非仅在浏览器侧采集。

## 用途

Grounded-SAM-2、SAM 3、YOLO、ONNXTools 与 RapidOCR 等 ML Backend 运行预标注或交互推理时，开发与运维人员需要实时观察 GPU 利用率、显存（VRAM）、温度、功耗以及容器 CPU / 内存占用，以便快速定位 OOM 或推理卡顿。常规 `health_meta` 为低频缓存快照，不适合观察秒级实时抖动。

## 触发与权限

- **快捷键**：`Ctrl+Shift+P`（全局快捷键，在 `apps/web/src/App.tsx` 注册）
- **TopBar 入口**：头像左侧 Activity 图标按钮
- **权限边界**：仅 `super_admin` 与 `project_admin` 可见（`annotator` / `reviewer` / `viewer` 角色既不渲染切换按钮也不建立 WebSocket 订阅）

## 数据流

```text
┌────────────────────────────────────────┐
│ ML Backend 容器（5 类第一方推理服务）  │
│  GET /health 端点                      │
│   ├─ pynvml: util / temp / power       │
│   ├─ psutil: cpu_percent / mem         │
│   └─ torch.cuda.mem_get_info: VRAM     │
└────────────┬───────────────────────────┘
             │ HTTP /health
             ▼
┌────────────────────────────────────────┐
│ apps/api Celery beat                   │
│  publish_ml_backend_stats (每 1s)      │
│   1. 读 ml-backend-stats:subscribers   │
│   2. 0 订阅者 → skip（零空闲开销）     │
│   3. >0 → 拉活跃 backend 的 /health    │
│   4. publish ml-backend-stats:global   │
└────────────┬───────────────────────────┘
             │ Redis Pub/Sub
             ▼
┌────────────────────────────────────────┐
│ apps/api WS /ws/ml-backend-stats       │
│  - 仅限 super_admin / project_admin    │
│  - 连接建立 INCR 订阅计数              │
│  - 连接断开 DECR 订阅计数              │
└────────────┬───────────────────────────┘
             │ WS frame (1s 粒度)
             ▼
┌────────────────────────────────────────┐
│ apps/web/src/components/PerfHud/       │
│  useMLBackendStats hook                │
│   - 60s ring buffer (4 项核心指标)     │
│   - 仅在浮窗展开时建立 WebSocket 连接  │
│  PerfHud.tsx                           │
│   - 浮动面板 + 实例切换下拉框          │
│   - 4 条进度条 + SVG Sparkline 曲线    │
└────────────────────────────────────────┘
```

## 关键文件

| 层级                  | 路径                                                              | 核心职责                                                                         |
| :-------------------- | :---------------------------------------------------------------- | :------------------------------------------------------------------------------- |
| **ML Backend**        | `apps/{grounded-sam2,sam3,yolo,onnxtools,rapidocr}-backend/`      | `/health` 端点调用 `sample_perfhud()`，在顶层返回 `gpu_info` 与 `host` 段        |
| **API Schema**        | `apps/api/app/schemas/ml_backend.py`                              | 定义 `GpuInfo`、`HostInfo`、`CacheStats`、`HealthMeta`、`MLBackendStatsSnapshot` |
| **API Client**        | `apps/api/app/services/ml_client.py`                              | `health_meta()` 抓取并透传 `gpu_info` 与 `host` 段                               |
| **API WS**            | `apps/api/app/api/v1/ws.py`                                       | `/ws/ml-backend-stats` 鉴权与 Redis 订阅者引用计数                               |
| **API Worker & Beat** | `apps/api/app/workers/ml_health.py`、`celery_app.py`              | `publish_ml_backend_stats` 任务与 1s 周期调度                                    |
| **前端组件**          | `apps/web/src/components/PerfHud/`、`components/shell/TopBar.tsx` | 浮窗渲染、60s 环形缓冲区与实例选择切换                                           |

> **新接入 Backend 须遵守同一 `/health` 观测契约**：将 GPU 与容器指标放入响应顶层的 `gpu_info` 与 `host` 字段（字段定义见 `_PERFHUD_META_KEYS`），否则 `publish_ml_backend_stats` 无法提取实时指标。

## 性能开销与设计边界

- **零无人订阅开销**：1s 粒度的轮询仅在 WebSocket 订阅者数量 `> 0` 时触发；无人打开浮窗时完全跳过探活。
- **多人共享广播**：Celery Beat 单实例每秒采集一次并经 Redis Pub/Sub 广播给所有订阅客户端，不会随观察人数增加而放大后端探测压力。
- **轻量前端渲染**：关闭浮窗即断开 WebSocket；环形缓冲区固定保留最近 60 帧，使用原生 SVG `Sparkline.tsx` 绘制，无额外图表库开销。长期历史趋势请使用 Prometheus + Grafana 看板。

## 相关文档

- 上游协议：[ML Backend 协议](../reference/ml-backend-protocol)
- 监控与告警：[可观测性总览](/ops/observability/)
- 模型集成架构：[AI 模型集成](./ai-models)
