import type { ReactNode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useWorkbenchShortcutPreferences,
  type ShortcutWriteRequest,
} from "./useWorkbenchShortcutPreferences";
import { userPreferencesQueryKey } from "./useUserPreferences";
import { authApi, type MeResponse, type UserPreferences } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

vi.mock("@/api/auth", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  authApi: {
    getPreferences: vi.fn(),
    updatePreferences: vi.fn(),
  },
}));

const getPreferencesMock = vi.mocked(authApi.getPreferences);
const updatePreferencesMock = vi.mocked(authApi.updatePreferences);
const basePrefs = { workbench: {}, ai: {}, ui: {} } as UserPreferences;
const clients = new Set<QueryClient>();

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function login(id: string) {
  useAuthStore.getState().setAuth(`token-${id}`, { id } as MeResponse);
}

function mount(
  initial: UserPreferences | null = basePrefs,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  clients.add(client);
  if (initial) client.setQueryData(userPreferencesQueryKey("user-1"), initial);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(() => useWorkbenchShortcutPreferences(), { wrapper }) };
}

const request = (key: string, commandId = "image.tool.box"): ShortcutWriteRequest => ({
  domain: "image",
  commandId,
  bindings: [{ key, modifiers: [] }],
});
const saved = (key: string, commandId = "image.tool.box"): UserPreferences => ({
  ...basePrefs,
  workbench: {
    ...basePrefs.workbench,
    shortcuts: { schemaVersion: 1, image: { [commandId]: [{ key, modifiers: [] }] } },
  },
});
const keys = (state: ReturnType<typeof useWorkbenchShortcutPreferences>, id = "image.tool.box") =>
  state.effective.get(id)?.bindings.map((binding) => binding.key);

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  login("user-1");
  getPreferencesMock.mockResolvedValue(basePrefs);
});
afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  localStorage.clear();
});

describe("useWorkbenchShortcutPreferences", () => {
  it("空子树使用默认；成功响应直接更新生效组合", async () => {
    updatePreferencesMock.mockResolvedValue(saved("i"));
    const { result } = mount();
    expect(keys(result.current)).toEqual(["b", "1"]);
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(keys(result.current)).toEqual(["i"]));
    expect(result.current.pendingKeys.size).toBe(0);
    expect(updatePreferencesMock).toHaveBeenCalledWith({
      workbench: { shortcuts: { image: { "image.tool.box": [{ key: "i", modifiers: [] }] } } },
    });
  });

  it("派发前同命令编辑合并为最新值", async () => {
    updatePreferencesMock.mockResolvedValue(saved("x"));
    const { result } = mount();
    act(() => {
      result.current.saveCommand(request("i"));
      result.current.saveCommand(request("x"));
    });
    await waitFor(() => expect(result.current.pendingKeys.size).toBe(0));
    expect(updatePreferencesMock).toHaveBeenCalledTimes(1);
    expect(updatePreferencesMock.mock.calls[0][0].workbench?.shortcuts?.image).toEqual({
      "image.tool.box": [{ key: "x", modifiers: [] }],
    });
  });

  it("在途更新继续串行；预览保留新值而运行使用已确认值", async () => {
    const first = deferred<UserPreferences>();
    const second = deferred<UserPreferences>();
    updatePreferencesMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    act(() => result.current.saveCommand(request("x")));
    expect(result.current.previewEffective.get("image.tool.box")?.bindings[0].key).toBe("x");
    expect(keys(result.current)).toEqual(["b", "1"]);
    await act(async () => first.resolve(saved("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(2));
    expect(keys(result.current)).toEqual(["i"]);
    expect(result.current.previewEffective.get("image.tool.box")?.bindings[0].key).toBe("x");
    await act(async () => second.resolve(saved("x")));
    await waitFor(() => expect(keys(result.current)).toEqual(["x"]));
    expect(result.current.pendingKeys.size).toBe(0);
  });

  it("预览同时纳入不同命令的未完成编辑与冲突", async () => {
    updatePreferencesMock.mockReturnValue(new Promise(() => {}));
    const { result } = mount();
    act(() => {
      result.current.saveCommand(request("i"));
      result.current.saveCommand(request("i", "image.tool.select"));
    });
    expect(result.current.previewEffective.get("image.tool.box")?.conflictsWith).toContain(
      "image.tool.select",
    );
    expect(result.current.effective.get("image.tool.box")?.conflictsWith).toEqual([]);
  });

  it("失败保留预览，重试成功或放弃后清除", async () => {
    updatePreferencesMock.mockRejectedValueOnce(new Error("offline"));
    const { result } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(result.current.failedMap.size).toBe(1));
    expect(result.current.pendingKeys.size).toBe(0);
    expect(result.current.previewEffective.get("image.tool.box")?.bindings[0].key).toBe("i");
    expect(keys(result.current)).toEqual(["b", "1"]);
    updatePreferencesMock.mockResolvedValueOnce(saved("i"));
    act(() => result.current.retrySave("image", "image.tool.box"));
    await waitFor(() => expect(keys(result.current)).toEqual(["i"]));
    expect(result.current.failedMap.size).toBe(0);
    updatePreferencesMock.mockRejectedValueOnce(new Error("offline"));
    act(() => result.current.saveCommand(request("x")));
    await waitFor(() => expect(result.current.failedMap.size).toBe(1));
    act(() => result.current.discardPending("image", "image.tool.box"));
    expect(result.current.failedMap.size).toBe(0);
    expect(result.current.previewEffective.get("image.tool.box")?.bindings[0].key).toBe("i");
  });

  it("放弃排队编辑不会补发；在途已成功保存仍反映服务端状态", async () => {
    const first = deferred<UserPreferences>();
    updatePreferencesMock.mockReturnValue(first.promise);
    const { result } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    act(() => {
      result.current.saveCommand(request("x"));
      result.current.discardPending("image", "image.tool.box");
    });
    await act(async () => first.resolve(saved("i")));
    await waitFor(() => expect(keys(result.current)).toEqual(["i"]));
    expect(updatePreferencesMock).toHaveBeenCalledTimes(1);
    expect(result.current.pendingKeys.size).toBe(0);
  });

  it.each([false, true])("卸载后取消旧队列，切账号也不会补发（switch=%s）", async (switchUser) => {
    const first = deferred<UserPreferences>();
    updatePreferencesMock.mockReturnValue(first.promise);
    const { result, unmount, client } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    act(() => result.current.saveCommand(request("x")));
    unmount();
    if (switchUser) act(() => login("user-2"));
    await act(async () => first.resolve(saved("i")));
    expect(updatePreferencesMock).toHaveBeenCalledTimes(1);
    expect(client.getQueryData(userPreferencesQueryKey("user-2"))).toBeUndefined();
    expect(client.getQueryData(userPreferencesQueryKey("user-1"))).toEqual(
      switchUser ? basePrefs : saved("i"),
    );
  });

  it.each([false, true])(
    "同账号重新挂载的写入等待退休请求完成（oldFails=%s）",
    async (oldFails) => {
      const oldWrite = deferred<UserPreferences>();
      const newWrite = deferred<UserPreferences>();
      updatePreferencesMock
        .mockReturnValueOnce(oldWrite.promise)
        .mockReturnValueOnce(newWrite.promise);
      const oldView = mount();
      act(() => oldView.result.current.saveCommand(request("i")));
      await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
      // This queued edit belongs to the retiring owner and must never be sent.
      act(() => oldView.result.current.saveCommand(request("y")));
      oldView.unmount();
      const newView = mount(null, oldView.client);
      act(() => newView.result.current.saveCommand(request("x")));
      await act(async () => {});
      expect(updatePreferencesMock).toHaveBeenCalledTimes(1);
      expect(newView.result.current.previewEffective.get("image.tool.box")?.bindings[0].key).toBe(
        "x",
      );
      await act(async () => {
        if (oldFails) oldWrite.reject(new Error("retired write failed"));
        else oldWrite.resolve(saved("i"));
      });
      await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(2));
      expect(updatePreferencesMock.mock.calls[1][0].workbench?.shortcuts?.image).toEqual({
        "image.tool.box": [{ key: "x", modifiers: [] }],
      });
      expect(newView.result.current.pendingKeys.size).toBe(1);
      expect(newView.result.current.failedMap.size).toBe(0);
      await act(async () => newWrite.resolve(saved("x")));
      await waitFor(() => expect(keys(newView.result.current)).toEqual(["x"]));
      expect(newView.result.current.pendingKeys.size).toBe(0);
      expect(oldView.client.getQueryData(userPreferencesQueryKey("user-1"))).toEqual(saved("x"));
    },
  );

  it("凭据在派发前被其他标签页替换时不发送旧账号编辑", async () => {
    const { result } = mount();
    act(() => {
      result.current.saveCommand(request("i"));
      localStorage.setItem("token", "token-user-2");
    });
    await act(async () => {});
    expect(updatePreferencesMock).not.toHaveBeenCalled();
  });

  it("旧请求失败不能删除新队列的串行链", async () => {
    const first = deferred<UserPreferences>();
    const second = deferred<UserPreferences>();
    updatePreferencesMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockResolvedValue(saved("z"));
    const { result } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    act(() => result.current.saveCommand(request("x")));
    await act(async () => first.reject(new Error("old failed")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(2));
    act(() => result.current.saveCommand(request("z")));
    await act(async () => {});
    expect(updatePreferencesMock).toHaveBeenCalledTimes(2);
    await act(async () => second.resolve(saved("x")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.pendingKeys.size).toBe(0));
  });

  it("切账号时旧响应不修改新账号缓存或正在保存的队列", async () => {
    const old = deferred<UserPreferences>();
    const current = deferred<UserPreferences>();
    updatePreferencesMock.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
    const { result, client } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    act(() => result.current.saveCommand(request("x")));
    act(() => {
      client.setQueryData(userPreferencesQueryKey("user-2"), saved("y"));
      login("user-2");
    });
    await waitFor(() => expect(result.current.userId).toBe("user-2"));
    act(() => result.current.saveCommand(request("z")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(2));
    await act(async () => old.reject(new Error("old failed")));
    expect(keys(result.current)).toEqual(["y"]);
    expect(result.current.pendingKeys.size).toBe(1);
    expect(result.current.failedMap.size).toBe(0);
    await act(async () => current.resolve(saved("z")));
    await waitFor(() => expect(keys(result.current)).toEqual(["z"]));
  });

  it("PATCH 成功后 GET 失败仍保留已确认值与其他写入方的最新缓存", async () => {
    const mutation = deferred<UserPreferences>();
    updatePreferencesMock.mockReturnValue(mutation.promise);
    const { result, client } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    act(() =>
      client.setQueryData(userPreferencesQueryKey("user-1"), {
        ...saved("x", "image.tool.select"),
        ai: { selected: "new-backend" },
      }),
    );
    getPreferencesMock.mockRejectedValue(new Error("GET offline"));
    await act(async () => mutation.resolve(saved("i")));
    await waitFor(() => expect(result.current.pendingKeys.size).toBe(0));
    await act(async () => {
      await client.refetchQueries({ queryKey: userPreferencesQueryKey("user-1") });
    });
    expect(keys(result.current)).toEqual(["i"]);
    expect(keys(result.current, "image.tool.select")).toEqual(["x"]);
    expect(client.getQueryData(userPreferencesQueryKey("user-1"))).toMatchObject({
      ai: { selected: "new-backend" },
    });
    expect(result.current.failedMap.size).toBe(0);
  });

  it("保存期间开始的旧 GET 响应不能回滚已确认的快捷键", async () => {
    const staleGet = deferred<UserPreferences>();
    const mutation = deferred<UserPreferences>();
    updatePreferencesMock.mockReturnValue(mutation.promise);
    getPreferencesMock.mockReturnValue(staleGet.promise);
    const { result, client } = mount();
    act(() => result.current.saveCommand(request("i")));
    await waitFor(() => expect(updatePreferencesMock).toHaveBeenCalledTimes(1));
    void client.refetchQueries({ queryKey: userPreferencesQueryKey("user-1") });
    await act(async () => mutation.resolve(saved("i")));
    await act(async () => staleGet.resolve(basePrefs));
    await waitFor(() => expect(result.current.pendingKeys.size).toBe(0));
    expect(keys(result.current)).toEqual(["i"]);
  });

  it("未完成初次读取或加载失败时禁止写入", async () => {
    const get = deferred<UserPreferences>();
    getPreferencesMock.mockReturnValue(get.promise);
    const { result } = mount(null);
    expect(result.current.loaded).toBe(false);
    act(() => result.current.saveCommand(request("i")));
    await act(async () => get.reject(new Error("GET offline")));
    await waitFor(() => expect(result.current.loadError).toBeTruthy());
    act(() => result.current.saveCommand(request("x")));
    expect(updatePreferencesMock).not.toHaveBeenCalled();
  });

  it("损坏存量条目被排除出执行并记入 issues", () => {
    const { result } = mount({
      workbench: { shortcuts: { schemaVersion: 1, image: { "legacy.command": [{ key: "i" }] } } },
    } as unknown as UserPreferences);
    expect(result.current.issues.map((issue) => issue.commandId)).toEqual(["legacy.command"]);
    expect(result.current.effective.get("image.tool.box")?.customized).toBe(false);
  });
});
