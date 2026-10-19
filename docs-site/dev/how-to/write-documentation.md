---
title: 编写与维护文档
description: docs-site 文档页面的结构选型、锚点稳定性、媒体放置规范与唯一事实源原则
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# 编写与维护文档

在 `docs-site` 中新增或重构文档页面时，请遵循本指南。我们的目标是让每篇文档快速回答一类明确的问题、保持公开链接与锚点稳定，并引导读者直接查阅拥有该事实的权威来源。

<span id="choose-the-page-shape" aria-hidden="true"></span>

## 选择页面体裁

动笔前先明确读者的任务目标，每篇页面应专注于以下五种体裁之一：

| 体裁                  | 页面开篇重点                               | 典型目录                        |
| --------------------- | ------------------------------------------ | ------------------------------- |
| 教程（Tutorial）      | 达成结果、前置条件与循序渐进的引导路径     | `dev/tutorials/`、`user-guide/` |
| 操作指南（How-to）    | 具体任务目标与最短、最安全的操作步骤       | `dev/how-to/`、`ops/runbooks/`  |
| 参考手册（Reference） | 速查表、精确字段定义与兼容性规则           | `dev/reference/`、`api/guides/` |
| 架构阐释（Concepts）  | 读者理解系统所需的边界关系、模块职责与决策 | `dev/concepts/`                 |
| 运维手册（Runbook）   | 故障症状、排查定位、恢复操作与闭环验证     | `ops/runbooks/`                 |

不要为了让内容更容易被搜到而把整篇文档复制到另一个栏目。请将权威解释放在归属该主题的页面中，再从栏目导览页（Hub）或相关文章通过链接指向它。

<span id="open-with-the-readers-next-action" aria-hidden="true"></span>

## 以读者的下一步行动开篇

每页只使用一个一级标题（H1）搭配简短导语。编写教程（Tutorial）和操作指南（How-to）时，请将预期结果和前置条件放在可选截图、背景介绍或高阶变体之前：

```md
# 导入图像数据集

完成本指南后，你的图像文件将作为标注任务导入到项目中。

## 开始之前

- 确认已具备目标项目与存储连接权限。
- 准备好受支持的图像文件格式。

## 导入数据集

1. 打开目标项目。
2. 选择数据集来源。
3. 核对预览结果并启动导入。
```

操作步骤请使用原生有序列表（`1. 2. 3.`），将每一步的预期结果紧跟在产生该结果的步骤旁，并在主流程中直接展示必要的风险提示。页面可以使用更深层级的子标题组织操作，但读者应当无需滚过大幅媒体截图即可看到第一个有效步骤。

<span id="keep-headings-and-anchors-stable" aria-hidden="true"></span>

## 保持标题与锚点稳定

Markdown 标题会自动生成公开的 URL 片段（`#anchor`），请将已有标题 ID 视为页面外部契约的一部分：

- 若现有标题仍准确描述章节内容，请保持原文本不变。
- 若重命名章节导致生成的锚点 ID 改变，请使用自定义锚点或相邻别名保留旧 ID。仅调整标题层级（如 `###` 改为 `##`）通常不会改变 ID，添加别名前可先比对渲染产物。
- 每个显式锚点必须使用全局唯一的描述性 ID，切勿在同一页面创建两个相同 ID 的元素。
- 不要在当前文档的章节标题中加入发布版本号（如 `v0.x.y`）；历史演进记录应归档至 `CHANGELOG.md`、ADR 或代码注释中。

保留旧锚点的示例写法：

```md
<span id="old-section-name" aria-hidden="true"></span>

## 当前章节标题
```

同一文档域内部优先使用相对路径链接（如 `../how-to/add-page`）；跨文档域引用时使用根路径链接（如 `/api/guides/tasks-and-annotations`）。移动页面后请确认规范路由（canonical route）保持有效，且历史深层链接仍能跳转到对应章节。

<span id="place-media-beside-the-operation" aria-hidden="true"></span>

## 将媒体紧邻对应操作放置

请使用站点内置的图片、视频、架构图（`<ExcalidrawDiagram>`）与灯箱组件。将媒体素材紧跟在它所解释的操作步骤或设计决策之后，并编写能够指引读者观察重点的 `alt` 文本或图注。关键限制条件必须写在正文中，不可仅靠图片表达。

不要连续平铺同一操作的亮色与暗色两张截图。若同时存在亮暗两套截图，请将它们成对放入 `doc-theme-images` 容器（亮色在前、暗色在后），站点的 `html.dark` 主题会自动切换显示匹配的截图（含点击放大视图）：

```md
<div class="doc-theme-images">

![登录页](./images/login.png)

![暗色模式下的登录页](./images/login.dark.png)

</div>
```

若无需区分亮暗主题，仅放一张代表性截图即可。不同设备布局或加载中、空状态、错误态截图，仅在能说明实质性差异时才单独列出。

对于会打断主干阅读流的可选变体（如移动端视图、找回密码支线或冗长的排障输出），请使用 `<details>` 折叠块：

```md
<details>
<summary>查看移动端示例</summary>

![窄屏下的同一表单](./images/example.mobile.png)

</details>
```

仅当目标位置确实属于该媒体资产的真实公开引用时才移动引用路径。请保持源文件、清单（manifest）与审查元数据不变；文档排版调整本身不代表批准新截图或变更其溯源记录。

<span id="use-native-containers-for-meaning" aria-hidden="true"></span>

## 使用原生语义容器

使用 VitePress 原生 Markdown 容器承载简短的语义提示：

```md
::: tip
该快捷键为可选项，仅用于加快面板切换速度。
:::

::: warning
此操作会立即变更下一个任务的状态，继续前请先核对队列。
:::
```

`<details>` 仅用于补充材料，切勿把必选步骤或影响读者下一步操作的安全警告折入其中。代码组（`::: code-group`）仅用于真正等价的替代命令，不要用来折叠需要按顺序执行的步骤：

````md
::: code-group

```bash [pnpm]
pnpm docs:build
```

```bash [npm]
npm run docs:build
```

:::
````

参数矩阵与状态对比适合使用 Markdown 表格；在窄屏下保持表格局部横向滚动，不要撑宽整篇正文容器。

<span id="respect-canonical-sources" aria-hidden="true"></span>

## 遵循唯一事实源（Canonical Sources）

修改文档中的具体数值或契约前，请先确认拥有该事实的权威来源：

- **API 路由与 Schema**：源自后端应用代码与受跟踪的 OpenAPI 快照（`openapi.snapshot.json`）。接口契约变更后请重新生成 API 产物。
- **自动生成页面与索引**：由各自的生成脚本刷新；只要上游数据源存在，切勿手改 `*.generated.md`。
- **环境变量文档**：以 `.env.example` 与环境变量生成脚本（`pnpm docs:gen-env-vars`）为准。
- **架构决策记录（ADR）**：真源位于 `docs/adr/`，文档站中的 `/dev/adr/` 是构建期同步镜像。
- **截图、视频、架构图及清单**：保留各自的归属脚本与审查记录（详见[更新文档截图](./update-screenshots)）。

对于容易随代码演进而漂移的配置值或字段列表，优先链接到权威页面或源码文件，避免在多处重复抄写。当事实修正跨越 API、运行时或部署边界时，请结合代码实现一并更新受影响的文档。

<span id="run-the-focused-checks" aria-hidden="true"></span>

## 运行定向校验

完成常规 Markdown 文档修改后，请运行覆盖改动范围的校验命令：

```bash
pnpm exec prettier --check docs-site/dev/how-to/write-documentation.md
pnpm --filter @anno/docs-site check:frontmatter
pnpm --filter @anno/docs-site check:codegen
pnpm docs:build
git diff --check
```

若移动或修改了媒体引用，请同时运行[更新文档截图](./update-screenshots)中说明的孤儿图片检查与媒体审计脚本；若修改了侧边栏导航或公开路由，请运行导航覆盖率检查（`check:nav`）并在本地预览中抽查深层链接。

若进行了阅读版式调整或多页面结构重组，请在修改前先抓取基线锚点，并在修改完成后针对预览服务做回归校验：

```bash
node docs-site/scripts/check-reading-layout.mjs capture http://127.0.0.1:5173/ /tmp/docs-reading-anchors.json
node docs-site/scripts/check-reading-layout.mjs verify http://127.0.0.1:5173/ /tmp/docs-reading-anchors.json
```

请使用 `pnpm docs:dev` 实际打印的预览 URL（包含部署路径前缀）。`capture` 不会覆盖已存在的基线文件；`verify` 会自动检查代表性路由、历史锚点 ID、正文宽度、侧边栏激活态以及亮/暗两种主题模式。
