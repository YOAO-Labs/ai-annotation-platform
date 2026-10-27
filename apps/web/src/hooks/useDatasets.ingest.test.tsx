import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mockScanItems, mockDimensions, mockMedia } = vi.hoisted(() => ({
  mockScanItems: vi.fn(),
  mockDimensions: vi.fn(),
  mockMedia: vi.fn(),
}));

vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    scanItems: mockScanItems,
    backfillDimensions: mockDimensions,
    backfillMedia: mockMedia,
  },
}));
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { useBackfillDimensions, useBackfillMedia, useScanDatasetItems } from "./useDatasets";

function setActor(id: string) {
  useAuthStore.getState().setAuth(`token-${id}`, { id } as MeResponse);
}

function makeWrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  setActor("actor-1");
});

afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
  vi.clearAllMocks();
});

describe.each([
  ["dimensions", useBackfillDimensions, mockDimensions],
  ["media", useBackfillMedia, mockMedia],
] as const)("%s backfill", (_kind, useBackfill, api) => {
  it("reuses an uncertain operation and starts fresh after acceptance or actor change", async () => {
    api
      .mockRejectedValueOnce(new Error("response lost"))
      .mockImplementation((...args) =>
        Promise.resolve({ operation_id: args[args.length - 1].idempotencyKey, status: "pending" }),
      );
    const keyAt = (index: number) => {
      const args = api.mock.calls[index];
      return args[args.length - 1].idempotencyKey;
    };
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: 2, retryDelay: 0 } },
    });
    const { result, rerender } = renderHook(() => useBackfill("ds1"), {
      wrapper: makeWrapper(queryClient),
    });
    await act(async () => {
      await expect(result.current.mutateAsync(undefined)).rejects.toThrow("response lost");
    });
    expect(api).toHaveBeenCalledTimes(1);
    const initialKey = keyAt(0);
    await act(async () => {
      await result.current.mutateAsync(undefined);
    });
    expect(keyAt(1)).toBe(initialKey);
    await act(async () => {
      await result.current.mutateAsync(undefined);
    });
    expect(keyAt(2)).not.toBe(initialKey);

    api.mockRejectedValueOnce(new Error("response lost again"));
    await act(async () => {
      await expect(result.current.mutateAsync(undefined)).rejects.toThrow("response lost again");
    });
    const priorActorKey = keyAt(3);
    await act(async () => setActor("actor-2"));
    rerender();
    await act(async () => {
      await result.current.mutateAsync(undefined);
    });
    expect(keyAt(4)).not.toBe(priorActorKey);
    queryClient.clear();
  });
});

describe("useScanDatasetItems", () => {
  it("retains a scan key after failure, avoids automatic retries, and resets after success", async () => {
    mockScanItems
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValue({ status: "ok", new_items: 0, linked_tasks: 0 });
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: 2, retryDelay: 0 } },
    });
    const { result } = renderHook(() => useScanDatasetItems("ds1"), {
      wrapper: makeWrapper(queryClient),
    });

    await act(async () => {
      await expect(result.current.mutateAsync(undefined)).rejects.toThrow("response lost");
    });
    expect(mockScanItems).toHaveBeenCalledTimes(1);
    const firstKey = mockScanItems.mock.calls[0][1].idempotencyKey;

    await act(async () => {
      await result.current.mutateAsync(undefined);
    });
    expect(mockScanItems.mock.calls[1][1].idempotencyKey).toBe(firstKey);

    await act(async () => {
      await result.current.mutateAsync(undefined);
    });
    const nextKey = mockScanItems.mock.calls[2][1].idempotencyKey;
    expect(nextKey).toMatch(/^[0-9a-f-]{36}$/);
    expect(nextKey).not.toBe(firstKey);
    queryClient.clear();
  });
});
