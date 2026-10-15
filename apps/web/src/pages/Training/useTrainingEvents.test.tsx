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
  expect(trainingRunsApi.events).toHaveBeenCalledWith("project", "run", "first", 100);
  extra = true;
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["training-events", "project", "run", "first"] });
  });
  await waitFor(() => expect(hook.result.current.data).toHaveLength(106));
  expect(trainingRunsApi.events).toHaveBeenLastCalledWith("project", "run", "first", 105);
  hook.rerender({ attempt: "second" });
  await waitFor(() => expect(hook.result.current.data).toHaveLength(2));
  expect(trainingRunsApi.events).toHaveBeenLastCalledWith("project", "run", "second", 0);
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
  });
});
