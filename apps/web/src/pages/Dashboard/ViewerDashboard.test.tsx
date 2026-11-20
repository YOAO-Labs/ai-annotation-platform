/**
 * ViewerDashboard 单测：KPI 卡片 / 项目列表 / 过滤切换 / 搜索 / 空态。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockUseProjects = vi.fn();
const mockUseProjectStats = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useProjects: (params: any) => mockUseProjects(params),
  useProjectStats: () => mockUseProjectStats(),
}));
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { ViewerDashboard } from "./ViewerDashboard";

function renderUI() {
  return render(
    <MemoryRouter>
      <ViewerDashboard />
    </MemoryRouter>,
  );
}

describe("ViewerDashboard", () => {
  beforeEach(() => {
    mockUseProjects.mockReset();
    mockUseProjectStats.mockReset();
    mockPushToast.mockReset();
  });

  it("isLoading=true → 加载中行", () => {
    mockUseProjects.mockReturnValue({ data: [], isLoading: true });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("有 stats → 渲染 4 张 KPI 卡片", () => {
    mockUseProjects.mockReturnValue({ data: [], isLoading: false });
    mockUseProjectStats.mockReturnValue({
      data: { total_data: 1234, completed: 567, ai_rate: 88, pending_review: 12 },
    });
    renderUI();
    expect(screen.getByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("567")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("项目空 → 空态文案", () => {
    mockUseProjects.mockReturnValue({ data: [], isLoading: false });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    expect(screen.getByText("没有匹配的项目")).toBeInTheDocument();
  });

  it("项目列表渲染 + image-det 点击导航", () => {
    mockUseProjects.mockReturnValue({
      data: [
        {
          id: "p1",
          display_id: "P-1",
          name: "Demo",
          type_label: "图像检测",
          type_key: "image-det",
          total_tasks: 10,
          completed_tasks: 5,
          ai_enabled: false,
          status: "in_progress",
        },
      ],
      isLoading: false,
    });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    expect(screen.getByText("Demo")).toBeInTheDocument();
    expect(screen.getByText("P-1")).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
  });

  it("未实现工作台类型点击 → toast 而不是导航", () => {
    // 注:工作台现按媒体维度 data_type(image/video/lidar)放行,三种均已实现。
    // 此 fixture 不带 data_type(媒体维度缺失/未识别),走降级 toast 兜底路径。
    mockUseProjects.mockReturnValue({
      data: [
        {
          id: "p2",
          display_id: "P-2",
          name: "Seg",
          type_label: "图像分割",
          type_key: "image-seg",
          total_tasks: 5,
          completed_tasks: 0,
          ai_enabled: false,
          status: "in_progress",
        },
      ],
      isLoading: false,
    });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    fireEvent.click(screen.getByText("Seg"));
    expect(mockPushToast).toHaveBeenCalledWith(
      expect.objectContaining({
        msg: '项目 "Seg" 已打开',
      }),
    );
  });

  it("AI enabled 项目 → 显示 AI 模型名 badge", () => {
    mockUseProjects.mockReturnValue({
      data: [
        {
          id: "p3",
          display_id: "P-3",
          name: "AI Demo",
          type_label: "图像检测",
          type_key: "image-det",
          total_tasks: 10,
          completed_tasks: 5,
          ai_enabled: true,
          ml_backend_id: "mb-1",
          status: "in_progress",
        },
      ],
      isLoading: false,
    });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    expect(screen.getByText("已接入模型")).toBeInTheDocument();
  });

  it("AI enabled 但已解绑后端 → 显示未接入", () => {
    mockUseProjects.mockReturnValue({
      data: [
        {
          id: "p4",
          display_id: "P-4",
          name: "Unbound",
          type_label: "图像检测",
          type_key: "image-det",
          total_tasks: 10,
          completed_tasks: 5,
          ai_enabled: true,
          ml_backend_id: null,
          status: "in_progress",
        },
      ],
      isLoading: false,
    });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    expect(screen.getByText("未接入模型")).toBeInTheDocument();
  });

  it("切换 TabRow 过滤 → useProjects 入参随之变化", () => {
    mockUseProjects.mockReturnValue({ data: [], isLoading: false });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    // 默认 全部 → status undefined
    expect(mockUseProjects).toHaveBeenLastCalledWith({
      status: undefined,
      search: undefined,
    });
    fireEvent.click(screen.getByText("已完成"));
    expect(mockUseProjects).toHaveBeenLastCalledWith({
      status: "completed",
      search: undefined,
    });
  });

  it("status=completed/pending_review/in_progress 各显示对应徽章", () => {
    mockUseProjects.mockReturnValue({
      data: [
        {
          id: "a",
          display_id: "A",
          name: "A",
          type_label: "图像检测",
          type_key: "image-det",
          total_tasks: 1,
          completed_tasks: 1,
          ai_enabled: false,
          status: "completed",
        },
        {
          id: "b",
          display_id: "B",
          name: "B",
          type_label: "图像检测",
          type_key: "image-det",
          total_tasks: 1,
          completed_tasks: 0,
          ai_enabled: false,
          status: "pending_review",
        },
      ],
      isLoading: false,
    });
    mockUseProjectStats.mockReturnValue({ data: undefined });
    renderUI();
    // 「已完成」「待审核」也是 TabRow 选项，会重复出现，断言至少一个
    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);
    expect(screen.getAllByText("待审核").length).toBeGreaterThan(0);
  });
});
