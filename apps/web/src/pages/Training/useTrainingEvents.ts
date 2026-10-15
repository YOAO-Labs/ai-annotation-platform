import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { trainingRunsApi, type TrainingEpochEvent } from "@/api/trainingRuns";
import type { AssertTrainingOwner } from "./trainingViewOwner";

export function trainingEventsOptions(
  client: QueryClient,
  projectId: string,
  runId: string,
  attemptId: string,
  enabled = true,
  poll = true,
  scope = "",
  assertOwner?: AssertTrainingOwner,
) {
  const baseKey = ["training-events", projectId, runId, attemptId, ...(scope ? [scope] : [])];
  const key = [...baseKey, poll ? "live" : "final"];
  return {
    queryKey: key,
    enabled: enabled && !!runId && !!attemptId,
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const items = [
        ...(client.getQueryData<TrainingEpochEvent[]>(key) ??
          client.getQueryData<TrainingEpochEvent[]>([...baseKey, "live"]) ??
          []),
      ];
      let after = items[items.length - 1]?.sequence ?? 0;
      for (;;) {
        signal.throwIfAborted();
        assertOwner?.(signal);
        const page = await trainingRunsApi.events(projectId, runId, attemptId, after, { signal });
        signal.throwIfAborted();
        assertOwner?.(signal);
        if (page.attempt_id !== attemptId) throw new Error("训练执行已变化，请重新选择执行记录");
        let previous = after;
        for (const event of page.items) {
          if (!Number.isInteger(event.sequence) || event.sequence <= previous)
            throw new Error("训练事件分页未推进，请重试");
          previous = event.sequence;
        }
        items.push(...page.items);
        if (page.items.length < 100) return items;
        const cursor = page.items[page.items.length - 1]?.sequence ?? after;
        if (cursor <= after) throw new Error("训练事件分页未推进，请重试");
        after = cursor;
      }
    },
    refetchInterval: poll ? 3000 : (false as const),
  };
}

/** Fetch pages for one attempt; a new attempt always starts an independent curve. */
export function useTrainingEvents(
  projectId: string,
  runId: string,
  attemptId: string,
  enabled = true,
  poll = true,
  scope = "",
  assertOwner?: AssertTrainingOwner,
) {
  const client = useQueryClient();
  return useQuery(
    trainingEventsOptions(client, projectId, runId, attemptId, enabled, poll, scope, assertOwner),
  );
}
