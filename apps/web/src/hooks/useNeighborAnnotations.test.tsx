import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    getNeighborAnnotations: vi.fn(),
  },
}));

import { tasksApi } from "@/api/tasks";
import { useNeighborAnnotations } from "./useNeighborAnnotations";

const mockGet = tasksApi.getNeighborAnnotations as unknown as ReturnType<typeof vi.fn>;

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function ann(id: string, track_id: string | null) {
  return { id, task_id: "x", track_id, geometry: { type: "box_3d" } } as any;
}

describe("useNeighborAnnotations", () => {
  beforeEach(() => mockGet.mockReset());

  it("enabled=false → 短路不发请求", () => {
    renderHook(() => useNeighborAnnotations("t0", 5, "trk_5", false), {
      wrapper: makeWrapper(),
    });
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("k<=0 → 短路不发请求", () => {
    renderHook(() => useNeighborAnnotations("t0", 0, null, true), {
      wrapper: makeWrapper(),
    });
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("批量返回 frames → 展开为 byTask", async () => {
    mockGet.mockResolvedValue({
      scene_id: "s1",
      frame_index: 2,
      frames: [
        { task_id: "t1", frame_index: 1, annotations: [ann("a1", "trk_5")] },
        { task_id: "t2", frame_index: 3, annotations: [ann("b1", "trk_5"), ann("b2", "trk_5")] },
      ],
    });
    const { result } = renderHook(() => useNeighborAnnotations("t0", 5, "trk_5", true), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(mockGet).toHaveBeenCalledWith(
      "t0",
      5,
      "trk_5",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(result.current.byTask["t1"].map((a) => a.id)).toEqual(["a1"]);
    expect(result.current.byTask["t2"].map((a) => a.id)).toEqual(["b1", "b2"]);
  });

  it("scope=all 时 trackId=null 仍发请求(回全部)", async () => {
    mockGet.mockResolvedValue({ scene_id: "s1", frame_index: 2, frames: [] });
    renderHook(() => useNeighborAnnotations("t0", 3, null, true), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGet).toHaveBeenCalledWith(
      "t0",
      3,
      null,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });
});
