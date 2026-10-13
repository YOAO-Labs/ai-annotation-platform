/**
 * ModelMarketPage 主 TAB 行为单测（plan §3 / §5 · 阶段一）。
 *
 * 覆盖：默认目录页、深链恢复、未知枚举规范化、项目管理员角色回退（不发超管
 * 请求、不渲染运行时面板）、显式 TAB 切换 push 浏览器历史、旧书签
 * ?tab=failed 重定向、父页面全局统计卡移除后的骨架。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";
import { type ReactNode } from "react";

let mockRole = "super_admin";
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: mockRole }),
}));

vi.mock("./CapabilityCatalogPanel", () => ({
  CapabilityCatalogPanel: () => <div data-testid="catalog-panel">目录面板</div>,
}));
vi.mock("./RuntimeObservePanel", () => ({
  RuntimeObservePanel: () => <div data-testid="runtime-panel">运行时面板</div>,
}));
vi.mock("./RegisteredBackendsTab", () => ({
  RegisteredBackendsTab: () => <div data-testid="registry-panel">注册面板</div>,
}));

import { ModelMarketPage } from "./ModelMarketPage";

function LocationProbe(): ReactNode {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <div
      data-testid="location-probe"
      data-pathname={location.pathname}
      data-search={location.search}
      data-navigation-type={navigationType}
    />
  );
}

function renderPage(initialUrl = "/model-market"): void {
  render(
    <MemoryRouter initialEntries={[initialUrl]}>
      <Routes>
        <Route path="/model-market" element={<ModelMarketPage />} />
        <Route path="/ai-pre/jobs" element={<div data-testid="ai-pre-jobs" />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("ModelMarketPage 主 TAB（阶段一骨架）", () => {
  beforeEach(() => {
    mockRole = "super_admin";
  });

  it("默认渲染能力目录 + 一个 H1，无全局统计卡（plan §3）", () => {
    renderPage();
    expect(screen.getByTestId("catalog-panel")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "模型市场" })).toBeInTheDocument();
    // 旧三张全局统计卡已移除。
    expect(screen.queryByText("模型条目")).not.toBeInTheDocument();
    expect(screen.queryByText("使用项目")).not.toBeInTheDocument();
  });

  it("?tab=registry 深链恢复注册管理", () => {
    renderPage("/model-market?tab=registry");
    expect(screen.getByTestId("registry-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("catalog-panel")).not.toBeInTheDocument();
  });

  it("未知 tab 枚举回退目录并规范化 URL（删除 tab 键）", async () => {
    renderPage("/model-market?tab=bogus");
    expect(screen.getByTestId("catalog-panel")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location-probe").dataset.search).toBe("");
    });
  });

  it("显式 TAB 切换写入 URL 并 push 浏览器历史（plan §5）", async () => {
    const user = userEvent.setup();
    renderPage("/model-market");
    await user.click(screen.getByRole("tab", { name: /注册管理/ }));
    expect(screen.getByTestId("registry-panel")).toBeInTheDocument();
    expect(screen.getByTestId("location-probe").dataset.search).toBe("?tab=registry");
    // PUSH 而非 REPLACE——返回按钮可回到上一个主 TAB。
    expect(screen.getByTestId("location-probe").dataset.navigationType).toBe("PUSH");
    await user.click(screen.getByRole("tab", { name: /能力目录/ }));
    expect(screen.getByTestId("location-probe").dataset.search).toBe("");
  });

  it("?tab=runtime 深链对超管可用", () => {
    renderPage("/model-market?tab=runtime");
    expect(screen.getByTestId("runtime-panel")).toBeInTheDocument();
  });

  describe("项目管理员", () => {
    beforeEach(() => {
      mockRole = "project_admin";
    });

    it("?tab=runtime 回退能力目录并规范化 URL，不渲染运行时面板", async () => {
      renderPage("/model-market?tab=runtime");
      expect(screen.queryByTestId("runtime-panel")).not.toBeInTheDocument();
      expect(screen.getByTestId("catalog-panel")).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.getByTestId("location-probe").dataset.search).toBe("");
      });
    });

    it("主导航只有能力目录 + 注册管理两个 TAB", () => {
      renderPage("/model-market?tab=registry");
      expect(screen.getByRole("tab", { name: /能力目录/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /注册管理/ })).toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /运行时观测/ })).not.toBeInTheDocument();
    });
  });

  it("旧书签 ?tab=failed 重定向到 /ai-pre/jobs?status=failed", async () => {
    renderPage("/model-market?tab=failed");
    await waitFor(() => {
      expect(screen.getByTestId("ai-pre-jobs")).toBeInTheDocument();
    });
  });
});
