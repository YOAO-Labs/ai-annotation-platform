import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within, fireEvent, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { copyToClipboard } from "@/lib/clipboard";

vi.mock("@/components/connections/StorageLocationsConsole", () => ({
  StorageLocationsConsole: () => <div data-testid="locations-console" />,
}));
vi.mock("@/lib/clipboard", () => ({ copyToClipboard: vi.fn() }));

const mockPushToast = vi.fn();
const mockInvalidateQueries = vi.fn();
const mockRetryMutate = vi.fn();
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId, onClose }: { jobId: string; onClose: () => void }) => (
    <div role="dialog" aria-label="视频资产重试详情">
      <span>{jobId}</span>
      <button onClick={onClose}>关闭任务</button>
    </div>
  ),
}));

vi.mock("@tanstack/react-query", async () => {
  const actual =
    await vi.importActual<typeof import("@tanstack/react-query")>("@tanstack/react-query");
  return {
    ...actual,
    useQueryClient: () => ({
      invalidateQueries: mockInvalidateQueries,
    }),
  };
});

const mockUseStorageBuckets = vi.fn();
const mockUseVideoAssetFailures = vi.fn();
vi.mock("@/hooks/useStorage", () => ({
  useStorageBuckets: () => mockUseStorageBuckets(),
  useVideoAssetFailures: (limit?: number, offset?: number) =>
    mockUseVideoAssetFailures(limit, offset),
  useRetryVideoAsset: () => ({
    mutate: mockRetryMutate,
    isPending: false,
  }),
}));

const mockUseDatasets = vi.fn();
vi.mock("@/hooks/useDatasets", () => ({
  useDatasets: () => mockUseDatasets(),
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

import { StoragePage } from "./StoragePage";

const SAMPLE_VIDEO_FAILURE = {
  asset_key: "vaf-1",
  asset_type: "probe" as const,
  dataset_item_id: "di1",
  file_name: "video.mp4",
  chunk_id: null,
  frame_index: null,
  width: null,
  format: null,
  error: "timeout",
  updated_at: "2026-05-01T10:00:00Z",
  project_name: "Video Project",
  task_display_id: "T-001",
};

const queryClients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => {
  queryClients.splice(0).forEach((client) => client.clear());
});
function renderUI() {
  const client = createTestQueryClient();
  queryClients.push(client);
  return {
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <StoragePage />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    client,
  };
}

describe("StoragePage", () => {
  beforeEach(() => {
    vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true);
    mockUseStorageBuckets.mockClear();
    mockUseDatasets.mockClear();
    mockPushToast.mockReset();
    mockInvalidateQueries.mockReset().mockResolvedValue(undefined);
    mockRetryMutate.mockReset();
    mockUseDatasets.mockReturnValue({ data: { items: [], total: 0 } });
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [], total: 0, limit: 20, offset: 0 },
      isLoading: false,
      isError: false,
    });
  });

  it("renders the location console without bucket scans or a duplicate dataset query", () => {
    renderUI();
    expect(screen.getByText("存储管理")).toBeInTheDocument();
    expect(screen.getByTestId("locations-console")).toBeInTheDocument();
    expect(mockUseStorageBuckets).not.toHaveBeenCalled();
    expect(mockUseDatasets).not.toHaveBeenCalled();
    expect(screen.queryByText("总容量")).not.toBeInTheDocument();
  });

  it("视频资产失败列表有数据 → 渲染失败行 + 重试按钮", () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [SAMPLE_VIDEO_FAILURE], total: 1, limit: 20, offset: 0 },
      isLoading: false,
      isError: false,
    });
    renderUI();
    expect(screen.getAllByText("video.mp4").length).toBeGreaterThan(0);
    expect(screen.getByText("timeout")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument();
  });

  it("a successful empty query reports only the visible scope", () => {
    renderUI();
    expect(screen.getByRole("status", { name: "视频资产失败查询状态" })).toHaveTextContent(
      /可见范围.*无失败/,
    );
  });

  it("a rejected initial query is unavailable rather than an empty or healthy result", () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    renderUI();
    expect(screen.getByRole("status", { name: "视频资产失败查询状态" })).toHaveTextContent(
      /不可用/,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "视频资产失败分页" })).not.toBeInTheDocument();
  });

  it("a rejected refresh marks retained results stale without losing rows, paging or diagnostics", async () => {
    const error = "ffmpeg: failed\n帧 #43 · /media/input.mp4";
    const data = { items: [{ ...SAMPLE_VIDEO_FAILURE, error }], total: 21, limit: 20, offset: 0 };
    mockUseVideoAssetFailures.mockReturnValue({ data, isLoading: false, isError: false });
    const { rerender, client } = renderUI();
    expect(screen.getByRole("status", { name: "视频资产失败查询状态" })).toHaveTextContent(/21/);

    mockUseVideoAssetFailures.mockReturnValue({ data, isLoading: false, isError: true });
    rerender(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <StoragePage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("status", { name: "视频资产失败查询状态" })).toHaveTextContent(
      /不可用/,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/上次查询结果.*过期/);
    expect(screen.getByText("Video Project")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "视频资产失败分页" })).toHaveTextContent(/21/);
    fireEvent.click(screen.getByRole("button", { name: "复制 video.mp4 的完整错误信息" }));
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(error));
  });

  it("a rejected refresh does not turn a retained empty result into a healthy status", () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [], total: 0, limit: 20, offset: 0 },
      isLoading: false,
      isError: true,
    });
    renderUI();
    expect(screen.getByRole("status", { name: "视频资产失败查询状态" })).toHaveTextContent(
      /不可用/,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/上次查询结果.*过期/);
  });

  it("点击截断错误复制完整多行内容，等待复制完成后提示成功", async () => {
    const error = `ffmpeg version 5.1.9\n${"encoder diagnostics ".repeat(40)}\n编码失败: WebPAnimEncoderAssemble()`;
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [{ ...SAMPLE_VIDEO_FAILURE, error }], total: 1 },
    });
    let complete!: (copied: boolean) => void;
    vi.mocked(copyToClipboard).mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    renderUI();
    const copy = screen.getByRole("button", { name: "复制 video.mp4 的完整错误信息" });
    fireEvent.click(copy);
    expect(copyToClipboard).toHaveBeenCalledWith(error);
    expect(mockPushToast).not.toHaveBeenCalled();
    complete(true);
    await waitFor(() =>
      expect(mockPushToast).toHaveBeenCalledWith({
        msg: "已复制完整错误信息",
        kind: "success",
      }),
    );
    expect(mockRetryMutate).not.toHaveBeenCalled();
  });

  it("错误复制支持键盘触发，复制失败时提供提示", async () => {
    const user = userEvent.setup();
    vi.mocked(copyToClipboard).mockResolvedValue(false);
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [SAMPLE_VIDEO_FAILURE], total: 1 },
    });
    renderUI();
    screen.getByRole("button", { name: "复制 video.mp4 的完整错误信息" }).focus();
    await user.keyboard("{Enter}");
    expect(copyToClipboard).toHaveBeenCalledWith("timeout");
    expect(mockPushToast).toHaveBeenCalledWith({
      msg: "复制失败，请检查浏览器剪贴板权限",
      kind: "error",
    });
    expect(mockRetryMutate).not.toHaveBeenCalled();
  });

  it("点击重试按钮 → 调用 retry.mutate", () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [SAMPLE_VIDEO_FAILURE], total: 1, limit: 20, offset: 0 },
      isLoading: false,
      isError: false,
    });
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /重试/ }));
    expect(mockRetryMutate).toHaveBeenCalledTimes(1);
    expect(mockRetryMutate.mock.calls[0][0]).toMatchObject({
      asset_type: "probe",
      dataset_item_id: "di1",
    });
  });

  it("点击「刷新状态」→ invalidateQueries 被调用", () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /刷新状态/ }));
    expect(mockInvalidateQueries).toHaveBeenCalledWith({ queryKey: ["storage-source-summary"] });
    expect(mockInvalidateQueries).toHaveBeenCalledWith({ queryKey: ["storage-source-locations"] });
    expect(mockInvalidateQueries).toHaveBeenCalledWith({ queryKey: ["storage-bindings"] });
    expect(mockInvalidateQueries).toHaveBeenCalledWith({ queryKey: ["storage-buckets"] });
    for (const key of [
      "site-agent-sites",
      "site-agents",
      "site-agent-detail",
      "site-agent-locations",
    ]) {
      expect(mockInvalidateQueries).toHaveBeenCalledWith({ queryKey: [key] });
    }
  });

  it("重试后打开共享任务详情，关闭时刷新失败列表", () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [SAMPLE_VIDEO_FAILURE], total: 1 },
    });
    mockRetryMutate.mockImplementation((_payload, options) =>
      options.onSuccess({ job_id: "retry-job" }),
    );
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("retry-job");
    fireEvent.click(screen.getByRole("button", { name: "关闭任务" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ["storage-video-asset-failures"],
    });
  });

  it("an initial load does not report an empty or healthy result", () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });
    renderUI();
    expect(screen.getByRole("status", { name: "视频资产失败查询状态" })).toHaveTextContent(
      /加载中/,
    );
  });

  it("视频资产失败列表分页 → 使用 limit/offset 拉取下一页", async () => {
    mockUseVideoAssetFailures.mockReturnValue({
      data: { items: [SAMPLE_VIDEO_FAILURE], total: 21, limit: 20, offset: 0 },
      isLoading: false,
      isError: false,
    });
    renderUI();
    expect(mockUseVideoAssetFailures).toHaveBeenLastCalledWith(20, 0);

    fireEvent.click(
      within(screen.getByRole("navigation", { name: "视频资产失败分页" })).getByRole("button", {
        name: "下一页",
      }),
    );

    await waitFor(() => {
      expect(mockUseVideoAssetFailures).toHaveBeenLastCalledWith(20, 20);
    });
    expect(screen.getByText(/第 2 \/ 2 页/)).toBeInTheDocument();
  });
});
