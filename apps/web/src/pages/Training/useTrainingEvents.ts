import { useQuery, useQueryClient } from "@tanstack/react-query";
import { trainingRunsApi, type TrainingEpochEvent } from "@/api/trainingRuns";

/** Fetch pages for one attempt; a new attempt always starts an independent curve. */
export function useTrainingEvents(
  projectId: string,
  runId: string,
  attemptId: string,
  enabled = true,
  poll = true,
) {
  const client = useQueryClient();
  const key = ["training-events", projectId, runId, attemptId];
  return useQuery({
    queryKey: key,
    enabled: enabled && !!runId && !!attemptId,
    queryFn: async ({ signal }) => {
      const items = [...(client.getQueryData<TrainingEpochEvent[]>(key) ?? [])];
      let after = items[items.length - 1]?.sequence ?? 0;
      for (;;) {
        signal.throwIfAborted();
        const page = await trainingRunsApi.events(projectId, runId, attemptId, after);
        if (page.attempt_id !== attemptId) throw new Error("训练执行已变化，请重新选择执行记录");
        items.push(...page.items);
        if (page.items.length < 100) return items;
        const cursor = page.items[page.items.length - 1]?.sequence ?? after;
        if (cursor <= after) throw new Error("训练事件分页未推进，请重试");
        after = cursor;
      }
    },
    refetchInterval: poll ? 3000 : false,
  });
}
