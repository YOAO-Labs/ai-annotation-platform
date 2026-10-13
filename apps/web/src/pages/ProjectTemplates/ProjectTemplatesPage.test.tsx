/**
 * v0.10.14 · E2 · ProjectTemplatesPage 单测.
 *
 * 覆盖:
 * - 加载 / 空态
 * - tab 切换调用不同 scope 过滤
 * - 删除流程: confirm + mutation 触发
 * - "应用" 打开 Wizard 并透传 templateId
 * - canEdit 控制操作按钮可见性
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ProjectTemplateOut } from "@/api/projectTemplates";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";

const mockUseProjectTemplates = vi.fn();
const mockDuplicate = vi.fn();
const mockRemove = vi.fn();

vi.mock("@/hooks/useProjectTemplates", () => ({
  useProjectTemplates: (params: unknown) => mockUseProjectTemplates(params),
  useDeleteProjectTemplate: () => ({
    mutate: mockRemove,
    isPending: false,
  }),
  useDuplicateProjectTemplate: () => ({
    mutate: mockDuplicate,
    isPending: false,
  }),
  useCreateProjectTemplate: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateProjectTemplate: () => ({ mutate: vi.fn(), isPending: false }),
}));

const mockAuthStore = vi.fn();
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (sel: (s: { user: { id: string; role: string } }) => unknown) =>
    sel(mockAuthStore()),
}));

vi.mock("@/hooks/useProjects", () => ({
  useProjects: () => ({ data: [], isLoading: false }),
}));

vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({
    role: "super_admin",
    canAccessPage: () => true,
    hasPermission: () => true,
    hasAnyPermission: () => true,
    allowedPages: [],
  }),
}));

// CreateProjectWizard lazy-load 比较重, 直接 stub.
vi.mock("@/components/projects/CreateProjectWizard", () => ({
  CreateProjectWizard: ({ open, templateId }: { open: boolean; templateId?: string }) =>
    open ? <div data-testid="cp-wizard">{templateId}</div> : null,
}));

import { ProjectTemplatesPage } from "./ProjectTemplatesPage";

function makeTemplate(overrides: Partial<ProjectTemplateOut> = {}): ProjectTemplateOut {
  return {
    id: overrides.id ?? "t-1",
    display_id: overrides.display_id ?? "PT-1",
    name: overrides.name ?? "模板 A",
    description: overrides.description ?? null,
    type_label: "图像-检测",
    type_key: "image-det",
    classes: overrides.classes ?? ["car", "person"],
    classes_config: {},
    attribute_schema: { fields: [] },
    label_config: {},
    ai_enabled: false,
    sampling: "sequence",
    maximum_annotations: 1,
    show_overlap_first: false,
    iou_dedup_threshold: 0.7,
    box_threshold: 0.35,
    text_threshold: 0.25,
    rendering_config: {},
    annotation_guide: null,
    scope: overrides.scope ?? "private",
    organization_id: null,
    created_by: overrides.created_by ?? "user-1",
    created_by_name: "creator",
    source_project_id: null,
    usage_count: overrides.usage_count ?? 0,
    created_at: "2026-05-18T00:00:00Z",
    updated_at: "2026-05-18T00:00:00Z",
    ...overrides,
  };
}

function renderUI(initialPath = "/project-templates", navigateTo?: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        {navigateTo ? <NavigateOnMount to={navigateTo} /> : null}
        <LocationProbe />
        <ProjectTemplatesPage />
        <DecisionDialogHost />
      </MemoryRouter>
    </QueryClientProvider>,
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

describe("ProjectTemplatesPage", () => {
  beforeEach(() => {
    mockUseProjectTemplates.mockReset();
    mockDuplicate.mockReset();
    mockRemove.mockReset();
    mockAuthStore.mockReturnValue({
      user: { id: "user-1", role: "super_admin" },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("加载中 → 显示 loading 文案", () => {
    mockUseProjectTemplates.mockReturnValue({ data: [], isLoading: true });
    renderUI();
    expect(screen.getByText("加载中…")).toBeInTheDocument();
  });

  it("空列表 → 显示空态提示 + 新建提示", () => {
    mockUseProjectTemplates.mockReturnValue({ data: [], isLoading: false });
    renderUI();
    expect(screen.getByText(/暂无模板/)).toBeInTheDocument();
    expect(screen.getByText(/点击「新建模板」开始/)).toBeInTheDocument();
  });

  it("列表非空 → 渲染卡片", () => {
    mockUseProjectTemplates.mockReturnValue({
      data: [makeTemplate({ id: "t-1", name: "模板 A", usage_count: 3 })],
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("模板 A")).toBeInTheDocument();
    expect(screen.getByText(/使用 3 次/)).toBeInTheDocument();
  });

  it("点击删除 → 决策对话框确认 + 调用 remove mutation", async () => {
    mockUseProjectTemplates.mockReturnValue({
      data: [makeTemplate({ id: "t-1", created_by: "user-1", usage_count: 3 })],
      isLoading: false,
    });
    renderUI();
    fireEvent.click(screen.getByTestId("template-delete-t-1"));
    // 决策对话框先行,确认前不触发 mutation
    const dialog = await screen.findByRole("alertdialog");
    expect(screen.getByText("删除模板")).toBeInTheDocument();
    expect(dialog.textContent).toContain("模板");
    expect(dialog.textContent).toContain("已使用 3 次");
    expect(mockRemove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => expect(mockRemove).toHaveBeenCalledWith("t-1", expect.any(Object)));
  });

  it("点击应用 → 打开 Wizard 并透传 templateId", async () => {
    mockUseProjectTemplates.mockReturnValue({
      data: [makeTemplate({ id: "t-99" })],
      isLoading: false,
    });
    renderUI();
    fireEvent.click(screen.getByTestId("template-apply-t-99"));
    // CreateProjectWizard 走 React.lazy, 等待 Suspense 解析.
    const wiz = await screen.findByTestId("cp-wizard");
    expect(wiz).toBeInTheDocument();
    expect(wiz.textContent).toBe("t-99");
  });

  it("非 super_admin + 非 creator 的卡片 → 编辑/删除按钮隐藏", () => {
    mockAuthStore.mockReturnValue({ user: { id: "other-user", role: "project_admin" } });
    mockUseProjectTemplates.mockReturnValue({
      data: [makeTemplate({ id: "t-x", created_by: "user-1" })],
      isLoading: false,
    });
    renderUI();
    expect(screen.queryByTestId("template-delete-t-x")).not.toBeInTheDocument();
  });

  it("restores the template scope and q from the URL", () => {
    mockUseProjectTemplates.mockReturnValue({ data: [], isLoading: false });
    renderUI("/project-templates?q=car&scope=all");
    expect(screen.getByPlaceholderText("搜索模板名称…")).toHaveValue("car");
    expect(mockUseProjectTemplates).toHaveBeenLastCalledWith({ search: "car" });
    fireEvent.click(screen.getByRole("button", { name: "组织模板" }));
    expect(screen.getByTestId("location-search").textContent).toContain("scope=organization");
    expect(mockUseProjectTemplates).toHaveBeenLastCalledWith({
      scope: "organization",
      search: "car",
    });
  });

  it("debounces template q requests while writing q immediately", async () => {
    vi.useFakeTimers();
    mockUseProjectTemplates.mockReturnValue({ data: [], isLoading: false });
    renderUI();
    fireEvent.change(screen.getByPlaceholderText("搜索模板名称…"), {
      target: { value: "car" },
    });
    expect(screen.getByTestId("location-search").textContent).toContain("q=car");
    expect(mockUseProjectTemplates).toHaveBeenLastCalledWith({
      scope: "private",
      search: undefined,
    });
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(mockUseProjectTemplates).toHaveBeenLastCalledWith({
      scope: "private",
      search: "car",
    });
  });

  it("applies q and scope together after external URL navigation", async () => {
    mockUseProjectTemplates.mockReturnValue({ data: [], isLoading: false });
    renderUI("/project-templates?q=old&scope=private", "/project-templates?q=new&scope=all");
    await waitFor(() => {
      expect(mockUseProjectTemplates).toHaveBeenLastCalledWith({ search: "new" });
    });
    expect(
      mockUseProjectTemplates.mock.calls.some(
        ([params]) => params.scope === undefined && params.search === "old",
      ),
    ).toBe(false);
  });
});
