---
audience: [dev]
type: explanation
since: v0.1.0
status: stable
last_reviewed: 2026-05-09
---

# React useState 初始化器的 TDZ 陷阱

## 症状

打开某页（v0.9.5 时是 `SamTextPanel`），整页白屏 + console：

```
ReferenceError: Cannot access 'projectQ' before initialization
```

## 复现

```tsx
function SamTextPanel() {
  const [draft, setDraft] = useState(() => {
    // ⚠️ 在 projectQ 声明前就读它
    return loadDraft(projectQ.data?.id);
  });

  const projectQ = useProjectQuery(); // 这一行还没跑到
  // ...
}
```

`useState` 的**初始化器在组件首次渲染时同步执行**，此时下方 `const projectQ = ...` 还没走到，命中 TDZ（temporal dead zone）。

## 根因

JavaScript 的 `let`/`const` 在声明前访问会抛 `ReferenceError`（不像 `var` 拿到 `undefined`）。React `useState(initializer)` 的 initializer 在函数体执行到这一行时立即调用——但函数体内部声明的变量，要按词法顺序「执行到」才进入作用域。

容易踩的根本原因是：lazy initializer 看起来像「等需要时才跑」，开发者潜意识把它当成回调延迟执行。

## 修复

把被引用的声明前置，或改用 `useEffect` 在 mount 后初始化：

```tsx
// ✅ 方案 A：先声明依赖
const projectQ = useProjectQuery();
const [draft, setDraft] = useState(() => loadDraft(projectQ.data?.id));

// ✅ 方案 B：mount 后再载入
const [draft, setDraft] = useState<Draft | null>(null);
useEffect(() => {
  setDraft(loadDraft(projectQ.data?.id));
}, [projectQ.data?.id]);
```

## 教训

- `useState` 的 lazy initializer 不是回调，是同步求值。
- ESLint 的 `no-use-before-define` 默认对函数声明放行、对变量声明严格——但 hook 依赖的语义它检不出来，需要人工 review。
- 排查白屏先看 console 第一条 ReferenceError，TDZ 通常告诉你确切变量名。

## 相关

- commit: `8949455` fix(v0.9.5): SamTextPanel TDZ
- 代码：`apps/web/src/pages/Workbench/shell/AIInspectorPanel.tsx`（当时的 `SamTextPanel` 组件内嵌于此文件；该组件现已移除）
