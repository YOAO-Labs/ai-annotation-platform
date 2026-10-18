import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockUnlinkProject } = vi.hoisted(() => ({ mockUnlinkProject: vi.fn() }));

vi.mock("@/api/datasets", () => ({
  datasetsApi: { unlinkProject: mockUnlinkProject },
}));

import { useUnlinkProject } from "./useDatasets";

function makeWrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useUnlinkProject", () => {
  it("retries the frozen request and invalidates the original dataset/project keys", async () => {
    const datasetId = "dataset-accepted";
    const projectId = "project-original";
    const idempotencyKey = "11111111-1111-4111-8111-111111111111";
    mockUnlinkProject.mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce({
      deleted_tasks: 1,
      deleted_annotations: 2,
      deleted_batches: 0,
      deleted_batch_ids: [],
      soft: false,
    });

    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: 1, retryDelay: 0 } },
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUnlinkProject(), {
      wrapper: makeWrapper(queryClient),
    });
    const variables = Object.freeze({ datasetId, projectId, idempotencyKey });

    await act(async () => {
      await result.current.mutateAsync(variables);
    });

    expect(mockUnlinkProject).toHaveBeenCalledTimes(2);
    for (const call of mockUnlinkProject.mock.calls) {
      expect(call).toEqual([datasetId, projectId, { idempotencyKey }]);
    }

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["datasets"],
      ["dataset", datasetId],
      ["dataset-projects", datasetId],
      ["projects"],
      ["project", projectId],
      ["project-stats"],
      ["batches", projectId],
      ["project-datasets", projectId],
      ["unclassified-count", projectId],
    ]);

    queryClient.clear();
  });
});
