import { afterEach, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { datasetQualityReviewApi as api, type ReviewBatch } from "@/api/datasetQualityReview";
import { useReviewBatch, useReviewMembers } from "./useDatasetQualityReview";
vi.mock("@/hooks/useDatasets", () => ({
  useDatasetOwner: () => ({ assert: vi.fn(), key: ["actor", 1], enabled: true }),
}));
vi.mock("@/api/datasetQualityReview", () => ({
  datasetQualityReviewApi: { get: vi.fn(), members: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}
it("fetches final member outcomes even when the processed count stays unchanged", async () => {
  const batch = {
    id: "batch",
    quality_run_id: "run",
    status: "running",
    processed: 4,
  } as ReviewBatch;
  vi.mocked(api.members)
    .mockResolvedValueOnce({ items: [], total: 4 })
    .mockResolvedValueOnce({ items: [], total: 5 });
  const hook = renderHook(({ current }) => useReviewMembers("dataset", "run", current, 0, 20), {
    wrapper: wrapper(),
    initialProps: { current: batch },
  });
  await waitFor(() => expect(hook.result.current.data?.total).toBe(4));
  hook.rerender({ current: { ...batch, status: "partial" } });
  await waitFor(() => expect(hook.result.current.data?.total).toBe(5));
  expect(api.members).toHaveBeenCalledTimes(2);
});
it("rejects a batch response belonging to a different quality run", async () => {
  vi.mocked(api.get).mockResolvedValue({ id: "batch", quality_run_id: "other" } as ReviewBatch);
  const hook = renderHook(() => useReviewBatch("dataset", "run", "batch"), { wrapper: wrapper() });
  await waitFor(() => expect(hook.result.current.error?.message).toBe("批量复核归属已变化"));
  expect(hook.result.current.data).toBeUndefined();
});
