---
title: 图标语义规范
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 图标语义规范

> 新代码请按本表挑图标，避免 `sparkles` 在多个语义场景共用导致识别度归零的回退。

<!-- history: this table was stabilized after the early SAM toolbar icon cleanup. -->

图标层走 `@/components/ui/Icon` 包装的 lucide-react；新业务图标可直接 `import { X } from "lucide-react"` 用，但**语义重要的位置**必须按下表挑：

## AI 相关图标语义表

| 场景                                                                         | 图标 (Icon name)    | lucide 原名         | 设计理由                                          |
| ---------------------------------------------------------------------------- | ------------------- | ------------------- | ------------------------------------------------- |
| **AI 一键操作**（一键预标 / 智能切题 / 跑预标按钮）                          | `wandSparkles`      | `WandSparkles`      | "魔法棒"语义 = 触发动作，唯一占位                 |
| **AI 助手 / AI 模块身份徽标**（AIInspectorPanel 标题）                       | `bot`               | `Bot`               | 拟人化身份，非操作按钮                            |
| **AI 模型 / SAM 模型相关**（模型市场 / RegisteredBackendsTab 等）            | `bot`               | `Bot`               | 与 AI 助手共享，模型即"AI 实体"                   |
| **SAM 文本提示输入**（ToolDock 子工具 + AIInspectorPanel SamTextPanel 标题） | `messageSquareText` | `MessageSquareText` | "文本输入"语义，与 AI 魔法解耦                    |
| **AI 框装饰角标**（BoxListItem 置信度徽章 / BoxRenderer 画布角标）           | `sparkle`           | `Sparkle`           | 单星，弱化为装饰，告知「这是 AI 出品」即可        |
| **AI 待审计数 / 状态指示器**（StatusBar）                                    | `circleDot`         | `CircleDot`         | 数字才是主体，图标只做色彩锚点                    |
| **AI 配额 / Sidebar AI 段总徽标 / AI 预标注路由徽标**                        | `sparkles`          | `Sparkles`          | 三星"装饰"语义，导航 / 概念性入口适用，非操作按钮 |

## 何时用 `wandSparkles` vs `sparkles`

- **`wandSparkles`**：用户**点击**这个图标会触发一个 AI 推理动作（看到这个图标 = 准备好 GPU 工作）
- **`sparkles`**：作为**装饰性 / 标识性**指示「这块区域跟 AI 有关」，但点击它不直接触发 AI 推理

举例：

- ✅ Topbar「AI 一键预标」按钮 → `wandSparkles`（点了就跑 AI）
- ✅ Sidebar 「AI 预标注」导航入口 → `sparkles`（点了进页面，不直接跑 AI）
- ✅ AIInspectorPanel 「一键预标」按钮 → `wandSparkles`（点了就跑）
- ✅ Sidebar AI 配额段总徽标 → `sparkles`（纯装饰）

## SAM 子工具图标

`apps/web/src/pages/Workbench/shell/ToolDock.tsx` SAM_SUB_TOOLS：

| 子工具  | 图标                | 备注                            |
| ------- | ------------------- | ------------------------------- |
| `point` | `target`            | 十字准心，"点"语义              |
| `bbox`  | `rect`              | 矩形                            |
| `text`  | `messageSquareText` | 文本输入框，**不要用 sparkles** |

## 反例

如果 `sparkles` 同时出现在 9 个语义场景，识别度会归零：

- Topbar 智能切题 / Topbar 一键预标 / ToolDock SAM 文本子工具 / AIInspectorPanel AI 助手标题 / AIInspectorPanel 一键预标 / AIInspectorPanel SAM 文本提示标题 / BoxListItem 置信度徽章 / BoxRenderer 画布角标 / StatusBar AI 待审

后续新业务请按表挑，**不要再把 sparkles 挂在 AI 操作按钮上**。

## 添加新图标

1. lucide-react 找到合适图标（https://lucide.dev/icons/）
2. 在 `apps/web/src/components/ui/Icon.tsx` 加 import + `ICON_MAP` 条目（驼峰命名 key）
3. 如果是 AI 相关语义，更新本文档的表格
