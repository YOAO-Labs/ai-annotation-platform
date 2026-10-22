import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { activeLearningApi } from "@/api/activeLearning";
import { useAuthStore } from "@/stores/authStore";
import { ActiveLearningPage } from "./ActiveLearningPage";
import * as activeLearningScope from "./useActiveLearning";
vi.mock("@/api/activeLearning", () => ({ activeLearningApi: { list: vi.fn() } }));
const clients: QueryClient[] = [];
function mount(query = "") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/p1/learning/active-learning${query}`]}>
        <Routes>
          <Route path="/projects/:id/learning/active-learning" element={<ActiveLearningPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("list-token", { id: "u1" } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});
it("shows actionable empty state and correct model return route", async () => {
  vi.mocked(activeLearningApi.list).mockResolvedValue({ total: 0, items: [] });
  mount();
  await screen.findByText(/还没有选样轮次/);
  expect(screen.getByRole("link", { name: "返回模型版本" })).toHaveAttribute(
    "href",
    "/projects/p1/learning?tab=models",
  );
  expect(screen.getByRole("link", { name: "新建选样轮次" })).toHaveAttribute(
    "href",
    "/projects/p1/learning/active-learning/new",
  );
});
it("does not render a response for another project", async () => {
  vi.mocked(activeLearningApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: "r1", project_id: "other", name: "foreign round", spec: { round_id: "r1" } }],
  } as never);
  mount();
  await screen.findByText(/轮次列表所属项目或身份不符/);
  expect(screen.queryByText("foreign round")).not.toBeInTheDocument();
});
it("resets round pagination within the same owner and ignores a late response from the prior filter", async () => {
  vi.spyOn(activeLearningScope, "useActiveLearningScope").mockReturnValue("round-list-owner");
  let resolve: ((value: never) => void) | undefined;
  vi.mocked(activeLearningApi.list).mockImplementation(async (_project, offset, _options, task) => {
    if (offset === 20)
      return new Promise((done) => {
        resolve = done;
      });
    return {
      total: task === "segment" ? 1 : 21,
      items:
        task === "segment"
          ? [
              {
                id: "segment",
                project_id: "p1",
                name: "分割轮次",
                status: "succeeded",
                spec: {
                  round_id: "segment",
                  schema: 2,
                  task: "segment",
                  model_ref: { task: "segmentation" },
                  candidate_count: 4,
                  batch_size: 1,
                },
              },
            ]
          : [],
    } as never;
  });
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "选样轮次下一页" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "选样轮次下一页" }));
  await waitFor(() =>
    expect(activeLearningApi.list).toHaveBeenCalledWith(
      "p1",
      20,
      expect.any(Object),
      undefined,
      20,
    ),
  );
  fireEvent.change(screen.getByLabelText("选样任务"), { target: { value: "segment" } });
  expect(await screen.findByText("分割轮次")).toBeVisible();
  expect(activeLearningApi.list).toHaveBeenLastCalledWith(
    "p1",
    0,
    expect.any(Object),
    "segment",
    20,
  );
  await act(async () =>
    resolve?.({
      total: 21,
      items: [{ id: "old", project_id: "p1", name: "迟到旧轮次", spec: { round_id: "old" } }],
    } as never),
  );
  expect(screen.queryByText("迟到旧轮次")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "选样轮次上一页" })).toBeDisabled();
  expect(screen.getByLabelText("选样任务")).toHaveValue("segment");
});
it("withholds a mismatched task response from a segmentation filter", async () => {
  vi.mocked(activeLearningApi.list).mockResolvedValue({
    total: 1,
    items: [
      {
        id: "r1",
        project_id: "p1",
        name: "检测轮次",
        spec: { round_id: "r1", model_ref: { task: "detection" } },
      },
    ],
  } as never);
  mount("?task=segment");
  expect(await screen.findByText("轮次列表任务与当前筛选不一致")).toBeVisible();
  expect(screen.queryByText("检测轮次")).not.toBeInTheDocument();
});
