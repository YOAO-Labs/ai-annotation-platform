---
title: 约定与规范
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 约定与规范

本页汇总仓库的 Git 提交规范、分支与 PR 门禁、Python / TypeScript 命名约定以及代码格式化工具链。

## 提交信息规范（Conventional Commits）

仓库采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 格式：

```text
<type>(<scope>): <subject>

<body>

<footer>
```

### `type` 类型枚举

| `type`     | 适用场景                 |
| ---------- | ------------------------ |
| `feat`     | 新增功能或能力           |
| `fix`      | 修复缺陷                 |
| `refactor` | 重构（不改变外部行为）   |
| `perf`     | 性能优化                 |
| `docs`     | 文档增补或修订           |
| `test`     | 测试用例增补或修正       |
| `chore`    | 构建脚本、依赖或工程杂项 |
| `ci`       | GitHub Actions 工作流    |
| `style`    | 代码格式调整（无逻辑变） |

### 常用 `scope` 建议

`api` / `web` / `workbench` / `training` / `pointcloud` / `sdk` / `dashboard` / `auth` / `db` / `docs`

### 提交示例

```text
feat(training): 支持 COCO8 在线学习演示夹具与一键联调
fix(api): ProjectOut.batch_summary 改为 ProjectBatchSummary 显式模型
docs(dev): 梳理工作台状态边界与在线学习协议参考文档
```

---

## 分支与 Pull Request 门禁

- **主分支**：`main`
- **特性与修复分支**：`feat/<scope>/<short-description>` 或 `fix/<scope>/<bug-id>`
- **PR 基本要求**：
  - 标题遵循相同的 Conventional Commits 格式；
  - 描述中包含清晰的测试验证说明（Test Plan）与关联 Issue；
  - 涉及 API、前端可见功能、架构或环境变量变更时，必须在同一提交中同步更新对应文档（详见[编写与维护文档](../how-to/write-documentation.md)）。
- **CI 必过门禁**：后端 `pytest`、前端 `vitest`、Ruff / ESLint / CSS Token Lint、OpenAPI 契约漂移检查（`openapi-contract`）以及文档站构建检查。

---

## 命名与代码组织约定

### Python（`apps/api`、`apps/training-executor`、`apps/_shared`、`packages/sdk`）

- **模块与函数**：`snake_case`
- **类与异常**：`PascalCase`
- **模块级常量**：`UPPER_SNAKE_CASE`
- **测试文件**：`tests/test_<topic>.py`

### TypeScript（`apps/web`、`docs-site`）

- **React 组件文件与组件名**：`PascalCase`（如 `Button.tsx`、`ProjectList.tsx`）
- **Hooks 与工具函数文件**：`camelCase`（如 `useUrlFilterState.ts`、`formatDate.ts`）
- **测试文件**：`<name>.test.ts(x)` 或 `__tests__/<name>.test.tsx`
- **类型声明**：优先使用 `type`，仅在需要声明合并（Declaration Merging）时使用 `interface`
- **路径别名**：`apps/web` 内统一使用 `@/` 指向 `apps/web/src/`

---

## 注释、文档字符串与错误处理

- **注释原则**：不要编写复述代码字面逻辑的注释；注释应聚焦于非显而易见的设计原因（WHY）、外部协议约束与并发不变量。不在源码注释或文档正文中添加 `vX.Y.Z` 版本溯源标记（历史演进统一记录在 Git 历史、`CHANGELOG.md` 或 ADR 中）。
- **公开 API Docstring**：FastAPI 路由函数与 SDK 导出函数必须编写清晰的 docstring，以便自动同步至 OpenAPI Schema 与 IDE 提示。
- **错误处理**：
  - 后端统一抛出 `HTTPException` 或领域异常，由全局异常处理器格式化响应；
  - 前端通过 TanStack Query 的错误边界与 Toast/CopyableError 统一呈现，严禁使用空 `catch` 吞掉异常。

---

## Linting 与格式化工具链

- **Python**：全仓第一方 Python 代码统一使用 **Ruff**（根配置位于 `ruff.toml`；`vendor` 目录保留上游格式）。
- **前端与文档**：TypeScript、JavaScript、CSS、Markdown、JSON、YAML 统一使用根级 **Prettier** 配置。
- **前端静态检查**：使用 ESLint Flat Config（`apps/web/eslint.config.js`）与设计令牌门禁（`pnpm --filter @anno/web lint:css-tokens`）。
- **一键命令**：`pnpm format` 自动修复全仓格式，`pnpm format:check` 以只读模式执行与 CI 相同的格式校验。

## 工程克制原则（不引入的冗余工具）

- **不引入 Changelog 自动生成工具**（如 changesets / standard-version）：`CHANGELOG.md` 遵循 Keep a Changelog 规范人工维护，聚焦真实用户影响。
- **不引入额外 Monorepo 编排壳层**（如 Nx / Turborepo）：直接由 `pnpm workspace` 管理多包依赖与脚本调度。
