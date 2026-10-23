import { useQuery } from "@tanstack/react-query";
import { datasetQualityApi, type QualityRun } from "@/api/datasetQuality";
import { useDatasetOwner } from "./useDatasets";
export const qualityActive = (run?: QualityRun) =>
  !!run && ["pending", "running"].includes(run.status);
export function useQualityRuns(id: string, page: number, size: number, enabled: boolean) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-quality", id, "runs", page, size, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetQualityApi.list(id, page, size, signal);
      owner.assert(signal);
      return value;
    },
    enabled: enabled && owner.enabled,
    refetchInterval: (q) => (q.state.data?.items.some(qualityActive) ? 2500 : false),
  });
}
export function useQualityRun(id: string, run: string | undefined, enabled: boolean) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-quality", id, "run", run, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetQualityApi.get(id, run!, signal);
      owner.assert(signal);
      if (value.dataset_id !== id || value.id !== run) throw new Error("质量运行归属已变化");
      return value;
    },
    enabled: !!run && enabled && owner.enabled,
    refetchInterval: (q) => (qualityActive(q.state.data) ? 2500 : false),
  });
}
export function useQualityResults(
  id: string,
  run: QualityRun | undefined,
  params: URLSearchParams,
) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: [
      "dataset-quality",
      id,
      "results",
      run?.id,
      run?.status,
      run?.processed,
      params.toString(),
      ...owner.key,
    ],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetQualityApi.results(id, run!.id, params, signal);
      owner.assert(signal);
      if (value.items.some((row) => row.run_id !== run!.id)) throw new Error("质量结果归属已变化");
      return value;
    },
    enabled: !!run && owner.enabled,
    refetchInterval: qualityActive(run) ? 2500 : false,
  });
}
