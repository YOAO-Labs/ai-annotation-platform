---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 前端分层

前端应用位于 `apps/web/`（包名 `@anno/web`，路径别名 `@/*` → `apps/web/src/*`），采用 React 18 + TypeScript + Vite + React Router 构建。

## 核心依赖方向

```text
pages (路由页面与领域工作流)
  ├── components/shell + components/ui + components/shadcn/ui (视图组件)
  ├── stores (全局壳层 Zustand Store) + TanStack Query (服务端缓存)
  └── api/*.ts (领域 API 封装) → api/generated/* (OpenAPI 与能力词表自动生成类型)
```

## 1. 页面与工作台分层（`src/pages/`）

普通管理页面按「一个路由模块一个目录」组织（如 `Projects/`、`Training/`、`ModelMarket/`、`AIPreAnnotate/`、`Review/`），通常拆分为：

- 页面主组件与局部子组件（`components/`）
- URL 查询参数纯函数解析器（`*UrlState.ts`，无网络副作用，配独立单测）

复杂度最高的标注工作台（`src/pages/Workbench/`）进一步按职责划分为五层（详见[工作台 Shell 架构](./workbench-shell)）：

- `shell/` — 顶栏、Dockview 面板工作区（`WorkbenchDockWorkspace`）、模态框与浮层宿主
- `stages/` — `image`（Konva 2D）、`video`（Konva + WebCodecs）、`three-d`（Three.js / WebGPU 点云）三套 Stage 适配器与交互 Hook
- `stage/` — 画布底层图元与播放器基础组件
- `state/` — 工作台领域状态模型（`useWorkbenchShellModel`）、命令历史、任务切换守卫（`taskNavigation.ts`）与 Mask 变更工作流
- `modes/` — 标注模式（`annotate`）与审核模式（`review`）的策略差异封装

## 2. 状态管理边界：TanStack Query vs Zustand

| 状态类型                 | 归属工具                                          | 位置与准则                                                                                                                        |
| :----------------------- | :------------------------------------------------ | :-------------------------------------------------------------------------------------------------------------------------------- |
| **服务端业务状态**       | TanStack Query (`useQuery` / `useMutation`)       | 项目、批次、任务、标注、训练实验等远端数据统一由 Query 缓存与失效机制管理，**禁止把服务端列表或详情镜像复制进全局 Zustand Store** |
| **全局应用壳层状态**     | Zustand (`src/stores/`)                           | 仅保存认证会话（`authStore`）、主题与工作台偏好、全局快捷键与 UI 抽屉开关                                                         |
| **工作台画布与编辑状态** | 领域 Hook + 局部 Store (`pages/Workbench/state/`) | 选中的框、工具激活态、撤销重做栈、乐观更新队列与任务级栅格资源协调器（按 `task.id` 生命周期隔离）                                 |

## 3. API 客户端与代码生成（`src/api/`）

```text
后端修改 Pydantic Schema 或能力词表
  → uv run python scripts/export_openapi.py / export_capability_registry.py
  → 更新 apps/api/openapi.snapshot.json 与 capability-registry.snapshot.json（提交入库）
  → pnpm codegen
  → 生成 apps/web/src/api/generated/{types.gen.ts, capabilityVocab.gen.ts}（gitignored）
```

- `src/api/generated/`：由 `@hey-api/openapi-ts` 与 `gen-capability-vocab.mjs` 自动生成，**严禁手动编辑**，也不提交至 Git（本地首次启动或切换分支后通过 `pnpm codegen` 生成，`pnpm build` 会通过 `prebuild` 自动按需触发）。
- `src/api/client.ts` 与各领域封装（`projects.ts`、`tasks.ts`、`training.ts` 等）：统一封装 `axios` / `fetch`、同源 `/api/v1` baseURL、JWT 自动刷新、幂等键注入与错误归一化；导出函数统一采用 `getXxx` / `createXxx` / `updateXxx` / `deleteXxx` 命名。

## 4. 组件与样式体系（`src/components/` & `src/styles/`）

- `components/shadcn/ui/`：本地拥有的 shadcn/Radix 无障碍原语组件。
- `components/ui/`：应用层通用 UI 适配组件（`Button`、`Badge`、`Modal`、`Sparkline` 等），仅接收 props 渲染，不直接发起 API 请求。
- `components/shell/`：应用级导航框架（`TopBar`、`Sidebar`、`BugReportDrawer`）。
- **设计系统与令牌约束**（详见[设计系统](../reference/design-system)）：
  - 主题变量唯一真源为 `src/styles/shadcn.css`，CSS Modules 仅读取 `--sc-*` 变量（严禁使用旧 `--color-*` 或硬编码裸色值）。
  - 暗色模式通过 `[data-theme="dark"]` 与 Tailwind `dark:` 驱动；修改样式后运行 `pnpm --filter @anno/web lint:css-tokens` 校验。

## 5. 共用 Markdown 编辑与阅读组件（`src/components/markdown/`）

标注指引、项目模板指引、BUG 反馈描述与评论共用一套安全的 Markdown 组件（底层持久化仍为标准 Markdown 字符串）：

- **按需加载与读写分离**：`MarkdownEditor` 基于 MDXEditor 构建，仅在进入编辑态时动态加载；只读视图 `MarkdownView`（及兼容包装 `GuideMarkdownView` / `MarkdownBlock`）使用轻量的 `react-markdown` + `remark-gfm`，不打包编辑器运行时。
- **核心属性契约**：

  | 属性                             | 职责说明                                                                                                                                            |
  | :------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------- |
  | `value` / `onChange`             | 页面持有草稿状态；初始化或切换模式时的编辑器内部规范化不会误触发人工修改标记                                                                        |
  | `documentId`                     | 切换文档时重建编辑上下文，隔离未完成的图片上传与过期回调                                                                                            |
  | `onUploadImage` / `resolveImage` | 业务图片上传返回内部标识（如 `guide-asset:KEY`）；读取时由 `useGuideAssets` 按项目作用域解析为短期预签名 URL，绝不将临时签名 URL 写回 Markdown 原文 |

- **安全与自动保存边界**：可视化编辑禁用原始 HTML 与 JSX 执行；项目指引自动保存由 `AnnotationGuideSection` 串行防抖提交，并在中文输入法（IME）组合输入结束后才发布变更（E2E 回归见 `guide-autosave.spec.ts` 与 `markdown-authoring.spec.ts`）。
