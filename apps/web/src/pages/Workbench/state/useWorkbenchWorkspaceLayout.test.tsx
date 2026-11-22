import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_WORKBENCH_PREFERENCES, type UserPreferences } from "@/api/auth";
import { ApiError } from "@/api/client";
import { createWorkspacePreset } from "../layout/workbenchLayoutPresets";
import type {
  WorkspaceContext,
  WorkspaceEnvelope,
  WorkspaceGroup,
  WorkspaceNode,
  WorkspaceSnapshot,
} from "../layout/workbenchLayoutSnapshot";
import { userPreferencesQueryKey } from "./useUserPreferences";
import { useWorkbenchWorkspaceLayout, workspaceStorageKey } from "./useWorkbenchWorkspaceLayout";

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  user: { id: "u1" },
}));

vi.mock("@/api/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/auth")>()),
  authApi: { getPreferences: mocks.get, updatePreferences: mocks.patch },
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: Object.assign(
    (selector: (state: { user: { id: string } }) => unknown) => selector({ user: mocks.user }),
    { getState: () => ({ user: mocks.user }) },
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const initial = createWorkspacePreset("standard");
const changed = createWorkspacePreset("review");
const latest = createWorkspacePreset("focus");
function legacySnapshot(snapshot: WorkspaceSnapshot, version = 1): unknown {
  const legacy = structuredClone(snapshot);
  const removed =
    version >= 3
      ? ["tri-view", "camera-view"]
      : ["ai-task", "video-tracker", "tri-view", "camera-view"];
  for (const id of removed) {
    delete legacy.layout.panels[id];
    delete legacy.returns[id as keyof typeof legacy.returns];
  }
  delete (legacy as Partial<WorkspaceSnapshot>).cameraPresentation;
  if (version < 3) delete (legacy as Partial<WorkspaceSnapshot>).visibilityIntent;
  else {
    delete (legacy.visibilityIntent as Partial<WorkspaceSnapshot["visibilityIntent"]>)["tri-view"];
    delete (legacy.visibilityIntent as Partial<WorkspaceSnapshot["visibilityIntent"]>)[
      "camera-view"
    ];
  }
  const strip = (group: WorkspaceGroup) => {
    group.views = group.views.filter((id) => !removed.includes(id));
    if (group.activeView && !group.views.includes(group.activeView))
      group.activeView = group.views[0];
  };
  const visit = (node: WorkspaceNode) =>
    node.type === "branch" ? node.data.forEach(visit) : strip(node.data);
  visit(legacy.layout.grid.root);
  return legacy;
}
const v1 = (snapshot = initial): WorkspaceEnvelope => ({
  schemaVersion: 1,
  snapshot: legacySnapshot(snapshot),
});
const v2 = (snapshot = initial): WorkspaceEnvelope => ({
  schemaVersion: 2,
  snapshot: legacySnapshot(snapshot),
});
const v5 = (snapshot = initial): WorkspaceEnvelope => ({ schemaVersion: 5, snapshot });

function preferences(
  envelope: WorkspaceEnvelope | undefined = v1(),
  context: WorkspaceContext = "annotate:image",
): UserPreferences {
  return {
    workbench: {
      ...DEFAULT_WORKBENCH_PREFERENCES,
      layout: {
        ...DEFAULT_WORKBENCH_PREFERENCES.layout,
        workspace: { engine: "dockview@8", contexts: envelope ? { [context]: envelope } : {} },
      },
    },
    ai: {},
    ui: {},
  };
}

function contextsOf(prefs: UserPreferences) {
  return (prefs.workbench.layout.workspace!.contexts ??= {});
}

function setup(context: WorkspaceContext = "annotate:image", paused = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return {
    client,
    ...renderHook<
      ReturnType<typeof useWorkbenchWorkspaceLayout>,
      { currentContext: WorkspaceContext; paused?: boolean }
    >(
      ({
        currentContext,
        paused: isPaused,
      }: {
        currentContext: WorkspaceContext;
        paused?: boolean;
      }) => useWorkbenchWorkspaceLayout(currentContext, initial, initial, isPaused),
      { wrapper, initialProps: { currentContext: context, paused } },
    ),
  };
}

beforeEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  mocks.user = { id: "u1" };
  window.localStorage.clear();
  mocks.get.mockResolvedValue(preferences());
  mocks.patch.mockImplementation(async (payload) => ({
    ...preferences(),
    workbench: { ...DEFAULT_WORKBENCH_PREFERENCES, layout: payload.workbench.layout },
  }));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("workspace layout owner", () => {
  it("migrates 3D visibility only from the initial account GET and persists it once", async () => {
    window.localStorage.setItem("workbench.triViewFloat", '{"collapsed":false,"x":9000}');
    window.localStorage.setItem("workbench.u1.triViewFloat", '{"collapsed":false,"x":9000}');
    window.localStorage.setItem(workspaceStorageKey("u1", "annotate:3d"), JSON.stringify(v1()));
    const remote = preferences(v1(), "annotate:3d");
    remote.workbench.layout.triViewFloat = { collapsed: true, x: 30, y: 20, w: 250, h: 600 };
    remote.workbench.layout.cameraPanels = { front: { x: 55, y: 70, collapsed: true } };
    mocks.get.mockResolvedValue(remote);
    const { result, client } = setup("annotate:3d");
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.snapshot.visibilityIntent["tri-view"]).toBe("hidden");
    expect(result.current.snapshot.visibilityIntent["camera-view"]).toBe("shown");
    expect(result.current.snapshot.cameraPresentation).toBe("floating");
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0][0].workbench.layout).not.toHaveProperty("cameraPanels");
    expect(
      mocks.patch.mock.calls[0][0].workbench.layout.workspace.contexts["annotate:3d"].schemaVersion,
    ).toBe(5);
    act(() => client.setQueryData(userPreferencesQueryKey("u1"), preferences(v1(), "annotate:3d")));
    expect(result.current.snapshot.visibilityIntent["tri-view"]).toBe("hidden");
    expect(window.localStorage.getItem("workbench.u1.triViewFloat")).toContain("9000");
  });
  it("renders the account cache, blocks every write until initial GET, then hydrates once", async () => {
    window.localStorage.setItem(
      workspaceStorageKey("u1", "annotate:image"),
      JSON.stringify(v1(changed)),
    );
    const get = deferred<UserPreferences>();
    mocks.get.mockReturnValue(get.promise);
    const { result, client } = setup();
    expect(result.current.snapshot).toEqual(changed);
    expect(result.current.readOnly).toBe(true);
    expect(result.current.save(latest)).toBe(false);
    expect(result.current.reset(initial)).toBe(false);
    expect(mocks.patch).not.toHaveBeenCalled();

    await act(async () => get.resolve(preferences(v1(latest))));
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.snapshot).toEqual(latest);
    expect(result.current.restoreRevision).toBe(1);
    expect(result.current.readOnly).toBe(false);
    act(() => client.setQueryData(userPreferencesQueryKey("u1"), preferences()));
    expect(result.current.snapshot).toEqual(latest);
    expect(result.current.restoreRevision).toBe(1);
  });

  it("waits for an initial refetch even when the shared query already has stale data", async () => {
    const get = deferred<UserPreferences>();
    mocks.get.mockReturnValue(get.promise);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(userPreferencesQueryKey("u1"), preferences(), { updatedAt: 1 });
    const { result } = renderHook(() => useWorkbenchWorkspaceLayout("annotate:image", initial), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    expect(result.current.initialized).toBe(false);
    await act(async () => get.resolve(preferences(v2(changed))));
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.snapshot).toEqual(changed);
  });

  it("unlocks local changes after an initial GET failure and ignores subsequent refetch", async () => {
    mocks.get.mockRejectedValue(new Error("offline"));
    const { result, client } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.readOnly).toBe(false);
    expect(result.current.error).toBeTruthy();
    vi.useFakeTimers();
    act(() => {
      expect(result.current.save(changed)).toBe(true);
    });
    act(() => client.setQueryData(userPreferencesQueryKey("u1"), preferences(v1(latest))));
    expect(result.current.snapshot).toEqual(initial);
    expect(result.current.restoreRevision).toBe(0);
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
  });

  it("persists the initial legacy conversion only when the authoritative context is absent", async () => {
    window.localStorage.setItem("wb:ai-popover-position", '{"left":48,"top":36}');
    window.localStorage.setItem("wb:video-tracker-panel-size", '{"w":420,"h":560}');
    const get = preferences();
    get.workbench.layout.workspace!.contexts = {};
    mocks.get.mockResolvedValue(get);
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.dirty).toBe(true);
    expect(result.current.restoreRevision).toBe(0);
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0][0].workbench.layout.workspace.contexts).toEqual({
      "annotate:image": v5(),
    });
    expect(window.localStorage.getItem("wb:ai-popover-position")).toBe('{"left":48,"top":36}');
    expect(window.localStorage.getItem("wb:video-tracker-panel-size")).toBe('{"w":420,"h":560}');
  });

  it("debounces, allows one request in flight and writes only the newest pending context snapshot", async () => {
    const requests = [deferred<UserPreferences>(), deferred<UserPreferences>()];
    mocks.patch.mockReturnValueOnce(requests[0].promise).mockReturnValueOnce(requests[1].promise);
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.save(changed));
    await act(async () => vi.advanceTimersByTimeAsync(299));
    expect(mocks.patch).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    act(() => {
      result.current.save(initial);
      result.current.save(latest);
    });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    await act(async () => requests[0].resolve(preferences(v1(changed))));
    expect(mocks.patch).toHaveBeenCalledTimes(2);
    expect(mocks.patch.mock.calls[1][0]).toEqual({
      workbench: {
        layout: { workspace: { engine: "dockview@8", contexts: { "annotate:image": v5(latest) } } },
      },
    });
    expect(result.current.snapshot).toEqual(initial);
    await act(async () => requests[1].resolve(preferences(v1(latest))));
    expect(result.current.dirty).toBe(false);
  });

  it("keeps failed saves dirty without retry loops or rolling back the visible layout", async () => {
    mocks.patch.mockRejectedValueOnce(new ApiError(500, "offline"));
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.save(changed));
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(result.current.dirty).toBe(true);
    expect(result.current.error).toBeTruthy();
    expect(result.current.readOnly).toBe(false);
    expect(
      JSON.parse(window.localStorage.getItem(workspaceStorageKey("u1", "annotate:image"))!),
    ).toEqual(v5(changed));
    act(() => result.current.save(latest));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(result.current.dirty).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("a schema downgrade conflict discards pending writes and makes reset read-only", async () => {
    const save = deferred<UserPreferences>();
    mocks.patch.mockReturnValue(save.promise);
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.save(changed));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    act(() => result.current.save(latest));
    await act(async () =>
      save.reject(new ApiError(409, "Conflict", { reason: "layout_schema_downgrade" })),
    );
    expect(result.current.readOnlyReason).toBe("newer-schema");
    expect(result.current.snapshot).toEqual(initial);
    expect(result.current.dirty).toBe(false);
    expect(result.current.reset(initial)).toBe(false);
    const revision = result.current.restoreRevision;
    act(() => result.current.failRestore());
    expect(result.current.readOnlyReason).toBe("newer-schema");
    expect(result.current.restoreRevision).toBe(revision);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
  });

  it.each([6, 7] as const)(
    "does not interpret a future schema %s as v1 or permit resetting it",
    async (schemaVersion) => {
      mocks.get.mockResolvedValue(
        preferences({ schemaVersion, snapshot: { future: true } } as unknown as WorkspaceEnvelope),
      );
      const { result } = setup();
      await waitFor(() => expect(result.current.initialized).toBe(true));
      expect(result.current.readOnlyReason).toBe("newer-schema");
      expect(result.current.reset(initial)).toBe(false);
      expect(mocks.patch).not.toHaveBeenCalled();
    },
  );

  it("invalid snapshots require an explicit reset before a standard layout may be saved", async () => {
    mocks.get.mockResolvedValue(
      preferences({ schemaVersion: 1, snapshot: {} } as WorkspaceEnvelope),
    );
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.readOnlyReason).toBe("invalid");
    expect(result.current.save(changed)).toBe(false);
    vi.useFakeTimers();
    act(() => {
      expect(result.current.reset(initial)).toBe(true);
    });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(result.current.readOnly).toBe(false);
    expect(mocks.patch).toHaveBeenCalledTimes(1);
  });

  it("reports a restore failure once without creating repeated restore revisions", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    act(() => {
      result.current.failRestore();
      result.current.failRestore();
    });
    expect(result.current.readOnlyReason).toBe("restore-failed");
    expect(result.current.restoreRevision).toBe(1);
    act(() => result.current.failRestore());
    expect(result.current.restoreRevision).toBe(1);
  });

  it("pauses initial desktop migration writes in compact mode and saves after resuming", async () => {
    const remote = preferences();
    remote.workbench.layout.workspace!.contexts = {};
    mocks.get.mockResolvedValue(remote);
    const { result, rerender } = setup("annotate:image", true);
    await waitFor(() => expect(result.current.initialized).toBe(true));
    vi.useFakeTimers();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(result.current.dirty).toBe(true);
    expect(mocks.patch).not.toHaveBeenCalled();
    rerender({ currentContext: "annotate:image", paused: false });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(
      mocks.patch.mock.calls[0][0].workbench.layout.workspace.contexts["annotate:image"],
    ).toEqual(v5());
  });

  it("waits for a previous context save and keeps its cache authoritative when returning", async () => {
    const patch = deferred<UserPreferences>();
    mocks.patch.mockReturnValue(patch.promise);
    const remote = preferences();
    contextsOf(remote)["review:video"] = v1(changed);
    mocks.get.mockResolvedValue(remote);
    const { result, rerender, client } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.save(latest));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    rerender({ currentContext: "review:video" });
    rerender({ currentContext: "annotate:image" });
    expect(result.current.snapshot).toEqual(latest);
    expect(result.current.initialized).toBe(false);
    await act(async () => {
      patch.resolve(preferences(v1(latest)));
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.initialized).toBe(true);
    expect(result.current.snapshot).toEqual(latest);
    expect(
      client.getQueryData<UserPreferences>(userPreferencesQueryKey("u1"))?.workbench.layout
        .workspace?.contexts?.["annotate:image"],
    ).toEqual(v5(latest));
  });

  it("flushes a pending layout before switching contexts", async () => {
    const patch = deferred<UserPreferences>();
    mocks.patch.mockReturnValue(patch.promise);
    const remote = preferences();
    contextsOf(remote)["review:video"] = v1(changed);
    mocks.get.mockResolvedValue(remote);
    const { result, rerender } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    act(() => result.current.save(latest));

    rerender({ currentContext: "review:video" });
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(mocks.patch.mock.calls[0][0].workbench.layout.workspace.contexts).toEqual({
      "annotate:image": v5(latest),
    });

    rerender({ currentContext: "annotate:image" });
    await act(async () => {
      patch.resolve(preferences(v5(latest)));
      await patch.promise;
    });
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.snapshot).toEqual(latest);
  });

  it.each([
    "broken",
    null,
    { engine: "dockview@8", contexts: null },
    { engine: "dockview@8", contexts: [] },
    { engine: "dockview@8", contexts: { "annotate:image": null } },
    { engine: "dockview@8", contexts: { unexpected: v1() } },
  ])(
    "keeps malformed workspace wrappers read-only instead of crashing or migrating over them",
    async (workspace) => {
      const remote = preferences();
      remote.workbench.layout.workspace = workspace as typeof remote.workbench.layout.workspace;
      mocks.get.mockResolvedValue(remote);
      const { result } = setup();
      await waitFor(() => expect(result.current.initialized).toBe(true));
      expect(result.current.readOnlyReason).toBe("invalid");
      expect(result.current.snapshot).toEqual(initial);
      expect(result.current.dirty).toBe(false);
      expect(result.current.save(changed)).toBe(false);
      expect(mocks.patch).not.toHaveBeenCalled();
    },
  );

  it("seeds the initial layout for an account that only saved named presets", async () => {
    const remote = preferences(undefined);
    remote.workbench.layout.workspace = {
      engine: "dockview@8",
      namedPresets: {
        p1: { schemaVersion: 5, snapshot: initial, name: "审核宽讨论", context: "review:image" },
      },
    };
    mocks.get.mockResolvedValue(remote);
    const { result } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.readOnlyReason).toBe(null);
    expect(result.current.readOnly).toBe(false);
    expect(result.current.snapshot).toEqual(initial);
  });

  it("keeps saved named presets in the shared cache after a layout write", async () => {
    const preset = {
      schemaVersion: 5 as const,
      snapshot: initial,
      name: "审核宽讨论",
      context: "review:image" as const,
    };
    const remote = preferences();
    remote.workbench.layout.workspace!.namedPresets = { p1: preset };
    mocks.get.mockResolvedValue(remote);
    const { result, client } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    act(() => void result.current.save(changed));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch.mock.calls[0][0].workbench.layout.workspace).not.toHaveProperty(
      "namedPresets",
    );
    await waitFor(() =>
      expect(
        client.getQueryData<UserPreferences>(userPreferencesQueryKey("u1"))?.workbench.layout
          .workspace?.namedPresets,
      ).toEqual({ p1: preset }),
    );
  });

  it("flushes a previous context's pending snapshot before switching contexts", async () => {
    const get = preferences();
    contextsOf(get)["review:video"] = v1(latest);
    mocks.get.mockResolvedValue(get);
    const { result, rerender } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    act(() => result.current.save(changed));
    rerender({ currentContext: "review:video" });
    await waitFor(() => expect(result.current.initialized).toBe(true));
    expect(result.current.snapshot).toEqual(latest);
    expect(result.current.dirty).toBe(false);
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(
      JSON.parse(window.localStorage.getItem(workspaceStorageKey("u1", "annotate:image"))!),
    ).toEqual(v5(changed));
  });

  it("isolates cache, initial hydration and in-flight callbacks when the account changes", async () => {
    const patch = deferred<UserPreferences>();
    mocks.patch.mockReturnValue(patch.promise);
    const { result, rerender } = setup();
    await waitFor(() => expect(result.current.initialized).toBe(true));
    vi.useFakeTimers();
    act(() => result.current.save(changed));
    await act(async () => vi.advanceTimersByTimeAsync(300));
    mocks.user = { id: "u2" };
    const get = deferred<UserPreferences>();
    mocks.get.mockReturnValue(get.promise);
    window.localStorage.setItem(
      workspaceStorageKey("u2", "annotate:image"),
      JSON.stringify(v1(latest)),
    );
    rerender({ currentContext: "annotate:image" });
    expect(result.current.snapshot).toEqual(latest);
    expect(result.current.initialized).toBe(false);
    await act(async () => patch.resolve(preferences(v1(changed))));
    expect(result.current.snapshot).toEqual(latest);
    await act(async () => {
      get.resolve(preferences());
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.snapshot).toEqual(initial);
    expect(result.current.initialized).toBe(true);
  });
});
