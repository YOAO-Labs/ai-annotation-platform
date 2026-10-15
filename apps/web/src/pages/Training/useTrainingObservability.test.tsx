import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import {
  trainingObservabilityApi,
  type LogPage,
  type TelemetryPage,
} from "@/api/trainingObservability";
import {
  mergeObservationPage,
  observationOptions,
  useTrainingObservability,
} from "./useTrainingObservability";
import { estimateLabel, resourceValue } from "./trainingObservability";

vi.mock("@/api/trainingObservability", () => ({
  trainingObservabilityApi: { telemetry: vi.fn(), logs: vi.fn() },
}));
const clients: QueryClient[] = [];
const client = () => {
  const value = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(value);
  return value;
};
function logs(from = 1, count = 1, stream = "stream"): LogPage {
  const end = from + count - 1;
  return {
    attempt_id: "attempt",
    stream_id: stream,
    earliest_cursor: 1,
    latest_cursor: end,
    next_cursor: end,
    has_more: false,
    gaps: [],
    summary: null,
    items: Array.from({ length: count }, (_, i) => ({
      sequence: from + i,
      producer_boot_id: "boot",
      captured_at: "2026-10-15T00:00:00Z",
      received_at: "2026-10-15T00:00:00Z",
      text: `line ${from + i}\n`,
    })),
  };
}
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((value) => value.clear());
  vi.resetAllMocks();
});

describe("bounded observation cursors", () => {
  it("catches up at most three independent pages, keeps the log window bounded, and consumes empty tail gaps", async () => {
    const query = client();
    const options = observationOptions<LogPage>(
      query,
      "logs",
      "project",
      "run",
      "attempt",
      "owner",
      true,
      true,
    );
    query.setQueryData(options.queryKey, mergeObservationPage(undefined, logs(1, 512), 512));
    vi.mocked(trainingObservabilityApi.logs).mockImplementation(async (_p, _r, _a, cursor) => ({
      ...logs((cursor?.after ?? 0) + 1, 120),
      latest_cursor: 2000,
      has_more: true,
    }));
    const result = await query.fetchQuery(options);
    expect(trainingObservabilityApi.logs).toHaveBeenCalledTimes(3);
    expect(result.items).toHaveLength(512);
    expect(result.next_cursor).toBe(872);
    vi.mocked(trainingObservabilityApi.logs).mockResolvedValue({
      ...logs(873, 0),
      latest_cursor: 900,
      next_cursor: 900,
      gaps: [{ after: 872, through: 900, reason: "producer_retention" }],
    });
    const tail = await query.fetchQuery(options);
    expect(tail.next_cursor).toBe(900);
    expect(tail.gaps).toHaveLength(1);
    const telemetry = observationOptions<TelemetryPage>(
      query,
      "telemetry",
      "project",
      "run",
      "attempt",
      "owner",
      true,
      true,
    );
    vi.mocked(trainingObservabilityApi.telemetry).mockResolvedValue({
      ...logs(1, 0),
      items: [],
      metric_definitions: {},
    });
    await query.fetchQuery(telemetry);
    expect(trainingObservabilityApi.telemetry).toHaveBeenCalledWith(
      "project",
      "run",
      "attempt",
      undefined,
      { signal: expect.any(AbortSignal) },
    );
  });

  it("resets a changed stream, retains a changed producer boot, and fetches a fresh terminal tail", async () => {
    const query = client();
    const live = observationOptions<LogPage>(
      query,
      "logs",
      "project",
      "run",
      "attempt",
      "owner",
      true,
      true,
    );
    query.setQueryData(live.queryKey, mergeObservationPage(undefined, logs(100), 512));
    vi.mocked(trainingObservabilityApi.logs)
      .mockResolvedValueOnce(logs(101, 0, "new"))
      .mockResolvedValueOnce(logs(1, 2, "new"));
    const changed = await query.fetchQuery(live);
    expect(changed.items.map((item) => item.sequence)).toEqual([1, 2]);
    expect(trainingObservabilityApi.logs).toHaveBeenLastCalledWith(
      "project",
      "run",
      "attempt",
      { after: undefined },
      { signal: expect.any(AbortSignal) },
    );
    const next = logs(3, 1, "new");
    next.items[0].producer_boot_id = "boot-after-restart";
    vi.mocked(trainingObservabilityApi.logs).mockResolvedValue(next);
    expect((await query.fetchQuery(live)).items).toHaveLength(3);
    const final = observationOptions<LogPage>(
      query,
      "logs",
      "project",
      "run",
      "attempt",
      "owner",
      true,
      false,
    );
    vi.mocked(trainingObservabilityApi.logs).mockResolvedValue(logs(900, 1, "new"));
    expect((await query.fetchQuery(final)).items[0].sequence).toBe(900);
    expect(trainingObservabilityApi.logs).toHaveBeenLastCalledWith(
      "project",
      "run",
      "attempt",
      { after: undefined },
      { signal: expect.any(AbortSignal) },
    );
  });

  it("aborts switched attempt requests and rejects a late owner result", async () => {
    const query = client();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={query}>{children}</QueryClientProvider>
    );
    let resolve!: (value: TelemetryPage) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(trainingObservabilityApi.telemetry)
      .mockImplementationOnce((_p, _r, _a, _after, options) => {
        signal = options?.signal as AbortSignal;
        return new Promise((yes) => {
          resolve = yes;
        });
      })
      .mockResolvedValue({
        ...logs(1, 0),
        attempt_id: "second",
        items: [],
        metric_definitions: {},
      });
    const hook = renderHook(
      ({ attempt, scope }) =>
        useTrainingObservability("project", "run", attempt, scope, true, false, false),
      { wrapper, initialProps: { attempt: "attempt", scope: "old" } },
    );
    await waitFor(() => expect(signal).toBeDefined());
    hook.rerender({ attempt: "second", scope: "new" });
    await waitFor(() => expect(hook.result.current.telemetry.data?.attempt_id).toBe("second"));
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve({ ...logs(1, 0), items: [], metric_definitions: {} }));
    expect(hook.result.current.telemetry.data?.attempt_id).toBe("second");
  });
});

it("keeps true zero separate from unavailable values and explains the ETA basis", () => {
  expect(resourceValue("gpu_utilization", 0)).toBe("0%");
  expect(resourceValue("rss_bytes", null)).toBe("不可用");
  expect(estimateLabel()).toContain("至少 3");
  expect(
    estimateLabel({
      phase: "training",
      progress: {
        eta: { available: true, minimum_seconds: 120, maximum_seconds: 180, sample_count: 4 },
      },
    }),
  ).toContain("基于 4 个周期");
  expect(estimateLabel({ phase: "uploading" })).toContain("未知");
  const training = {
    phase: "training" as const,
    progress: {
      eta: { available: true, minimum_seconds: 120, maximum_seconds: 180, sample_count: 4 },
    },
  };
  for (const status of ["failed", "cancelled", "succeeded"])
    expect(estimateLabel(training, status)).toContain("已结束");
  for (const status of ["lost", "cancelling"])
    expect(estimateLabel(training, status)).toContain("暂不估算");
  expect(estimateLabel(training, "training", true)).toContain("已过期");
});
