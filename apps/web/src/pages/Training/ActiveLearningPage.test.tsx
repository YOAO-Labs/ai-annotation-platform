import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { activeLearningApi } from "@/api/activeLearning";
import { useAuthStore } from "@/stores/authStore";
import { ActiveLearningPage } from "./ActiveLearningPage";
vi.mock("@/api/activeLearning", () => ({ activeLearningApi: { list: vi.fn() } }));
const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/p1/learning/active-learning"]}>
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
