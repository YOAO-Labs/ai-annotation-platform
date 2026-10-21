import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const mockListByProject = vi.fn();

vi.mock("../api/tasks", () => ({
  tasksApi: {
    listByProject: (...args: unknown[]) => mockListByProject(...args),
  },
}));

import { flattenTaskPages, useTaskList } from "./useTasks";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useTaskList pagination plumbing", () => {
  it("跨页合并时保留首次出现的任务，避免重复行", () => {
    const first = { id: "t1" } as any;
    const second = { id: "t2" } as any;
    expect(
      flattenTaskPages([{ items: [first, second] }, { items: [first, { id: "t3" }] }]),
    ).toEqual([first, second, { id: "t3" }]);
    expect(flattenTaskPages(undefined)).toEqual([]);
  });

  it("请求携带 AbortSignal，切换项目时可以取消旧列表请求", async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    mockListByProject.mockImplementation((projectId: string) => {
      if (projectId === "p1") {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve({ items: [], total: 0, limit: 100, offset: 0, next_cursor: null });
    });

    const hook = renderHook(({ projectId }: { projectId: string }) => useTaskList(projectId), {
      initialProps: { projectId: "p1" },
      wrapper,
    });
    await waitFor(() =>
      expect(mockListByProject).toHaveBeenCalledWith(
        "p1",
        { limit: 100, cursor: undefined },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    const firstSignal = mockListByProject.mock.calls[0][2].signal as AbortSignal;

    hook.rerender({ projectId: "p2" });
    await waitFor(() =>
      expect(mockListByProject).toHaveBeenCalledWith(
        "p2",
        { limit: 100, cursor: undefined },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(firstSignal.aborted).toBe(true);
    resolveFirst?.({ items: [], total: 0, limit: 100, offset: 0, next_cursor: null });
  });
  it("allows business lists to request their selected row count", async () => {
    mockListByProject.mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
      next_cursor: null,
    });
    renderHook(() => useTaskList("p1", { batch_id: "b1", limit: 20 }), { wrapper });
    await waitFor(() =>
      expect(mockListByProject).toHaveBeenCalledWith(
        "p1",
        { batch_id: "b1", limit: 20, cursor: undefined },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });
});
