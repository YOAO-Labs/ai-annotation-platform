# AI 标注平台深度调研报告

> 历史快照：保留原研究索引的基线摘要与调研方法，不描述平台当前能力。当前入口见[研究索引](../README.md)。

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
