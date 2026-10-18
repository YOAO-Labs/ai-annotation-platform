/**
 * DatasetsPage 单测 — 加载态 / 空态 / 正常渲染 / 新建交互 / 搜索过滤.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { useEffect } from "react";

const mockUseDatasets = vi.fn();
const mockUseCreateDataset = vi.fn();
const mockUseDatasetItems = vi.fn();
const mockUseDatasetProjects = vi.fn();
const mockUseProjects = vi.fn();
const mockUseUnlinkProject = vi.fn();
const mockUseLinkProject = vi.fn();
const mockUseScanDatasetItems = vi.fn();
const mockUseBackfillDimensions = vi.fn();
const mockUseBackfillMedia = vi.fn();
const mockUseUpdateDataset = vi.fn();
const mockPushToast = vi.fn();
const scrollIntoView = vi.fn();

vi.mock("@/hooks/useDatasets", () => ({
  useDatasets: (...args: unknown[]) => mockUseDatasets(...args),
  useCreateDataset: () => mockUseCreateDataset(),
  useDatasetItems: () => mockUseDatasetItems(),
  useDatasetProjects: () => mockUseDatasetProjects(),
  useUnlinkProject: () => mockUseUnlinkProject(),
  useLinkProject: () => mockUseLinkProject(),
  useScanDatasetItems: () => mockUseScanDatasetItems(),
  useBackfillDimensions: () => mockUseBackfillDimensions(),
  useBackfillMedia: () => mockUseBackfillMedia(),
  useUpdateDataset: () => mockUseUpdateDataset(),
}));

vi.mock("@/hooks/useProjects", () => ({
  useProjects: () => mockUseProjects(),
}));

vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    previewUnlink: vi.fn().mockResolvedValue({
      will_delete_tasks: 0,
      will_delete_annotations: 0,
      will_delete_batches: 0,
    }),
  },
}));

vi.mock("@/components/datasets/ImportDatasetWizard", () => ({
  ImportDatasetWizard: ({ open }: any) => (open ? <div data-testid="import-wizard" /> : null),
}));

vi.mock("@/components/connections/StorageConnectionsPanel", () => ({
  StorageConnectionsPanel: () => <div data-testid="storage-connections-panel" />,
}));

vi.mock("@tanstack/react-query", async () => {
  const actual = await vi.importActual<any>("@tanstack/react-query");
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  };
});

vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { DatasetsPage } from "./DatasetsPage";

const idleMutation = { mutate: vi.fn(), isPending: false };

function renderUI(initialPath = "/datasets", navigateTo?: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      {navigateTo ? <NavigateOnMount to={navigateTo} /> : null}
      <LocationProbe />
      <DatasetsPage />
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

describe("DatasetsPage", () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    scrollIntoView.mockClear();
    mockUseDatasets.mockReset();
    mockPushToast.mockReset();
    mockUseCreateDataset.mockReturnValue(idleMutation);
    mockUseDatasetItems.mockReturnValue({ data: undefined, isLoading: false });
    mockUseDatasetProjects.mockReturnValue({ data: [] });
    mockUseProjects.mockReturnValue({ data: [] });
    mockUseUnlinkProject.mockReturnValue(idleMutation);
    mockUseLinkProject.mockReturnValue(idleMutation);
    mockUseScanDatasetItems.mockReturnValue(idleMutation);
    mockUseBackfillDimensions.mockReturnValue(idleMutation);
    mockUseBackfillMedia.mockReturnValue(idleMutation);
    mockUseUpdateDataset.mockReturnValue(idleMutation);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("isLoading=true → 显示加载中", () => {
    mockUseDatasets.mockReturnValue({ data: undefined, isLoading: true });
    renderUI();
    expect(screen.getAllByText("加载中...").length).toBeGreaterThan(0);
  });

  it("空数据 → 渲染页面标题 + 暂无数据集文案", () => {
    mockUseDatasets.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    renderUI();
    expect(screen.getAllByText("数据集").length).toBeGreaterThan(0);
    expect(screen.getByText(/暂无数据集/)).toBeInTheDocument();
  });

  it("有数据 → 渲染数据集行 + stat 卡片", () => {
    mockUseDatasets.mockReturnValue({
      data: {
        items: [
          {
            id: "ds1",
            display_id: "D-1",
            name: "测试数据集",
            description: "描述",
            data_type: "image",
            has_scenes: true,
            is_temporal: true,
            file_count: 42,
            project_count: 2,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("测试数据集")).toBeInTheDocument();
    expect(screen.getByText(/D-1/)).toBeInTheDocument();
    expect(screen.getByText("含 Scene")).toBeInTheDocument();
    // stat cards
    expect(screen.getByText("数据集总数")).toBeInTheDocument();
    expect(screen.getByText("文件总量")).toBeInTheDocument();
  });

  it("展开数据集 → 显示 scene 信息", () => {
    mockUseDatasetItems.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    mockUseDatasets.mockReturnValue({
      data: {
        items: [
          {
            id: "ds1",
            display_id: "D-1",
            name: "scene 数据集",
            description: "",
            data_type: "point_cloud",
            has_scenes: true,
            is_temporal: true,
            file_count: 0,
            project_count: 0,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
    });
    renderUI();
    fireEvent.click(screen.getByText("scene 数据集"));
    expect(screen.getByText("Scene 信息")).toBeInTheDocument();
    expect(screen.getByText("已识别")).toBeInTheDocument();
    expect(screen.getByText("时序数据集")).toBeInTheDocument();
  });

  it("重试同一坐标系更新复用 key，变更意图及成功后使用新 key", () => {
    mockUseDatasetItems.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    mockUseDatasets.mockReturnValue({
      data: {
        items: [
          {
            id: "ds-axis",
            display_id: "D-AXIS",
            name: "坐标系重试数据集",
            description: "",
            data_type: "point_cloud",
            axis_convention: "iso_8855",
            has_scenes: false,
            is_temporal: false,
            file_count: 0,
            project_count: 0,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
    });
    let respondWithSuccess = false;
    const updateMutation = {
      mutate: vi.fn(
        (
          _variables: unknown,
          callbacks: {
            onError?: (error: Error) => void;
            onSuccess?: () => void;
          },
        ) => {
          if (respondWithSuccess) callbacks.onSuccess?.();
          else callbacks.onError?.(new Error("response lost"));
        },
      ),
      isPending: false,
    };
    mockUseUpdateDataset.mockReturnValue(updateMutation);

    renderUI();
    fireEvent.click(screen.getByText("坐标系重试数据集"));
    const select = screen.getByDisplayValue("ISO 8855 (+X 前 / +Y 左)");
    fireEvent.change(select, { target: { value: "apollo" } });
    fireEvent.change(select, { target: { value: "apollo" } });
    expect(updateMutation.mutate).toHaveBeenCalledTimes(2);
    const first = updateMutation.mutate.mock.calls[0][0] as {
      payload: { axis_convention: string };
      idempotencyKey: string;
    };
    const retry = updateMutation.mutate.mock.calls[1][0] as typeof first;
    expect(retry.payload).toEqual(first.payload);
    expect(retry.idempotencyKey).toBe(first.idempotencyKey);

    fireEvent.change(select, { target: { value: "kitti_camera" } });
    expect(updateMutation.mutate).toHaveBeenCalledTimes(3);
    const changed = updateMutation.mutate.mock.calls[2][0] as typeof first;
    expect(changed.payload.axis_convention).toBe("kitti_camera");
    expect(changed.idempotencyKey).not.toBe(first.idempotencyKey);

    respondWithSuccess = true;
    fireEvent.change(select, { target: { value: "kitti_camera" } });
    expect(updateMutation.mutate).toHaveBeenCalledTimes(4);
    const success = updateMutation.mutate.mock.calls[3][0] as typeof first;
    expect(success.idempotencyKey).toBe(changed.idempotencyKey);
    fireEvent.change(select, { target: { value: "kitti_camera" } });
    expect(updateMutation.mutate).toHaveBeenCalledTimes(5);
    const afterSuccess = updateMutation.mutate.mock.calls[4][0] as typeof first;
    expect(afterSuccess.payload).toEqual(success.payload);
    expect(afterSuccess.idempotencyKey).not.toBe(success.idempotencyKey);
  });

  it("点击「新建数据集」按钮 → 弹出新建+上传向导", () => {
    mockUseDatasets.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    renderUI();
    expect(screen.queryByTestId("import-wizard")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /新建数据集/ }));
    expect(screen.getByTestId("import-wizard")).toBeInTheDocument();
  });

  it("搜索框输入后 q 写入 URL，且只在 debounce 后传入 useDatasets", async () => {
    vi.useFakeTimers();
    mockUseDatasets.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    renderUI();
    const input = screen.getByPlaceholderText("搜索数据集...");
    fireEvent.change(input, { target: { value: "my-ds" } });
    expect(screen.getByTestId("location-search").textContent).toContain("q=my-ds");
    expect(mockUseDatasets).toHaveBeenLastCalledWith(
      expect.objectContaining({ search: undefined }),
    );
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(mockUseDatasets).toHaveBeenLastCalledWith(expect.objectContaining({ search: "my-ds" }));
  });

  it("搜索无结果 → 显示「没有匹配的数据集」", () => {
    mockUseDatasets.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    renderUI();
    const input = screen.getByPlaceholderText("搜索数据集...");
    fireEvent.change(input, { target: { value: "不存在" } });
    // 触发重渲染：mock 保持空结果
    expect(screen.getByText("没有匹配的数据集")).toBeInTheDocument();
  });

  it("restores q/type and expands a matching dataset deep link", async () => {
    mockUseDatasetItems.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    mockUseDatasets.mockReturnValue({
      data: {
        items: [
          {
            id: "ds1",
            display_id: "D-1",
            name: "视频数据集",
            description: "",
            data_type: "video",
            has_scenes: false,
            is_temporal: false,
            file_count: 1,
            project_count: 0,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
    });
    renderUI("/datasets?q=clip&data_type=video&dataset=ds1");
    expect(screen.getByPlaceholderText("搜索数据集...")).toHaveValue("clip");
    expect(mockUseDatasets).toHaveBeenLastCalledWith({
      search: "clip",
      data_type: "video",
    });
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("q=clip"));
    expect(screen.getByRole("button", { name: /收起/ })).toBeInTheDocument();
  });

  it("filter changes clear the expanded dataset selection", () => {
    mockUseDatasetItems.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    mockUseDatasets.mockReturnValue({
      data: {
        items: [
          {
            id: "ds1",
            display_id: "D-1",
            name: "scene 数据集",
            description: "",
            data_type: "point_cloud",
            has_scenes: true,
            is_temporal: true,
            file_count: 0,
            project_count: 0,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
    });
    renderUI();
    fireEvent.click(screen.getByText("scene 数据集"));
    expect(screen.getByText("Scene 信息")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "图像" }));
    expect(screen.queryByText("Scene 信息")).not.toBeInTheDocument();
  });

  it("applies q and data_type together after external URL navigation", async () => {
    mockUseDatasets.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false });
    renderUI("/datasets?q=old&data_type=image", "/datasets?q=new&data_type=video");
    await waitFor(() => {
      expect(mockUseDatasets).toHaveBeenLastCalledWith({
        search: "new",
        data_type: "video",
      });
    });
    expect(
      mockUseDatasets.mock.calls.some(
        ([params]) => params.data_type === "video" && params.search === "old",
      ),
    ).toBe(false);
  });
});
