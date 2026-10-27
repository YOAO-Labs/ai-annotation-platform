import { afterEach, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type PropsWithChildren } from "react";
import { datasetQualityApi, type QualityRun } from "@/api/datasetQuality";
import { useQualityResults } from "./useDatasetQuality";
vi.mock("@/hooks/useDatasets", () => ({
  useDatasetOwner: () => ({ assert: vi.fn(), key: ["user", 1], enabled: true }),
}));
vi.mock("@/api/datasetQuality", () => ({ datasetQualityApi: { results: vi.fn() } }));
afterEach(cleanup);
it("reloads the last result page when a running job reaches terminal status", async () => {
  const run = { id: "run", dataset_id: "dataset", status: "running", processed: 4 } as QualityRun;
  vi.mocked(datasetQualityApi.results)
    .mockResolvedValueOnce({ items: [], total: 4 })
    .mockResolvedValueOnce({ items: [], total: 5 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    ({ current }) => useQualityResults("dataset", current, new URLSearchParams("limit=20")),
    { wrapper, initialProps: { current: run } },
  );
  await waitFor(() => expect(hook.result.current.data?.total).toBe(4));
  hook.rerender({ current: { ...run, status: "completed", processed: 5 } });
  await waitFor(() => expect(hook.result.current.data?.total).toBe(5));
  expect(datasetQualityApi.results).toHaveBeenCalledTimes(2);
});
