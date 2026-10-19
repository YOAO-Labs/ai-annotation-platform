import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { trainingObservabilityApi, type LogPage } from "@/api/trainingObservability";
import { TrainingLogViewer } from "./TrainingLogViewer";
import { mergeObservationPage } from "./useTrainingObservability";
import { copyToClipboard } from "@/lib/clipboard";

vi.mock("@/api/trainingObservability", () => ({ trainingObservabilityApi: { logs: vi.fn() } }));
vi.mock("@/lib/clipboard", () => ({ copyToClipboard: vi.fn() }));
const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.resetAllMocks();
  vi.restoreAllMocks();
});
const page = (from: number, count: number): LogPage => ({
  attempt_id: "attempt",
  stream_id: "stream",
  earliest_cursor: 1,
  latest_cursor: from + count - 1,
  next_cursor: from + count - 1,
  has_more: false,
  gaps: [],
  summary: null,
  items: Array.from({ length: count }, (_, index) => ({
    sequence: from + index,
    producer_boot_id: "boot",
    captured_at: "2026-10-15T00:00:00Z",
    received_at: "2026-10-15T00:00:00Z",
    text: `entry-${from + index}\n`,
  })),
});

it("copies every loaded log block verbatim, including multiline content", async () => {
  const client = new QueryClient();
  clients.push(client);
  vi.mocked(copyToClipboard).mockResolvedValue(true);
  const logs = page(10, 3);
  logs.items[1].text = `  Traceback\n${"diagnostic ".repeat(200)}\n`;
  const data = mergeObservationPage(undefined, logs, 512);
  render(
    <QueryClientProvider client={client}>
      <TrainingLogViewer
        projectId="project"
        runId="run"
        attemptId="attempt"
        data={data}
        pending={false}
        assertOwner={() => {}}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "复制当前日志" }));
  await waitFor(() =>
    expect(copyToClipboard).toHaveBeenCalledWith(logs.items.map((item) => item.text).join("")),
  );
  expect(screen.getByLabelText("脱敏日志内容")).toHaveAttribute("tabindex", "0");
});

it("loads earlier escaped content without moving the live cursor, bounds the viewport, and resumes following explicitly", async () => {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  clients.push(client);
  const user = userEvent.setup();
  const tail = mergeObservationPage(undefined, page(601, 120), 512);
  vi.mocked(trainingObservabilityApi.logs).mockImplementation(async (_p, _r, _a, cursor) => {
    const from = (cursor?.before ?? 121) - 120;
    const older = page(from, 120);
    if (from === 481) older.items[0].text = "<script>window.attacked=true</script>\n";
    return older;
  });
  const view = (data = tail) => (
    <QueryClientProvider client={client}>
      <TrainingLogViewer
        projectId="project"
        runId="run"
        attemptId="attempt"
        data={data}
        pending={false}
        assertOwner={() => {}}
      />
    </QueryClientProvider>
  );
  const result = render(view());
  const box = screen.getByLabelText("脱敏日志内容");
  const rectangles = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      const index = this.dataset.sequence
        ? [...(this.parentElement?.children ?? [])].indexOf(this)
        : 0;
      const top = this.dataset.sequence ? index * 10 - (this.parentElement?.scrollTop ?? 0) : 0;
      return {
        top,
        bottom: top + (this.dataset.sequence ? 10 : 100),
        left: 0,
        right: 320,
        width: 320,
        height: this.dataset.sequence ? 10 : 100,
        x: 0,
        y: top,
        toJSON: () => ({}),
      };
    });
  box.scrollTop = 200;
  Object.defineProperties(box, {
    scrollHeight: { configurable: true, get: () => box.children.length * 10 },
    clientHeight: { configurable: true, value: 100 },
  });
  fireEvent.scroll(box);
  await user.click(screen.getByRole("button", { name: "加载更早日志" }));
  await waitFor(() =>
    expect(screen.getByLabelText("脱敏日志内容").textContent).toContain("<script>"),
  );
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByRole("checkbox", { name: "跟随最新日志" })).not.toBeChecked();
  expect(box.scrollTop).toBe(1400);
  rectangles.mockRestore();
  for (let index = 0; index < 4; index++) {
    await user.click(screen.getByRole("button", { name: "加载更早日志" }));
    await waitFor(() => expect(trainingObservabilityApi.logs).toHaveBeenCalledTimes(index + 2));
    await waitFor(() =>
      expect(box.querySelector("[data-sequence]")).toHaveAttribute(
        "data-sequence",
        String(361 - index * 120),
      ),
    );
  }
  expect(
    screen.getByLabelText("脱敏日志内容").textContent?.split("\n").filter(Boolean),
  ).toHaveLength(512);
  expect(tail.next_cursor).toBe(720);
  expect(trainingObservabilityApi.logs).toHaveBeenCalledWith(
    "project",
    "run",
    "attempt",
    { before: 601 },
    { signal: expect.any(AbortSignal) },
  );
  const next = mergeObservationPage(tail, page(721, 1), 512);
  result.rerender(view(next));
  expect(screen.getByLabelText("脱敏日志内容").textContent).not.toContain("entry-721");
  await user.click(screen.getByRole("checkbox", { name: "跟随最新日志" }));
  await waitFor(() =>
    expect(screen.getByLabelText("脱敏日志内容").textContent).toContain("entry-721"),
  );
});

it("discards a superseded earlier request's abort error when the stream changes", async () => {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  clients.push(client);
  let reject!: (error: Error) => void;
  let signal: AbortSignal | undefined;
  vi.mocked(trainingObservabilityApi.logs).mockImplementation((_p, _r, _a, _cursor, options) => {
    signal = options?.signal as AbortSignal;
    return new Promise((_yes, no) => {
      reject = no;
    });
  });
  const tail = mergeObservationPage(undefined, page(601, 120), 512);
  const view = (data = tail) => (
    <QueryClientProvider client={client}>
      <TrainingLogViewer
        projectId="project"
        runId="run"
        attemptId="attempt"
        data={data}
        pending={false}
        assertOwner={() => {}}
      />
    </QueryClientProvider>
  );
  const result = render(view());
  await userEvent.setup().click(screen.getByRole("button", { name: "加载更早日志" }));
  result.rerender(
    view({ ...mergeObservationPage(undefined, page(1, 1), 512), stream_id: "replacement" }),
  );
  await waitFor(() => expect(signal?.aborted).toBe(true));
  await act(async () => reject(new DOMException("Superseded stream", "AbortError")));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByLabelText("脱敏日志内容").textContent).toBe("entry-1\n");
});
