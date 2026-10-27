import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MeResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import {
  mlBackendOperationsApi,
  type ManagedMLAuthStatus,
  type MLBackendOperationJob,
  type MLBackendOperationsStatus,
} from "@/api/mlBackendOperations";
import { useAuthStore } from "@/stores/authStore";
import { MLBackendOperationsSection } from "./MLBackendOperationsSection";

vi.mock("@/api/mlBackendOperations", () => ({
  mlBackendOperationsApi: {
    managedAuth: vi.fn(),
    initialize: vi.fn(),
    status: vi.fn(),
    rebuild: vi.fn(),
    job: vi.fn(),
  },
}));

const providers = ["gsam2", "sam3", "yolo", "onnxtools", "rapidocr"] as const;
const admin = { id: "admin-1", role: "super_admin" } as MeResponse;
const authStatus: ManagedMLAuthStatus = {
  items: providers.map((provider) => ({
    provider,
    service_name: `${provider}-backend`,
    url: `http://${provider}-backend:8000`,
    credential_state: "missing",
    registries: [],
  })),
};
const operationsStatus: MLBackendOperationsStatus = {
  available: true,
  reason: null,
  deployment: "test-deployment",
  runner_id: "test-runner",
  services: providers.map((provider) => ({ provider, can_rebuild: true, reason: null })),
  jobs: [],
};
function job(overrides: Partial<MLBackendOperationJob> = {}): MLBackendOperationJob {
  return {
    id: "job-1",
    provider: "yolo",
    status: "queued",
    phase: "queued",
    created_at: "2026-10-19T10:00:00Z",
    updated_at: "2026-10-19T10:00:00Z",
    deployment: "test-deployment",
    error: null,
    events: [],
    ...overrides,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const clients: QueryClient[] = [];
function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MLBackendOperationsSection />
      </QueryClientProvider>,
    ),
  };
}
async function loaded() {
  await screen.findByText("Grounded SAM 2");
  await screen.findByText(/当前部署：test-deployment/);
}
function openRebuild() {
  fireEvent.click(screen.getByRole("button", { name: "重建 YOLO 镜像与容器" }));
}

describe("MLBackendOperationsSection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    useAuthStore.getState().setAuth("admin-token", admin);
    vi.mocked(mlBackendOperationsApi.managedAuth).mockResolvedValue(structuredClone(authStatus));
    vi.mocked(mlBackendOperationsApi.status).mockResolvedValue(structuredClone(operationsStatus));
    vi.mocked(mlBackendOperationsApi.initialize).mockResolvedValue(authStatus);
    vi.mocked(mlBackendOperationsApi.rebuild).mockResolvedValue(job());
    vi.mocked(mlBackendOperationsApi.job).mockResolvedValue(job());
  });
  afterEach(() => {
    cleanup();
    clients.splice(0).forEach((client) => client.clear());
    vi.useRealTimers();
  });

  it("shows five independent credential states without exposing secret inputs or making writes", async () => {
    const result = structuredClone(authStatus);
    result.items[0].credential_state = "ready";
    result.items[0].registries = [
      {
        id: "registry-1",
        name: "Grounded registry",
        auth_method: "managed:gsam2",
        state: "synced",
      },
    ];
    vi.mocked(mlBackendOperationsApi.managedAuth).mockResolvedValue(result);
    const { container } = renderSection();
    await loaded();
    expect(screen.getByText("凭据已保存")).toBeInTheDocument();
    expect(screen.getAllByText("尚未初始化")).toHaveLength(4);
    expect(screen.getByText(/Grounded registry · 系统托管/)).toBeInTheDocument();
    expect(screen.getByText(/None 表示不发送认证信息/)).toBeInTheDocument();
    expect(screen.getByText(/容器健康和模型业务就绪需要分别检查/)).toBeInTheDocument();
    expect(container.querySelector("input")).toBeNull();
    expect(mlBackendOperationsApi.initialize).not.toHaveBeenCalled();
    expect(mlBackendOperationsApi.rebuild).not.toHaveBeenCalled();
  });

  it("does not read or render privileged operations for a non-admin", () => {
    useAuthStore.getState().setAuth("viewer-token", { ...admin, role: "viewer" });
    const { container } = renderSection();
    expect(container).toBeEmptyDOMElement();
    expect(mlBackendOperationsApi.managedAuth).not.toHaveBeenCalled();
    expect(mlBackendOperationsApi.status).not.toHaveBeenCalled();
  });

  it("shows unavailable runner reasons and disables rebuild while auth initialization remains available", async () => {
    vi.mocked(mlBackendOperationsApi.status).mockResolvedValue({
      ...operationsStatus,
      available: false,
      reason: "维护执行器未接入当前部署",
    });
    renderSection();
    await screen.findByText(/维护不可用：维护执行器未接入当前部署/);
    expect(screen.getByRole("button", { name: "重建 YOLO 镜像与容器" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重建 YOLO 镜像与容器" })).toHaveAttribute(
      "title",
      "维护执行器未接入当前部署",
    );
    expect(screen.getByRole("button", { name: "初始化 / 同步全部" })).toBeEnabled();
  });

  it("submits one explicit provider once and reports preserved manual configuration", async () => {
    const pending = deferred<ManagedMLAuthStatus>();
    vi.mocked(mlBackendOperationsApi.initialize).mockReturnValue(pending.promise);
    renderSection();
    await loaded();
    const button = screen.getByRole("button", { name: "初始化 / 同步 YOLO" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(mlBackendOperationsApi.initialize).toHaveBeenCalledTimes(1));
    expect(mlBackendOperationsApi.initialize).toHaveBeenCalledWith(["yolo"]);
    expect(screen.getByRole("button", { name: "初始化 / 同步全部" })).toBeDisabled();
    const result = structuredClone(authStatus);
    result.items[2].outcome = "manual_preserved";
    result.items[2].message = "现有 Token 未修改";
    await act(async () => pending.resolve(result));
    expect(await screen.findByText("已保留手动认证：现有 Token 未修改")).toBeInTheDocument();
  });

  it("allows retry after initialization failure and displays per-service outcomes", async () => {
    const result = structuredClone(authStatus);
    result.items[0].outcome = "initialized";
    result.items[1].outcome = "unavailable";
    result.items[1].message = "服务未启动";
    vi.mocked(mlBackendOperationsApi.initialize)
      .mockRejectedValueOnce(new Error("同步连接失败"))
      .mockResolvedValueOnce(result);
    renderSection();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "初始化 / 同步全部" }));
    expect(await screen.findByText(/认证初始化 \/ 同步失败：同步连接失败/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "初始化 / 同步全部" }));
    expect(await screen.findByText("本地凭据已初始化")).toBeInTheDocument();
    expect(screen.getByText("服务暂不可用：服务未启动")).toBeInTheDocument();
    expect(mlBackendOperationsApi.initialize).toHaveBeenLastCalledWith(undefined);
  });

  it("requires target-specific confirmation and preserves cancellation", async () => {
    renderSection();
    await loaded();
    openRebuild();
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("重建 YOLO 镜像与容器？");
    expect(dialog).toHaveTextContent("目标部署：test-deployment");
    expect(dialog).toHaveTextContent("推理会短暂停止");
    expect(dialog).toHaveTextContent("空闲跟踪会话缓存会丢失");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(mlBackendOperationsApi.rebuild).not.toHaveBeenCalled();
  });

  it("freezes the confirmed deployment and runner and never retries a changed target automatically", async () => {
    vi.mocked(mlBackendOperationsApi.rebuild).mockRejectedValue(
      new ApiError(409, "维护部署已变化，请重新核对目标", {
        error_code: "backend_manager_target_changed",
      }),
    );
    const { client } = renderSection();
    await loaded();
    openRebuild();
    act(() => {
      client.setQueryData(["ml-backend-operations", admin.id, useAuthStore.getState().sessionId], {
        ...operationsStatus,
        deployment: "replacement-deployment",
        runner_id: "replacement-runner",
      });
    });
    expect(screen.getByRole("alertdialog")).toHaveTextContent("目标部署：test-deployment");
    expect(screen.getByRole("alertdialog")).not.toHaveTextContent("replacement-deployment");
    fireEvent.click(screen.getByRole("button", { name: "确认重建" }));
    expect(
      await screen.findByText("维护任务未创建：维护部署已变化，请重新核对目标"),
    ).toBeInTheDocument();
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledTimes(1);
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledWith({
      provider: "yolo",
      expected_deployment: "test-deployment",
      expected_runner_id: "test-runner",
    });
    expect(mlBackendOperationsApi.job).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("prevents repeated rebuilds and polls phase logs until completion", async () => {
    const pending = deferred<MLBackendOperationJob>();
    vi.mocked(mlBackendOperationsApi.rebuild).mockReturnValue(pending.promise);
    vi.mocked(mlBackendOperationsApi.job)
      .mockResolvedValueOnce(
        job({
          status: "running",
          phase: "building",
          events: [{ at: "2026-10-19T10:00:00Z", kind: "info", message: "正在构建 YOLO 镜像" }],
        }),
      )
      .mockResolvedValueOnce(job({ status: "succeeded", phase: "complete" }));
    renderSection();
    await loaded();
    openRebuild();
    fireEvent.click(screen.getByRole("button", { name: "确认重建" }));
    expect(screen.getByRole("button", { name: "重建 YOLO 镜像与容器" })).toBeDisabled();
    await act(async () => pending.resolve(job()));
    expect(await screen.findByText("正在构建 YOLO 镜像")).toBeInTheDocument();
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledTimes(1);
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledWith({
      provider: "yolo",
      expected_deployment: "test-deployment",
      expected_runner_id: "test-runner",
    });
    expect(screen.getByRole("button", { name: "初始化 / 同步全部" })).toBeDisabled();
    expect(await screen.findByText("维护完成", {}, { timeout: 3500 })).toBeInTheDocument();
    expect(screen.getByText(/模型加载和业务推理就绪请在模型市场进一步检查/)).toBeInTheDocument();
  });

  it("recovers the conflicting job instead of resubmitting a rebuild", async () => {
    vi.mocked(mlBackendOperationsApi.rebuild).mockRejectedValue(
      new ApiError(409, "已有维护任务正在执行", {
        error_code: "backend_operation_conflict",
        job_id: "existing-job",
      }),
    );
    vi.mocked(mlBackendOperationsApi.job).mockResolvedValue(
      job({ id: "existing-job", status: "running", phase: "checking" }),
    );
    renderSection();
    await loaded();
    openRebuild();
    fireEvent.click(screen.getByRole("button", { name: "确认重建" }));
    expect(await screen.findByText(/维护任务未创建：已有维护任务正在执行/)).toBeInTheDocument();
    expect(await screen.findByText("检查容器")).toBeInTheDocument();
    expect(mlBackendOperationsApi.job).toHaveBeenCalledWith("existing-job", expect.any(Object));
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledTimes(1);
  });

  it("keeps failed rebuilds explicit and allows a separately confirmed retry", async () => {
    vi.mocked(mlBackendOperationsApi.rebuild)
      .mockRejectedValueOnce(new Error("维护执行器暂不可用"))
      .mockResolvedValueOnce(
        job({ id: "retry-job", status: "failed", phase: "building", error: "镜像构建失败" }),
      );
    vi.mocked(mlBackendOperationsApi.job).mockResolvedValue(
      job({ id: "retry-job", status: "failed", phase: "building", error: "镜像构建失败" }),
    );
    renderSection();
    await loaded();
    openRebuild();
    fireEvent.click(screen.getByRole("button", { name: "确认重建" }));
    expect(await screen.findByText("维护任务未创建：维护执行器暂不可用")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重建 YOLO 镜像与容器" })).toBeEnabled();
    openRebuild();
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "确认重建" }));
    expect(await screen.findByText("维护失败")).toBeInTheDocument();
    expect(screen.getByText("镜像构建失败")).toBeInTheDocument();
    expect(mlBackendOperationsApi.rebuild).toHaveBeenCalledTimes(2);
  });

  it("keeps task read failures recoverable and explains interrupted recovery", async () => {
    vi.mocked(mlBackendOperationsApi.status).mockResolvedValue({
      ...operationsStatus,
      jobs: [job()],
    });
    vi.mocked(mlBackendOperationsApi.job)
      .mockRejectedValueOnce(new Error("连接中断"))
      .mockResolvedValueOnce(
        job({ status: "interrupted", phase: "recreating", error: "执行器进程重启" }),
      );
    renderSection();
    await loaded();
    expect(await screen.findByText("任务进度读取失败：连接中断")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试读取任务" }));
    expect(await screen.findByText("维护已中断")).toBeInTheDocument();
    expect(screen.getByText(/在宿主机确认恢复后再创建维护任务/)).toBeInTheDocument();
    expect(mlBackendOperationsApi.rebuild).not.toHaveBeenCalled();
  });

  it("shows authorization errors without enabling writes from failed status reads", async () => {
    vi.mocked(mlBackendOperationsApi.managedAuth).mockRejectedValue(
      new ApiError(403, "需要超级管理员权限"),
    );
    vi.mocked(mlBackendOperationsApi.status).mockRejectedValue(
      new ApiError(403, "需要超级管理员权限"),
    );
    renderSection();
    expect(await screen.findByText("无法读取认证状态：需要超级管理员权限")).toBeInTheDocument();
    expect(screen.getByText("无法读取维护能力：需要超级管理员权限")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "初始化 / 同步全部" })).not.toBeInTheDocument();
  });

  it("discards late initialization results after the same administrator starts a new session", async () => {
    const pending = deferred<ManagedMLAuthStatus>();
    vi.mocked(mlBackendOperationsApi.initialize).mockReturnValue(pending.promise);
    renderSection();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "初始化 / 同步全部" }));
    await waitFor(() => expect(mlBackendOperationsApi.initialize).toHaveBeenCalledTimes(1));
    act(() => useAuthStore.getState().setAuth("new-admin-token", admin));
    await loaded();
    const stale = structuredClone(authStatus);
    stale.items[0].outcome = "initialized";
    stale.items[0].message = "旧会话操作结果";
    await act(async () => pending.resolve(stale));
    expect(screen.queryByText(/旧会话操作结果/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "初始化 / 同步全部" })).toBeEnabled();
  });

  it("discards late rebuild errors after account replacement", async () => {
    const pending = deferred<MLBackendOperationJob>();
    vi.mocked(mlBackendOperationsApi.rebuild).mockReturnValue(pending.promise);
    renderSection();
    await loaded();
    openRebuild();
    fireEvent.click(screen.getByRole("button", { name: "确认重建" }));
    act(() => useAuthStore.getState().setAuth("other-token", { ...admin, id: "admin-2" }));
    await loaded();
    await act(async () => pending.reject(new Error("旧账号维护失败")));
    expect(screen.queryByText(/旧账号维护失败/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重建 YOLO 镜像与容器" })).toBeEnabled();
  });
});
