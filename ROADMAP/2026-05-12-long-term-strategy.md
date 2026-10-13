# 长期规划（12 个月以外的方向）

> 状态：**strategic（2026-05-12）**。这份文件不包含具体 sprint 任务，而是「方向 + 触发条件 + 关联 epic」清单，用于回答"做完手头这一批之后还能做什么"。
>
> 文件性质：**对未来 1-3 年可能落地的能力做提前盘点**。每一项都标注：触发条件、预估体量、是否阻塞当前发布。**当前 P0/P1 epic 完成前不开工**。
>
> 阅读顺序：先看 §0 总览矩阵，再按兴趣方向跳读。

---

## 0. 总览矩阵

| 方向                                 | 体量 | 触发条件                               | 当前可起步动作                 |
| ------------------------------------ | ---- | -------------------------------------- | ------------------------------ |
| L1 数据中台与版本治理                | XL   | 客户提"复现训练" / 多次重训            | 数据集 snapshot 协议草案       |
| L2 主动学习与训练闭环                | XL   | 累计 ≥3 个客户在外部做 retrain         | 训练队列占位接入 ML 协议       |
| L3 模型评估与基准                    | L    | 第二个 backend 上线（即 v0.10.x sam3） | 评测集表结构设计               |
| L4 跨模态标注（音频/文本/3D）        | XL   | 客户拿到非图像/视频任务                | 不动手                         |
| L5 协同与众包                        | L    | 单项目并发标注 ≥10 人                  | R11 segment 模型先到位         |
| L6 插件 / 扩展机制                   | L    | 第三方想加自有 ML backend / 工具       | 当前 backend 协议已是雏形      |
| L7 公开 API / SDK                    | M    | 第一个客户写自动化集成                 | OpenAPI codegen 已就位         |
| L8 安全合规认证（SOC2 / ISO / GDPR） | XL   | 进入欧美企业销售                       | 审计日志已就位（v0.8.1）       |
| L9 移动端 / Web 平板适配             | M    | 户外标注 / 现场审核需求                | 暂缓                           |
| L10 端侧推理 / 离线模式              | L    | 客户内网部署 + GPU 节点受限            | 现有 ML 协议已支持私有部署     |
| L11 合成数据 / Active Generation     | XL   | 标注成本 > 训练数据生成成本            | 调研期                         |
| L12 商业化（多租户 SaaS / 计费）     | XL   | 决定走 SaaS 路线                       | 当前是单租户私有部署           |
| L13 可观测性与 SRE 成熟度            | M    | DAU ≥ 50 或部署 ≥ 5 实例               | Prometheus 基线已到位          |
| L14 国际化 / 本土化                  | M    | 第一个非中文客户                       | i18n 框架已列 §B（ROADMAP.md） |
| L15 标注质量 AI 审计                 | L    | 标注员规模 ≥ 20，IAA 需求显化          | predictions 表已就位           |

> 体量：S（≤2 周）/ M（1-2 月）/ L（1 季度）/ XL（≥半年，含跨团队）

---

## L1 · 数据中台与版本治理

**问题**：当前一份数据集被多个项目消费，标注变更后无法回溯到某次训练用的是哪个"快照"。客户做模型对比时常常对不上账。

**长期目标**：把"数据集 + 标注 + schema"打包成不可变快照，可被外部训练流水线引用、可复现、可审计。

**子方向**：

- **L1.1 Snapshot**：原子化的 `DatasetSnapshot(id, dataset_id, items, annotations, schema_version, created_at, checksum)`，导出后 hash 锁定。已列 ROADMAP §A「数据集版本（snapshot）」。
- **L1.2 Lineage**：标注的来源链 `(human | model_v1 | model_v2 | active_learning_round_3)` 与版本号绑定，回放任意时点的标注状态。
- **L1.3 Diff / Compare**：两份 snapshot 之间的标注 diff（新增 / 删除 / 修改），可视化展示。
- **L1.4 Schema 演化**：类别表、属性 schema 改动后旧 snapshot 数据自动归类（向后兼容矩阵）。
- **L1.5 外部消费**：暴露 `s3://snapshots/{id}/manifest.parquet`（Iceberg / Delta 格式选型留 ADR），可被 PyTorch / HuggingFace `datasets` 直接 load。

**触发条件**：客户提出"上次训练用的是哪份数据"超过 2 次。
**关联**：[`ROADMAP.md`](../ROADMAP.md) §A 数据 & 存储；后续可独立 ADR + epic。

---

## L2 · 主动学习与训练闭环

**问题**：标注员浪费时间标"模型已经会的"，关键困难样本反而没标。

**长期目标**：把"标注 → 训练 → 评估 → 选困难样本 → 再标注"做成闭环，平台主动推荐"最值得标"的样本。

**子方向**：

- **L2.1 训练队列后端**：`/training` 路由已占位。接入第一个 trainer（YOLO / DETR / Mask2Former）做闭环 demo。
- **L2.2 Uncertainty 排序**：基于 prediction 的 entropy / margin / variance 选样本，扩展 `U` 键准确度（已列 ROADMAP §C.3）。
- **L2.3 主动学习策略可插拔**：策略 registry（`uncertainty | diversity | core-set | bald`），让客户按场景切换。
- **L2.4 自动 retrain trigger**：标注量到阈值 / 时间到点 / 评估指标下降时自动起新一轮训练。
- **L2.5 模型注册表**：训练产物（weights / metrics / training-set snapshot）落地 `ModelRegistry`，可与 v0.10.x 的 ml_backends 表打通。

**触发条件**：第二个客户提"训练侧自动化"。
**关联**：依赖 L1（snapshot），与 R10（前端 AI tracker）共享推理基础设施。

---

## L3 · 模型评估与基准

**问题**：v0.10.x 即将有 sam3 / grounded-sam2 并存，但"哪个更好用"没有客观数据。

**长期目标**：内置评测集（gold set）+ 自动跑分 + 历史趋势图。

**子方向**：

- **L3.1 Gold Set 管理**：标注完成后人工 review 出"标准答案"集合，与 dataset 解耦。
- **L3.2 自动评估 worker**：模型上线 / 升级时自动在 gold set 上跑，输出 mAP / IoU / 类别混淆矩阵。
- **L3.3 AB 对比 UI**：已在 v0.10.x ROADMAP，但当前只对比单次预测；本目标是"长期趋势对比"。
- **L3.4 标注员 vs 模型 vs 共识** 三向对比：识别系统性偏差（特定类别上人比模型差 / 反之）。

**触发条件**：v0.10.x M0-M3 sam3-backend 上线后。
**关联**：v0.10.x SAM 3 ROADMAP / L2 训练闭环。

---

## L4 · 跨模态标注

**问题**：当前仅 image-det / video-track / image-seg（partial），客户可能拿到音频转写、文本 NER、3D 点云、多模态对话等任务。

**长期目标**：基于 `WorkbenchStageHost` 的 stage 抽象，追加新模态 stage。

**子方向**：

- **L4.1 3D / LiDAR** ✅ 已落地（`ThreeDWorkbench`、点云质检与多相机成员；跨厂商 GPU 资格与 polygon / mask 成员仍开放）：`ThreeDWorkbench` 占位已就位（lidar），需要 Three.js / babylon.js / OpenSeadragon3D 选型 + 点云 viewer + cuboid 编辑。**等真实客户**。
- **L4.2 音频 / 语音转写**：waveform viewer + 时间区间标注 + ASR 模型集成。借鉴视频 R4 时间轴。
- **L4.3 文本 NER / 情感**：纯前端 token span 标注，可走 Label Studio 风格 UI。
- **L4.4 多模态对话评估**：HRLF / RLHF 偏好排序界面（A vs B 投票），与现有标注流不冲突，新独立 stage。
- **L4.5 文档版面 / OCR**：扫描件 + 多区域 + 转写。可与图片 viewport（R8）共享 pan/zoom。

**触发条件**：每个子方向独立触发，按客户优先级排序。
**关联**：`ROADMAP.md` §C.4 工作台架构分层（已为多模态留接口）。

---

## L5 · 协同与众包

**问题**：当前单项目并发上限受 Celery / WS 影响；超过 ~20 人并行时压力测试缺失。

**长期目标**：从"一个项目 1-5 人"扩展到"一个项目 50-200 人"，包括众包外发场景。

**子方向**：

- **L5.1 Segment 级协同** ✅ 已落地（视频 roadmap Phase 5：segment fragment、claim / lease 与分段提交）：见 `archive/2026-05-12-video-backend-frame-service.md` B4。
- **L5.2 任务自动分发策略**：按熟练度 / 历史质量 / 类别经验自动分配（已部分由 batch scheduler 实现，需要细化）。
- **L5.3 共识合并算法**：多人对同一 task 的标注，取交集 / 投票 / 加权融合（已列 §A 批次延伸）。
- **L5.4 众包平台对接**：MTurk / Scale 外发，结果回流到本平台 review 流。
- **L5.5 实时聊天 / 评论**：标注员在 task 上下文里讨论，超出单纯审核评论。
- **L5.6 标注员激励 / 排行榜** ✅ 个人 / 团队绩效看板已落地：可选模块，按工时 / 准确率 / 速度统计。

**触发条件**：单项目并发标注员 ≥10。
**关联**：R11（前端 segment UI）、B4（后端 segment 模型）。

---

## L6 · 插件 / 扩展机制

**问题**：现有 backend 协议是隐式约定（HTTP + 字段集合），第三方接入需要看代码。

**长期目标**：把 ML backend / 工具 / 导出格式都做成"插件"，可第三方编写。

**子方向**：

- **L6.1 ML backend marketplace**：注册 backend 时填 manifest（输入 / 输出 schema、健康检查 endpoint、显存需求），平台校验。
- **L6.2 自定义导出格式**：当前 COCO / YOLO / VOC 写死在后端；改为 `ExportPlugin` 接口，可写 Python 自定义。
- **L6.3 自定义工具**：标注侧加"自定义形状"（如六边形 / 三维 cuboid），通过前端 plugin slot 渲染。
- **L6.4 Webhook 扩展**：任务完成 / 批次状态变更 / 质检失败可触发 webhook，对接客户自有系统（如内部 PM）。

**触发条件**：第三个外部客户 / 第一个集成商。
**关联**：当前协议见 `docs-site/dev/reference/ml-backend-protocol.md`。

---

## L7 · 公开 API / SDK

**问题**：当前所有 API 都是前端私用，第三方写集成要自己摸索 OpenAPI。

**长期目标**：发布 Python / TypeScript SDK + 完整 OpenAPI 文档站。

**子方向**：

- **L7.1 OpenAPI 完整性 review**：现有 `apps/api/app/api/` 不少接口的 response model 不严格，先补齐。
- **L7.2 Python SDK** ✅ 已落地（`packages/python-sdk`）：`pip install annotation-platform`，封装鉴权 + dataset CRUD + annotation push/pull + training trigger。
- **L7.3 TypeScript SDK**：复用前端 codegen，导出独立 npm 包。
- **L7.4 API token 管理** ◑ 长寿命 token 与能力 scopes 已落地（`api_keys`）；按资源细分（read-only / dataset-scoped）的权限粒度仍未实现。
- **L7.5 文档站「集成示例」专区** ✅ 已落地（`docs-site/dev/sdk/`）：notebook + curl + Python 示例并列。

**触发条件**：第一个客户写自动化脚本。
**关联**：现有 OpenAPI codegen 已在 `apps/web/src/codegen/`。

---

## L8 · 安全合规认证

**问题**：要进入欧美企业销售，必须有 SOC2 / ISO 27001 / GDPR 合规。

**长期目标**：通过认证审计；并把合规要求作为产品功能可选模块。

**子方向**：

- **L8.1 数据驻留 / 区域隔离**：客户数据可指定存储区域（中国 / 欧盟 / 美国），跨域不同步。
- **L8.2 加密**：rest（已有 DB 加密磁盘）+ transit（TLS）+ 应用层敏感字段加密（PII 单独）。
- **L8.3 审计日志完整性**：v0.8.1 已落分区 + 归档；扩展：append-only / WORM 存储、签名链。
- **L8.4 RBAC 细粒度**：当前角色（super_admin / admin / annotator / viewer）粗，需要项目级 + 资源级权限。
- **L8.5 数据保留与删除**：GDPR "被遗忘权"，删除用户时联动删除其上传的素材 / 标注（保留审计但脱敏）。
- **L8.6 SSO + SAML**：v0.10.x 已计划 OAuth2，SAML 走企业 IdP。
- **L8.7 vulnerability 管理**：依赖扫描（已有 `pnpm audit` / `safety`）+ 定期渗透测试。

**触发条件**：第一个欧美客户进销售 pipeline。
**关联**：§B 安全 / 治理。

---

## L9 · 移动端 / 平板适配

**问题**：当前前端只在桌面浏览器测过；标注台触屏体验未知。

**长期目标**：iPad / Android 平板可做轻量审核 / 标注；手机不做。

**子方向**：

- **L9.1 触屏手势**：pinch 缩放 / 双指平移 / 单指拖框，与 viewport (R8) 共生。
- **L9.2 响应式布局**：右侧栏可折叠 / 浮层化。
- **L9.3 ApplePencil / 触控笔**：polygon 顶点点击精度提升。
- **L9.4 离线缓存**：service worker，弱网 / 无网下可继续标注，连上线再同步（与 offline queue 一脉相承）。
- **L9.5 摄像头取证**：现场标注场景，可拍照后立即标注上传。

**触发条件**：客户提"现场作业"需求。

---

## L10 · 端侧推理 / 离线部署

**问题**：客户内网 / 边缘节点无法连云端 GPU。

**长期目标**：模型可量化后跑在客户本地 CPU / 边缘 GPU。

**子方向**：

- **L10.1 量化模型导出**：SAM / DINO 走 ONNX / TensorRT，FP16/INT8。
- **L10.2 端侧 WebGPU 推理**：浏览器跑 SAM-tiny，无需后端 GPU。配合 R5.2 ImageBitmap 缓存。
- **L10.3 离线 docker bundle**：客户拿 USB 装包安装，含模型权重 + 镜像 + offline license。
- **L10.4 增量同步**：边缘节点完成的标注离线后批量回传中心。

**触发条件**：客户硬件预算只能跑 CPU。

---

## L11 · 合成数据 / Active Generation

**问题**：某些类别（罕见缺陷 / 长尾事件）现实样本不够；标 100 张比生成 10000 张更费力。

**长期目标**：把生成式模型作为"标注辅助"或"补充数据源"。

**子方向**：

- **L11.1 SD / SDXL inpaint 增广**：选中物体后用 inpaint 改变形状 / 位置 / 颜色，标注框跟随变换。
- **L11.2 文本→图像合成**：客户输入 "一辆停在十字路口的红色卡车"，生成 100 张候选，自动 SAM 标注。
- **L11.3 视频合成**：Sora 类 model 生成短视频，逐帧 SAM 3 video 自动标注，用于训练。
- **L11.4 真实性校验**：合成数据上跑判别模型，过滤明显不真实的样本。
- **L11.5 合规标签**：合成数据集独立标记，下游训练时可选纳入 / 排除。

**触发条件**：客户在罕见类别上反映"标不动"。
**关联**：L2 主动学习；L3 评估（合成 vs 真实样本上的模型表现）。

---

## L12 · 商业化（SaaS / 多租户 / 计费）

**问题**：当前是单租户私有部署模型。

**长期目标**：可选 SaaS 入口（cloud.annotation.com），按用量计费，与私有部署并行运营。

**子方向**：

- **L12.1 多租户隔离**：DB schema-per-tenant 或 row-level（schema 更彻底但运维重）。
- **L12.2 计费**：标注量 / 存储 / GPU 时长 / 用户数计费，Stripe 接入。
- **L12.3 配额管理**：套餐 / 上限 / 超额告警。
- **L12.4 自助开通**：注册 / 试用 / 升级流程。
- **L12.5 合规与隔离的折中**：SaaS 客户的合规预期通常低于私有部署；但仍需 SOC2 起步（见 L8）。

**触发条件**：业务侧决定 SaaS 路线。**当前不优先**。

---

## L13 · 可观测性与 SRE 成熟度

**问题**：当前 Prometheus + grafana 基线在，但很多业务指标缺。

**长期目标**：完整 SLO / SLI 体系，按运营节奏告警。

**子方向**：

- **L13.1 业务 SLI**：标注成功率（提交 / 总尝试）、批次完成 P50/P95 周期、SAM 响应 P95、Bug 反馈周转。
- **L13.2 用户分群分析**：标注员效率 / 类别正确率 / 流失率（与 L5.6 排行榜共享数据）。
- **L13.3 SLO 看板**：99.5% 月度可用率 / 标注响应 <2s P95 等承诺，自动生成报告。
- **L13.4 Trace 全链路**：OpenTelemetry，从前端 click → API → Celery → ML backend 串联。
- **L13.5 容量预测**：基于历史增长率预测存储 / GPU 需求，提前扩容。

**触发条件**：DAU ≥ 50 或部署实例 ≥ 5。

---

## L14 · 国际化 / 本土化

**问题**：当前全中文硬编码。

**长期目标**：英语先行，逐步加日 / 韩 / 德 / 法。

**子方向**：

- **L14.1 i18n 框架**：已列 §B，react-intl / i18next。
- **L14.2 翻译流程**：crowdin / lokalise 接入，PR-time 校验。
- **L14.3 区域化日期 / 时区 / 数字格式**：依赖 i18n 库内置。
- **L14.4 RTL 语言**：低优，等真实需求。

**触发条件**：第一个非中文客户。

---

## L15 · 标注质量 AI 审计

**问题**：当前 review 是全人工，规模化后审不过来。

**长期目标**：AI 主动找"可疑标注"提交人工 review。

**子方向**：

- **L15.1 单帧异常检测**：基于 prediction vs 人工的 IoU 差异 / 类别冲突，自动标记可疑。
- **L15.2 跨标注员一致性**：相同 task / 类似 task 分给多人比对（IAA 计算）。
- **L15.3 时间序列异常**：标注员某天突然准确率掉，自动告警（疲劳 / 误判 / 故意刷量）。
- **L15.4 标注规范学习**：从历史 reject 评论里学"什么算错"，自动应用到新标注上。
- **L15.5 半自动 spot check**：抽 N% 标注送 AI auditor，置信度低的再给人。

**触发条件**：标注员规模 ≥20，全量人工 review 不可持续。
**关联**：L2 / L3 共享推理基础设施。

---

## 触发 → 准备清单

> 每个方向真要启动前，对应"现在可起步"的轻量动作：

| 方向 | 现在可做的轻量动作（不进入版本计划）            |
| ---- | ----------------------------------------------- |
| L1   | 写 `DatasetSnapshot` 表结构草案 ADR             |
| L2   | 给 `/training` 路由加一个能跑通的本地 YOLO demo |
| L3   | 在现有 predictions 表上跑 mAP 脚本作为评估 PoC  |
| L4   | 收集客户的非图像 / 视频任务样本                 |
| L5   | R11 segment 协议早一些和后端对齐                |
| L6   | ML backend 协议文档补全 manifest 字段           |
| L7   | OpenAPI response model 严格化（持续 chip）      |
| L8   | 跟潜在欧美客户对齐合规优先级                    |
| L9   | 桌面 + 平板浏览器 viewport 兼容性回归           |
| L10  | 调研 SAM ONNX / WebGPU 现状                     |
| L11  | 调研 SD inpaint + bbox 跟随的可行性             |
| L12  | 暂不动                                          |
| L13  | OpenTelemetry SDK 接入做单接口 PoC              |
| L14  | i18n 库选型 ADR                                 |
| L15  | predictions 表上写一个简单 IAA 计算脚本         |

---

## 关联文档

- 当前焦点：[`ROADMAP.md`](../ROADMAP.md)
- 视频工作台综合：[`ROADMAP/archive/2026-05-12-video-workbench-rendering-optimization.md`](archive/2026-05-12-video-workbench-rendering-optimization.md)（已合并原 2026-05-11-video-workbench.md 功能线）
- 后端帧服务：[`ROADMAP/archive/2026-05-12-video-backend-frame-service.md`](archive/2026-05-12-video-backend-frame-service.md)
- 图片工作台优化：[`ROADMAP/archive/2026-05-12-image-workbench-optimization.md`](archive/2026-05-12-image-workbench-optimization.md)
- 当前发布周期：[`ROADMAP/archive/0.10.x.md`](archive/0.10.x.md)
