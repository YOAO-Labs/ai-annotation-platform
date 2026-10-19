---
title: 新增前端页面
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# How-to：新增前端页面

本指南演示如何在 `apps/web` 中新增一个遵循 React Router v6 懒加载路由、TanStack Query 数据获取与设计系统规范的前端页面。

## 目录结构约定

```text
apps/web/src/pages/<PageName>/
├── index.tsx              # 页面主入口（默认导出，供 React.lazy 按需加载）
├── components/            # 页面专属子组件
├── hooks/                 # 页面专属自定义 Hooks（useXxx）
├── state/                 # 局部 Zustand store（可选，仅在跨多层复杂交互时启用）
└── __tests__/
    └── <PageName>.test.tsx
```

## 1. 编写入口组件

服务端状态统一由 TanStack Query 管理，API 请求调用封装在 `apps/web/src/api/` 下：

```tsx
// apps/web/src/pages/Widgets/index.tsx
import { useQuery } from "@tanstack/react-query";
import { listWidgets } from "@/api/widgets";

export default function WidgetsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["widgets"],
    queryFn: listWidgets,
  });

  if (isLoading) {
    return <div className="p-6 text-sm text-muted-foreground">加载中...</div>;
  }

  return (
    <div className="p-6">
      <h1 className="text-md font-semibold text-foreground">组件列表</h1>
      <ul className="mt-4 space-y-2">
        {data?.items.map((w) => (
          <li key={w.id} className="rounded border border-border bg-card p-3 text-sm">
            {w.name}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

## 2. 注册路由与侧边栏导航

前端采用 **React Router v6**（`apps/web/src/App.tsx`）管理路由，并按需配合 `RequireAuth` / `RequireSuperAdmin` 守卫与 `AppShell` 布局：

1. **在 `apps/web/src/App.tsx` 中声明懒加载路由**：

   ```tsx
   const WidgetsPage = lazy(() => import("@/pages/Widgets"));

   // 在对应权限分组（如 RequireAuth + AppShell）下添加 <Route>：
   <Route path="/widgets" element={<WidgetsPage />} />;
   ```

2. **在侧边栏添加导航入口（如需）**：
   编辑 `apps/web/src/components/shell/Sidebar.tsx`，在对应分组的导航描述符数组中补充路由路径、名称与图标（复用 `components/ui/Icon.tsx`）。

## 3. 编写组件单元测试

使用 Vitest + Testing Library + MSW（Mock Service Worker）编写测试，包裹 `MemoryRouter` 与 `QueryClientProvider`：

```tsx
// apps/web/src/pages/Widgets/__tests__/Widgets.test.tsx
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import WidgetsPage from "../index";

const wrap = (ui: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>
  );
};

it("空态：渲染空列表", async () => {
  server.use(http.get("*/api/v1/widgets", () => HttpResponse.json({ items: [] })));
  render(wrap(<WidgetsPage />));
  expect(await screen.findByRole("list")).toBeEmptyDOMElement();
});

it("有数据：渲染条目", async () => {
  server.use(
    http.get("*/api/v1/widgets", () => HttpResponse.json({ items: [{ id: "1", name: "demo" }] })),
  );
  render(wrap(<WidgetsPage />));
  expect(await screen.findByText("demo")).toBeInTheDocument();
});
```

## 4. 样式与设计令牌检查

- 优先使用 `apps/web/src/styles/shadcn.css` 驱动的语义化 Tailwind 类名（如 `bg-background`、`bg-card`、`text-foreground`、`border-border`、`text-status-danger bg-status-danger-soft`）。
- 局部复杂布局可使用同名 CSS Module（`Widgets.module.css`），内部仅消费 `--sc-*` 变量，严禁使用遗留 `--color-*`、裸色值或 JSX <code v-pre>style={{...}}</code>。
- 完成后运行样式门禁验证：
  ```bash
  pnpm --filter @anno/web lint:css-tokens
  pnpm --filter @anno/web test
  ```

详见[设计系统参考](../reference/design-system)。
