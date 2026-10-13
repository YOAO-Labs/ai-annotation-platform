import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useRef, useState } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { MeResponse } from "@/api/auth";
import type { AsyncJob } from "@/api/asyncJobs";
import { useAuthStore } from "@/stores/authStore";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  retry: vi.fn(),
  dataset: vi.fn(),
  project: vi.fn(),
}));
vi.mock("@/api/asyncJobs", () => ({ asyncJobsApi: { get: mocks.get, retryFailed: mocks.retry } }));
vi.mock("@/api/datasets", () => ({ datasetsApi: { get: mocks.dataset } }));
vi.mock("@/api/projects", () => ({ projectsApi: { get: mocks.project } }));
import { AsyncJobDetailModal } from "./AsyncJobDetailModal";

const job: AsyncJob = {
  id: "j1",
  kind: "dataset_import",
  project_id: "p1",
  user_id: "u1",
  project_name: "巡检照片",
  project_display_id: "P-1",
  status: "completed",
  progress_pct: 100,
  payload: { dataset_id: "d1", dataset_name: "九月样本" },
  result: { imported: 10, skipped: 0, error_count: 0 },
  error_message: null,
  celery_task_id: null,
  started_at: "2026-09-09T00:00:00Z",
  completed_at: "2026-09-09T00:01:00Z",
  created_at: "2026-09-09T00:00:00Z",
  updated_at: "2026-09-09T00:01:00Z",
};

function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
}
function renderModal({
  jobId = "j1",
  onRetryQueued,
  navigate,
}: {
  jobId?: string;
  onRetryQueued?: (queued: number) => void;
  navigate?: (to: string) => Promise<boolean> | boolean;
} = {}) {
  const onClose = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Location />
        <AsyncJobDetailModal
          jobId={jobId}
          onClose={onClose}
          onRetryQueued={onRetryQueued}
          navigate={navigate}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, onClose };
}

describe("共享后台任务详情", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useAuthStore
      .getState()
      .setAuth("detail-u1-token", { id: "u1", role: "employee" } as MeResponse);
    mocks.get.mockResolvedValue(job);
    mocks.retry.mockResolvedValue({ queued: 1 });
    mocks.dataset.mockResolvedValue({ id: "d1" });
  });
  afterEach(() => {
    act(() => useAuthStore.getState().logout());
  });

  it.each([
    ["查看数据集", "/datasets?dataset=d1"],
    ["查看项目数据", "/projects/p1/data-manager"],
    ["返回项目列表", "/dashboard"],
  ])(
    "%s awaits Workbench admission and keeps the detail open on cancellation",
    async (label, url) => {
      mocks.project.mockResolvedValue({ id: "p1" });
      if (label === "返回项目列表") {
        mocks.get.mockResolvedValue({ ...job, kind: "export", result: {} });
      }
      let finish!: (allowed: boolean) => void;
      const navigate = vi.fn(
        () =>
          new Promise<boolean>((resolve) => {
            finish = resolve;
          }),
      );
      const { onClose } = renderModal({ navigate });
      fireEvent.click(await screen.findByRole("button", { name: label }));
      await waitFor(() => expect(navigate).toHaveBeenCalledWith(url));
      expect(screen.getByTestId("location").textContent).toBe("/");
      expect(onClose).not.toHaveBeenCalled();
      await act(async () => finish(false));
      expect(onClose).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: label })).toBeEnabled();
      navigate.mockResolvedValueOnce(true);
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    },
  );

  it("读回指定作业结果并直接打开通过权限检查的数据集", async () => {
    const { onClose } = renderModal();
    expect(await screen.findByText("导入 10 / 跳过 0 / 错误 0")).toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledWith("j1");
    expect(screen.getByText("巡检照片 · 九月样本")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看数据集" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/datasets?dataset=d1"),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps the detail mounted when a sibling leave dialog is cancelled by pointer", async () => {
    const onClose = vi.fn();
    function WorkbenchDialogs() {
      const [detailOpen, setDetailOpen] = useState(true);
      const [confirmOpen, setConfirmOpen] = useState(false);
      const settle = useRef<(allowed: boolean) => void>(() => {});
      return (
        <>
          {detailOpen && (
            <AsyncJobDetailModal
              jobId="j1"
              onClose={() => {
                onClose();
                setDetailOpen(false);
              }}
              navigate={() =>
                new Promise<boolean>((resolve) => {
                  settle.current = resolve;
                  setConfirmOpen(true);
                })
              }
            />
          )}
          <AlertDialog
            open={confirmOpen}
            onOpenChange={(open) => {
              setConfirmOpen(open);
              if (!open) settle.current(false);
            }}
          >
            <AlertDialogContent>
              <AlertDialogTitle>切换视频工具</AlertDialogTitle>
              <AlertDialogDescription>当前源帧还有未完成的绘制。</AlertDialogDescription>
              <AlertDialogCancel>继续绘制</AlertDialogCancel>
            </AlertDialogContent>
          </AlertDialog>
        </>
      );
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <WorkbenchDialogs />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "查看数据集" }));
    await user.click(await screen.findByRole("button", { name: "继续绘制" }));
    // The lower Radix layer defers outside-pointer dismissal until after click.
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "后台任务详情" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看数据集" })).toBeEnabled();
    await user.click(screen.getByTestId("modal-overlay"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it.each([403, 404])("详情 %s 不展示陈旧结果，并给出重新加载入口", async (status) => {
    mocks.get.mockRejectedValue(new ApiError(status, "gone"));
    renderModal();
    expect(await screen.findByRole("alert")).toHaveTextContent(/无权|不存在/);
    expect(screen.queryByRole("button", { name: "查看数据集" })).not.toBeInTheDocument();
    mocks.get.mockResolvedValue(job);
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("导入 10 / 跳过 0 / 错误 0")).toBeInTheDocument();
  });

  it("关联目标被删除时保留本次结果而不跳到另一个数据集", async () => {
    mocks.dataset.mockRejectedValue(new ApiError(404, "gone"));
    renderModal();
    fireEvent.click(await screen.findByRole("button", { name: "查看数据集" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("关联目标已被删除");
    expect(screen.getByTestId("location").textContent).toBe("/");
    expect(screen.getByText("导入 10 / 跳过 0 / 错误 0")).toBeInTheDocument();
  });

  it("已结束预标仅重试失败项；导入失败不提供重复导入按钮", async () => {
    mocks.get.mockResolvedValue({
      ...job,
      kind: "batch_predict",
      status: "failed",
      result: { failed_count: 1, failed_prediction_ids: ["fp1"] },
    });
    const view = renderModal();
    fireEvent.click(await screen.findByRole("button", { name: "重试失败项" }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("j1"));
    view.unmount();
    mocks.get.mockResolvedValue({ ...job, status: "failed", error_message: "文件总量超过预算" });
    renderModal();
    expect(await screen.findByText("文件总量超过预算")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
  });

  it("过期导出指向重新生成入口，不打开旧预签名链接", async () => {
    mocks.get.mockResolvedValue({
      ...job,
      kind: "export",
      result: { download_url: "https://files.example/old.zip", expires_at: "2000-01-01T00:00:00Z" },
    });
    renderModal();
    expect(await screen.findByText(/下载链接已过期/)).toHaveAttribute("role", "status");
    expect(screen.queryByRole("link", { name: "下载导出文件" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回项目列表" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("详情关闭后迟到的目标读取不再导航", async () => {
    let resolve!: (value: unknown) => void;
    mocks.dataset.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const view = renderModal();
    fireEvent.click(await screen.findByRole("button", { name: "查看数据集" }));
    view.unmount();
    await act(async () => resolve({ id: "d1" }));
    expect(view.onClose).not.toHaveBeenCalled();
  });

  it("重试响应迟到到另一账号时不触发原账号回调", async () => {
    let resolveRetry!: (value: unknown) => void;
    mocks.get.mockResolvedValue({
      ...job,
      kind: "batch_predict",
      status: "failed",
      result: { failed_count: 1, failed_prediction_ids: ["fp1"] },
    });
    mocks.retry.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRetry = resolve;
        }),
    );
    const onRetryQueued = vi.fn();
    renderModal({ onRetryQueued });
    fireEvent.click(await screen.findByRole("button", { name: "重试失败项" }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledOnce());
    act(() =>
      useAuthStore
        .getState()
        .setAuth("detail-u2-token", { id: "u2", role: "employee" } as MeResponse),
    );
    await act(async () => resolveRetry({ queued: 1 }));
    expect(onRetryQueued).not.toHaveBeenCalled();
  });

  it("目标读取期间 localStorage 账号被另一 tab 替换时不导航或关闭", async () => {
    let resolveDataset!: (value: unknown) => void;
    mocks.dataset.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDataset = resolve;
        }),
    );
    const view = renderModal();
    fireEvent.click(await screen.findByRole("button", { name: "查看数据集" }));
    localStorage.setItem("token", "detail-u2-token");
    await act(async () => resolveDataset({ id: "d1" }));
    expect(view.onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId("location")).toHaveTextContent("/");
  });

  it("jobId 改变时清除旧的关联目标错误和打开状态", async () => {
    mocks.dataset.mockRejectedValue(new ApiError(404, "gone"));
    const view = renderModal();
    fireEvent.click(await screen.findByRole("button", { name: "查看数据集" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("关联目标已被删除");

    mocks.get.mockResolvedValue({ ...job, id: "j2" });
    view.rerender(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <Location />
          <AsyncJobDetailModal jobId="j2" onClose={view.onClose} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("导入 10 / 跳过 0 / 错误 0");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
