/**
 * v0.10.16 · JobsBell 单测：badge 计数 / drawer 展开 / 空态 / 状态 pill。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

const mockList = vi.fn();
const mockCancel = vi.fn();
const mockGet = vi.fn();
vi.mock("@/api/asyncJobs", () => ({
  CANCELLABLE_ASYNC_JOB_KINDS: new Set([
    "batch_predict",
    "dataset_import",
    "mask_qc",
    "mask_repair",
    "mask_format_import",
  ]),
  asyncJobsApi: {
    list: (params: unknown, init?: RequestInit) => mockList(params, init),
    cancel: (id: string) => mockCancel(id),
    get: (id: string) => mockGet(id),
  },
}));

import { JobsBell } from "./JobsBell";

function renderBell() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <JobsBell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const baseRow = {
  id: "j1",
  kind: "batch_predict",
  project_id: "p1",
  user_id: "u1",
  status: "running" as const,
  progress_pct: 42,
  payload: {},
  result: {},
  error_message: null,
  celery_task_id: "c1",
  started_at: "2026-05-19T10:00:00Z",
  completed_at: null,
  created_at: "2026-05-19T09:59:00Z",
  updated_at: "2026-05-19T10:00:30Z",
};

describe("JobsBell", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.getState().setAuth("jobs-u1-token", { id: "u1", role: "employee" } as MeResponse);
    mockList.mockReset();
    mockCancel.mockReset();
    mockGet.mockReset();
    mockCancel.mockResolvedValue({ status: "cancel_requested", id: "j1" });
  });

  it("空列表 → 不显示 badge，drawer 打开显示空态", async () => {
    mockList.mockResolvedValue({ items: [], total: 0 });
    renderBell();
    await screen.findByTestId("jobs-bell-trigger");
    expect(screen.queryByTestId("jobs-bell-badge")).toBeNull();
    fireEvent.click(screen.getByTestId("jobs-bell-trigger"));
    await screen.findByText("暂无后台任务");
  });

  it("有 running job → 显示 badge 计数 + drawer 列出 row", async () => {
    mockList.mockResolvedValue({
      items: [baseRow, { ...baseRow, id: "j2", status: "running" }],
      total: 2,
    });
    renderBell();
    const badge = await screen.findByTestId("jobs-bell-badge");
    expect(badge.textContent).toBe("2");
    fireEvent.click(screen.getByTestId("jobs-bell-trigger"));
    expect(await screen.findByTestId("job-row-j1")).toBeInTheDocument();
    expect(screen.getByTestId("job-row-j2")).toBeInTheDocument();
  });

  it("terminal jobs (completed/failed) 不计入 badge", async () => {
    mockList.mockResolvedValue({
      items: [
        { ...baseRow, status: "completed", progress_pct: 100 },
        { ...baseRow, id: "j2", status: "failed", error_message: "boom" },
      ],
      total: 2,
    });
    renderBell();
    await screen.findByTestId("jobs-bell-trigger");
    expect(screen.queryByTestId("jobs-bell-badge")).toBeNull();
  });

  it("加载更早记录后可直达指定作业，跨页重复记录只显示一次", async () => {
    mockList.mockImplementation(({ offset }: { offset: number }) =>
      offset === 0
        ? { items: [baseRow], total: 3 }
        : {
            items: [
              baseRow,
              { ...baseRow, id: "older", kind: "dataset_import", status: "completed" },
            ],
            total: 3,
          },
    );
    mockGet.mockResolvedValue({
      ...baseRow,
      id: "older",
      kind: "dataset_import",
      status: "completed",
      result: { imported: 6, skipped: 0 },
    });
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    fireEvent.click(await screen.findByRole("button", { name: "加载更早任务" }));
    expect(await screen.findByTestId("job-row-older")).toBeInTheDocument();
    expect(screen.getAllByTestId("job-row-j1")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "查看数据集导入详情" }));
    expect(await screen.findByText("导入 6 / 跳过 0")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("older");
    expect(mockList).toHaveBeenCalledWith(expect.objectContaining({ limit: 20, offset: 1 }), {
      signal: expect.any(AbortSignal),
    });
  });

  it("查询失败显示重试，不误报没有后台任务", async () => {
    mockList.mockRejectedValue(new Error("offline"));
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    expect(await screen.findByRole("alert")).toHaveTextContent("后台任务加载失败");
    expect(screen.queryByText("暂无后台任务")).not.toBeInTheDocument();
  });

  it("导出作业显示多目标格式和产物摘要", async () => {
    mockList.mockResolvedValue({
      items: [
        {
          ...baseRow,
          kind: "export",
          status: "completed" as const,
          progress_pct: 100,
          payload: { project_display_id: "P-DEMO", targets: ["coco", "yolo-det"] },
          result: {
            download_url: "https://download.example/export.zip",
            file_count: 3,
            size_bytes: 1536,
            expires_at: "2099-01-01T00:00:00Z",
          },
        },
      ],
      total: 1,
    });
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    const row = await screen.findByTestId("job-row-j1");
    expect(row).toHaveTextContent("P-DEMO · COCO + YOLO DET");
    expect(row).toHaveTextContent("ZIP · 3 个文件 · 1.5 KB");
    expect(screen.getByTestId("job-download-j1")).toHaveAttribute(
      "href",
      "https://download.example/export.zip",
    );
  });

  it("仅为运行中的可取消 kind 展示取消入口并调用统一取消 API", async () => {
    mockList.mockResolvedValue({
      items: [
        baseRow,
        { ...baseRow, id: "j2", kind: "export" },
        {
          ...baseRow,
          id: "j3",
          kind: "predictions_import",
        },
        { ...baseRow, id: "j4", kind: "audit_archive" },
      ],
      total: 4,
    });
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    fireEvent.click(await screen.findByTestId("job-cancel-j1"));
    await waitFor(() => expect(mockCancel).toHaveBeenCalledWith("j1"));
    expect(screen.queryByTestId("job-cancel-j2")).toBeNull();
    expect(screen.queryByTestId("job-cancel-j3")).toBeNull();
    expect(screen.queryByTestId("job-cancel-j4")).toBeNull();
  });

  // v0.11.17 · 筛选 + 终态 dismiss
  const mixedRows = {
    items: [
      { ...baseRow, id: "run1", status: "running" as const },
      { ...baseRow, id: "done1", status: "completed" as const, progress_pct: 100 },
      { ...baseRow, id: "fail1", status: "failed" as const, error_message: "boom" },
    ],
    total: 3,
  };

  it("进行中筛选只显示 pending/running，剔除终态", async () => {
    mockList.mockResolvedValue(mixedRows);
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    expect(await screen.findByTestId("job-row-done1")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("jobs-bell-filter-active"));
    expect(screen.getByTestId("job-row-run1")).toBeInTheDocument();
    expect(screen.queryByTestId("job-row-done1")).toBeNull();
    expect(screen.queryByTestId("job-row-fail1")).toBeNull();
  });

  it("dismiss 一条终态任务后消失；进行中无 ✕ 且不被「清空已结束」移除", async () => {
    mockList.mockResolvedValue(mixedRows);
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    // 进行中无 dismiss 入口
    expect(screen.queryByTestId("job-dismiss-run1")).toBeNull();
    // 单条 dismiss completed
    fireEvent.click(await screen.findByTestId("job-dismiss-done1"));
    expect(screen.queryByTestId("job-row-done1")).toBeNull();
    // 「清空已结束」清掉剩余终态，保留进行中
    fireEvent.click(screen.getByTestId("jobs-bell-clear-terminal"));
    expect(screen.queryByTestId("job-row-fail1")).toBeNull();
    expect(screen.getByTestId("job-row-run1")).toBeInTheDocument();
    // 无可清项后按钮消失
    expect(screen.queryByTestId("jobs-bell-clear-terminal")).toBeNull();
  });

  it("filter 与 dismiss 持久化，重挂载后保留", async () => {
    mockList.mockResolvedValue(mixedRows);
    const first = renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    fireEvent.click(await screen.findByTestId("job-dismiss-done1"));
    fireEvent.click(screen.getByTestId("jobs-bell-filter-active"));
    expect(localStorage.getItem("wb:jobsbell:filter:u1")).toBe("active");
    expect(JSON.parse(localStorage.getItem("wb:jobsbell:dismissed:u1") ?? "[]")).toContain("done1");

    first.unmount();
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    expect(screen.getByTestId("jobs-bell-filter-active").getAttribute("aria-pressed")).toBe("true");
    // 切回「全部」仍隐藏已 dismiss 的 done1
    fireEvent.click(screen.getByTestId("jobs-bell-filter-all"));
    expect(await screen.findByTestId("job-row-fail1")).toBeInTheDocument();
    expect(screen.queryByTestId("job-row-done1")).toBeNull();
  });

  it("dismiss 集合收敛：滑出窗口的 id 从 localStorage 清掉", async () => {
    localStorage.setItem("wb:jobsbell:dismissed:u1", JSON.stringify(["done1", "slid-out-id"]));
    mockList.mockResolvedValue({
      items: [{ ...baseRow, id: "done1", status: "completed" as const, progress_pct: 100 }],
      total: 1,
    });
    renderBell();
    await screen.findByTestId("jobs-bell-trigger");
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem("wb:jobsbell:dismissed:u1") ?? "[]")).toEqual([
        "done1",
      ]);
    });
  });

  it("切换账号时旧列表迟到响应不会泄露到新账号", async () => {
    let resolveFirst!: (value: unknown) => void;
    mockList
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValueOnce({ items: [], total: 0 });
    renderBell();
    await waitFor(() =>
      expect(mockList).toHaveBeenCalledWith(expect.objectContaining({ limit: 20, offset: 0 }), {
        signal: expect.any(AbortSignal),
      }),
    );

    act(() =>
      useAuthStore
        .getState()
        .setAuth("jobs-u2-token", { id: "u2", role: "employee" } as MeResponse),
    );
    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
    await act(async () => resolveFirst({ items: [{ ...baseRow, id: "alice-job" }], total: 1 }));
    expect(screen.queryByTestId("job-row-alice-job")).not.toBeInTheDocument();
  });

  it("同一账号切换 token epoch 时旧列表迟到响应不会泄露", async () => {
    let resolveFirst!: (value: unknown) => void;
    let firstSignal!: AbortSignal;
    mockList
      .mockImplementationOnce((_params: unknown, init?: RequestInit) => {
        firstSignal = init?.signal as AbortSignal;
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      })
      .mockResolvedValueOnce({ items: [], total: 0 });
    renderBell();
    await waitFor(() =>
      expect(mockList).toHaveBeenCalledWith(expect.objectContaining({ limit: 20, offset: 0 }), {
        signal: expect.any(AbortSignal),
      }),
    );

    act(() =>
      useAuthStore
        .getState()
        .setAuth("jobs-u1-token-2", { id: "u1", role: "employee" } as MeResponse),
    );
    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
    expect(firstSignal.aborted).toBe(true);
    await act(async () => resolveFirst({ items: [{ ...baseRow, id: "old-token-job" }], total: 1 }));
    expect(screen.queryByTestId("job-row-old-token-job")).not.toBeInTheDocument();
  });

  it("切换账号时已选详情立即关闭，不保留旧账号作业", async () => {
    mockList.mockResolvedValue({
      items: [{ ...baseRow, status: "completed" as const, progress_pct: 100 }],
      total: 1,
    });
    mockGet.mockResolvedValue({ ...baseRow, status: "completed" as const, progress_pct: 100 });
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    fireEvent.click(await screen.findByRole("button", { name: "查看批量预标详情" }));
    expect(await screen.findByRole("dialog", { name: "后台任务详情" })).toBeInTheDocument();

    act(() =>
      useAuthStore
        .getState()
        .setAuth("jobs-u2-token", { id: "u2", role: "employee" } as MeResponse),
    );
    expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
  });

  it("取消响应迟到到另一账号时不提示成功也不触发新账号刷新", async () => {
    let resolveCancel!: (value: unknown) => void;
    mockList.mockResolvedValue({ items: [baseRow], total: 1 });
    mockCancel.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCancel = resolve;
        }),
    );
    renderBell();
    fireEvent.click(await screen.findByTestId("jobs-bell-trigger"));
    fireEvent.click(await screen.findByTestId("job-cancel-j1"));
    act(() =>
      useAuthStore
        .getState()
        .setAuth("jobs-u2-token", { id: "u2", role: "employee" } as MeResponse),
    );
    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
    await act(async () => resolveCancel({ status: "cancelled", id: "j1" }));
    expect(mockList).toHaveBeenCalledTimes(2);
  });
});
