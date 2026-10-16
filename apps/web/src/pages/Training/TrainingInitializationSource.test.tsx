import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingRunsApi, type TrainingInitialization } from "@/api/trainingRuns";
import { useAuthStore } from "@/stores/authStore";
import {
  TrainingInitializationSource,
  useTrainingInitializationSource,
} from "./TrainingInitializationSource";

vi.mock("@/api/trainedModels", () => ({ trainedModelsApi: { list: vi.fn() } }));
vi.mock("@/api/trainingRuns", () => ({ trainingRunsApi: { initializationSource: vi.fn() } }));
const modelId = "11111111-1111-4111-8111-111111111111";
const pin = {
  model_version_id: modelId,
  model_name: "离页候选",
  base_model: "yolo11n",
  classes: ["car"],
  artifact: { sha256: "a".repeat(64), size_bytes: 123 },
  source_digest: "b".repeat(64),
  dataset_id: "data",
  validation_digest: "c".repeat(64),
};
const clients: QueryClient[] = [];
function Content({ initial }: { initial?: TrainingInitialization }) {
  const [value, setValue] = useState(initial);
  const qualification = useTrainingInitializationSource(
    "project",
    "ultralytics",
    "yolo11n",
    "dataset",
    value,
  );
  return (
    <>
      <TrainingInitializationSource
        projectId="project"
        value={value}
        disabled={false}
        qualification={qualification}
        onChange={setValue}
      />
      <output>{JSON.stringify(value)}</output>
    </>
  );
}
function mount(initial?: TrainingInitialization) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <Content initial={initial} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("source-session", { id: "source-owner" } as never);
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 0, items: [] });
  vi.mocked(trainingRunsApi.initializationSource).mockResolvedValue({
    model_version_id: modelId,
    eligible: true,
    reasons: [],
    pin,
  } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

it("looks up the selected ID independently and keeps an unavailable pin unchanged", async () => {
  const initial = {
    kind: "trained_model" as const,
    model_version_id: modelId,
    source_digest: pin.source_digest,
  };
  vi.mocked(trainingRunsApi.initializationSource).mockResolvedValue({
    model_version_id: modelId,
    eligible: false,
    reasons: ["原生类别顺序不同"],
    pin,
  } as never);
  mount(initial);
  await screen.findByText("原生类别顺序不同");
  expect(screen.getByRole("option", { name: /离页候选.*保留的选择/ })).toHaveValue(modelId);
  expect(screen.getByRole("status")).toHaveTextContent(JSON.stringify(initial));
  expect(trainingRunsApi.initializationSource).toHaveBeenCalledWith(
    "project",
    modelId,
    "ultralytics",
    "yolo11n",
    "dataset",
    expect.any(Object),
  );
});

it("pages archive-inclusive candidates and preserves object failures", async () => {
  vi.mocked(trainedModelsApi.list)
    .mockResolvedValueOnce({ total: 21, items: [] })
    .mockResolvedValueOnce({
      total: 21,
      items: [{ id: modelId, name: "归档候选", framework: "ultralytics", archived: true }],
    } as never);
  vi.mocked(trainingRunsApi.initializationSource).mockRejectedValue(new Error("权重缺失"));
  mount();
  await userEvent.click(await screen.findByRole("button", { name: "加载更早的候选模型" }));
  await userEvent.selectOptions(await screen.findByLabelText("初始化权重"), modelId);
  await screen.findByText(/来源暂不可用：权重缺失/);
  expect(screen.getByLabelText("初始化权重")).toHaveValue(modelId);
  expect(vi.mocked(trainedModelsApi.list).mock.calls[1]).toEqual([
    "project",
    20,
    expect.any(Object),
    "include",
  ]);
});

it("rejects delayed source A after choosing B and A again", async () => {
  let resolve!: (result: never) => void;
  const late = new Promise<never>((yes) => {
    resolve = yes;
  });
  vi.mocked(trainingRunsApi.initializationSource)
    .mockReturnValueOnce(late)
    .mockResolvedValue({ model_version_id: modelId, eligible: true, reasons: [], pin } as never);
  mount({ kind: "trained_model", model_version_id: modelId, source_digest: pin.source_digest });
  await waitFor(() => expect(trainingRunsApi.initializationSource).toHaveBeenCalledTimes(1));
  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("初始化权重"), "");
  // The original off-page ID remains explicitly available only while selected.
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: modelId, name: "候选 A", framework: "ultralytics" }],
  } as never);
  clients[0].invalidateQueries({ queryKey: ["training-initialization-choices"] });
  await screen.findByRole("option", { name: /候选 A/ });
  await user.selectOptions(screen.getByLabelText("初始化权重"), modelId);
  await screen.findByText(/离页候选 ·/);
  await act(async () =>
    resolve({ model_version_id: modelId, eligible: false, reasons: ["旧 A 错误"], pin } as never),
  );
  expect(screen.queryByText("旧 A 错误")).not.toBeInTheDocument();
});

it("rejects a delayed qualification after a batched authentication ABA", async () => {
  let resolve!: (result: never) => void;
  vi.mocked(trainingRunsApi.initializationSource)
    .mockReturnValueOnce(
      new Promise<never>((yes) => {
        resolve = yes;
      }),
    )
    .mockResolvedValue({ model_version_id: modelId, eligible: true, reasons: [], pin } as never);
  mount({ kind: "trained_model", model_version_id: modelId, source_digest: pin.source_digest });
  await waitFor(() => expect(trainingRunsApi.initializationSource).toHaveBeenCalledTimes(1));
  act(() => {
    useAuthStore.getState().setAuth("other", { id: "other" } as never);
    useAuthStore.getState().setAuth("source-session", { id: "source-owner" } as never);
  });
  await waitFor(() => expect(trainingRunsApi.initializationSource).toHaveBeenCalledTimes(2));
  await act(async () =>
    resolve({ model_version_id: modelId, eligible: false, reasons: ["旧会话错误"], pin } as never),
  );
  expect(screen.queryByText("旧会话错误")).not.toBeInTheDocument();
});
