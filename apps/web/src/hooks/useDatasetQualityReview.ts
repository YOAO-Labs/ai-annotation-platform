import { useQuery } from "@tanstack/react-query";
import { datasetQualityReviewApi as api, type ReviewBatch } from "@/api/datasetQualityReview";
import { useDatasetOwner } from "./useDatasets";

export const reviewBatchActive = (batch?: ReviewBatch) =>
  !!batch && ["pending", "running"].includes(batch.status);

export function useReviewBatches(
  dataset: string,
  run: string | undefined,
  page: number,
  size: number,
) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-quality-review", dataset, run, "history", page, size, ...owner.key],
    enabled: !!run && owner.enabled,
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await api.list(dataset, run!, page, size, signal);
      owner.assert(signal);
      if (value.items.some((row) => row.quality_run_id !== run))
        throw new Error("批量复核归属已变化");
      return value;
    },
    refetchInterval: (q) => (q.state.data?.items.some(reviewBatchActive) ? 2500 : false),
  });
}

export function useReviewBatch(dataset: string, run: string | undefined, batch: string | null) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-quality-review", dataset, run, "batch", batch, ...owner.key],
    enabled: !!run && !!batch && owner.enabled,
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await api.get(dataset, run!, batch!, signal);
      owner.assert(signal);
      if (value.quality_run_id !== run || value.id !== batch) throw new Error("批量复核归属已变化");
      return value;
    },
    refetchInterval: (q) => (reviewBatchActive(q.state.data) ? 1500 : false),
  });
}

export function useReviewMembers(
  dataset: string,
  run: string | undefined,
  batch: ReviewBatch | undefined,
  page: number,
  size: number,
) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: [
      "dataset-quality-review",
      dataset,
      run,
      "members",
      batch?.id,
      batch?.status,
      batch?.processed,
      page,
      size,
      ...owner.key,
    ],
    enabled: !!run && !!batch && owner.enabled,
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await api.members(dataset, run!, batch!.id, page, size, signal);
      owner.assert(signal);
      return value;
    },
    refetchInterval: reviewBatchActive(batch) ? 1500 : false,
  });
}
