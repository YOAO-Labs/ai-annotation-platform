import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    getPointCloudManifest: vi.fn(),
  },
}));
vi.mock("@/pages/Workbench/stages/three-d/geometry/loadNeighborPcd", () => ({
  loadNeighborPcdPositions: vi.fn(),
}));

import { tasksApi } from "@/api/tasks";
import { loadNeighborPcdPositions } from "@/pages/Workbench/stages/three-d/geometry/loadNeighborPcd";
import { resolveNeighborPointCloudPlan, useNeighborPointClouds } from "./useNeighborPointClouds";

const getManifest = vi.mocked(tasksApi.getPointCloudManifest);
const loadPositions = vi.mocked(loadNeighborPcdPositions);

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("useNeighborPointClouds ownership", () => {
  beforeEach(() => {
    getManifest.mockReset();
    loadPositions.mockReset();
    getManifest.mockImplementation(
      async (taskId) =>
        ({
          task_id: taskId,
          point_cloud_url: `${taskId}.pcd`,
        }) as never,
    );
  });

  it("clears retired task results and never exposes their late completion", async () => {
    let finishOld!: (positions: Float32Array) => void;
    loadPositions.mockImplementation((url) => {
      if (url === "old.pcd") {
        return new Promise((resolve) => {
          finishOld = resolve;
        });
      }
      return Promise.resolve(new Float32Array([4, 5, 6]));
    });
    let neighbors = [{ taskId: "old", frameIndex: 1 }];
    const hook = renderHook(() => useNeighborPointClouds(neighbors, "iso_8855", 100, true), {
      wrapper: makeWrapper(),
    });
    await waitFor(() =>
      expect(loadPositions).toHaveBeenCalledWith(
        "old.pcd",
        "iso_8855",
        100,
        expect.any(AbortSignal),
      ),
    );

    neighbors = [{ taskId: "new", frameIndex: 9 }];
    hook.rerender();
    await waitFor(() =>
      expect(hook.result.current.items.map((item) => item.taskId)).toEqual(["new"]),
    );

    finishOld(new Float32Array([1, 2, 3]));
    await Promise.resolve();
    expect(hook.result.current.items.map((item) => item.taskId)).toEqual(["new"]);
  });

  it("disabling neighbor assistance immediately retires its visual data", async () => {
    loadPositions.mockResolvedValue(new Float32Array([1, 2, 3]));
    let enabled = true;
    const hook = renderHook(
      () =>
        useNeighborPointClouds([{ taskId: "neighbor", frameIndex: 1 }], "iso_8855", 100, enabled),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(hook.result.current.items).toHaveLength(1));

    enabled = false;
    hook.rerender();
    expect(hook.result.current.items).toEqual([]);
  });
});

describe("resolveNeighborPointCloudPlan", () => {
  it("uses at most the nearest pair for implicit surface assistance", () => {
    expect(resolveNeighborPointCloudPlan("points", false, 3)).toEqual({
      frameCount: 0,
      displayMode: "surface",
    });
    expect(resolveNeighborPointCloudPlan("surface", false, 3)).toEqual({
      frameCount: 1,
      displayMode: "surface",
    });
  });

  it("lets an explicit overlay keep its configured K and diagnostic colors", () => {
    expect(resolveNeighborPointCloudPlan("surface", true, 3)).toEqual({
      frameCount: 3,
      displayMode: "diagnostic",
    });
    expect(resolveNeighborPointCloudPlan("points", true, 2)).toEqual({
      frameCount: 2,
      displayMode: "diagnostic",
    });
  });
});
