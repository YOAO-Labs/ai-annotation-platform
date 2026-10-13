import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { useEffect } from "react";

const mockUseProjectPage = vi.fn();
const mockUseProjectStats = vi.fn();
const mockPushToast = vi.fn();
const mockBuildWorkbenchUrl = vi.hoisted(() => vi.fn((id: string) => `/workbench/${id}`));

vi.mock("@/hooks/useProjects", () => ({
  useProjectPage: (...args: unknown[]) => mockUseProjectPage(...args),
  useProjectStats: () => mockUseProjectStats(),
}));

vi.mock("@/components/projects/CreateProjectWizard", () => ({
  CreateProjectWizard: ({ open }: { open?: boolean }) =>
    open ? <div data-testid="cp-wizard" /> : null,
}));

vi.mock("@/components/datasets/ImportDatasetWizard", () => ({
  ImportDatasetWizard: ({ open }: { open?: boolean }) =>
    open ? <div data-testid="id-wizard" /> : null,
}));

vi.mock("./ProjectFilterControl", () => ({ ProjectFilterControl: () => null }));
vi.mock("./ProjectFilterSummary", () => ({ ProjectFilterSummary: () => null }));

vi.mock("./ExportModal", () => ({
  ExportModal: () => null,
}));

vi.mock("@/utils/workbenchNavigation", () => ({
  buildProjectEntryUrl: (id: string) => mockBuildWorkbenchUrl(id),
  currentWorkbenchReturnTo: () => "/dashboard",
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: typeof mockPushToast }) => T) =>
      sel({ push: mockPushToast }),
  };
});

function pageResult(items: unknown[] = [], total = items.length, page = 1, pageSize = 20) {
  return { items, total, page, page_size: pageSize, pages: Math.ceil(total / pageSize) };
}

import { AdminProjectsDashboard } from "./AdminProjectsDashboard";

function renderUI(initialPath = "/dashboard", navigateTo?: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      {navigateTo ? <NavigateOnMount to={navigateTo} /> : null}
      <LocationProbe />
      <AdminProjectsDashboard />
    </MemoryRouter>,
  );
}

function NavigateOnMount({ to }: { to: string }) {
  const navigate = useNavigate();
  useEffect(() => {
    navigate(to, { replace: true });
  }, [navigate, to]);
  return null;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

describe("AdminProjectsDashboard", () => {
  beforeEach(() => {
    mockUseProjectPage.mockReset();
    mockUseProjectStats.mockReset();
    mockPushToast.mockReset();
    mockBuildWorkbenchUrl.mockClear();
    mockUseProjectPage.mockReturnValue({ data: pageResult([]), isLoading: false });
    mockUseProjectStats.mockReturnValue({ data: undefined });
  });

  it("renders a super-admin project management surface", () => {
    renderUI();
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "全部项目" })).toBeInTheDocument();
    expect(screen.queryByText("我的项目")).not.toBeInTheDocument();
  });

  it("uses real stat series instead of mocked trend hints", () => {
    mockUseProjectStats.mockReturnValue({
      data: {
        total_data: 97,
        completed: 0,
        ai_rate: 0,
        pending_review: 0,
        total_data_series: [40, 44, 48, 50, 55, 60, 66, 70, 76, 82, 90, 97],
        completed_series: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ai_rate_series: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        pending_review_series: [2, 2, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0],
      },
    });
    renderUI();
    expect(screen.getByText("97")).toBeInTheDocument();
    expect(screen.getByText("AI 派生标注率")).toBeInTheDocument();
    expect(screen.getAllByText("近 12 周").length).toBeGreaterThan(0);
    expect(screen.queryByText("↑ 12%")).not.toBeInTheDocument();
  });

  it("does not open projects from row text; the open button still enters workbench", () => {
    mockUseProjectPage.mockReturnValue({
      data: pageResult([
        {
          id: "p1",
          display_id: "P-1",
          name: "Demo项目",
          type_label: "图像 · 目标检测",
          type_key: "image-det",
          data_type: "image",
          owner_id: "u1",
          owner_name: "Alice",
          member_count: 2,
          status: "in_progress",
          total_tasks: 10,
          completed_tasks: 5,
          review_tasks: 0,
          in_progress_tasks: 0,
          ai_enabled: false,
        },
      ]),
      isLoading: false,
    });
    renderUI();
    fireEvent.click(screen.getByText("Demo项目"));
    expect(mockBuildWorkbenchUrl).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /打开/ }));
    expect(mockBuildWorkbenchUrl).toHaveBeenCalledWith("p1");
  });

  it("restores status from the URL and tab changes the query", () => {
    renderUI("/dashboard?status=pending_review&new=1&from=p1&layout=grid");
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "pending_review" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "已完成" }));
    expect(screen.getByTestId("location-search").textContent).toContain("status=completed");
    expect(screen.getByTestId("location-search").textContent).toContain("new=1");
    expect(screen.getByTestId("location-search").textContent).toContain("from=p1");
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "completed" }),
    );
  });

  it("applies q and status together after external URL navigation", async () => {
    renderUI("/dashboard?q=old&status=in_progress", "/dashboard?q=new&status=pending_review");
    await waitFor(() => {
      expect(mockUseProjectPage).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "pending_review", search: "new" }),
      );
    });
    expect(
      mockUseProjectPage.mock.calls.some(
        ([params]) => params.status === "pending_review" && params.search === "old",
      ),
    ).toBe(false);
  });

  it("keeps the pending search draft when the wizard opens before debounce", async () => {
    vi.useFakeTimers();
    renderUI();
    const input = screen.getByPlaceholderText("搜索项目...");
    fireEvent.change(input, { target: { value: "car" } });
    fireEvent.click(screen.getByTestId("new-project-btn"));
    expect(screen.getByTestId("cp-wizard")).toBeInTheDocument();
    expect(input).toHaveValue("car");
    expect(screen.getByTestId("location-search").textContent).toContain("new=1");

    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(input).toHaveValue("car");
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(expect.objectContaining({ search: "car" }));
    const search = screen.getByTestId("location-search").textContent ?? "";
    expect(search).toContain("q=car");
    expect(search).toContain("new=1");
  });
});
