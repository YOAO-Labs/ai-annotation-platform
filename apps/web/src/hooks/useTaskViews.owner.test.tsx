import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

const { queryTask, querySummary } = vi.hoisted(() => ({
  queryTask: vi.fn(),
  querySummary: vi.fn(),
}));

vi.mock("@/api/taskViews", () => ({
  taskViewsApi: {
    list: vi.fn(),
    query: (...args: unknown[]) => queryTask(...args),
    schema: vi.fn(),
    queryObjects: vi.fn(),
    queryTracks: vi.fn(),
    objectDetail: vi.fn(),
    trackDetail: vi.fn(),
    summary: (...args: unknown[]) => querySummary(...args),
    matches: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    copy: vi.fn(),
  },
}));

import { useDataManagerSummary, useProjectTaskQuery } from "./useTaskViews";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function payload(marker: string) {
  return {
    filter_json: { marker },
    sort_json: [],
    columns_json: [],
    limit: 20,
    offset: 0,
  };
}

describe("Data Manager auth-owned query state", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("owner-token-a", { id: "owner-a" } as MeResponse);
    queryTask.mockReset();
    querySummary.mockReset();
  });

  afterEach(() => {
    act(() => useAuthStore.getState().logout());
  });

  it("keeps same-owner filter placeholders but drops previous project and token data", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const requests: Array<{
      projectId: string;
      payload: {
        filter_json: { marker: string };
        sort_json: never[];
        columns_json: never[];
        limit: number;
        offset: number;
      };
      pending: ReturnType<typeof deferred<{ marker: string }>>;
    }> = [];
    queryTask.mockImplementation(
      (
        projectId: string,
        requestPayload: {
          filter_json: { marker: string };
          sort_json: never[];
          columns_json: never[];
          limit: number;
          offset: number;
        },
      ) => {
        const pending = deferred<{ marker: string }>();
        requests.push({ projectId, payload: requestPayload, pending });
        return pending.promise;
      },
    );

    const hook = renderHook(
      ({
        projectId,
        requestPayload,
      }: {
        projectId: string;
        requestPayload: ReturnType<typeof payload>;
      }) => useProjectTaskQuery(projectId, requestPayload),
      {
        initialProps: { projectId: "project-a", requestPayload: payload("first") },
        wrapper: wrapper(client),
      },
    );
    await waitFor(() => expect(requests).toHaveLength(1));
    requests[0].pending.resolve({ marker: "first" });
    await waitFor(() => expect(hook.result.current.data).toEqual({ marker: "first" }));

    hook.rerender({ projectId: "project-a", requestPayload: payload("second") });
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(hook.result.current.data).toEqual({ marker: "first" });
    expect(hook.result.current.isPlaceholderData).toBe(true);
    requests[1].pending.resolve({ marker: "second" });
    await waitFor(() => expect(hook.result.current.data).toEqual({ marker: "second" }));

    hook.rerender({ projectId: "project-b", requestPayload: payload("other-project") });
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(hook.result.current.data).toBeUndefined();
    expect(hook.result.current.isPlaceholderData).toBe(false);
    requests[2].pending.resolve({ marker: "other-project" });
    await waitFor(() => expect(hook.result.current.data).toEqual({ marker: "other-project" }));

    act(() => useAuthStore.getState().setToken("owner-token-b"));
    await waitFor(() => expect(requests).toHaveLength(4));
    expect(hook.result.current.data).toBeUndefined();
    expect(hook.result.current.isPlaceholderData).toBe(false);
    expect(
      client.getQueryCache().findAll({ queryKey: ["project-task-query"] })[0]?.queryKey,
    ).toEqual([
      "project-task-query",
      "project-b",
      {
        filter_json: { marker: "other-project" },
        sort_json: [],
        columns_json: [],
        limit: 20,
        offset: 0,
      },
      "owner-a",
      "owner-token-b",
    ]);
    requests[3].pending.resolve({ marker: "rotated-token" });
    await waitFor(() => expect(hook.result.current.data).toEqual({ marker: "rotated-token" }));
  });

  it("does not present previous rows as a different page or row count", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    queryTask.mockImplementation((_project, request) =>
      request.offset === 0 && request.limit === 20
        ? Promise.resolve({ items: [{ id: "first-page" }], total: 40, limit: 20, offset: 0 })
        : new Promise(() => undefined),
    );
    const hook = renderHook(
      ({ limit, offset }) => useProjectTaskQuery("p1", { ...payload("first"), limit, offset }),
      {
        initialProps: { limit: 20, offset: 0 },
        wrapper: wrapper(client),
      },
    );
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    hook.rerender({ limit: 20, offset: 20 });
    expect(hook.result.current.data).toBeUndefined();
    expect(hook.result.current.isPlaceholderData).toBe(false);
    hook.rerender({ limit: 10, offset: 0 });
    expect(hook.result.current.data).toBeUndefined();
  });

  it("scopes summary cache keys to the same auth owner", async () => {
    querySummary.mockResolvedValue({ marker: "summary" });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const hook = renderHook(
      ({ filterJson }: { filterJson: Record<string, unknown> }) =>
        useDataManagerSummary("project-a", filterJson),
      {
        initialProps: { filterJson: { marker: "summary" } },
        wrapper: wrapper(client),
      },
    );
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(
      client.getQueryCache().findAll({ queryKey: ["data-manager-summary"] })[0]?.queryKey,
    ).toEqual([
      "data-manager-summary",
      "project-a",
      { marker: "summary" },
      "owner-a",
      "owner-token-a",
    ]);
  });
});
