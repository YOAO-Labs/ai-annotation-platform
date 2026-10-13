# AI 标注平台深度调研报告

> 拆分自《AI标注平台深度调研报告》§0-§1

> 面向 CV(图像 / 视频)生产级私有部署平台。聚焦"模型预标注 + 人工校正"与"Agent 自动化标注流水线"两大方向。
>
> 对标基准:`yyq19990828/ai-annotation-platform` v0.2.0（2026-04-27）
>
> 调研者:Claude · 调研日期:2026-04-27

---

## 0. 摘要（给赶时间的人）

调研了 4 个**仍在活跃迭代**的开源平台 + 5 个商业产品的最新动向:

| 维度            | 你 (v0.2.0)                          | Label Studio                                       | CVAT                                 | X-AnyLabeling           | Adala                     |
| --------------- | ------------------------------------ | -------------------------------------------------- | ------------------------------------ | ----------------------- | ------------------------- |
| 形态            | Web 全栈                             | Web 全栈(Django)                                   | Web 全栈(Django)                     | 桌面端 + 远程服务       | LLM Agent 框架            |
| AI 模型集成     | 字段占位                             | HTTP MLBackend 抽象                                | Nuclio Serverless                    | 本地 ONNX + 远程 server | Skill / Runtime / LiteLLM |
| 数据模型层级    | User/Project/Task/Annotation(4 张表) | + Org / Pred / TaskLock / Draft / PredMeta(20+ 张) | + Job / Segment / Track / GT(20+ 张) | 文件级,无后端           | Skill 注册表              |
| Active Learning | 未实现                               | uncertainty + cluster 采样                         | 通过 Job stage 流转                  | 阈值过滤                | LLM judge + 反馈          |
| 视频/Tracking   | 未实现                               | 弱(主要图像)                                       | **强**(LabeledTrack/TrackedShape)    | SAM2 视频               | 不适用                    |
| 质检 / 一致性   | 未实现                               | LSE 才有 IAA + 低一致性队列                        | quality_control + consensus 模块     | 无                      | LLM judge                 |
| LLM/Agent       | 无                                   | 通过 Adala 衔接                                    | AI Agents (2024-2025)                | Florence2/Gemini API    | 原生                      |
| Cost tracking   | 无                                   | **PredictionMeta 含 token cost**                   | 无                                   | 无                      | runtime cost estimate     |

**最值得抄给你 repo 的 5 件事**（后面有详细说明）:

1. **把 Annotation 和 Prediction 拆成两张表**——你现在用 `source` 字段区分 `human / ai / ai-accepted`,会在多模型/多版本场景立刻爆掉
2. **抽象出 `MLBackend` 表,模型即 HTTP 服务**——LS 把模型当远端 REST 服务的设计是教科书级
3. **加 `TaskLock` + `is_labeled` + `overlap`**——分布式标注必备,缺一个就出脏数据
4. **`PredictionMeta` 记录 token cost / 推理耗时**——LLM 时代,谁不记账谁亏钱
5. **任务调度抽象成 `next_task` 策略链**——你 Workbench 现在是按 ID 顺序取的,这块直接抄 LS 的 6 策略链最快

---

## 1. 调研范围与方法

### 1.1 入选标准

- **仍在活跃迭代**（2025-2026 年有 release / 商业更新）
- **CV 优先**（图像 / 视频 / 检测 / 分割 / 追踪）
- **AI 能力是产品核心而非附属**
- 至少能拉到源码或者有详细公开文档

### 1.2 入选名单与定位

| 平台                           | 类型          | 定位                       | 调研深度     |
| ------------------------------ | ------------- | -------------------------- | ------------ |
| **Label Studio** (HumanSignal) | 开源 + 企业版 | 通用全栈 + 配置驱动        | **源码深读** |
| **CVAT** (CVAT.ai)             | 开源 + 企业版 | CV 专用,视频追踪强         | **源码深读** |
| **X-AnyLabeling** (CVHub520)   | 纯开源        | 桌面端 SAM/foundation 模型 | **源码深读** |
| **Adala** (HumanSignal)        | 纯开源        | LLM Agent 标注框架         | **源码深读** |
| Roboflow                       | 商业          | 一站式 + Universe 模型市场 | 文档分析     |
| Encord                         | 商业          | 数据策展 + SAM2 + GPT-4o   | 文档分析     |
| V7 Darwin                      | 商业          | 医疗影像 + 视频 + SAM3     | 文档分析     |
| Refuel Autolabel               | 开源库        | LLM 批量标注(NLP 主)       | 文档分析     |
| Argilla                        | 开源(HF)      | LLM/NLP 数据策展           | 文档分析     |

> 商业平台:无法拉到源码,基于公开文档与博客分析其暴露出来的工作流和能力点。

---

## 子文档索引

| 编号 | 文档                                                                                              | 内容                                                                                                                                                                                                                                                                                   |
| ---- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 01   | [Label Studio](./01-label-studio.md)                                                              | §2.1 Label Studio 深度拆解                                                                                                                                                                                                                                                             |
| 02   | [Adala](./02-adala.md)                                                                            | §2.2 Adala LLM Agent 标注框架                                                                                                                                                                                                                                                          |
| 03   | [CVAT](./03-cvat.md)                                                                              | §2.3 CVAT CV 专用平台                                                                                                                                                                                                                                                                  |
| 04   | [X-AnyLabeling](./04-x-anylabeling.md)                                                            | §2.4 X-AnyLabeling 桌面端                                                                                                                                                                                                                                                              |
| 05   | [商业平台速览](./05-commercial.md)                                                                | §2.5 Roboflow、Encord、V7、Refuel、Argilla                                                                                                                                                                                                                                             |
| 06   | [AI 赋能模式拆解](./06-ai-patterns.md)                                                            | §3 五种 AI 赋能模式 A-E                                                                                                                                                                                                                                                                |
| 07   | [生产级能力](./07-production-capabilities.md)                                                     | §4 用户管理/数据存储/协同                                                                                                                                                                                                                                                              |
| 08   | [对比矩阵](./08-comparison-matrix.md)                                                             | §5 关键能力对比矩阵（4 张表格）                                                                                                                                                                                                                                                        |
| 09   | [借鉴建议](./09-recommendations.md)                                                               | §6 具体借鉴建议                                                                                                                                                                                                                                                                        |
| 10   | [路线图](./10-roadmap.md)                                                                         | §7 路线图建议（v0.3-v0.6+）                                                                                                                                                                                                                                                            |
| 11   | [参考资料](./11-references.md)                                                                    | §8-§9 参考资料 + 总结                                                                                                                                                                                                                                                                  |
| 12   | [大数据集分批策略](./12-large-dataset-batching.md)                                                | 智能切批 / 不可变快照 / 主动学习闭环                                                                                                                                                                                                                                                   |
| 13   | [mask→polygon simplify tolerance 评测](./13-simplify-tolerance-eval.md)                           | v0.9.4 phase 3 默认 tolerance 选定依据                                                                                                                                                                                                                                                 |
| 14   | [点云 + 图像联合标注](./14-point-cloud-image-fusion.md)                                           | 融合原理 + SUSTechPOINTS/xtreme1/CVAT 源码级拆解 + 本平台 gap 分析与改动评估                                                                                                                                                                                                           |
| 15   | [标注员绩效能力对标](./15-annotator-performance.md)                                               | CVAT/LS 源码 + 6 商业产品的绩效指标 taxonomy + gap 分析(IAA/honeypot/项目级范围/导出)                                                                                                                                                                                                  |
| 16   | [质量体系前置调研](./16-quality-system-1t1a1r.md)                                                 | 1 task : 1 annotator : 1 reviewer 模式下 GT / Honeypot / Consensus 的可行边界                                                                                                                                                                                                          |
| 17   | [图片 Raster Mask 工作台基准](./17-image-raster-mask-workbench-benchmark.md)                      | 1080p Worker decode / analyze / bitmap p50/p95、临时内存与 20 Mask 缓存字节基线                                                                                                                                                                                                        |
| 18   | [Mask 高级操作基准](./18-mask-advanced-operations-benchmark.md)                                   | 1080p component / hole / morphology / split 的主线程与 Worker p50/p95、Long Task 和重复 20 次 heap 基线                                                                                                                                                                                |
| 19   | [Mask 大画布性能基线](./19-mask-large-canvas-performance-baseline.md)                             | 720p / 1080p / 4K 渲染、缓存、编辑历史、快速切帧、5K / 8K 内存边界与实施后退出门                                                                                                                                                                                                       |
| 20   | [Mask 质检与格式生态合同](./20-mask-qc-format-contracts.md)                                       | QC golden、格式事实标准、consumer fixture、稳定 loss / skip code 与并发 / 安全风险                                                                                                                                                                                                     |
| 21   | [WebGPU 视频工作台适用性](./21-webgpu-video-workbench.md)                                         | 客户端 / 服务端资源边界、Linux NVIDIA 实测、packed XOR 与 immutable base cache production A/B、ROI 路由与字节预算                                                                                                                                                                      |
| 22   | [Supervisely 与 CVAT 标注工作台](./22-supervisely-cvat-workbench.md)                              | 官方文档、源码、教程与媒体深读；UI 交互、3D 工作台逻辑、能力差距、借鉴优先级与验收标准                                                                                                                                                                                                 |
| 23   | [Supervisely 与 CVAT 图片、视频工作台](./23-supervisely-cvat-image-video-workbench.md)            | 图片与视频 UI 交互专项；高频摩擦、工具状态、AI / Mask、轨迹时间轴、审核定位与实施验收                                                                                                                                                                                                  |
| 24   | [3D 时序对象与轨迹生命周期](./24-3d-temporal-object-lifecycle.md)                                 | 八个商业/开放核心平台、五个开源项目与 OpenLABEL；对象身份、存在区间、时间角色、可逆修复和领域基础                                                                                                                                                                                      |
| 25   | [macOS WebGPU 性能实测](./25-macos-webgpu-performance.md)                                         | Apple Metal 上的 Raster Mask 与 3D 点云 Chrome/Safari 性能、正确性、恢复能力与推广门结果；2026-09-15 补齐 one-pass 复测、超大图 tile 首次资格与 3D 多轮资格                                                                                                                            |
| 26   | [仓库优化 P0 基线](./26-repository-optimization-baseline.md)                                      | 全仓目录分类、测试清单与运行入口、计划第 3 节不变量到测试 ID 的映射、60 次真实 CI 运行分类与首次失败证据、P0-A 初稿校准记录；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                                          |
| 27   | [仓库优化改造台账](./27-repository-optimization-ledger.md)                                        | P0–P10 阶段状态、每阶段验收门与证据回填约定、当前有效决定（CI 权威源、工作树自有数据库、保护现状）、未决事项与未执行检查                                                                                                                                                               |
| 28   | [仓库优化 P1 契约固化](./28-repository-optimization-p1-contracts.md)                              | 第 3 节 8 条不变量到测试的 HEAD 复核映射、旧平台角色测试数据修正（7 个既有后端文件 + 1 个新增信号回归 + 27 个前端文件）、`signals.py` 信号兜底直接回归、破坏→失败→恢复变异证据与 P2/P6 后续归属；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)      |
| 29   | [仓库优化 P2 后端 fixture](./29-repository-optimization-p2-fixtures.md)                           | 旧 kwargs fixture 迁 `tool_bindings`、`httpx_client_bound` 别名删除、测试库解析 fail-closed 守卫、工厂收敛与 conftest 注释治理；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                                       |
| 30   | [仓库优化 P3 前端测试边界](./30-repository-optimization-p3-frontend-tests.md)                     | data-manager 流程测试改 MSW API 边界、纯 URL 规则下沉、覆盖率口径治理与 schema 重试缺陷修复；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                                                                          |
| 31   | [仓库优化 P7 E2E 下沉与隔离](./31-repository-optimization-p7-e2e-isolation.md)                    | 请求失败分类收敛为有类型策略（`e2e/helpers/request-errors.ts`）、`seed.reset` 迁移到 owned 命名空间、隔离端口与真实浏览器复跑；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                                        |
| 32   | [仓库优化 P4 后端规则归属](./32-repository-optimization-p4-backend-ownership.md)                  | 授权真值收敛 `project_access`、`signals.py` 薄骨架与 `async_job_terminal` 封闭分派、成员 factory 收敛及其余入口授权审计；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                                              |
| 33   | [仓库优化 P5 工作台收敛](./33-repository-optimization-p5-workbench.md)                            | 装配 model 8919 → 6647 行、六条领域规则下沉同目录领域/纯模块、两组局部视图迁入 shell 组件、`canBatchConvert`/helpers 有据 KEEP 与逐行等价核对；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                        |
| 34   | [仓库优化 P6 ML/共享运行时](./34-repository-optimization-p6-ml-runtime.md)                        | `managed_pool` 下沉 `backend_runtime` 与并发套件合并（含负向变异验证）、`gpu_lifecycle`/`embedding_cache` 保留边界、ML backend 注释 provenance 清理与本地 CPU 实测；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                   |
| 35   | [仓库优化 P6 剩余稳定域](./35-repository-optimization-p6-remaining.md)                            | seed persona/成员职责修复与一次性库实测、AdminPeople 徽章、Review 纯 URL 下沉、gpu_arbitration provenance 与扫描器 allowlist、SDK/scripts/config 版本叙事清理、通知谓词与回滚取消审计、测试清单补录；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)  |
| 36   | [仓库优化 P8 CI 选择与报告](./36-repository-optimization-p8-ci.md)                                | 影子选择（legacy 门禁逐字节不变，新增 `shadow=`/`required=`/`ml_cpu=`）、fail-closed 路径分类、指纹构建复用与 SHA256SUMS 校验、运行诊断（首过/flaky/超时/未运行）、ML CPU caller 接线；门禁切换属 P9；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md) |
| 37   | [仓库优化 P8 CPU 测试 CI](./37-repository-optimization-p8-cpu-ci.md)                              | 可复用 `ml-cpu-test.yml` + `scripts/run-ml-cpu-tests.sh`（8 套件、每套件独立 venv；gs2/sam3 装 CPU torch，yolo 不用 torch/ultralytics）；PR caller 已于 P8 收口                                                                                                                        |
| 38   | [仓库优化 P8 核心 smoke](./38-repository-optimization-p8-smoke.md)                                | §6.2 核心 smoke 契约（9 例 / 4 个既有 spec 文件、显式 `--grep` 成员）与首过实测；P8 阶段产物                                                                                                                                                                                           |
| 39   | [仓库优化 §7.2 迁移验证](./39-repository-optimization-migration-validation.md)                    | 真实四阶段迁移验证 runner（fresh / 可逆段真实 downgrade / 前向数据断言 / 备份恢复）+ fail-closed 修订图策略 + 33 例策略测试；取代 stamp 式 round-trip；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                |
| 40   | [仓库优化 P10 后端/SDK/ML 契约验收](./40-repository-optimization-backend-contract-acceptance.md)  | 后端 4428 collected / 0 failed / 0 errors / 15 skipped（coverage line 73.61 / branch 55.33）、OpenAPI 漂移检查、SDK 322 passed + `uv build`、示例 5/15、ML CPU 8 套件（含 gs2 150 / sam3 196）；P10 验证通道（非 P10 完成）                                                            |
| 41   | [仓库优化 P10 前端验收](./41-repository-optimization-frontend-acceptance.md)                      | 覆盖率 562 文件 / 5626 用例（72.28/79.33/66.92/72.28，阈值 45/45/45/70 未变）、生产构建 / size / typecheck / lint / format 与 docs 构建通过；P10 验证通道                                                                                                                              |
| 42   | [仓库优化 P10 渲染器资格验收](./42-repository-optimization-renderer-acceptance.md)                | 点云严格 WebGPU 21 例（`PLAYWRIGHT_POINTCLOUD_WEBGPU=1`）、WebCodecs 精确帧 9 例；浏览器自报 adapter 证据与限制；P10 验证通道                                                                                                                                                          |
| 43   | [仓库优化 P9 影子对照与门禁切换](./43-repository-optimization-p9-shadow-comparison.md)            | planned 选择 + 必需 suite 审计 + `core-flaky` 门禁；冻结 `898` 失败 campaign 历史（default-four 13P/1F/53notrun，实跑审计 exit 1），集成根 `13d274326` composed 审计 exit 0（10 套件按变更影响范围复用 + `layout-stress`/`default-four` 修正构建重跑替换）；P9 完成                    |
| 44   | [仓库优化 P7 点云夹具消费者回归修复](./44-repository-optimization-pointcloud-fixture-fix.md)      | 6 个点云 spec + `lidar-export-trusted` 从全局 `admin@e2e.test` 改为 `SeedData.admin_email`（lidar 行按精确项目名定位）；干净一次性库实测；隶属 [仓库优化计划](../plans/archive/1789880018_repository-optimization-plan.md)                                                             |
| 45   | [仓库优化 P10 最终验收记录](./45-repository-optimization-final-acceptance.md)                     | 计划 §10 全部 22 项按实际证据勾选；before/after（`9cec9751a`→集成根 `13d274326` / 报告 `8c4276297`）；P9 composed 门禁 exit 0（10 套件按**变更影响范围**复用 + `layout-stress`/`default-four` 重跑）；唯一跨项限制：远端 CI 未运行；**P10 完成，台账关闭**                             |
| 46   | [仓库优化 P9 域执行证据（历史）](./46-repository-optimization-p9-domains.md)                      | `dc972be08` 候选上 pointcloud 37P/1F、video-pipeline 43P/20skip 但 teardown exit 1（**历史失败**，后续由聚焦修复与 `898` campaign 取代）                                                                                                                                               |
| 47   | [仓库优化 P9 owned seed 清理修复](./47-repository-optimization-seed-cleanup-fix.md)               | deadlock 竞争根因 + fail-fast 清理、takeover 命名空间化、共享 `BUG-E2E-FILTER-` 前缀边界；聚焦回归与共享产物预览验证                                                                                                                                                                   |
| 48   | [仓库优化 P9 Markdown 表格单元格图片重试修复](./48-repository-optimization-markdown-retry-fix.md) | 根因证实 + 产品修复 `69960028d` 预览验收；删除测试 `:889` 最终由 `default-one` 覆盖（旧 19PASS 属修正前测试修订）                                                                                                                                                                      |
| 49   | [仓库优化 P9 顶部栏中等宽度重叠修复](./49-repository-optimization-topbar-fix.md)                  | `min-w-max` + `<1000px` 分层；边界/审核/位置几何证据；视频选择器探针仅覆盖 `collaboration_enabled`，其余为真实 video/claim API（不称完全未 mock）                                                                                                                                      |
| 50   | [仓库优化 P9 工作台根边停靠与保留分组](./50-repository-optimization-workbench-root-edge-dock.md)  | 100px parking 真实网格范围 → 1px 恢复异常 → `failRestore`；最终修复在 restore 前再折叠 live parking；`normalizeParking` 掩盖 live 几何的变体被拒                                                                                                                                       |

> 注：P8 的 CI 选择与报告证据（`36-repository-optimization-p8-ci.md`）已随 P8 收口并入本树（root `6c47d68de`）；P9 影子验证/门禁切换（`43`）、聚焦修复（`46`–`50`）与 P10 最终验收记录（`45`）均已在集成根收口。`45` 现为 P10 完成记录（原草案性质已由本轮收尾取代）。
