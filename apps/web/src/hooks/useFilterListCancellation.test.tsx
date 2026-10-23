import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";

const { listProjects, listDatasets, listTemplates, projectStats } = vi.hoisted(() => ({
  listProjects: vi.fn().mockResolvedValue([]),
  listDatasets: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 }),
  listTemplates: vi.fn().mockResolvedValue([]),
  projectStats: vi.fn().mockResolvedValue({}),
}));

vi.mock("@/api/projects", () => ({
  projectsApi: {
    list: (...args: unknown[]) => listProjects(...args),
    stats: (...args: unknown[]) => projectStats(...args),
  },
}));

vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    list: (...args: unknown[]) => listDatasets(...args),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    listItems: vi.fn(),
    scanItems: vi.fn(),
    backfillDimensions: vi.fn(),
    backfillMedia: vi.fn(),
    linkProject: vi.fn(),
    unlinkProject: vi.fn(),
    getLinkedProjects: vi.fn(),
    listForProject: vi.fn(),
  },
}));

vi.mock("@/api/storageConnections", () => ({
  storageConnectionsApi: { importFromConnection: vi.fn() },
}));

vi.mock("@/api/projectTemplates", () => ({
  projectTemplatesApi: {
    list: (...args: unknown[]) => listTemplates(...args),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    duplicate: vi.fn(),
  },
}));

import { useDatasets } from "./useDatasets";
import { useProjectTemplates } from "./useProjectTemplates";
import { useProjectStats, useProjects } from "./useProjects";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("filter list cancellation plumbing", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("filter-list-token", { id: "filter-list-user" } as never);
    listProjects.mockReset().mockResolvedValue([]);
    listDatasets.mockReset().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    listTemplates.mockReset().mockResolvedValue([]);
    projectStats.mockReset().mockResolvedValue({});
  });

  afterEach(() => {
    useAuthStore.getState().logout();
  });

  it("forwards TanStack Query AbortSignals and cancels an obsolete project request", async () => {
    let resolveObsolete: ((value: unknown) => void) | undefined;
    listProjects.mockImplementation((params: { search?: string }) => {
      if (params.search === "old") {
        return new Promise((resolve) => {
          resolveObsolete = resolve;
        });
      }
      return Promise.resolve([]);
    });

    const hook = renderHook(
      ({ search }: { search: string }) => ({
        projects: useProjects({ search }),
        datasets: useDatasets({ search: "car" }),
        templates: useProjectTemplates({ scope: "private", search: "car" }),
      }),
      { initialProps: { search: "old" }, wrapper },
    );

    await waitFor(() => {
      expect(listProjects).toHaveBeenCalledWith(
        { search: "old" },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(listDatasets).toHaveBeenCalledWith(
        { search: "car" },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(listTemplates).toHaveBeenCalledWith(
        { scope: "private", search: "car" },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
    });
    const obsoleteSignal = listProjects.mock.calls[0][1].signal as AbortSignal;

    hook.rerender({ search: "new" });
    await waitFor(() =>
      expect(listProjects).toHaveBeenCalledWith(
        { search: "new" },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    await waitFor(() => expect(obsoleteSignal.aborted).toBe(true));
    resolveObsolete?.([]);
    hook.unmount();
  });

  it("scopes filter-list cache entries to the authenticated account and token", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const hook = renderHook(
      () => ({
        projects: useProjects({ search: "car" }),
        stats: useProjectStats(),
        datasets: useDatasets({ search: "car" }),
        templates: useProjectTemplates({ scope: "private", search: "car" }),
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    );

    await waitFor(() => expect(hook.result.current.projects.isSuccess).toBe(true));
    expect(client.getQueryCache().findAll({ queryKey: ["projects"] })[0]?.queryKey).toEqual([
      "projects",
      { search: "car" },
      "filter-list-user",
      "filter-list-token",
    ]);
    expect(client.getQueryCache().findAll({ queryKey: ["datasets"] })[0]?.queryKey).toEqual([
      "datasets",
      { search: "car" },
      "filter-list-user",
      "filter-list-token",
      getAuthSessionRevision(),
    ]);
    expect(client.getQueryCache().findAll({ queryKey: ["project-stats"] })[0]?.queryKey).toEqual([
      "project-stats",
      "filter-list-user",
      "filter-list-token",
    ]);
    hook.unmount();
  });

  it("retires a dataset read across a same-account login with the same token", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 60_000 } },
    });
    let resolveOld: ((value: unknown) => void) | undefined;
    listDatasets.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    const hook = renderHook(() => useDatasets({ search: "car" }), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await waitFor(() => expect(listDatasets).toHaveBeenCalledTimes(1));
    const obsoleteSignal = listDatasets.mock.calls[0][1].signal as AbortSignal;
    act(() => {
      useAuthStore.getState().logout();
      useAuthStore.getState().setAuth("filter-list-token", { id: "filter-list-user" } as never);
    });
    await waitFor(() => expect(listDatasets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(obsoleteSignal.aborted).toBe(true);
    await act(async () => resolveOld?.({ items: [{ id: "retired" }], total: 1 }));
    expect(hook.result.current.data?.items).toEqual([]);
    hook.unmount();
    client.clear();
  });
});
