import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockScanItems } = vi.hoisted(() => ({ mockScanItems: vi.fn() }));

vi.mock("@/api/datasets", () => ({ datasetsApi: { scanItems: mockScanItems } }));

import { useScanDatasetItems } from "./useDatasets";

function makeWrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
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
