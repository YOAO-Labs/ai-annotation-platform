/**
 * useNotificationSocket 单测：连接生命周期、reauth 重连、查询失效与
 * 重要消息瞬时提醒（偏好门控、去重、1s 批量、可见标签页限制）。
 *
 * 不打真实 WS server，用 MockWebSocket 替换 globalThis.WebSocket，
 * 然后通过 instance 的 dispatchEvent / onclose 模拟服务端行为。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast as sonnerToast } from "sonner";
import { useAuthStore } from "@/stores/authStore";

// 断言「弹了什么 toast」通过 mock sonner 的 toast 入口观测，
// 不给 useToastStore 增加可观测状态（见 Toast.tsx 的红线注释）。
vi.mock("sonner", () => {
  const fn = vi.fn();
  return {
    toast: Object.assign(fn, {
      success: vi.fn(),
      warning: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
    }),
  };
});

const refreshMock = vi.fn();
vi.mock("@/api/auth", () => ({
  authApi: {
    refresh: () => refreshMock(),
  },
}));
vi.mock("@/api/notifications", () => ({
  notificationsApi: {
    getPreferences: async () => ({ items: [] }),
  },
}));

import { useNotificationSocket } from "../useNotificationSocket";

function errorToastMessages(): string[] {
  return (sonnerToast as unknown as { error: ReturnType<typeof vi.fn> }).error.mock.calls.map(
    (call) => String(call[0]),
  );
}

function warningToastMessages(): string[] {
  return (sonnerToast as unknown as { warning: ReturnType<typeof vi.fn> }).warning.mock.calls.map(
    (call) => String(call[0]),
  );
}

function toastCallCount(): number {
  const t = sonnerToast as unknown as {
    error: ReturnType<typeof vi.fn>;
    warning: ReturnType<typeof vi.fn>;
    success: ReturnType<typeof vi.fn>;
    info: ReturnType<typeof vi.fn>;
  };
  return (
    t.error.mock.calls.length +
    t.warning.mock.calls.length +
    t.success.mock.calls.length +
    t.info.mock.calls.length
  );
}

function clearToastMocks() {
  const t = sonnerToast as unknown as {
    error: ReturnType<typeof vi.fn>;
    warning: ReturnType<typeof vi.fn>;
    success: ReturnType<typeof vi.fn>;
    info: ReturnType<typeof vi.fn>;
  };
  t.error.mockClear();
  t.warning.mockClear();
  t.success.mockClear();
  t.info.mockClear();
}

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  readyState = 0;
  closedManually = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close() {
    this.closedManually = true;
  }
  // 测试钩子
  triggerClose(code: number) {
    this.onclose?.({ code, wasClean: false } as CloseEvent);
  }
  triggerOpen() {
    this.onopen?.(new Event("open"));
  }
  triggerMessage(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
  triggerRaw(data: unknown) {
    this.onmessage?.({ data } as MessageEvent);
  }
}

let originalWS: typeof globalThis.WebSocket | undefined;

function seedPreferences(
  qc: QueryClient,
  userId: string,
  items: { type: string; in_app?: boolean; toast?: boolean }[],
) {
  qc.setQueryData(["notification-preferences", userId], {
    items: items.map((it) => ({
      type: it.type,
      in_app: it.in_app ?? true,
      email: false,
      toast: it.toast ?? false,
    })),
  });
}

beforeEach(() => {
  MockWebSocket.instances = [];
  refreshMock.mockReset();
  clearToastMocks();
  vi.useFakeTimers();
  originalWS = globalThis.WebSocket;
  Object.defineProperty(globalThis, "WebSocket", {
    value: MockWebSocket,
    writable: true,
    configurable: true,
  });
  useAuthStore.setState({
    token: "old-token",
    user: { id: "u1", role: "employee" } as never,
  });
  Object.defineProperty(document, "visibilityState", {
    value: "visible",
    configurable: true,
  });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(globalThis, "WebSocket", {
    value: originalWS,
    writable: true,
    configurable: true,
  });
});

function wrap(qc?: QueryClient) {
  const client =
    qc ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function mount(qc?: QueryClient) {
  return renderHook(() => useNotificationSocket(), { wrapper: wrap(qc) });
}

describe("useNotificationSocket", () => {
  it("挂载时用 token 拼接 ws URL 并建立连接", () => {
    mount();
    expect(MockWebSocket.instances.length).toBe(1);
    expect(MockWebSocket.instances[0].url).toContain("token=old-token");
    // v0.9.11 修复: WS URL 从错误的 /api/v1/ws/notifications 改为 /ws/notifications
    // (ws_router 在 main.py 是 app.include_router(ws_router) 无 prefix).
    expect(MockWebSocket.instances[0].url).toMatch(/\/ws\/notifications/);
    expect(MockWebSocket.instances[0].url).not.toMatch(/\/api\/v1\/ws\/notifications/);
  });

  it("token 为空时不建立连接", () => {
    useAuthStore.setState({ token: null, user: null });
    mount();
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("连接打开时即刷新通知查询（含重连补拉）", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mount(qc);
    act(() => {
      MockWebSocket.instances[0].triggerOpen();
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["notifications"] });
  });

  it("close code 1008 时调 /auth/refresh 并用新 token 重连", async () => {
    refreshMock.mockResolvedValue({
      access_token: "new-token",
      token_type: "bearer",
    });

    mount();
    expect(MockWebSocket.instances.length).toBe(1);

    // 模拟 1008（鉴权过期）关闭
    await act(async () => {
      MockWebSocket.instances[0].triggerClose(1008);
      // 让 .then 回调跑（refresh 是异步）
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(refreshMock).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().token).toBe("new-token");

    // scheduleRetry 1s 后用新 token 重连
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(MockWebSocket.instances.length).toBe(2);
    expect(MockWebSocket.instances[1].url).toContain("token=new-token");
  });

  it("close code 1008 + refresh 失败 → 不再重连", async () => {
    refreshMock.mockRejectedValue(new Error("token revoked"));

    mount();
    await act(async () => {
      MockWebSocket.instances[0].triggerClose(1008);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(refreshMock).toHaveBeenCalled();

    // catch 后 closedManually = true → schedule 不会触发新连接
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(MockWebSocket.instances.length).toBe(1);
  });

  it("迟到的 refresh 结果不能写回已替换的会话", async () => {
    let resolveRefresh: (value: { access_token: string }) => void = () => {};
    refreshMock.mockReturnValue(
      new Promise((resolve) => {
        resolveRefresh = resolve;
      }),
    );

    mount();
    await act(async () => {
      MockWebSocket.instances[0].triggerClose(1008);
      await Promise.resolve();
    });
    // 会话被替换（登出+新登录）后 refresh 才返回
    useAuthStore.setState({
      token: "session-b-token",
      user: { id: "u2", role: "employee" } as never,
    });
    await act(async () => {
      resolveRefresh({ access_token: "stale-token" });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(useAuthStore.getState().token).toBe("session-b-token");
  });

  it("普通 close（非鉴权码）走 backoff 重连，不调 refresh", async () => {
    mount();
    act(() => {
      MockWebSocket.instances[0].triggerClose(1006);
    });
    expect(refreshMock).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(MockWebSocket.instances.length).toBe(2);
  });

  it("notifications.sync 只刷新通知查询，不触发业务查询副作用", () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        type: "notifications.sync",
        reason: "read",
      });
    });

    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["notifications"] });
    expect(toastCallCount()).toBe(0);
  });

  it("notifications.sync reason=preferences 额外刷新偏好查询", () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        type: "notifications.sync",
        reason: "preferences",
      });
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["notifications"] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["notification-preferences", "u1"],
    });
    expect(toastCallCount()).toBe(0);
  });

  it("真实通知刷新查询；偏好未加载时不弹提醒", () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        target_type: "task",
        target_id: "t1",
        payload: { task_display_id: "T-1" },
      });
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["notifications"] });
    // 偏好查询没有数据 → 抑制瞬时提醒，但仍计数
    act(() => {
      vi.advanceTimersByTimeAsync(1500);
    });
    expect(toastCallCount()).toBe(0);
  });

  it("隐藏标签页不弹提醒", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seedPreferences(qc, "u1", [{ type: "task.rejected", toast: true }]);
    mount(qc);
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        payload: { task_display_id: "T-1" },
      });
    });
    expect(toastCallCount()).toBe(0);
  });

  it("批量窗口内切到后台时丢弃已排队提醒", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seedPreferences(qc, "u1", [{ type: "task.rejected", toast: true }]);
    mount(qc);

    // 入队时标签页可见，两条事件合并进同一批。
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        payload: { task_display_id: "T-1" },
      });
      MockWebSocket.instances[0].triggerMessage({
        id: "n2",
        type: "task.rejected",
        payload: { task_display_id: "T-2" },
      });
    });
    // flush 前切到后台：重新检查可见性后丢弃整批，不产生回到前台才看到的 toast。
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(toastCallCount()).toBe(0);
  });

  it("默认重要类型（接收+弹出开启）单条弹出短文案", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seedPreferences(qc, "u1", [{ type: "task.rejected", toast: true }]);
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        payload: { task_display_id: "T-1", reject_reason: "漏标" },
      });
    });
    expect(toastCallCount()).toBe(0); // 1s 批量窗口内
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(toastCallCount()).toBe(1);
    expect(errorToastMessages()[0]).toContain("T-1");
    expect(errorToastMessages()[0]).toContain("漏标");
  });

  it("偏好关闭弹出或关闭接收时不弹；未选择的普通类型默认不弹", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seedPreferences(qc, "u1", [
      { type: "task.rejected", toast: false },
      { type: "job.failed", in_app: false, toast: true },
    ]);
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "a",
        type: "task.rejected",
        payload: {},
      });
      // export.ready 无偏好记录：未知/普通类型默认不弹
      MockWebSocket.instances[0].triggerMessage({
        id: "b",
        type: "export.ready",
        payload: {},
      });
      MockWebSocket.instances[0].triggerMessage({
        id: "c",
        type: "job.failed",
        payload: {},
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(toastCallCount()).toBe(0);
  });

  it("未知类型默认不弹", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seedPreferences(qc, "u1", [{ type: "task.rejected", toast: true }]);
    mount(qc);
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "x",
        type: "future.unknown_event",
        payload: {},
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(toastCallCount()).toBe(0);
  });

  it("1 秒内多条重要通知合并为一条 toast；按 ID 去重", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seedPreferences(qc, "u1", [
      { type: "task.rejected", toast: true },
      { type: "export.failed", toast: true },
    ]);
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        payload: { task_display_id: "T-1" },
      });
      MockWebSocket.instances[0].triggerMessage({
        id: "n2",
        type: "export.failed",
        payload: {},
      });
      // 重复 ID 不重复提醒
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        payload: { task_display_id: "T-1" },
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(toastCallCount()).toBe(1);
    expect(warningToastMessages()).toEqual(["收到 2 条重要通知，请查看通知中心"]);

    // 窗口外的重复 ID 仍被去重
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        id: "n1",
        type: "task.rejected",
        payload: { task_display_id: "T-1" },
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(toastCallCount()).toBe(1);
  });

  it("ping 与畸形消息不触发失效或提醒", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    seedPreferences(qc, "u1", [{ type: "task.rejected", toast: true }]);
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({ type: "ping" });
      MockWebSocket.instances[0].triggerRaw("not-json{");
      MockWebSocket.instances[0].triggerRaw({ nope: 1 });
      MockWebSocket.instances[0].triggerMessage({ payload: { id: "n1" } });
    });
    expect(invalidateSpy).not.toHaveBeenCalled();
    expect(toastCallCount()).toBe(0);
  });

  it("job.* / failed_prediction.retry.* 仍触发对应业务查询失效", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mount(qc);

    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        type: "failed_prediction.retry.started",
        payload: {},
      });
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["admin", "failed-predictions"] });

    invalidateSpy.mockClear();
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        type: "job.completed",
        payload: { kind: "batch_predict" },
      });
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["async-jobs"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["notifications"] });
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ["admin", "failed-predictions"] });

    invalidateSpy.mockClear();
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        type: "job.completed",
        payload: { kind: "prediction_retry" },
      });
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["async-jobs"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["admin", "failed-predictions"] });
  });

  it("账号变化会断开旧连接并重开新连接", () => {
    mount();
    expect(MockWebSocket.instances.length).toBe(1);
    act(() => {
      useAuthStore.setState({
        token: "token-b",
        user: { id: "u2", role: "employee" } as never,
      });
    });
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
    expect(MockWebSocket.instances.length).toBe(2);
    expect(MockWebSocket.instances[1].url).toContain("token=token-b");
  });

  it("卸载时手动 close、不再 retry", () => {
    const { unmount } = mount();
    unmount();
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
  });
});
