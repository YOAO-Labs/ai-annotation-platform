import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { trainingRunsApi } from "@/api/trainingRuns";
import { useTrainingEvents } from "./useTrainingEvents";
import { epochPoints } from "./trainingPresentation";

vi.mock("@/api/trainingRuns", () => ({ trainingRunsApi: { events: vi.fn() } }));
const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.resetAllMocks();
});

it("paginates beyond 100 events, then fetches only new events and isolates resumed attempts", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  let extra = false;
  vi.mocked(trainingRunsApi.events).mockImplementation(
    async (_project, _run, attempt, after = 0) => ({
      attempt_id: attempt!,
      items: Array.from({ length: attempt === "second" ? 2 : extra ? 106 : 105 }, (_, i) => ({
        sequence: i + 1,
        kind: "epoch",
        epoch: i + 1,
        metrics: { "train/loss": 10 / (i + 1) },
      }))
        .filter((event) => event.sequence > after)
        .slice(0, 100),
    }),
  );
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    ({ attempt }) => useTrainingEvents("project", "run", attempt, true, false),
    { wrapper, initialProps: { attempt: "first" } },
  );
  await waitFor(() => expect(hook.result.current.data).toHaveLength(105));
  expect(trainingRunsApi.events).toHaveBeenCalledWith("project", "run", "first", 100, {
    signal: expect.any(AbortSignal),
  });
  extra = true;
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["training-events", "project", "run", "first"] });
  });
  await waitFor(() => expect(hook.result.current.data).toHaveLength(106));
  expect(trainingRunsApi.events).toHaveBeenLastCalledWith("project", "run", "first", 105, {
    signal: expect.any(AbortSignal),
  });
  hook.rerender({ attempt: "second" });
  await waitFor(() => expect(hook.result.current.data).toHaveLength(2));
  expect(trainingRunsApi.events).toHaveBeenLastCalledWith("project", "run", "second", 0, {
    signal: expect.any(AbortSignal),
  });
});

describe("native metric semantics", () => {
  it("keeps zero, omits unavailable COCO values and duplicate aliases, and never relabels Libre precision", () => {
    const points = epochPoints(
      [
        {
          sequence: 1,
          kind: "epoch",
          epoch: 2,
          metrics: {
            "metrics/mAP50": 0,
            "metrics/mAP50(B)": 0,
            "metrics/mAP_small": -1,
            "metrics/precision": 0.4,
            "metrics/recall": 0.2,
            "train/loss": 2,
          },
        },
      ],
      "libreyolo",
    );
    expect(points).toEqual([{ epoch: 2, "metrics/mAP50": 0, "train/loss": 2 }]);
    expect(
      epochPoints(
        [
          {
            sequence: 1,
            kind: "epoch",
            epoch: 1,
            metrics: { "metrics/precision(B)": -1, "metrics/recall(B)": 0 },
          },
        ],
        "ultralytics",
      ),
    ).toEqual([{ epoch: 1, "metrics/recall(B)": 0 }]);
  });
});

it("fetches the last events on transition to terminal and retains the incremental cursor", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  let completed = false;
  vi.mocked(trainingRunsApi.events).mockImplementation(async (_p, _r, attempt, after = 0) => ({
    attempt_id: attempt!,
    items: [
      { sequence: 1, kind: "epoch", epoch: 1, metrics: { "metrics/mAP50": 0 } },
      ...(completed
        ? [{ sequence: 2, kind: "epoch", epoch: 2, metrics: { "metrics/mAP50": 0.4 } }]
        : []),
    ].filter((e) => e.sequence > after),
  }));
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    ({ poll }) => useTrainingEvents("project", "run", "attempt", true, poll, "owner"),
    { wrapper, initialProps: { poll: true } },
  );
  await waitFor(() => expect(hook.result.current.data).toHaveLength(1));
  completed = true;
  hook.rerender({ poll: false });
  await waitFor(() => expect(hook.result.current.data).toHaveLength(2));
  expect(trainingRunsApi.events).toHaveBeenLastCalledWith("project", "run", "attempt", 1, {
    signal: expect.any(AbortSignal),
  });
});

it("aborts an old owner's request and never populates the new owner's event cache", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  let resolve!: (value: Awaited<ReturnType<typeof trainingRunsApi.events>>) => void;
  let oldSignal: AbortSignal | undefined;
  vi.mocked(trainingRunsApi.events)
    .mockImplementationOnce((_p, _r, _a, _c, options) => {
      oldSignal = options?.signal as AbortSignal;
      return new Promise((yes) => {
        resolve = yes;
      });
    })
    .mockResolvedValue({
      attempt_id: "attempt",
      items: [{ sequence: 1, kind: "epoch", epoch: 1, metrics: { "metrics/mAP50": 0.2 } }],
    });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    ({ owner }) => useTrainingEvents("project", "run", "attempt", true, false, owner),
    { wrapper, initialProps: { owner: "old" } },
  );
  await waitFor(() => expect(oldSignal).toBeDefined());
  hook.rerender({ owner: "new" });
  await waitFor(() => expect(hook.result.current.data?.[0].metrics["metrics/mAP50"]).toBe(0.2));
  expect(oldSignal!.aborted).toBe(true);
  await act(async () =>
    resolve({
      attempt_id: "attempt",
      items: [{ sequence: 1, kind: "epoch", epoch: 9, metrics: { "metrics/mAP50": 0.9 } }],
    }),
  );
  expect(hook.result.current.data?.[0].epoch).toBe(1);
});
