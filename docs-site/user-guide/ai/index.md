---
title: AI 辅助标注
audience: [annotator, project_admin, super_admin]
type: how-to
since: v0.9.0
status: stable
last_reviewed: 2026-08-16
---

# AI 辅助标注

这一组文档按“现在要完成什么任务”组织 AI 能力。页面不按某个模型或后端拆分：先选任务，再按需要进入模型配置、协议或运维文档。

## 五类入口与页面边界

<ExcalidrawDiagram
  src="/diagrams/shared/ai/task-entry-map.svg"
  alt="AI 五类执行入口及工作台、AI 预标、模型市场三类页面边界关系图"
  caption="五类 AI 执行入口：工作台承载图片交互式 AI、当前题 AI、二次推理和视频 AI 追踪，AI 预标页承载批量预标；模型市场与项目模型设置只提供能力声明和路由"
/>

图中五个编号才是执行入口。模型市场与项目模型设置负责声明能力、选择路由、检查健康状态和保存项目编排，但不会自行发起一次推理。候选型结果需要人工审阅；二次推理则直接更新当前已确认标注。

## 按角色开始

| 角色       | 常用 AI 任务                                               | 从哪里开始                                                                                                                                                                          |
| ---------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 标注员     | 在当前图片生成辅助结果、审阅候选、追踪视频目标             | [图片交互式 AI](../workbench/sam-tool) · [审阅 AI 候选](./candidate-review) · [视频 AI 追踪](../workbench/video-propagate)                                                          |
| 项目管理员 | 启用模型、批量预标、复用编排、导入外部预测、重试失败项     | [项目 ML 模型](../projects/ml-backends) · [AI 预标](../projects/ai-preannotate) · [全局编排库](../projects/pipeline-library) · [外部预测导入](../datasets/prediction-import-export) |
| 超级管理员 | 管理全局 backend、查看模型能力与运行状态、协助恢复失败任务 | [模型市场](../superadmin/model-market) · [失败预测排查](../superadmin/failed-predictions)                                                                                           |

## 按任务进入

| 我想做的事                                       | 详页                                                         |
| ------------------------------------------------ | ------------------------------------------------------------ |
| 用点、框、示例或文本辅助完成当前图片             | [图片交互式 AI](../workbench/sam-tool)                       |
| 对当前整图运行模型，或对已确认标注补属性 / 子框  | [当前题 AI 与二次推理](./current-task-inference)             |
| 接受、拒绝或继续编辑图片候选                     | [审阅 AI 候选](./candidate-review)                           |
| 为多个图片任务运行模型，或配置多阶段预标         | [AI 预标](../projects/ai-preannotate)                        |
| 对视频轨迹发起模型追踪、补种子并审阅整批结果     | [视频关键帧传播与 AI 追踪](../workbench/video-propagate)     |
| 导入外部模型的预测、替换或清理候选               | [外部预测导入 / 导出](../datasets/prediction-import-export)  |
| 让项目可用某个 backend，并处理模型置灰或路由问题 | [项目 ML 模型](../projects/ml-backends)                      |
| 保存、共享或套用一套多阶段预标编排               | [全局编排库](../projects/pipeline-library)                   |
| 查看任务历史、取消批量预标或重试可恢复项         | [AI 任务与失败恢复](../workflows/failed-prediction-recovery) |

## 结果会去哪里

批量预标和外部预测先写入 **Prediction（候选）**。标注员在工作台接受后，候选才变成可继续编辑的 **Annotation（正式标注）**；拒绝或清理候选不会删除已经接受的人工标注。

视频 AI 追踪也先生成 job 级候选，必须在候选审阅条中整批接受或丢弃。图片交互工具的候选与确认方式因工具而异，尤其 Magic Box 会在结果返回后立即进入类别确认；以[图片交互式 AI](../workbench/sam-tool)的说明为准。

## AI 功能不可用时

先按这个顺序检查：

1. 当前账号是否有对应入口的角色权限。
2. 项目是否启用了所需工具单位和交互式 AI 开关。
3. 项目是否启用了声明该能力的 ML Backend；视频追踪还要求 backend 声明对应 tracker。
4. 任务或批次是否满足运行前置条件。批量预标要求批次处于 `active`，或为尚未分派标注员与质检员的 `draft` 批次。

具体的模型置灰、能力路由和项目配置见[项目 ML 模型](../projects/ml-backends)；运行失败后的恢复路径见[失败预测恢复](../workflows/failed-prediction-recovery)。

## 相关概念

- [平台概念与术语](../concepts)解释 Prediction、Annotation、批次和关键帧。
- [AI 预标注接管](/dev/concepts/ai-preannotate-handoff)解释候选、正式标注与状态流转的实现边界。
- [模型市场](../superadmin/model-market)面向管理员展示模型能力、注册管理与运行时观测。
