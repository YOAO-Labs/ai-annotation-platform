import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    getNeighbors: vi.fn(),
  },
}));

import { tasksApi } from "@/api/tasks";
import { useFrameNeighbors } from "./useFrameNeighbors";

const mockGetNeighbors = tasksApi.getNeighbors as unknown as ReturnType<typeof vi.fn>;

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const sample = {
  scene_id: "scene-1",
  scene_name: "sc",
  frame_index: 2,
  scene_total_frames: 5,
  prev: [{ task_id: "t1", frame_index: 1 }],
  next: [{ task_id: "t3", frame_index: 3 }],
};

describe("useFrameNeighbors", () => {
  beforeEach(() => mockGetNeighbors.mockReset());

  it("taskId 为 null 时不发请求", () => {
    renderHook(() => useFrameNeighbors(null), { wrapper: makeWrapper() });
    expect(mockGetNeighbors).not.toHaveBeenCalled();
  });

  it("调端点并透传数据, 默认 k=1", async () => {
    mockGetNeighbors.mockResolvedValue(sample);
    const { result } = renderHook(() => useFrameNeighbors("t2"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(mockGetNeighbors).toHaveBeenCalledWith(
      "t2",
      1,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(result.current.data?.next?.[0].task_id).toBe("t3");
  });
});
