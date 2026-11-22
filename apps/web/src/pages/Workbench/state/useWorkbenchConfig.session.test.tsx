import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  authApi,
  DEFAULT_WORKBENCH_PREFERENCES,
  type MeResponse,
  type UserPreferencesPatch,
} from "@/api/auth";
import { bindAuthQueryCache } from "@/stores/authQueryCache";
import { bindAuthStorage, useAuthStore } from "@/stores/authStore";
import { useWorkbenchConfig } from "./useWorkbenchConfig";
import { userPreferencesQueryKey } from "./useUserPreferences";

const pushToast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: { getState: () => ({ push: pushToast }) },
}));

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function storeOtherTabLogin(id: string, token: string) {
  const sessionId = crypto.randomUUID();
  localStorage.setItem("token", token);
  localStorage.setItem(
    "auth-storage",
    JSON.stringify({ state: { token, user: { id }, sessionId }, version: 0 }),
  );
  return sessionId;
}

function syncOtherTabLogin() {
  window.dispatchEvent(
    new StorageEvent("storage", { key: "auth-storage", storageArea: localStorage }),
  );
}

const initialPreferences = { workbench: { image: { controlPointsSize: 8 } } };
const nextPreferences = {
  workbench: { image: { controlPointsSize: 11, cssImageFilter: "invert(1)" } },
};
let getPreferences: () => Response | Promise<Response>;
let patchPreferences: (patch: UserPreferencesPatch) => Response | Promise<Response>;
let patches: { bearer: string | null; payload: UserPreferencesPatch }[];
let client: QueryClient;
let unbindCache: () => void;
let unbindStorage: () => void;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  pushToast.mockClear();
  localStorage.clear();
  useAuthStore.getState().setAuth("alice-token", { id: "alice" } as MeResponse);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  unbindCache = bindAuthQueryCache(client);
  unbindStorage = bindAuthStorage();
  patches = [];
  getPreferences = () => Response.json(initialPreferences);
  patchPreferences = (patch) => Response.json(patch);
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    expect(path).toBe("/api/v1/auth/me/preferences");
    if (init.method !== "PATCH") return getPreferences();
    const payload = JSON.parse(String(init.body)) as UserPreferencesPatch;
    patches.push({ bearer: new Headers(init.headers).get("Authorization"), payload });
    return patchPreferences(payload);
  });
});

afterEach(() => {
  cleanup();
  unbindStorage();
  unbindCache();
  client.clear();
  useAuthStore.getState().logout();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it.each([
  ["另一个账号", "bob", "bob-token"],
  ["同账号重新登录并复用 token", "alice", "alice-token"],
])("其他标签页切换为%s时取消待写入值，等待新偏好再编辑", async (_, id, token) => {
  const { result } = renderHook(() => [useWorkbenchConfig(), useWorkbenchConfig()] as const, {
    wrapper,
  });
  await waitFor(() => expect(result.current.every((part) => part.loaded)).toBe(true));
  const old = result.current[0];
  const originalSession = useAuthStore.getState().sessionId;
  vi.useFakeTimers();
  act(() => old.setFields({ image: { controlPointsSize: 17 } }));
  expect(result.current[1].config.image.controlPointsSize).toBe(17);

  const pending = deferredResponse();
  getPreferences = () => pending.promise;
  const newSession = storeOtherTabLogin(id, token);
  expect(newSession).not.toBe(originalSession);
  await act(async () => {
    syncOtherTabLogin();
  });
  expect(useAuthStore.getState().sessionId).toBe(newSession);
  expect(result.current.every((part) => !part.loaded)).toBe(true);
  expect(result.current[1].config.image.controlPointsSize).not.toBe(17);
  await act(async () => {
    old.setFields({ image: { controlPointsSize: 19 } });
    await expect(old.update({ image: { controlPointsSize: 19 } })).rejects.toMatchObject({
      name: "AbortError",
    });
    result.current[0].setFields({ image: { controlPointsSize: 20 } });
    result.current[0].setLayout({ rightOpen: false });
    await expect(
      result.current[1].update({ image: { controlPointsSize: 20 } }),
    ).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(300);
  });
  expect(patches).toEqual([]);
  expect(client.getQueryData(userPreferencesQueryKey(id))).toBeUndefined();

  vi.useRealTimers();
  await act(async () => pending.resolve(Response.json(nextPreferences)));
  await waitFor(() => expect(result.current.every((part) => part.loaded)).toBe(true));
  expect(result.current[0].config.image.controlPointsSize).toBe(11);
  await act(async () => result.current[0].update({ image: { controlPointsSize: 12 } }));
  expect(patches).toMatchObject([
    {
      bearer: `Bearer ${token}`,
      payload: {
        workbench: { image: { controlPointsSize: 12, cssImageFilter: "invert(1)" } },
      },
    },
  ]);
  expect(result.current[1].config.image.controlPointsSize).toBe(12);
});

it.each(["before-storage-event", "after-logout"] as const)(
  "卸载不会在凭据变化后补交原会话偏好 (%s)",
  async (transition) => {
    const { result, unmount } = renderHook(useWorkbenchConfig, { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.setFields({ image: { controlPointsSize: 15 } }));
    act(() => {
      if (transition === "before-storage-event") storeOtherTabLogin("bob", "bob-token");
      else useAuthStore.getState().logout();
      unmount();
    });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(patches).toEqual([]);
    expect(pushToast).not.toHaveBeenCalled();
  },
);

it.each(["debounce", "unmount"] as const)(
  "同一会话续签后使用新 token 保存已捕获的值 (%s)",
  async (mode) => {
    const { result, unmount } = renderHook(useWorkbenchConfig, { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    const sessionId = useAuthStore.getState().sessionId;
    vi.useFakeTimers();
    act(() => result.current.setFields({ image: { controlPointsSize: 16 } }));
    getPreferences = () => new Promise(() => undefined);
    act(() => useAuthStore.getState().setToken("refreshed-token"));
    expect(useAuthStore.getState().sessionId).toBe(sessionId);
    await act(async () => {
      if (mode === "unmount") unmount();
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(patches).toMatchObject([
      {
        bearer: "Bearer refreshed-token",
        payload: { workbench: { image: { controlPointsSize: 16 } } },
      },
    ]);
    expect(client.getQueryData(userPreferencesQueryKey("alice"))).toMatchObject({
      workbench: { image: { controlPointsSize: 16 } },
    });
  },
);

it("同一会话续签不丢弃已在途的偏好确认", async () => {
  const { result } = renderHook(useWorkbenchConfig, { wrapper });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  const pending = deferredResponse();
  patchPreferences = () => pending.promise;
  let completion!: Promise<void>;
  act(() => {
    completion = result.current.update({ image: { controlPointsSize: 16 } });
  });
  expect(patches[0].bearer).toBe("Bearer alice-token");
  act(() => useAuthStore.getState().setToken("refreshed-token"));
  await act(async () => {
    pending.resolve(Response.json({ workbench: { image: { controlPointsSize: 15 } } }));
    await completion;
  });
  expect(result.current.config.image.controlPointsSize).toBe(15);
  expect(client.getQueryData(userPreferencesQueryKey("alice"))).toMatchObject({
    workbench: { image: { controlPointsSize: 15 } },
  });
});

it.each([
  { mode: "immediate", id: "bob", status: 200 },
  { mode: "immediate", id: "alice", status: 200 },
  { mode: "debounce", id: "bob", status: 200 },
  { mode: "unmount", id: "bob", status: 200 },
  { mode: "debounce", id: "bob", status: 503 },
] as const)("不发布 $mode 的迟到结果 ($id / $status)", async ({ mode, id, status }) => {
  const editor = renderHook(useWorkbenchConfig, { wrapper });
  const reader = renderHook(useWorkbenchConfig, { wrapper });
  await waitFor(() =>
    expect(editor.result.current.loaded && reader.result.current.loaded).toBe(true),
  );
  const pending = deferredResponse();
  patchPreferences = () => pending.promise;
  vi.useFakeTimers();
  let completion: Promise<unknown> | undefined;
  const patch = { image: { controlPointsSize: 17 } };
  act(() => {
    if (mode === "immediate") {
      completion = editor.result.current.update(patch).catch((error: unknown) => error);
    } else editor.result.current.setFields(patch);
  });
  expect(reader.result.current.config.image.controlPointsSize).toBe(17);
  if (mode === "unmount") editor.unmount();
  await act(async () => vi.advanceTimersByTimeAsync(300));
  expect(patches).toMatchObject([{ bearer: "Bearer alice-token", payload: { workbench: patch } }]);

  vi.useRealTimers();
  getPreferences = () => Response.json(nextPreferences);
  storeOtherTabLogin(id, id === "bob" ? "bob-token" : "alice-token");
  act(syncOtherTabLogin);
  await waitFor(() => expect(reader.result.current.config.image.controlPointsSize).toBe(11));
  await act(async () => {
    pending.resolve(Response.json({ workbench: { image: { controlPointsSize: 33 } } }, { status }));
    if (completion) expect(await completion).toMatchObject({ name: "AbortError" });
  });
  expect(reader.result.current.config.image.controlPointsSize).toBe(11);
  expect(reader.result.current.saving).toBe(false);
  expect(client.getQueryData(userPreferencesQueryKey(id))).toEqual(nextPreferences);
  if (id === "bob") expect(client.getQueryData(userPreferencesQueryKey("alice"))).toBeUndefined();
  expect(pushToast).not.toHaveBeenCalled();
});

it("同会话最新编辑快照替代另一实例的防抖计时器", async () => {
  const first = renderHook(useWorkbenchConfig, { wrapper });
  const second = renderHook(useWorkbenchConfig, { wrapper });
  await waitFor(() =>
    expect(first.result.current.loaded && second.result.current.loaded).toBe(true),
  );
  vi.useFakeTimers();
  act(() => first.result.current.setFields({ image: { controlPointsSize: 18 } }));
  await act(async () => vi.advanceTimersByTimeAsync(100));
  act(() => second.result.current.setFields({ image: { smoothImage: false } }));
  first.unmount();
  await act(async () => vi.advanceTimersByTimeAsync(200));
  expect(patches).toEqual([]);
  await act(async () => vi.advanceTimersByTimeAsync(100));
  expect(patches).toMatchObject([
    {
      bearer: "Bearer alice-token",
      payload: { workbench: { image: { controlPointsSize: 18, smoothImage: false } } },
    },
  ]);
});

it("当前会话直接保存失败会回滚并向调用者报告失败", async () => {
  const { result } = renderHook(useWorkbenchConfig, { wrapper });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  patchPreferences = () => Response.json({ detail: "保存失败" }, { status: 503 });
  await act(async () => {
    await expect(result.current.update({ image: { controlPointsSize: 18 } })).rejects.toMatchObject(
      {
        status: 503,
      },
    );
  });
  expect(result.current.config.image.controlPointsSize).toBe(8);
  expect(result.current.saving).toBe(false);
  expect(client.getQueryData(userPreferencesQueryKey("alice"))).toEqual(initialPreferences);
  expect(pushToast).toHaveBeenCalledWith({
    kind: "error",
    msg: "工作台设置未同步",
    sub: expect.stringContaining("恢复"),
  });
});

it("偏好传输拒绝已经退休但 token 未变化的会话，不依赖 React 先收到 storage 事件", async () => {
  const sessionId = useAuthStore.getState().sessionId!;
  storeOtherTabLogin("alice", "alice-token");
  await expect(
    authApi.updatePreferences(
      { workbench: { image: { ...DEFAULT_WORKBENCH_PREFERENCES.image, smoothImage: false } } },
      { userId: "alice", sessionId },
    ),
  ).rejects.toMatchObject({ status: 401 });
  expect(patches).toEqual([]);
});
