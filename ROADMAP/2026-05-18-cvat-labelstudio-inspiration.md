# CVAT / Label Studio 取经合集（2026-05-18）

> **性质**：研究输入，不是行动清单。
> **来源**：对照阅读 `cvat`（v2.x 主干）+ `label-studio`（OSS 1.x 主干）两份代码，结合当前平台 ROADMAP.md / CHANGELOG.md 的现状摸底。
> **使用方式**：
>
> 1. 先看 §7 "与现 ROADMAP 的映射表"，确认每条**是否已在主 ROADMAP 覆盖** / **需要升级范围** / **本次新增**。
> 2. "新增"或"升级"条目，按颗粒度决定是回流到 `ROADMAP.md` 的 §A/§B/§C，还是单独拆 epic 子文档。
> 3. "避坑清单（§6）"不是 TODO，而是**保持当前正确选择**的备忘，未来 PR review 时引用。
>
> **不要**把这份文档当 sprint backlog 用 —— 它没经过价值/成本评估排序。

---

## 1. 立即可做的小颗粒（每条 ≤ 3 天，可塞进任一 v0.11.x 收尾窗口）

按"工作量 / 风险"从低到高排，每条都不依赖未做的基建。

> **§1.1–1.7 已完成**（v0.10.13 / v0.10.15 / v0.10.16，第 1 波收尾）。落地细节与复盘见各自 CHANGELOG / plan / ADR；后续延伸均已转录到主 [ROADMAP.md](../ROADMAP.md) §A/§B。以下仅留一行登记，详情走 SoT。

- **§1.1 Annotation Guide** ✅ v0.10.13 — 项目级 Markdown 指引 + asset（CodeMirror 6 + 工作台浮层）。SoT: CHANGELOG v0.10.13 / [plan](../../docs/plans/archive/2026-05-18-v0.10.13-annotation-guide.md)；延伸见 ROADMAP §A「Annotation Guide 配套延伸」。
- **§1.2 `reject_reason_type` 结构化枚举** ✅ v0.10.16 — 4 类 enum（`missing/extra/wrong_label/wrong_geometry`）+ RejectReasonModal。SoT: CHANGELOG v0.10.16。
- **§1.3 Webhook `event_version` 信封** ✅ v0.10.16（仅 ADR + Pydantic 占位）— [ADR-0025](../../docs/adr/0025-webhook-event-envelope-versioning.md) Proposed；publisher/delivery 表留 §2.1 epic 实施。
- **§1.4 截图 fixture** — 主 ROADMAP 已有，本文不重复（仅 §7 登记）。
- **§1.5 Predictions Import 端点** ✅ v0.10.15 — `POST /projects/{id}/predictions/import?format=aap_json|coco`，lenient + dry_run + overwrite。SoT: CHANGELOG v0.10.15 / [ADR-0024](../../docs/adr/archive/0024-aap-json-format.md)；延伸见 ROADMAP §A「Predictions Import / AAP JSON 后续延伸」。
- **§1.6 DuckDB 离线分析视图** ✅ v0.10.16 — 3 固定面板 + super_admin 守卫；升级 ClickHouse 触发=`task_events` 单月 > 1000万 或单 query > 10s。SoT: CHANGELOG v0.10.16。
- **§1.7 统一异步任务表 `async_jobs`（MVP）** ✅ v0.10.16 — 双写双轨（async_jobs 汇总索引 + 专表 domain 真值）+ Topbar JobsBell。SoT: CHANGELOG v0.10.16；延伸（kind 注册中心 / WS 进度 / 全 kind cancel）见 ROADMAP §B。

---

## 2. 协议 / 数据模型类（中等颗粒，需 ADR）

### 2.1 Webhook 系统（一等公民）

- **来源**：CVAT [`cvat/apps/webhooks`](../../cvat/cvat/apps/webhooks)。两表分离：`Webhook(scope, events[])` + `WebhookDelivery(status_code, request, response, redelivered_at)`。
- **现 ROADMAP**：§B 治理/合规只有一句「Slack/Webhook 集成」，缺设计。
- **建议**：升级为独立 ADR + epic。
- **核心字段**：

  ```python
  class Webhook:
      id: UUID
      scope: Literal["organization", "project"]
      scope_id: int
      target_url: str
      secret: str  # HMAC sign payload
      events: list[str]  # ["annotation.created", "task.approved", ...]
      enable_ssl: bool
      is_active: bool

  class WebhookDelivery:
      id: UUID
      webhook_id: UUID
      event: str
      payload: JSONB  # 含 event_version (§1.3)
      status_code: int | None
      response_body: str | None
      attempt: int
      next_retry_at: datetime | None
  ```

- **重试策略**：失败指数退避（30s / 5m / 1h / 6h / 24h）共 5 次，全失败标记 `webhook.is_active=false` + 给 owner 发通知。
- **事件清单（首版）**：5 类够用 —— `annotation.created/updated`、`task.assigned/approved/rejected`、`batch.state_changed`、`prediction.completed/failed`、`bug_report.created`。
- **签名头**：`X-Signature-256: sha256=<hmac>`（GitHub 同款），客户端可直接复用 GitHub webhook 验签代码。
- **UI**：ProjectSettings + AdminDashboard 各加「Webhook 管理」section（增删改 + 测试投递 + 最近 20 次 delivery 历史）。

### 2.2 AnnotationFeedback 表（Issue / Comment / BugReport 收敛）◑ 核心已落，剩切单源

> 统一模型本身已落地（[ADR-0027](../../docs/adr/archive/0027-annotation-feedback-unified-table.md)）：`annotation_feedbacks` 表（migration 0076 / v0.10.19）、`v_annotation_feedback_unified` UNION ALL view（0077 / v0.10.21，段二）、bug/comment/reject 三源同事务双写 mirror（`feedback.py mirror_*`）、双写一致性对账安全网（`feedback_reconcile.py` / v0.11.0）均就位。实现的 `kind` / `anchor_type` 枚举与本节原设计基本一致。
>
> **唯一仍 open**：ADR-0027 第三段「切单源 + 旧表退役」（标 v0.11.9+，drift 长期为 0 后才切），在主 ROADMAP 优先级表 I4/I12/I18 行跟踪。

- **来源**：CVAT [`Issue`](../../cvat/cvat/apps/engine/issue.py) + Comment thread，所有反馈都锚到 `(frame, position)`。

### 2.3 Consensus / Replica Jobs（与 GT honeypot 分开做）

- **来源**：CVAT `Task.consensus_replicas` + [`cvat/apps/consensus`](../../cvat/cvat/apps/consensus) 的 intersect_merge。
- **现 ROADMAP**：§C.7 I19 把 GT job / Consensus / IAA 捆成一个 L 体量 epic。
- **建议拆分**：
  - **I19a · Consensus replicas（先做，更容易）**：`task.consensus_n: int = 1`，>1 时调度器自动分给 N 个不同标注员；提交后 IoU > 0.7 自动合并，否则进 reviewer 队列。**不需要"看不见"的伪装机制**。
  - **I19b · GT honeypot（后做，更难）**：从已完成 task 抽 N% 标记为 GT，新任务里偷偷重新分发；标注员视角无法分辨。需要 task 复制 + 标注隐藏 + 评分回灌。
- **价值**：低预算高质量场景（医疗影像 / 法务）通常用 consensus 而非 honeypot —— 业主愿意付双倍标注成本换取共识 ground truth。

### 2.4 Tracker / Auto-Annotation 协议层抽象 ✅ 已完成 v0.10.37（结论已演进）

> 已由 [能力协商 epic](archive/2026-05-22-ml-backend-modality-and-ai-preannotate-redesign.md) 落地，但**结论与原设想不同**：原提议「R23 前置 capabilities 协议、保留 Tracker Registry UI」，实际**取消了注册表 UI**，改为 backend `/setup` 自报能力 + 平台动态发现（`health_meta["capabilities"]` + `ml_capabilities.derive_modalities` 派生 image/video modality），「启停」即 backend 暂停/恢复。CVAT `FunctionKind` enum 未照搬，改用 capabilities 快照 + modality 派生。SoT：视频 roadmap [§3.2/§3.3](2026-05-21-video-workbench-roadmap.md)、[`ml_backend.py`](../apps/api/app/services/ml_backend.py)、CHANGELOG v0.10.37。

- **来源**：CVAT `FunctionKind` enum (`DETECTOR / INTERACTOR / REID / TRACKER`)。

### 2.5 标注规则版本化（Project Fork / Branch）

- **来源**：CVAT 没做，Label Studio 也没做 —— 这是**真空地带**，但客户场景常见。
- **场景**：「类别 schema 改了，老数据按老规则保留，新数据按新规则跑」。
- **现 ROADMAP**：只有"数据集 snapshot"（L1），没有"项目规则 snapshot"。
- **建议**：
  - `projects.config_version: int`（每次 classes_config / attributes_schema 修改 +1）。
  - `annotations.config_version: int`（继承自标注时项目当前版本）。
  - 导出时按 config_version 分组（避免混合规则的标注被错误归到同一类）。
- **优先级**：P3，等第一个客户反馈"我改了类别现在历史数据怎么办" 再做。

### 2.6 平台原生 task JSON 格式（"AAP JSON"） ✅ 已完成 v0.10.15

> AAP JSON v1.0 已作导出格式与 COCO/YOLO/VOC 并列、并作 Predictions Import 推荐格式落地（与 §1.5 同窗口）。关键决策（schema_version 必备、annotations/predictions 双数组分开、导出写满 null/导入 lenient、内部 geometry 格式、task_match display_id 优先）均按本节原设计实现。SoT：[ADR-0024](../../docs/adr/archive/0024-aap-json-format.md)、[schema](../../apps/api/app/schemas/aap_json.py)、CHANGELOG v0.10.15。后续延伸（manifest 打包 / 多 shape / video_track 导入 / annotations import / export_meta.filter）见 [ROADMAP §A AI/模型](../ROADMAP.md#ai--模型)「Predictions Import / AAP JSON 后续延伸」。

---

## 3. 生态扩面类（差异化竞争点）

### 3.1 公开 Python SDK + CLI ✅ 已完成（`packages/python-sdk`）

> 独立包 `ai-annotation-sdk` 与 `aap` CLI / TUI 已落地，覆盖鉴权、项目 / 数据集 / 任务 / 标注 / 预测 / 导出 / 批次 / 成员 / 模型服务与后台任务；OpenAPI 契约测试与能力台账 `api-coverage.toml` 随 SDK 独立发版，文档见 `docs-site/dev/sdk/`。SoT：`packages/python-sdk/` 与 CHANGELOG。仅 PyPI 发布不在仓库内可证。

- **来源**：CVAT [`cvat-sdk`](../../cvat/cvat-sdk) + [`cvat-cli`](../../cvat/cvat-cli)。
- **现 ROADMAP**：L7 "公开 SDK"在 12 月+ 长期规划里。
- **建议升级到 P2**：SDK 是**客户决策物料**——大学实验室 / 初创公司在选标注工具时几乎都会问"有没有 Python SDK"。
- **零成本做法**：
  - FastAPI OpenAPI schema 已完整 → `openapi-python-client` 一键生成 typed client。
  - CLI 用 `typer` 包一层，覆盖 8 个动作：`login / project list/create / dataset push/pull / task list / annotation export / prediction import`。
  - 发到 PyPI（`ai-annotation-sdk` 包名占住）。
  - docs-site 加 `dev/sdk/` 一栏 + 3~5 个 Jupyter notebook 示例。
- **工作量**：3~4d（codegen + CLI + 文档）。
- **强依赖**：API schema 稳定性（当前已经够稳）。

### 3.2 Cloud Storage Sync（pull 模式）

- **来源**：Label Studio `io_storages` + CVAT `CloudStorage`。
- **平台现状**：上传只有 push 模式（用户从浏览器上传）。
- **价值**：客户场景"数据已经在我的 S3 bucket，不想再上传一遍"，**且数据合规上数据不离自家 bucket** 是企业客户硬需求。
- **设计**：
  - 新表 `external_storage_sources(id, project_id, provider, bucket, prefix, credentials_ref, sync_state, last_synced_at)`。
  - Celery beat 周期 list bucket → 差量同步：新增文件创 task，已删除文件软删 task。
  - 复用现有 StorageService 抽象。
- **工作量**：4~5d（S3 first，GCS/Azure 留扩展点）。
- **触发**：第一个企业客户提需求即做。

### 3.3 Datumaro 集成（中间格式 / 导入导出中转层）

- **来源**：CVAT 用 [`datumaro`](https://github.com/openvinotoolkit/datumaro) 做 25+ 格式适配的中转层。
- **现 ROADMAP**：导出格式靠自己实现 COCO/YOLO/VOC 三件套。
- **建议**：**不要自己加格式**。客户要 KITTI / MOT / DAVIS / Cityscapes / LabelMe 时，走"先导 COCO/JSON → 用 datumaro CLI 转"路径，文档里附 datumaro 命令模板。
- **进阶**：把 datumaro 装进 `apps/api` 作可选依赖，加 `POST /export?format=datumaro:kitti` 透传。
- **配合 §2.6**：转换链 `AAP JSON ↔ Datumaro internal ↔ { COCO, YOLO, VOC, KITTI, MOT, ... }`，AAP JSON 是 datumaro 链的原生输入源（无损），COCO/YOLO/VOC 是 datumaro 链的有损投影。
- **价值**：用 30 行代码换 20+ 格式支持，**不增加维护负担**。

### 3.4 Tool Plugin Registration（L7 公开 SDK 的具体接口形态）

- **来源**：CVAT Lambda + Nuclio（不要学，太重）；建议自创"webhook-only plugin"轻量形态。
- **设计**：
  ```json
  {
    "id": "custom-polygon-snap",
    "label": "智能贴边",
    "hotkey": "Alt+S",
    "applies_to": ["polygon"],
    "endpoint": "https://my-tool.example.com/snap",
    "input_schema": { "geometry": "polygon", "image_url": "string" },
    "output_schema": { "geometry": "polygon" }
  }
  ```
- **平台只做**：注册 manifest（JSON 粘贴）+ 工作台按 manifest 在工具栏加按钮 + 调用时发签名 POST → 拿结果落地标注。
- **客户负担**：自己 host HTTP 端点，自己升级。
- **价值**：开放生态又不背安全锅；和长期 L7 SDK 协同。
- **优先级**：P3，等第一个客户问"能不能加自定义工具" 再做。

---

## 4. 分析与观测类

### 4.1 Annotator Performance Dashboard ✅ 已完成

> 个人页 `/me/performance`、管理端 `/admin/people` 与项目成员绩效看板已落地，覆盖人均吞吐、耗时分布（p50/p95）、reject 原因与类别覆盖、首审质量与 CSV 导出；IAA 维度随共识能力另立。SoT：`apps/web/src/pages/Me/MyPerformancePage.tsx`、`apps/api/app/api/v1/dashboard/annotator.py`、CHANGELOG。

- **数据源**：`task_events.duration_ms` + `mv_user_perf_daily` 物化视图 + §1.6 DuckDB 视图。
- **已交付**：管理端 `/admin/people` 与项目成员绩效看板（人均吞吐 / 耗时分布 p50·p95 / reject 原因 / 类别覆盖 / 首审质量 / CSV 导出），以及个人页 `/me/performance`（趋势对标团队）。
- **仍缺**：IAA 维度，随共识能力另立。
- **价值**：**付费决策项**。项目管理客户能据此发奖金 / 排红线，对标注员能据此自我改进。CVAT 有简版，LS Enterprise 才完整 —— **可做差异化**。

### 4.2 请求级 Trace ID（轻量 OpenTelemetry）

- **现状**：API / Celery / ML backend 三段日志没串起来。
- **建议**：
  - API 入口生成 `X-Trace-Id` UUID，贯穿 Celery task header + ML backend `/predict` header。
  - 三段日志都打 trace_id。
  - **暂不上 OTel collector**（运维负担太重）；先满足"按 trace_id grep 全链路日志"即可。
- **工作量**：1.5d。
- **升级路径**：未来需要分布式追踪时换 OTel exporter，trace_id 兼容。

### 4.3 ClickHouse 升级触发条件

- **观察项，不做**。
- 触发条件：`task_events` 单月行数 > 1000万 或 DuckDB 查询 > 10s。
- 之前先用 §1.6 DuckDB 顶。

---

## 5. LLM 时代差异化

### 5.1 LLM-as-Judge / Prompts 模块

- **来源**：Label Studio 新加的 [Prompts](https://github.com/HumanSignal/label-studio) 模块；Adala 项目。
- **场景**：
  - **Reject 原因建议**：reviewer reject 时 LLM 看一眼标注 + 图，给一个 `reject_reason_type` 候选 + 文字理由初稿。
  - **类别澄清助手**：标注员对某 task 不确定时，调 VLM 拿"这张图里 X 在哪 / 是不是 Y"的辅助回答。
  - **BugReport 聚类去重**：§B 已规划，本质是同一类 LLM 接入。
- **平台优势**：`PredictionMeta(prompt_tokens, completion_tokens, cost)` 已落，**成本追踪基建就绪**。
- **建议**：先做"reject 原因建议"作 MVP（与 §1.2 reject_reason_type 同窗口），LLM SDK 接入收口一次。
- **工作量**：MVP 2d（LLM SDK + 一个固定 prompt + reviewer UI 集成）。
- **优先级**：P2，因为这是**对标 LS Prompts 的关键差异化点**，做了能进 demo 视频。

### 5.2 Adala-style 主动学习闭环（MVP 形态）

- **来源**：Label Studio + Adala 项目的 agent loop。
- **现 ROADMAP**：L1/L2 主动学习闭环太抽象，没拆触发条件。
- **建议 MVP**：闭环打通**比效果调优更重要**。
  ```
  unlabeled pool
   → 调 ML backend 预测
   → uncertainty 排序写入 next_task 优先级
   → 标注员领取
   → 提交后入"已标注 pool"
   → 触发 webhook 给外部训练系统（Vertex / Sagemaker / 客户自托管）
   → 外部训练完成回调 ML backend 新版本注册
   → 回到第 1 步
  ```
- **平台只做前后两端**：webhook out + ml_backend 版本注册；**训练系统不内置**（避免和 §A "训练队列"占位绑死）。
- **工作量**：3d，前提是 §2.1 Webhook 已落。
- **价值**：能讲完整 "AI 辅助标注 → 模型迭代" 故事，对学术/AI lab 客户尤其。

---

## 6. 避坑清单（保持当前选择，不要走回头路）

这一节**不是 TODO**，而是 PR review 时的参考底线。

| 主题                   | CVAT / LS 的坑                                                                                          | 平台当前正确选择                             | 何时检查                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------ |
| Job 状态字段           | CVAT `Job.status`/`stage`/`state` 三字段并存（[models.py:1165](../../cvat/cvat/apps/engine/models.py)） | 单 status enum                               | 加新状态前看一眼是否能用现有 enum 表达                             |
| Label Config           | LS XML DSL 难维护、难校验                                                                               | JSONB `classes_config` + `attributes_schema` | 永远不要回退到 DSL，要灵活就扩 JSONB schema                        |
| Task 双重含义          | LS task 既是标注题目也是后台 job                                                                        | 题目 / Celery 分离                           | §1.7 async_jobs 落地后强化                                         |
| Django app 碎片        | LS 24+ apps 跨 module 依赖混乱                                                                          | apps/api 单仓                                | 不要因为"模块化"动机拆出新 apps/\*                                 |
| Enterprise vs OSS 分叉 | LS `if settings.LABEL_STUDIO_EE` 满地                                                                   | 单分支无功能开关                             | 商业化前不要拆 OSS/EE，灰度走 feature_flags                        |
| 格式适配膨胀           | CVAT 维护 25+ 格式适配                                                                                  | COCO/YOLO/VOC 三件套                         | 客户要新格式走 §3.3 datumaro，**不自己加**                         |
| 权限引擎               | CVAT Rego/OPA 学习成本高                                                                                | 单 RBAC 中间件                               | 不引入 policy engine；权限复杂化时先看是否能在 RBAC 内表达         |
| AI backend 部署        | CVAT Nuclio 运维复杂                                                                                    | HTTP `/predict` 协议 + 独立容器（ADR-0012）  | 保持；§3.4 plugin 也走 HTTP                                        |
| Skeleton 递归嵌套      | CVAT sublabel 无限递归实际很少用                                                                        | 当前无 skeleton 实现                         | §C.7 I10 实现时**只支持 2 层**（label + sublabel），不开放任意嵌套 |
| Consensus 合并规则     | CVAT 用 Rego 配置                                                                                       | 当前未实现                                   | §2.3 I19a 实现时用固定算法 + 阈值参数，不引入策略 DSL              |

---

## 7. 与现 ROADMAP 的映射表

| 本文档条目                   | 现 ROADMAP 状态                                                   | 建议动作                                                                                            |
| ---------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| §1.1 Annotation Guide        | ✅ **已完成 v0.10.13**（2026-05-18）                              | 配套延伸条目已转录到 ROADMAP §A 项目模块                                                            |
| §1.2 reject_reason_type      | ✅ **已完成 v0.10.16**（2026-05-19）                              | 4 类 enum + RejectReasonModal 改造 + DuckDB 面板联动                                                |
| §1.3 webhook event_version   | ✅ **已完成 v0.10.16**（2026-05-19，仅 ADR 草案 + Pydantic 占位） | ADR-0025 Proposed，§2.1 epic 实施时按此 ADR 落地                                                    |
| §1.4 截图 fixture            | 已在 §A 后续观察项                                                | 不动                                                                                                |
| §1.5 Predictions Import      | ✅ **已完成 v0.10.15**（2026-05-19）                              | 后续延伸条目已转录到 ROADMAP §A "Predictions Import / AAP JSON 后续延伸"                            |
| §1.6 DuckDB 离线视图         | ✅ **已完成 v0.10.16**（2026-05-19）                              | 三面板 + super_admin 守卫 + 升级路径 PG → DuckDB → ClickHouse 待触发                                |
| §1.7 async_jobs 统一表       | ✅ **已完成 v0.10.16**（2026-05-19）                              | 双写双轨 + Topbar 铃铛 + 4 kind 接入；cancel 全 kind / WebSocket 进度推送留 v0.10.17                |
| §2.1 Webhook 系统            | §B 治理/合规 有 1 句话                                            | **升级范围**：拆独立 epic + ADR-0018 草案                                                           |
| §2.2 AnnotationFeedback 收敛 | ◑ **核心已落 v0.10.19–v0.11.0**                                   | 统一表 + view + 双写 + 对账就位；剩 ADR-0027 段三切单源（v0.11.9+，主 ROADMAP I18 跟踪）            |
| §2.3 Consensus / GT 拆分     | §C.7 I19 是 L 体量打包                                            | **升级范围**：建议 I19a/I19b 拆分                                                                   |
| §2.4 Tracker 协议层          | ✅ **已完成 v0.10.37**（能力协商 epic）                           | 结论演进：取消 Tracker Registry UI，改 `/setup` 自报 + 动态发现；详见 §2.4 与视频 roadmap §3.2/§3.3 |
| §2.5 项目规则版本化          | **新增**                                                          | 回流到 §A "数据 & 存储" 或长期规划                                                                  |
| §2.6 平台原生 AAP JSON       | ✅ **已完成 v0.10.15**（2026-05-19，与 §1.5 同窗口）              | 后续延伸条目已转录到 ROADMAP §A "Predictions Import / AAP JSON 后续延伸"                            |
| §3.1 公开 SDK + CLI          | ✅ **已完成**（`packages/python-sdk`）                            | 独立包 `ai-annotation-sdk` + `aap` CLI / TUI + `docs-site/dev/sdk/`；仅 PyPI 发布不在仓库内可证     |
| §3.2 Cloud Storage Sync      | **新增**                                                          | 回流到 §A "数据 & 存储"，触发=企业客户需求                                                          |
| §3.3 Datumaro 集成           | **新增**                                                          | 写入 §A "导出" 子节（如有），或新增                                                                 |
| §3.4 Tool Plugin 注册式      | 长期规划 L7                                                       | **细化**：作 L7 的具体接口形态备忘                                                                  |
| §4.1 Annotator Dashboard     | ✅ **已完成**                                                     | 个人页 `/me/performance` + 管理端 `/admin/people` + 项目成员绩效；IAA 维度随共识能力另立            |
| §4.2 Trace ID                | **新增**                                                          | 回流到 §B "测试 / 开发体验"                                                                         |
| §4.3 ClickHouse 升级         | **新增**                                                          | 回流到 "等业务规模触发" 区块                                                                        |
| §5.1 LLM-as-Judge            | §B Bug 反馈 LLM 聚类是同源                                        | **升级范围**：扩为 LLM SDK 接入统一窗口（聚类 / reject 建议 / 类别澄清）                            |
| §5.2 主动学习闭环 MVP        | 长期规划 L1/L2 抽象                                               | **细化**：作 L1/L2 的 MVP 形态备忘，依赖 §2.1 Webhook                                               |
| §6 避坑清单                  | 散落于各处                                                        | **新增**：在 ROADMAP.md 末尾或单独维护"决策底线"备忘                                                |

---

## 8. 建议的回流节奏

不一次性全部回流，按下面三波推进，每波收尾后再评估下一波：

**第 1 波（v0.11 内开工）✅ 全部收尾 v0.10.13 / v0.10.15 / v0.10.16**：~~§1.1 Annotation Guide~~ ✅ v0.10.13 / ~~§1.2 reject_reason_type~~ ✅ v0.10.16 / ~~§1.5 Predictions Import~~ ✅ v0.10.15（与 §2.6 同窗口）/ ~~§1.7 async_jobs~~ ✅ v0.10.16（与 §1.3 / §1.6 同窗口）。
**特征**：低风险、低工作量、高可见价值；落地后给后续大项铺基建。第 1 波在 3 个版本内全部收尾，进入第 2 波。

**第 2 波（v0.11 中后期）**：§2.1 Webhook 系统 / §2.2 AnnotationFeedback 收敛 / ~~§3.1 公开 SDK~~ ✅ 已完成。
**特征**：协议层 + 生态扩面；做完平台从"内部工具"升级为"可对外集成的产品"。

**第 3 波（v0.12+）**：§2.3 Consensus / §4.1 Annotator Dashboard / §5.1 LLM-as-Judge。
**特征**：差异化竞争点；前提是前两波铺好基建。

> **注**：以上节奏未做价值/成本量化评估，只是基于依赖关系的合理顺序。具体排期以客户反馈和业务优先级为准。
