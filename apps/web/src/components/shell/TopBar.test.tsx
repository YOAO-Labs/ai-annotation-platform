import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockAuthApi = vi.hoisted(() => ({
  logout: vi.fn(),
}));
const mockOfflineQueue = vi.hoisted(() => ({
  count: vi.fn(),
  drain: vi.fn(),
  replaceAnnotationId: vi.fn(),
}));
const mockTasksApi = vi.hoisted(() => ({
  createAnnotation: vi.fn(),
  updateAnnotation: vi.fn(),
  deleteAnnotation: vi.fn(),
}));

vi.mock("@/api/auth", () => ({ authApi: mockAuthApi }));
vi.mock("@/api/tasks", () => ({ tasksApi: mockTasksApi }));
vi.mock("@/pages/Workbench/state/offlineQueue", () => ({
  ...mockOfflineQueue,
  countDurably: mockOfflineQueue.count,
}));
vi.mock("@/hooks/useTheme", () => ({
  useTheme: () => ({ resolved: "light", setTheme: vi.fn() }),
}));
vi.mock("./NotificationsPopover", () => ({
  NotificationsPopover: () => <button type="button">通知</button>,
}));
vi.mock("./PreannotateJobsBadge", () => ({ PreannotateJobsBadge: () => null }));
vi.mock("./JobsBell", () => ({ JobsBell: () => null }));
vi.mock("@/components/CommandPalette", () => ({ CommandPalette: () => null }));
vi.mock("@/components/PerfHud", () => ({
  usePerfHudStore: Object.assign(
    (selector: (state: { visible: boolean }) => unknown) => selector({ visible: false }),
    { getState: () => ({ toggle: vi.fn() }) },
  ),
}));

import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { TopBar } from "./TopBar";

const user: MeResponse = {
  id: "user-1",
  email: "annotator@example.com",
  name: "Annotator",
  role: "employee",
  group_name: null,
  status: "active",
  created_at: "2026-05-10T00:00:00Z",
};

function renderTopBar() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    // TopBar 的 useLogout 内部使用 useNavigate,需要 Router 上下文。
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }
  return render(<TopBar workspace="测试工作区" />, { wrapper: Wrapper });
}

describe("TopBar logout queue prompt", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.getState().setAuth("jwt", user);
    mockAuthApi.logout.mockResolvedValue(undefined);
    mockOfflineQueue.count.mockResolvedValue(0);
    mockOfflineQueue.drain.mockResolvedValue({ ok: 0, failed: 0 });
    mockOfflineQueue.replaceAnnotationId.mockResolvedValue(undefined);
  });

  it("keeps the shell available while notifications load", async () => {
    renderTopBar();

    expect(screen.getByRole("button", { name: "通知加载中" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled();
    expect(await screen.findByRole("button", { name: "通知" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "通知加载中" })).not.toBeInTheDocument();
  });

  it("有离线操作时先展示同步或保留记录的明确选择", async () => {
    mockOfflineQueue.count.mockResolvedValue(2);
    renderTopBar();

    fireEvent.click(screen.getByTitle("退出登录"));

    await waitFor(() => expect(screen.getByTestId("logout-offline-prompt")).toBeInTheDocument());
    expect(screen.getByTestId("logout-sync-queue")).toHaveTextContent("同步并退出");
    expect(screen.getByTestId("logout-keep-queue")).toHaveTextContent("保留记录并退出");
    expect(mockAuthApi.logout).not.toHaveBeenCalled();
  });

  it("选择保留记录并退出时不清空离线队列", async () => {
    mockOfflineQueue.count.mockResolvedValue(1);
    renderTopBar();

    fireEvent.click(screen.getByTitle("退出登录"));
    await waitFor(() => expect(screen.getByTestId("logout-keep-queue")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("logout-keep-queue"));

    await waitFor(() => expect(mockAuthApi.logout).toHaveBeenCalledOnce());
    expect(mockOfflineQueue.drain).not.toHaveBeenCalled();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("同步失败时保留对话框并显示可重试错误", async () => {
    mockOfflineQueue.count.mockResolvedValue(1);
    mockOfflineQueue.drain.mockRejectedValue(new Error("network unavailable"));
    renderTopBar();

    fireEvent.click(screen.getByTitle("退出登录"));
    await waitFor(() => expect(screen.getByTestId("logout-sync-queue")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("logout-sync-queue"));

    await waitFor(() => expect(screen.getByTestId("logout-sync-error")).toBeInTheDocument());
    expect(screen.getByTestId("logout-sync-error")).toHaveTextContent("network unavailable");
    expect(screen.getByTestId("logout-keep-queue")).toBeInTheDocument();
    expect(mockAuthApi.logout).not.toHaveBeenCalled();
  });
});
