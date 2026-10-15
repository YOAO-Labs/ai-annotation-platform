import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  trainingObservabilityApi,
  type LogPage,
  type TelemetryPage,
} from "@/api/trainingObservability";
import type { AssertTrainingOwner } from "./trainingViewOwner";

type Page = TelemetryPage | LogPage;
export type ObservationWindow<P extends Page> = P & { fetched_at: number };

export function mergeObservationPage<P extends Page>(
  previous: ObservationWindow<P> | undefined,
  page: P,
  keep: number,
): ObservationWindow<P> {
  const retained = previous?.stream_id === page.stream_id ? previous.items : [];
  const items = [
    ...new Map([...retained, ...page.items].map((item) => [item.sequence, item])).values(),
  ]
    .filter((item) => page.earliest_cursor == null || item.sequence >= page.earliest_cursor)
    .sort((a, b) => a.sequence - b.sequence)
    .slice(-keep);
  const gaps = [
    ...new Map(
      [...(previous?.stream_id === page.stream_id ? previous.gaps : []), ...page.gaps].map(
        (gap) => [`${gap.after}:${gap.through}:${gap.reason}`, gap],
      ),
    ).values(),
  ].slice(-32);
  return { ...page, items, gaps, fetched_at: Date.now() } as unknown as ObservationWindow<P>;
}

export function observationOptions<P extends Page>(
  client: QueryClient,
  kind: "telemetry" | "logs",
  project: string,
  run: string,
  attempt: string,
  scope: string,
  enabled: boolean,
  poll: boolean,
  assertOwner?: AssertTrainingOwner,
) {
  const key = [`training-${kind}`, project, run, attempt, scope, poll ? "live" : "final"];
  return {
    queryKey: key,
    enabled: enabled && !!run && !!attempt,
    retry: false,
    refetchInterval: poll ? 5000 : (false as const),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<ObservationWindow<P>> => {
      const check = () => {
        signal.throwIfAborted();
        assertOwner?.(signal);
      };
      // Terminal transitions explicitly request the latest retained tail.
      let window = poll ? client.getQueryData<ObservationWindow<P>>(key) : undefined;
      let after = window?.next_cursor;
      for (let index = 0; index < (poll ? 3 : 1); index++) {
        check();
        const page = (
          kind === "telemetry"
            ? await trainingObservabilityApi.telemetry(project, run, attempt, after, { signal })
            : await trainingObservabilityApi.logs(project, run, attempt, { after }, { signal })
        ) as P;
        check();
        if (page.attempt_id !== attempt) throw new Error("观测执行归属已变化，请重新选择");
        if (window && page.stream_id !== window.stream_id) {
          window = undefined;
          after = undefined;
          if (index < 2 && poll) continue;
          throw new Error("观测流已变化，请重新读取近期数据");
        }
        if (after != null && page.next_cursor < after) throw new Error("观测游标未推进，请重试");
        window = mergeObservationPage(window, page, kind === "telemetry" ? 720 : 512);
        if (!page.has_more) break;
        if (after != null && page.next_cursor <= after) throw new Error("观测分页未推进，请重试");
        after = page.next_cursor;
      }
      if (!window) throw new Error("观测流已变化，请刷新");
      return window;
    },
  };
}

export function useTrainingObservability(
  project: string,
  run: string,
  attempt: string,
  scope: string,
  enabled: boolean,
  poll: boolean,
  logsEnabled: boolean,
  assertOwner?: AssertTrainingOwner,
) {
  const client = useQueryClient();
  const telemetry = useQuery(
    observationOptions<TelemetryPage>(
      client,
      "telemetry",
      project,
      run,
      attempt,
      scope,
      enabled,
      poll,
      assertOwner,
    ),
  );
  const logs = useQuery(
    observationOptions<LogPage>(
      client,
      "logs",
      project,
      run,
      attempt,
      scope,
      enabled && logsEnabled,
      poll,
      assertOwner,
    ),
  );
  return { telemetry, logs };
}
