---
title: 迁移内联样式到 CSS Modules
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-22
---

# 把页面级 inline style 迁到 CSS modules

::: tip
现有 TSX 已由 lint guard 约束，新增样式使用 Tailwind 语义工具类或 CSS modules（仅消费 `--sc-*` 令牌）；不要重新引入 JSX `style=` / `<style>`。
:::

## 为什么要迁

[`CSP style-src nonce 收紧`](../../ops/security/) 的前置依赖是全站 <code v-pre>style={{...}}</code> 重构。生产 CSP 只接受同源样式表和带 nonce 的 HTML `<style>` 标签。

后续新增/回归样式必须继续遵守这个约束：用 Tailwind 语义类或 CSS modules 切换；真正动态值（如画布坐标或分割条宽度）用 ref 同步 CSS custom properties，不能重新引入 JSX `style=`。

## 步骤

### 1. 新建 `.module.css` 同名文件

与 TSX 同目录，命名 `<Component>.module.css`：

```text
sections/
  BatchesSection.tsx
  BatchesSection.module.css   ← 新增
```

### 2. 类名与设计令牌约定

- **静态布局类**：语义命名（`.toolbar` / `.viewToggle` / `.tableHeadRow`），不用 `bs-` 前缀（CSS modules 已 scope）。
- **设计令牌**：CSS Modules 内必须读取 `apps/web/src/styles/shadcn.css` 暴露的 `--sc-*` 变量，严禁读取已废弃的 `--color-*` 变量。
- **状态变体**：BEM-like 后缀（`.viewToggleButton` + `.viewToggleButtonActive`），TSX 端用 `cn(styles.x, isActive && styles.xActive)` 拼接。
- **Button 覆盖类**：优先使用组件现有 `variant` / `size` / `className` 能力；不要为了覆盖基础 UI 再引入 inline style。
- **真正动态值**：用 CSS custom property，并通过 `useElementStyle` / ref 写入 DOM style，避免 TSX JSX 上出现 `style=`。

### 3. 在 TSX 里 `import styles from "./X.module.css"` 并把 <code v-pre>style={{...}}</code> 替换成 `className={styles.xxx}`

简单工具函数（或复用 `@/lib/utils` 中的 `cn`）：

```tsx
function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}
```

### 4. 加 ESLint 文件级 guard

[`apps/web/eslint.config.js`](../../../apps/web/eslint.config.js) 已对 `src/**/*.tsx` 启用 inline-style guard：

```js
{
  files: ["src/**/*.tsx"],
  rules: {
    "no-restricted-syntax": [
      "error",
      {
        selector: "JSXAttribute[name.name='style']",
        message: "本文件已迁到 CSS modules。新增样式请加 CSS class; ...",
      },
    ],
  },
},
```

新增 TSX 文件会自动继承该约束；如确实需要动态样式，先抽 CSS class / CSS var，再用 ref 写入。

### 5. 验证

- `pnpm --filter @anno/web typecheck` 干净。
- `pnpm --filter @anno/web lint:css-tokens` 与 `pnpm --filter @anno/web lint` 干净。
- `pnpm --filter @anno/web test` 该模块既有测试全过（无视觉回归）。
- `rg -n --glob '*.tsx' "style\\s*=|<style" apps/web/src` 无输出。
- 本地预览实际渲染：进入该页面关键交互（创建 / 批量操作 / 看板切换 / Modal）确认亮/暗主题表现一致。

## 已知陷阱

- **CSS modules 类名是 hashed**：浏览器 inspect 看到 `_toolbarTitle_1wok9_22`，devtools 里 CSS modules 列里看原始名。
- **`color-mix` 兼容**：迁过来后保留 `color-mix(in oklab, ...)` 用法，与既有代码一致。
- **Pre-existing 类名冲突**：CSS modules 自动 scope，但全局 utility 类（`.mono` / `.btn`）不要在 module 里重定义，照旧用全局 className。

## 试点参考

完整对照：

- 现行示例：[`AIPreAnnotateJobsPage.tsx`](../../../apps/web/src/pages/AIPreAnnotate/AIPreAnnotateJobsPage.tsx) + [`AIPreAnnotateJobsPage.module.css`](../../../apps/web/src/pages/AIPreAnnotate/AIPreAnnotateJobsPage.module.css)。
- 同目录组件继续采用同名 `.module.css`；批量修改后用前端 lint 的 inline-style guard 与 `lint:css-tokens` 确认没有重新引入内联样式或遗留令牌。
