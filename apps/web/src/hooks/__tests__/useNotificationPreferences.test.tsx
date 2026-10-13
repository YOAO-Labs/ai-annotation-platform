import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

const api = vi.hoisted(() => ({
  getPreferences: vi.fn(),
  updatePreference: vi.fn(),
}));
vi.mock("@/api/notifications", () => ({
  notificationsApi: {
    getPreferences: api.getPreferences,
    updatePreference: api.updatePreference,
  },
}));

import {
  notificationPreferencesKey,
  useNotificationPreferences,
  useUpdateNotificationPreference,
} from "../useNotificationPreferences";

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function wrapper(client: QueryClient) {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getPreferences.mockResolvedValue({ items: [] });
  api.updatePreference.mockResolvedValue({ ok: true });
  useAuthStore.getState().setAuth("t1", { id: "u1", role: "employee" } as MeResponse);
});

describe("useNotificationPreferences", () => {
  it("查询 key 绑定账号", () => {
    expect(notificationPreferencesKey("u1")).toEqual(["notification-preferences", "u1"]);
    expect(notificationPreferencesKey(undefined)).toEqual(["notification-preferences", null]);
  });

  it("登录后按账号拉取偏好", async () => {
    const client = makeClient();
    const { result } = renderHook(() => useNotificationPreferences(), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.getPreferences).toHaveBeenCalled();
  });

  it("未登录不查询", async () => {
    useAuthStore.getState().logout();
    const client = makeClient();
    const { result } = renderHook(() => useNotificationPreferences(), {
      wrapper: wrapper(client),
    });
    expect(result.current.isPending).toBe(true);
    expect(result.current.fetchStatus).toBe("idle");
    expect(api.getPreferences).not.toHaveBeenCalled();
  });

  it("更新成功后失效当前账号的偏好查询", async () => {
    const client = makeClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useUpdateNotificationPreference(), {
      wrapper: wrapper(client),
    });
    result.current.mutate({ type: "task.rejected", toast: false });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.updatePreference).toHaveBeenCalledWith("task.rejected", { toast: false });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["notification-preferences", "u1"],
    });
  });

  it("保存成功后先把已提交值写入缓存再失效重拉", async () => {
    const client = makeClient();
    client.setQueryData(["notification-preferences", "u1"], {
      items: [
        { type: "task.rejected", in_app: true, email: false, toast: false },
        { type: "task.approved", in_app: true, email: false, toast: false },
      ],
    });
    // 重拉悬而未决：缓存仍应是本次提交值，而不是回跳到旧值。
    api.getPreferences.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useUpdateNotificationPreference(), {
      wrapper: wrapper(client),
    });
    await act(async () => {
      await result.current.mutateAsync({ type: "task.rejected", toast: true });
    });
    const cached = client.getQueryData<{
      items: { type: string; toast: boolean; in_app: boolean }[];
    }>(["notification-preferences", "u1"]);
    expect(cached?.items.find((item) => item.type === "task.rejected")).toMatchObject({
      toast: true,
      in_app: true,
    });
    expect(cached?.items.find((item) => item.type === "task.approved")?.toast).toBe(false);
  });

  it("迟到的旧账号响应不能失效新账号的查询", async () => {
    let resolveSave!: (value: { ok: boolean }) => void;
    api.updatePreference.mockReturnValue(new Promise((r) => (resolveSave = r)));
    const client = makeClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useUpdateNotificationPreference(), {
      wrapper: wrapper(client),
    });
    result.current.mutate({ type: "task.rejected", owner: "u1", in_app: false });
    // 保存期间账号被替换
    act(() => {
      useAuthStore.getState().setAuth("t2", { id: "u2", role: "employee" } as MeResponse);
    });
    await act(async () => {
      resolveSave({ ok: true });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
