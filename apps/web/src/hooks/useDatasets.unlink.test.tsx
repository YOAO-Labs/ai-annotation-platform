import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mockCreateDataset, mockUpdateDataset, mockUnlinkProject } = vi.hoisted(() => ({
  mockCreateDataset: vi.fn(),
  mockUpdateDataset: vi.fn(),
  mockUnlinkProject: vi.fn(),
}));

vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    create: mockCreateDataset,
    update: mockUpdateDataset,
    unlinkProject: mockUnlinkProject,
  },
}));

import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { useCreateDataset, useUnlinkProject, useUpdateDataset } from "./useDatasets";

function makeWrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  useAuthStore.getState().setAuth("token-a", { id: "owner-a" } as MeResponse);
});

afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
  vi.clearAllMocks();
});

describe("dataset mutation hooks", () => {
  it("keep the same create/update keys and bodies when React Query retries", async () => {
    const createKey = "11111111-1111-4111-8111-111111111111";
    const updateKey = "22222222-2222-4222-8222-222222222222";
    const createPayload = { name: "training", data_type: "image" };
    const updatePayload = { axis_convention: null };
    mockCreateDataset
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValue({ id: "ds1" });
    mockUpdateDataset
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValue({ id: "ds1" });

    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: 1, retryDelay: 0 } },
    });
    const { result } = renderHook(
      () => ({ create: useCreateDataset(), update: useUpdateDataset() }),
      { wrapper: makeWrapper(queryClient) },
    );

    await act(async () => {
      await result.current.create.mutateAsync({
        payload: createPayload,
        idempotencyKey: createKey,
      });
      await result.current.update.mutateAsync({
        id: "ds1",
        payload: updatePayload,
        idempotencyKey: updateKey,
      });
    });

    expect(mockCreateDataset.mock.calls).toEqual([
      [createPayload, { idempotencyKey: createKey }],
      [createPayload, { idempotencyKey: createKey }],
    ]);
    expect(mockUpdateDataset.mock.calls).toEqual([
      ["ds1", updatePayload, { idempotencyKey: updateKey }],
      ["ds1", updatePayload, { idempotencyKey: updateKey }],
    ]);
    queryClient.clear();
  });
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
