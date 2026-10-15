# ML Backend 能力协商 + AI 预标注模态化重设计（epic）

> **性质**：架构 epic，按阶段推进，**不绑版本号**（落地时按当时主线切片）。
> **缘起（2026-05-22）**：v0.10.35/36 接通真实视频 tracker 后，发现平台始终没把「模态 / 能力」当成一等维度——注册、绑定、预标注三处都是模态盲的。这三件事是同一问题的三个切面。
> **关联**：视频 roadmap [Phase 3.3 协议统一收口（I20.4）](../2026-05-21-video-workbench-roadmap.md)、[ADR-0012](../../docs/adr/archive/0012-sam-backend-as-independent-gpu-service.md)、[ADR-0020 能力协商](../../docs/adr/archive/0020-ml-backend-capability-negotiation.md)、主 [ROADMAP.md](../../ROADMAP.md) §A「项目多后端绑定 + 按后端参数面板」。

---

## 0. 核心问题（一句话）

**平台对 ML Backend 的「能力」和「模态」无持久化感知**：

| 现状 | 代码位置 |
|---|---|
| `MLBackend` 表无 modality / data_type / 能力字段 | 历史 `ml_backend.py`；当前[注册表模型](../../apps/api/app/db/models/ml_backend_registry.py) |
| `is_interactive` 是注册表单**手填**布尔，**不从 `/setup.is_interactive` 派生** → 双真相 | 历史 `MlBackendFormModal.tsx:47`；当前[服务注册表单](../../apps/web/src/pages/ModelMarket/GlobalBackendFormModal.tsx) |
| `/setup` 的 `supported_prompts` / `supported_trackers` / `params` **平台从不入库**，只在工作台临时拉来置灰工具 | [ml_backends.py:285](../../apps/api/app/api/v1/ml_backends.py) |
| `Project.ml_backend_id` 单值；绑定时**不校验** `Project.data_type` 与 backend 能力匹配 | [projects.py:361](../../apps/api/app/api/v1/projects.py) |
| `/ai-pre` 仅图像（`PredictionJob`、单 backend、单 prompt/阈值）；视频走完全独立的另一套（`VideoTrackerJob` + 工作台 Shift+T + ModelMarket 监控页） | [ProjectDetailPanel.tsx（当前路径）](../../apps/web/src/pages/AIPreAnnotate/components/ProjectDetailPanel.tsx) |

**已先行修复**（2026-05-22）：`/ai-pre` summary 加 `data_type=image` 过滤，挡住视频项目误入图像文本预标页（[admin_preannotate.py:329](../../apps/api/app/api/v1/admin_preannotate.py)）。这是症状止血，根因仍待本 epic。

**遵守的决策底线**（见 [ROADMAP.md](../../ROADMAP.md) 决策底线表）：
- AI backend 保持 HTTP `/predict` 协议 + 独立容器，不自管 serverless。
- 不为「灵活性」回退 DSL；能力靠 `/setup` 自描述 + JSONB 持久化。
- 能由现有数据派生的状态**不新增手填字段**（modality / is_interactive 都应派生而非手填）。
- `annotations[]` / `predictions[]` 双数组分开；两套 job 模型不强行合表。

---

## 阶段 1 · 能力协商落库 + 模态派生（前置基石）（v0.10.37 落地）

> 已落地，详见 [CHANGELOG v0.10.37](../../CHANGELOG.md) / [v0.10.37 计划](../../docs/plans/archive/2026-05-22-v0.10.37-ml-backend-capability-persistence.md)。后两阶段的设计前提（保留供参考）：
>
> - `check_health` 探 `/setup` 把能力快照（`supported_prompts`/`supported_trackers`/`supported_text_outputs`/`supported_geometric_outputs` + 派生 `modalities`）落进 `health_meta["capabilities"]`（无迁移）；`services/ml_capabilities.derive_modalities` 是模态派生的单一真值（`supported_prompts⇒image`、`supported_trackers⇒video`）。
> - `is_interactive` 改派生对账（以 backend `/setup` 自报为真值，注册表单不再手填）。
> - `PATCH /projects/{id}` 绑定按 `data_type` 校验（不兼容 422、探测失败 fail-open）；`Step4Ai` wizard 按模态标注。
>
> **后两阶段直接消费 `health_meta["capabilities"]` 与 `derive_modalities`，不必再实时拉 `/setup`。**

---

## 阶段 2 · AI 预标注模态化重设计（多 backend × 多数据类型）（v0.10.38 落地）

> 已落地，详见 [CHANGELOG v0.10.38](../../CHANGELOG.md) / [v0.10.38 计划](../../docs/plans/archive/2026-05-22-v0.10.38-ai-preannotate-modality-redesign.md)。落地要点：
>
> - **模态感知路由**：`/ai-pre` 撤回 v0.10.36 的 image-only 止血过滤，前端 `ProjectDetailPanel` 按 `data_type` 分流（image=文本批量预标 / video=工作台逐轨迹追踪引导卡片 / lidar=占位）。
> - **多 backend 选择 + 按后端参数面板**：基于已有 1:N 注册 + 请求显式 `ml_backend_id`（**未动单值 schema**）；执行页加 backend 选择器 + 复用 `SchemaForm` 按 `/setup.params` 渲染，值按 backend 记忆（`params_by_backend`），随请求 `params` 透传，worker 合并进 `/predict` context。项目级阈值收口为按后端动态（项目默认仍在 GeneralSection）。
> - **统一 job 视图层（未合表）**：纯前端展示层统一——`/ai-pre/jobs` 加模态 tab；两套 job 模型保留。
>
> **延后（按客户驱动）**：精细单 batch 多模型对比 modal（复用 v0.9.x orphan `PreannotateStepper`）；底层 `async_jobs` 统一表收敛（见 [取经合集 §1.7](../2026-05-18-cvat-labelstudio-inspiration.md)）。

---

## 阶段 3 · video-jobs 并入统一 job 历史（v0.10.38 落地）

> 已落地（与阶段 2 同版）：视频追踪监控从 ModelMarket（`/model-market/video-jobs`）迁入 `/ai-pre/jobs` 的「视频」模态 tab，复用 `GET /video-tracker-jobs` + 重构后的 `VideoTrackerJobsPanel`；旧路由 301 跳转。**边界守住**：ModelMarket 只留后端 / 显存池健康观测，ai-pre 收任务 job 历史。

---

## 依赖顺序

```
阶段 1 能力协商落库 + 模态派生   ← 前置（v0.10.37 落地）
        ↓
阶段 2 ai-pre 模态化重设计       ← v0.10.38 落地
        ↓
阶段 3 video-jobs 并入 ai-pre/jobs ← v0.10.38 落地
```

> **本 epic 三阶段已全部落地（v0.10.37 + v0.10.38）。** 剩余长期项（精细对比 modal、async_jobs 合表）按客户驱动另行排期。
