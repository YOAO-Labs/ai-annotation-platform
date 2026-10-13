import { useState, type ComponentType, type ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectPageParams, ProjectResponse } from "@/api/projects";

const mockPage = vi.fn();
const retry = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useProjectPage: (params: ProjectPageParams) => mockPage(params),
  useProjectStats: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useAudit", () => ({ useAuditLogs: () => ({ data: { items: [] } }) }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({ user: { id: "u1", role: "project_admin" } }),
}));
vi.mock("@/components/guards/Can", () => ({
  Can: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/projects/CreateProjectWizard", () => ({ CreateProjectWizard: () => null }));
vi.mock("@/components/datasets/ImportDatasetWizard", () => ({ ImportDatasetWizard: () => null }));
vi.mock("./ExportModal", () => ({ ExportModal: () => null }));
vi.mock("./ProjectFilterSummary", () => ({ ProjectFilterSummary: () => null }));
vi.mock("./ProjectFilterControl", () => ({
  ProjectFilterControl: ({ onApply }: { onApply: (filters: unknown) => void }) => (
    <button onClick={() => onApply({ data_type: ["video"] })}>测试视频筛选</button>
  ),
}));

import { AdminProjectsDashboard } from "./AdminProjectsDashboard";
import { DashboardPage } from "./DashboardPage";

const projects = Array.from({ length: 41 }, (_, index) => ({
  id: `p${index + 1}`,
  display_id: `P-${index + 1}`,
  name: `Project ${String(index + 1).padStart(2, "0")}`,
  type_key: "image-det",
  data_type: "image",
  owner_id: "u1",
  owner_name: "Owner",
  status: "in_progress",
  member_count: 0,
  total_tasks: 0,
  completed_tasks: 0,
  review_tasks: 0,
  ai_enabled: false,
})) as ProjectResponse[];

function result(params: ProjectPageParams, total = projects.length) {
  const matching = projects.filter((p) => !params.search || p.name.includes(params.search));
  const count = params.search ? matching.length : total;
  return {
    data: {
      items: matching.slice((params.page - 1) * params.page_size, params.page * params.page_size),
      total: count,
      page: params.page,
      page_size: params.page_size,
      pages: Math.ceil(count / params.page_size),
    },
    isLoading: false,
    isFetching: false,
    isError: false,
    refetch: retry,
  };
}

function Harness({ Page }: { Page: ComponentType }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [, rerender] = useState(0);
  return (
    <>
      <output data-testid="url">{location.search}</output>
      <button onClick={() => navigate(-1)}>测试后退</button>
      <button onClick={() => navigate("/dashboard?q=Project&page=3")}>测试外部导航</button>
      <button onClick={() => rerender((value) => value + 1)}>测试刷新结果</button>
      <Page />
    </>
  );
}

function renderPage(Page: ComponentType, path = "/dashboard?page=2") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Harness Page={Page} />
    </MemoryRouter>,
  );
}

describe.each([
  ["super admin", AdminProjectsDashboard],
  ["project admin", DashboardPage],
] as const)("%s project pagination", (_role, Page) => {
  beforeEach(() => {
    mockPage.mockReset().mockImplementation((params: ProjectPageParams) => result(params));
    retry.mockClear();
  });
  afterEach(() => vi.useRealTimers());

  it("shares page state between list/grid and restores history; size changes reset the page", async () => {
    renderPage(Page, "/dashboard?page=2&from=p1&keep=1");
    expect(screen.getByText("共 41 个项目 · 第 2 / 3 页")).toBeInTheDocument();
    expect(screen.getByText("Project 21")).toBeInTheDocument();
    expect(screen.queryByText("Project 01")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("切换到网格视图"));
    expect(screen.getByTestId("url")).toHaveTextContent("page=2");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByText("Project 41")).toBeInTheDocument();
    expect(screen.queryByText("Project 21")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "测试后退" }));
    await waitFor(() => expect(screen.getByTestId("url")).toHaveTextContent("page=2"));
    expect(screen.getByTestId("url")).toHaveTextContent("layout=grid");
    fireEvent.change(screen.getByLabelText("每页项目数"), { target: { value: "50" } });
    expect(mockPage).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, page_size: 50 }));
    expect(screen.getByTestId("url")).toHaveTextContent("page_size=50");
    expect(screen.getByTestId("url")).toHaveTextContent("from=p1");
    expect(screen.getByTestId("url")).toHaveTextContent("keep=1");
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });

  it("applies search and first page atomically, then resets pagination for advanced filters", async () => {
    vi.useFakeTimers();
    renderPage(Page);
    fireEvent.change(screen.getByPlaceholderText("搜索项目..."), {
      target: { value: "Project 01" },
    });
    expect(mockPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2, search: undefined }),
    );
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(mockPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, search: "Project 01" }),
    );
    expect(mockPage.mock.calls.some(([params]) => params.page === 1 && !params.search)).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "测试视频筛选" }));
    expect(mockPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, data_type: ["video"] }),
    );
  });

  it("discards pending search when external navigation restores the same q with another page", async () => {
    vi.useFakeTimers();
    renderPage(Page, "/dashboard?q=Project&page=2");
    fireEvent.change(screen.getByPlaceholderText("搜索项目..."), {
      target: { value: "obsolete draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "测试外部导航" }));
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(screen.getByPlaceholderText("搜索项目...")).toHaveValue("Project");
    expect(mockPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 3, search: "Project" }),
    );
    expect(screen.getByTestId("url")).toHaveTextContent("page=3");
    expect(screen.getByTestId("url")).not.toHaveTextContent("obsolete");
  });

  it("waits for refetch before correcting an out-of-range cached page", async () => {
    let fetching = true;
    let total = 20;
    mockPage.mockImplementation((params: ProjectPageParams) => ({
      ...result(params, total),
      isFetching: fetching,
    }));
    renderPage(Page);
    expect(screen.getByTestId("url")).toHaveTextContent("page=2");
    total = 41;
    fetching = false;
    fireEvent.click(screen.getByRole("button", { name: "测试刷新结果" }));
    expect(screen.getByTestId("url")).toHaveTextContent("page=2");
    expect(screen.getByText("共 41 个项目 · 第 2 / 3 页")).toBeInTheDocument();
    total = 20;
    fireEvent.click(screen.getByRole("button", { name: "测试刷新结果" }));
    await waitFor(() =>
      expect(mockPage).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1 })),
    );
    expect(screen.getByText("共 20 个项目 · 第 1 / 1 页")).toBeInTheDocument();
  });

  it("keeps pagination usable and exposes retry when cached data fails to refresh", () => {
    mockPage.mockImplementation((params: ProjectPageParams) => ({
      ...result(params),
      isError: true,
    }));
    renderPage(Page);
    expect(screen.getByRole("alert")).toHaveTextContent("刷新失败");
    expect(screen.queryByText("正在加载项目…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "重新加载" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
