import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { trainingRecipesApi, type TrainingRecipeOut } from "@/api/trainingRecipes";
import { TrainingRecipeSave } from "./TrainingRecipeLibrary";
import { effectiveChanges } from "./TrainingReuseDialog";
import { useAuthStore } from "@/stores/authStore";

vi.mock("@/api/trainingRecipes", async (original) => ({
  ...(await original<typeof import("@/api/trainingRecipes")>()),
  trainingRecipesApi: { list: vi.fn(), save: vi.fn(), get: vi.fn() },
}));
const recipe = {
  id: "recipe",
  head_revision: 1,
  archived: false,
  revision: { id: "revision", name: "原配方", ordinal: 1 },
} as TrainingRecipeOut;
const source = {
  configuration: {
    framework: "ultralytics" as const,
    base_model: "yolo11n",
    configuration: { schema_id: "schema", schema_digest: "a".repeat(64), values: { rect: true } },
  },
};
const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <TrainingRecipeSave projectId="project" source={source} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  useAuthStore.getState().setAuth("recipe-test", { id: "recipe-owner" } as never);
  vi.clearAllMocks();
  vi.mocked(trainingRecipesApi.list).mockResolvedValue({ total: 1, items: [recipe] });
  vi.mocked(trainingRecipesApi.save).mockResolvedValue(recipe);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

it("retries a lost response with the identical captured revision request", async () => {
  const user = userEvent.setup();
  mount();
  vi.mocked(trainingRecipesApi.save).mockRejectedValueOnce(new Error("网络中断"));
  await user.click(screen.getByRole("button", { name: "保存为配方" }));
  await user.type(screen.getByLabelText("配方名称"), "保留名称");
  await user.click(screen.getByRole("button", { name: "创建配方" }));
  await screen.findByText(/网络中断/);
  expect(screen.getByLabelText("配方名称")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "重试原保存请求" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(vi.mocked(trainingRecipesApi.save).mock.calls[1]).toEqual(
    vi.mocked(trainingRecipesApi.save).mock.calls[0],
  );
});

it("captures the expected head, preserves edits on conflict and requires an explicit new basis", async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole("button", { name: "保存为配方" }));
  await user.selectOptions(screen.getByLabelText("配方保存位置"), "recipe");
  await user.type(screen.getByLabelText("配方名称"), "保留编辑");
  await user.type(screen.getByLabelText("配方说明"), "说明");
  vi.mocked(trainingRecipesApi.save).mockRejectedValueOnce(new ApiError(409, "配方出现新修订"));
  vi.mocked(trainingRecipesApi.get).mockResolvedValue({
    ...recipe,
    head_revision: 2,
    revision: { ...recipe.revision, ordinal: 2, name: "别人的修订" },
  });
  await user.click(screen.getByRole("button", { name: "创建新修订" }));
  await screen.findByText(/最新修订 2/);
  expect(screen.getByLabelText("配方名称")).toHaveValue("保留编辑");
  expect(screen.getByLabelText("配方说明")).toHaveValue("说明");
  expect(vi.mocked(trainingRecipesApi.save).mock.calls[0][1].expected_head_revision).toBe(1);
  await user.click(screen.getByRole("button", { name: "保留编辑并以此修订为基准" }));
  await user.click(screen.getByRole("button", { name: "创建新修订" }));
  await waitFor(() => expect(trainingRecipesApi.save).toHaveBeenCalledTimes(2));
  const first = vi.mocked(trainingRecipesApi.save).mock.calls[0][1];
  const second = vi.mocked(trainingRecipesApi.save).mock.calls[1][1];
  expect(second.expected_head_revision).toBe(2);
  expect(second.request_id).not.toBe(first.request_id);
  expect(second.name).toBe(first.name);
  expect(second.configuration).toEqual(first.configuration);
});

it("diffs all effective values including dependency changes and platform fields", () => {
  const after = {
    framework: "ultralytics" as const,
    framework_version: "8.4.177",
    image_digest: "sha256:x",
    base_model: { id: "base", sha256: "sha", filename: "base.pt" },
    configuration: {
      identity: { schema_id: "schema", schema_digest: "digest" },
      user_values: { rect: true },
      effective_values: { rect: true, mosaic: 0, device: 0, epochs: 50 },
    },
  };
  const before = {
    ...after,
    configuration: {
      ...after.configuration,
      user_values: {},
      effective_values: { rect: false, mosaic: 1, device: 1, epochs: 50 },
    },
  };
  expect(effectiveChanges(before, after).map((change) => change.key)).toEqual([
    "device",
    "mosaic",
    "rect",
  ]);
});
