---
title: 第一个贡献
audience: [dev]
type: tutorial
since: v0.9.0
status: stable
last_reviewed: 2026-05-09
---

# 第一个贡献：改文案 → 跑测试 → 提 PR

本文引导你从零完成第一个贡献：修改一处前端文案，跑通测试，提交 PR。适合刚克隆仓库的新贡献者。

## 前提

- 本地环境已跑通（参见 [本地开发](./local-dev)）
- 已 fork 仓库并创建工作分支

## Step 1：选择一处文案修改

找一处前端界面上的中文文案（比如按钮、提示语）：

```
apps/web/src/
```

例如修改 `apps/web/src/pages/Workbench/shell/ContextToolbar.tsx` 中的某个 tooltip 文字。

## Step 2：跑前端测试

```bash
cd apps/web
pnpm test          # 单元测试（vitest）
pnpm test:coverage # 带覆盖率
```

确认测试通过（或你的改动不影响现有测试）。

## Step 3：本地预览

```bash
pnpm dev           # 启动 Vite dev server（localhost:3000）
```

在浏览器确认文案已更新。

## Step 4：提交与推送

遵守 [提交规范](/dev/reference/conventions)：

```bash
git add apps/web/src/pages/Workbench/shell/ContextToolbar.tsx
git commit -m "feat(workbench): 更新 ContextToolbar tooltip 文案"
git push origin your-branch
```

## Step 5：创建 PR

1. 在 GitHub 点击 **Compare & pull request**
2. 填写：改了什么、为什么改、如何验证
3. 等待 CI（lint + type-check + vitest）通过
4. 请求 Review

## 常见卡点

| 问题                       | 处理                                 |
| -------------------------- | ------------------------------------ |
| `pnpm test` 报 import 错误 | `pnpm install` 后重试                |
| TypeScript 类型报错        | `pnpm type-check` 查看完整错误       |
| CI 失败但本地通过          | 检查 Node 版本（需 ≥22）；查 CI 日志 |

## 下一步

- [新增 API 端点](/dev/how-to/add-api-endpoint)
- [新增前端页面](/dev/how-to/add-page)
- [架构地图](/dev/concepts/)
