// v0.10.10 · I17.3 · useWorkbenchConfig 单测：项目级覆盖合并优先级 + lockedFields。
// v0.15.3 · 偏好四分树:用户偏好走 image.* / common.* 子树;项目级 rendering_config 保持平铺。

import type { ReactNode } from "react";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGetPreferences = vi.hoisted(() => vi.fn());
const mockUpdatePreferences = vi.hoisted(() => vi.fn());
const mockPushToast = vi.hoisted(() => vi.fn());
const mockAuthUser = vi.hoisted(() => ({
  current: { id: "u1" } as { id: string; preferences?: unknown },
}));

vi.mock("@/api/auth", async () => {
  const actual = await vi.importActual<typeof import("@/api/auth")>("@/api/auth");
  return {
    ...actual,
    authApi: {
      getPreferences: mockGetPreferences,
      updatePreferences: mockUpdatePreferences,
    },
  };
});

// zustand store 既是 hook 又带 getState 静态方法; useWorkbenchConfig 的 flush 回调
// 走 useAuthStore.getState() 取 userId (无法在回调里调 hook), 故 mock 两者都要有。
vi.mock("@/stores/authStore", () => ({
  useAuthStore: Object.assign(
    (selector: (s: { user: unknown }) => unknown) => selector({ user: mockAuthUser.current }),
    { getState: () => ({ user: mockAuthUser.current }) },
  ),
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: { getState: () => ({ push: mockPushToast }) },
}));

import { useWorkbenchConfig } from "./useWorkbenchConfig";
import { userPreferencesQueryKey } from "./useUserPreferences";

function wrapper({ children }: { children: ReactNode }) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={c}>{children}</QueryClientProvider>;
}

describe("useWorkbenchConfig · v0.10.10 项目级覆盖", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    mockAuthUser.current = { id: "u1" };
    window.localStorage.clear();
  });

  it("首帧优先使用本地 layout 缓存，避免右栏按旧偏好闪开再收起", () => {
    window.localStorage.setItem("workbench.u1.rightOpen", "0");
    mockAuthUser.current = {
      id: "u1",
      preferences: {
        workbench: {
          layout: { rightOpen: true },
        },
      },
    };
    mockGetPreferences.mockReturnValue(new Promise(() => undefined));

    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });

    expect(result.current.loaded).toBe(false);
    expect(result.current.layout.rightOpen).toBe(false);
  });

  it("无项目覆盖时，config = DEFAULTS ∪ 用户偏好；lockedFields = []", async () => {
    mockGetPreferences.mockResolvedValue({
      workbench: { image: { smoothImage: false, cssImageFilter: "invert(1)" } },
    });
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.config.image.smoothImage).toBe(false);
    expect(result.current.config.image.cssImageFilter).toBe("invert(1)");
    // 未提供的字段走默认
    expect(result.current.config.image.controlPointsSize).toBe(6);
    expect(result.current.config.common.longTaskSampleRate).toBe(0.05);
    expect(result.current.config.common.showAnnotationComments).toBe(true);
    expect(result.current.lockedFields).toEqual([]);
  });

  it("保留账号明确关闭的标注评论提示，并按 false 写回", async () => {
    mockGetPreferences.mockResolvedValue({
      workbench: { common: { showAnnotationComments: false } },
    });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.config.common.showAnnotationComments).toBe(false);

    vi.useFakeTimers();
    act(() => result.current.setFields({ common: { showAnnotationComments: false } }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mockUpdatePreferences).toHaveBeenCalledWith({
      workbench: expect.objectContaining({
        common: expect.objectContaining({ showAnnotationComments: false }),
      }),
    });
    vi.useRealTimers();
  });

  it("项目级 rendering_config(平铺)覆盖用户级 image.* 子树；lockedFields 列出被覆盖字段", async () => {
    mockGetPreferences.mockResolvedValue({
      workbench: { image: { smoothImage: true, controlPointsSize: 10 } },
    });
    const { result } = renderHook(
      () =>
        useWorkbenchConfig({
          smoothImage: false,
          cssImageFilter: "grayscale(1)",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));
    // smoothImage 用户=true，项目=false → 项目胜
    expect(result.current.config.image.smoothImage).toBe(false);
    // cssImageFilter 用户未设，项目=grayscale → 项目胜
    expect(result.current.config.image.cssImageFilter).toBe("grayscale(1)");
    // controlPointsSize 项目未覆盖 → 沿用用户值 10
    expect(result.current.config.image.controlPointsSize).toBe(10);
    expect(result.current.lockedFields).toEqual(
      expect.arrayContaining(["smoothImage", "cssImageFilter"]),
    );
    expect(result.current.lockedFields).not.toContain("controlPointsSize");
  });

  it("项目级字段 = null/undefined 视作「不覆盖」，不进 lockedFields", async () => {
    mockGetPreferences.mockResolvedValue({
      workbench: { image: { smoothImage: true } },
    });
    const { result } = renderHook(
      () =>
        useWorkbenchConfig({
          smoothImage: null,
          cssImageFilter: undefined,
          controlPointsSize: 12,
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.config.image.smoothImage).toBe(true); // 沿用用户
    expect(result.current.config.image.controlPointsSize).toBe(12); // 项目覆盖
    expect(result.current.lockedFields).toEqual(["controlPointsSize"]);
  });

  it("setLayout 立即更新本地状态与 localStorage，并 debounce 全量 workbench PATCH", async () => {
    mockGetPreferences.mockResolvedValue({
      workbench: { image: { smoothImage: false }, layout: { rightOpen: true } },
    });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    vi.useFakeTimers();
    act(() => {
      result.current.setLayout({
        rightOpen: false,
        floatingDiscussion: {
          detached: true,
          x: 760,
          y: 180,
          w: 420,
          h: 560,
        },
        floatingInspector: {
          detached: true,
          x: 640,
          y: 80,
          w: 360,
          h: 600,
        },
      });
    });

    expect(result.current.layout.rightOpen).toBe(false);
    expect(result.current.layout.floatingInspector.detached).toBe(true);
    expect(result.current.layout.floatingDiscussion.detached).toBe(true);
    expect(window.localStorage.getItem("workbench.u1.rightOpen")).toBe("0");
    expect(window.localStorage.getItem("workbench.u1.floatingDiscussion")).toContain(
      '"detached":true',
    );
    expect(mockUpdatePreferences).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(mockUpdatePreferences).toHaveBeenCalledWith({
      workbench: expect.objectContaining({
        image: expect.objectContaining({ smoothImage: false }),
        layout: expect.objectContaining({
          rightOpen: false,
          floatingDiscussion: expect.objectContaining({
            detached: true,
            h: 560,
          }),
          floatingInspector: expect.objectContaining({
            detached: true,
            w: 360,
          }),
        }),
      }),
    });
    vi.useRealTimers();
  });

  // v0.16.8 · 选中标注浮动信息卡:位置 + 折叠态走偏好通道(跨设备),即本地立即生效 +
  // localStorage 缓存 + debounce 全量 PATCH 带上 floatingSelection。
  it("setLayout 持久化 floatingSelection(位置 + 折叠态,跨设备)", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    vi.useFakeTimers();
    act(() => {
      result.current.setLayout({
        floatingSelection: { collapsed: true, x: 900, y: 120, w: 340, h: 440 },
      });
    });

    expect(result.current.layout.floatingSelection).toEqual({
      collapsed: true,
      x: 900,
      y: 120,
      w: 340,
      h: 440,
    });
    expect(window.localStorage.getItem("workbench.u1.floatingSelection")).toContain(
      '"collapsed":true',
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(mockUpdatePreferences).toHaveBeenCalledWith({
      workbench: expect.objectContaining({
        layout: expect.objectContaining({
          floatingSelection: expect.objectContaining({ collapsed: true, x: 900, h: 440 }),
        }),
      }),
    });
    vi.useRealTimers();
  });

  it("setLayout 支持 3D 相机面板和主视角快照", async () => {
    window.localStorage.setItem("workbench.u1.triViewFloat", '{"collapsed":true,"x":44}');
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    const pointcloudCamera = {
      position: [1, 2, 3] as [number, number, number],
      target: [0, 0, 0] as [number, number, number],
      up: [0, 0, 1] as [number, number, number],
      mode: "orbit" as const,
    };

    act(() => {
      result.current.setLayout({
        cameraPanels: { front: { x: 120, y: 80, collapsed: true } },
        pointcloudCamera,
        triViewFloat: { collapsed: false, x: 100 },
      });
    });

    expect(result.current.layout.cameraPanels.front).toEqual({
      x: 120,
      y: 80,
      collapsed: true,
    });
    expect(result.current.layout.pointcloudCamera).toEqual(pointcloudCamera);
    expect(window.localStorage.getItem("workbench.u1.cameraPanels")).toContain("front");
    expect(window.localStorage.getItem("workbench.u1.pointcloudCamera")).toContain("position");
    expect(window.localStorage.getItem("workbench.u1.triViewFloat")).toBe(
      '{"collapsed":true,"x":44}',
    );
    await waitFor(() => expect(mockUpdatePreferences).toHaveBeenCalled());
    expect(mockUpdatePreferences.mock.calls.slice(-1)[0][0].workbench.layout).not.toHaveProperty(
      "triViewFloat",
    );
    expect(
      mockUpdatePreferences.mock.calls.slice(-1)[0][0].workbench.layout.cameraPanels.front.x,
    ).toBe(120);
  });

  it("较早的布局保存晚返回时不覆盖更新后的相机面板状态", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    const pending: Array<{
      payload: { workbench: Record<string, unknown> };
      resolve: (value: { workbench: Record<string, unknown> }) => void;
    }> = [];
    mockUpdatePreferences.mockImplementation(
      (payload: { workbench: Record<string, unknown> }) =>
        new Promise((resolve) => pending.push({ payload, resolve })),
    );
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    vi.useFakeTimers();
    act(() => {
      result.current.setLayout({
        cameraPanels: { front: { x: null, y: null, collapsed: true } },
      });
    });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(pending).toHaveLength(1);

    act(() => {
      result.current.setLayout({ cameraPanels: {} });
    });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(pending).toHaveLength(2);

    await act(async () => {
      pending[0].resolve(pending[0].payload);
      await Promise.resolve();
    });
    expect(result.current.layout.cameraPanels).toEqual({});

    await act(async () => {
      pending[1].resolve(pending[1].payload);
      await Promise.resolve();
    });
    expect(result.current.layout.cameraPanels).toEqual({});
    vi.useRealTimers();
  });
});

describe("useWorkbenchConfig · v0.15.3 setFields + 多实例广播", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    mockAuthUser.current = { id: "u1" };
    window.localStorage.clear();
  });

  it("setFields 子树级 patch 立即生效并 debounce PATCH", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    vi.useFakeTimers();
    act(() => {
      result.current.setFields({ image: { controlPointsSize: 12 } });
    });
    // 本地立即生效（画布实时预览），未到 300ms 不发请求
    expect(result.current.config.image.controlPointsSize).toBe(12);
    // 其余字段不被 patch 踩掉
    expect(result.current.config.image.smoothImage).toBe(true);
    expect(mockUpdatePreferences).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(mockUpdatePreferences).toHaveBeenCalledWith({
      workbench: expect.objectContaining({
        image: expect.objectContaining({ controlPointsSize: 12 }),
      }),
    });
    vi.useRealTimers();
  });

  it("keeps workspace in local config while both legacy setters omit it from PATCH", async () => {
    const workspace = { engine: "dockview@8", contexts: {} };
    mockGetPreferences.mockResolvedValue({ workbench: { layout: { workspace } } });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.layout.workspace).toEqual(workspace);

    vi.useFakeTimers();
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    act(() => result.current.setLayout({ rightOpen: false }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mockUpdatePreferences).toHaveBeenCalledTimes(2);
    for (const [payload] of mockUpdatePreferences.mock.calls) {
      expect(payload.workbench.layout).not.toHaveProperty("workspace");
    }
    vi.useRealTimers();
  });

  it("a late legacy save response cannot replace a newer workspace cache", async () => {
    const old = { workbench: { layout: { workspace: { engine: "dockview@8", contexts: {} } } } };
    mockGetPreferences.mockResolvedValue(old);
    let resolve!: (response: typeof old) => void;
    mockUpdatePreferences.mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useWorkbenchConfig(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    const workspace = {
      engine: "dockview@8",
      contexts: { "review:video": { schemaVersion: 2, snapshot: { updated: true } } },
    };
    act(() =>
      client.setQueryData(userPreferencesQueryKey("u1"), { workbench: { layout: { workspace } } }),
    );
    await act(async () => resolve(old));
    expect(client.getQueryData(userPreferencesQueryKey("u1"))).toMatchObject({
      workbench: { layout: { workspace } },
    });
    vi.useRealTimers();
  });

  it("a late settings response preserves shortcuts confirmed by their own writer", async () => {
    const old = { workbench: { shortcuts: { schemaVersion: 1, image: {} } } };
    mockGetPreferences.mockResolvedValue(old);
    let resolve!: (response: typeof old) => void;
    mockUpdatePreferences.mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, unmount } = renderHook(() => useWorkbenchConfig(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mockUpdatePreferences.mock.calls[0][0].workbench).not.toHaveProperty("shortcuts");
    const shortcuts = {
      schemaVersion: 1,
      image: { "image.tool.box": [{ key: "i", modifiers: [] }] },
    };
    act(() => client.setQueryData(userPreferencesQueryKey("u1"), { workbench: { shortcuts } }));
    await act(async () => resolve(old));
    expect(client.getQueryData(userPreferencesQueryKey("u1"))).toMatchObject({
      workbench: { shortcuts },
    });
    unmount();
    client.clear();
    vi.useRealTimers();
  });

  it("一个实例 setFields 后，另一实例(画布)同步收到新值 —— 抽屉实时预览链路", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const drawer = renderHook(() => useWorkbenchConfig(), { wrapper });
    const canvas = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(drawer.result.current.loaded).toBe(true));
    await waitFor(() => expect(canvas.result.current.loaded).toBe(true));

    act(() => {
      drawer.result.current.setFields({ image: { smoothImage: false } });
    });
    expect(canvas.result.current.config.image.smoothImage).toBe(false);
  });
});

describe("useWorkbenchConfig · 设置读写失败与离开页面", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    mockAuthUser.current = { id: "u1" };
    window.localStorage.clear();
  });

  it("加载中或首次读取失败不允许 setFields，重试成功后保留远端字段再写入", async () => {
    const error = new Error("offline");
    mockGetPreferences.mockRejectedValueOnce(error).mockResolvedValue({
      workbench: { image: { cssImageFilter: "invert(1)", controlPointsSize: 9 } },
    });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const { result, unmount } = renderHook(() => useWorkbenchConfig(), { wrapper });

    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    expect(result.current.config.image.controlPointsSize).toBe(6);
    await waitFor(() => expect(result.current.loadError).toBe(error));
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    expect(result.current.config.image.controlPointsSize).toBe(6);
    expect(mockUpdatePreferences).not.toHaveBeenCalled();

    act(() => result.current.retryLoad());
    await waitFor(() => expect(result.current.config.image.controlPointsSize).toBe(9));
    expect(result.current.loadError).toBeNull();
    expect(result.current.loaded).toBe(true);
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    unmount();
    expect(mockUpdatePreferences).toHaveBeenCalledWith({
      workbench: expect.objectContaining({
        image: expect.objectContaining({ cssImageFilter: "invert(1)", controlPointsSize: 12 }),
      }),
    });
  });

  it("已有成功数据时，后台刷新失败仍允许基于该数据编辑", async () => {
    mockGetPreferences.mockResolvedValueOnce({
      workbench: { image: { cssImageFilter: "invert(1)" } },
    });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, unmount } = renderHook(() => useWorkbenchConfig(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    mockGetPreferences.mockRejectedValue(new Error("offline"));
    act(() => result.current.retryLoad());
    await waitFor(() =>
      expect(client.getQueryState(["me", "preferences", "u1"])?.status).toBe("error"),
    );
    expect(result.current.loadError).toBeNull();
    expect(result.current.loaded).toBe(true);
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    expect(result.current.config.image.cssImageFilter).toBe("invert(1)");
    expect(result.current.config.image.controlPointsSize).toBe(12);
    unmount();
  });

  it("防抖保存失败提示且保留本地值，下一次修改重试完整配置", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences
      .mockRejectedValueOnce(new Error("offline"))
      .mockImplementation(async (payload) => payload);
    const { result } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    vi.useFakeTimers();
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(result.current.config.image.controlPointsSize).toBe(12);
    expect(result.current.saving).toBe(false);
    expect(mockPushToast).toHaveBeenCalledWith({
      kind: "error",
      msg: "工作台设置未同步",
      sub: expect.stringContaining("保留本次修改"),
    });

    act(() => result.current.setFields({ image: { smoothImage: false } }));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mockUpdatePreferences).toHaveBeenLastCalledWith({
      workbench: expect.objectContaining({
        image: expect.objectContaining({ controlPointsSize: 12, smoothImage: false }),
      }),
    });
    expect(mockPushToast).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("防抖结束前卸载会立即提交最后编辑，并更新共享缓存", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences.mockImplementation(async (payload) => payload);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, unmount } = renderHook(() => useWorkbenchConfig(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => {
      result.current.setFields({ image: { cssImageFilter: "invert(1)" } });
      result.current.setFields({ image: { cssImageFilter: "grayscale(1)" } });
    });
    expect(mockUpdatePreferences).not.toHaveBeenCalled();
    unmount();
    expect(mockUpdatePreferences).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(client.getQueryData(["me", "preferences", "u1"])).toMatchObject({
        workbench: { image: { cssImageFilter: "grayscale(1)" } },
      }),
    );
  });

  it("卸载后的 flush 失败仍显示提示", async () => {
    mockGetPreferences.mockResolvedValue({ workbench: {} });
    mockUpdatePreferences.mockRejectedValue(new Error("offline"));
    const { result, unmount } = renderHook(() => useWorkbenchConfig(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    act(() => result.current.setFields({ image: { controlPointsSize: 12 } }));
    unmount();
    await waitFor(() =>
      expect(mockPushToast).toHaveBeenCalledWith({
        kind: "error",
        msg: "工作台设置未同步",
        sub: expect.stringContaining("离开页面前的修改保存失败"),
      }),
    );
  });
});
