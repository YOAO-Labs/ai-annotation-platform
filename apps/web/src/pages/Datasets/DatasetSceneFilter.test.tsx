import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { DatasetSceneFilter } from "./DatasetSceneFilter";
import { ApiError } from "@/api/client";

const api = vi.hoisted(() => ({ sceneOptions: vi.fn() }));
const auth = vi.hoisted(() => ({ revision: 1, session: "one", user: "user" }));
vi.mock("@/api/datasets", () => ({ datasetsApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionRevision: () => auth.revision }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (state: unknown) => unknown) =>
    select({ user: { id: auth.user }, sessionId: auth.session }),
}));
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
const selected = { id: "outside", display_id: "S-99", name: "已选街道" };
function mount(sceneId?: string) {
  const change = vi.fn();
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <DatasetSceneFilter datasetId="dataset" sceneId={sceneId} onChange={change} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { change, client, refresh: () => view.rerender(tree()) };
}
beforeEach(() => {
  vi.clearAllMocks();
  auth.revision = 1;
  auth.session = "one";
  api.sceneOptions.mockImplementation((_id, params) =>
    Promise.resolve({
      items: params.offset
        ? [{ id: "last", display_id: "S-21", name: "最后街道" }]
        : [{ id: "first", display_id: "S-01", name: "首条街道" }],
      total: 21,
      selected: params.selected_id ? selected : null,
    }),
  );
});
it("keeps off-page Scene selection while browsing bounded option pages", async () => {
  const view = mount("outside");
  await screen.findByRole("option", { name: "已选街道" });
  expect(screen.getByRole("combobox", { name: "Scene 筛选" })).toHaveValue("outside");
  fireEvent.click(screen.getByRole("button", { name: "下一页 Scene" }));
  await screen.findByRole("option", { name: "最后街道" });
  expect(api.sceneOptions).toHaveBeenLastCalledWith(
    "dataset",
    { limit: 20, offset: 20, search: undefined, selected_id: "outside" },
    { signal: expect.any(AbortSignal) },
  );
  expect(screen.getByRole("combobox")).toHaveValue("outside");
  expect(view.change).not.toHaveBeenCalled();
});
it("searches choices without changing the sample scope and applies only explicit selection", async () => {
  const view = mount("outside");
  await screen.findByRole("option", { name: "已选街道" });
  fireEvent.change(screen.getByRole("textbox", { name: "查找 Scene" }), {
    target: { value: "首条" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查找" }));
  await waitFor(() =>
    expect(api.sceneOptions).toHaveBeenLastCalledWith(
      "dataset",
      expect.objectContaining({ search: "首条", offset: 0, selected_id: "outside" }),
      { signal: expect.any(AbortSignal) },
    ),
  );
  expect(view.change).not.toHaveBeenCalled();
  await screen.findByRole("option", { name: "首条街道" });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "first" } });
  expect(view.change).toHaveBeenCalledWith("first");
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
  expect(view.change).toHaveBeenLastCalledWith(undefined);
});
it("retains an invalid or deleted Scene ID on failure until the user clears it", async () => {
  api.sceneOptions.mockRejectedValue(new ApiError(404, "Scene missing"));
  const view = mount("dead");
  await screen.findByRole("alert");
  expect(screen.getByRole("combobox")).toHaveValue("dead");
  expect(screen.getByRole("option", { name: "指定 Scene" })).toBeInTheDocument();
  expect(view.change).not.toHaveBeenCalled();
});
it("hides retained names after read access is revoked", async () => {
  const view = mount("outside");
  await screen.findByRole("option", { name: "已选街道" });
  api.sceneOptions.mockRejectedValue(new ApiError(403, "Access revoked"));
  await act(async () => {
    await view.client.invalidateQueries({ queryKey: ["dataset-scene-options"] });
  });
  await screen.findByRole("alert");
  expect(screen.queryByRole("option", { name: "已选街道" })).not.toBeInTheDocument();
  expect(screen.getByRole("combobox")).toHaveValue("outside");
});
it("retires late option responses when the authenticated session changes", async () => {
  let resolve!: (value: unknown) => void;
  api.sceneOptions.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const view = mount("outside");
  auth.revision++;
  auth.session = "two";
  api.sceneOptions.mockResolvedValue({ items: [], total: 0, selected: null });
  view.refresh();
  await screen.findByText("暂无 Scene 选项。");
  await act(async () => resolve({ items: [selected], total: 1, selected }));
  expect(screen.queryByRole("option", { name: "已选街道" })).not.toBeInTheDocument();
  expect(view.change).not.toHaveBeenCalled();
});
