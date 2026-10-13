import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RequireProjectAccess } from "./RequireProjectAccess";

const mockAccess = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useProjectAccess", () => ({ useProjectAccess: mockAccess }));

function state(overrides: Record<string, unknown> = {}) {
  return {
    access: undefined,
    capabilities: new Set<string>(),
    hasCapability: () => false,
    projectRole: null,
    membershipVersion: null,
    isManager: false,
    isPending: false,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    ...overrides,
  };
}

function renderGuard() {
  return render(
    <MemoryRouter initialEntries={["/projects/p1/annotate"]}>
      <Routes>
        <Route
          path="/projects/:id/annotate"
          element={
            <RequireProjectAccess capability="annotation.write">
              <div data-testid="editor" />
            </RequireProjectAccess>
          }
        />
        <Route path="/dashboard" element={<div data-testid="dashboard" />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RequireProjectAccess", () => {
  beforeEach(() => mockAccess.mockReset());

  it("加载中不挂载可编辑内容", () => {
    mockAccess.mockReturnValue(state({ isLoading: true }));
    renderGuard();
    expect(screen.queryByTestId("editor")).not.toBeInTheDocument();
    expect(screen.getByText(/正在校验项目权限/)).toBeInTheDocument();
  });

  it("具备能力时挂载内容", () => {
    mockAccess.mockReturnValue(state({ hasCapability: () => true }));
    renderGuard();
    expect(screen.getByTestId("editor")).toBeInTheDocument();
  });

  it("缺少能力时重定向且不挂载内容", () => {
    mockAccess.mockReturnValue(state({ hasCapability: () => false }));
    renderGuard();
    expect(screen.queryByTestId("editor")).not.toBeInTheDocument();
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
  });

  it("访问解析失败时不挂载内容", () => {
    mockAccess.mockReturnValue(state({ isError: true, error: new Error("x") }));
    renderGuard();
    expect(screen.queryByTestId("editor")).not.toBeInTheDocument();
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
  });
});
