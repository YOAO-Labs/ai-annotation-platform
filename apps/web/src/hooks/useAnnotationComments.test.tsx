import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider, type InfiniteData } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/comments", () => ({
  commentsApi: { create: vi.fn(), patch: vi.fn(), remove: vi.fn() },
}));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (sel: (s: { push: () => void }) => unknown) => sel({ push: vi.fn() }),
}));

import { commentsApi } from "@/api/comments";
import { useCreateComment, useDeleteComment } from "./useAnnotationComments";
import { discussionKeys } from "@/pages/Workbench/state/discussionTypes";

const mockRemove = commentsApi.remove as unknown as ReturnType<typeof vi.fn>;
const mockCreate = commentsApi.create as unknown as ReturnType<typeof vi.fn>;
const mockPatch = commentsApi.patch as unknown as ReturnType<typeof vi.fn>;

function seedPage(items: { id: string }[]): InfiniteData<{ items: unknown[]; next_cursor: null }> {
  return { pages: [{ items, next_cursor: null }], pageParams: [undefined] };
}

let qc: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useDeleteComment 乐观删除", () => {
  beforeEach(() => {
    qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    mockRemove.mockReset();
    mockCreate.mockReset();
    mockPatch.mockReset();
  });

  it("从任务级聚合缓存乐观剔除被删项", async () => {
    mockRemove.mockResolvedValue(undefined);
    qc.setQueryData(["task-comments-page", "task-1"], seedPage([{ id: "c1" }, { id: "c2" }]));

    const { result } = renderHook(() => useDeleteComment(null), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("c1");
    });

    const data = qc.getQueryData<InfiniteData<{ items: { id: string }[] }>>([
      "task-comments-page",
      "task-1",
    ]);
    expect(data?.pages[0].items.map((c) => c.id)).toEqual(["c2"]);
  });

  it("删除失败时回滚任务级缓存", async () => {
    mockRemove.mockRejectedValue(new Error("boom"));
    qc.setQueryData(["task-comments-page", "task-1"], seedPage([{ id: "c1" }, { id: "c2" }]));

    const { result } = renderHook(() => useDeleteComment(null), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("c1").catch(() => {});
    });

    const data = qc.getQueryData<InfiniteData<{ items: { id: string }[] }>>([
      "task-comments-page",
      "task-1",
    ]);
    expect(data?.pages[0].items.map((c) => c.id)).toEqual(["c1", "c2"]);
  });

  it("标注评论写入成功后失效权威任务 discussion feed", async () => {
    mockRemove.mockResolvedValue(undefined);
    const feedKey = discussionKeys.feed("task-1", "all", null, 50);
    qc.setQueryData(feedKey, seedPage([{ id: "c1" }]));

    const { result } = renderHook(() => useDeleteComment("annotation-1", "task-1"), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("c1");
    });

    expect(qc.getQueryState(feedKey)?.isInvalidated).toBe(true);
  });

  it("创建评论使用提交快照的 annotation/task，而不是 hook 初始闭包", async () => {
    mockCreate.mockResolvedValue({ annotation_id: "annotation-b" });
    const feedKey = discussionKeys.feed("task-b", "all", null, 50);
    qc.setQueryData(feedKey, seedPage([{ id: "c1" }]));

    const { result } = renderHook(() => useCreateComment("annotation-a", "task-a"), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        annotationId: "annotation-b",
        taskId: "task-b",
        payload: {
          body: "snapshot target",
          mentions: [],
          attachments: [],
          canvas_drawing: null,
        },
      });
    });

    expect(mockCreate).toHaveBeenCalledWith("annotation-b", {
      body: "snapshot target",
      mentions: [],
      attachments: [],
      canvas_drawing: null,
    });
    expect(qc.getQueryState(feedKey)?.isInvalidated).toBe(true);
  });

  it("带 task scope 的删除失败只回滚原任务缓存", async () => {
    mockRemove.mockRejectedValue(new Error("offline"));
    qc.setQueryData(["task-comments-page", "task-a"], seedPage([{ id: "c1" }]));
    qc.setQueryData(["task-comments-page", "task-b"], seedPage([{ id: "c1" }, { id: "c2" }]));

    const { result } = renderHook(() => useDeleteComment("annotation-a", "task-a"), { wrapper });
    await act(async () => {
      await result.current
        .mutateAsync({ id: "c1", annotationId: "annotation-a", taskId: "task-a" })
        .catch(() => {});
    });

    const taskA = qc.getQueryData<InfiniteData<{ items: { id: string }[] }>>([
      "task-comments-page",
      "task-a",
    ]);
    const taskB = qc.getQueryData<InfiniteData<{ items: { id: string }[] }>>([
      "task-comments-page",
      "task-b",
    ]);
    expect(taskA?.pages[0].items.map((item) => item.id)).toEqual(["c1"]);
    expect(taskB?.pages[0].items.map((item) => item.id)).toEqual(["c1", "c2"]);
  });
});
