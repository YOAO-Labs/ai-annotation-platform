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
function Content({
  initial,
  task = "detect",
}: {
  initial?: TrainingInitialization;
  task?: string;
}) {
  const [value, setValue] = useState(initial);
  const [pretrained, setPretrained] = useState(true);
  const qualification = useTrainingInitializationSource(
    "project",
    "ultralytics",
    task === "segment" ? "yolo11n-seg" : "yolo11n",
    "dataset",
    value,
    task,
  );
  return (
    <>
      <TrainingInitializationSource
        projectId="project"
        value={value}
        disabled={false}
        qualification={qualification}
        pretrained={pretrained}
        supportsScratch
        onChange={(next, usePretrained = true) => {
          setValue(next);
          setPretrained(usePretrained);
        }}
      />
      <output>{JSON.stringify(value)}</output>
      <p>{pretrained ? "pretrained" : "scratch"}</p>
    </>
  );
}
function mount(initial?: TrainingInitialization, task = "detect") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <Content initial={initial} task={task} />
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

it("switches explicitly between pretrained, random initialization and project weights", async () => {
  mount({ kind: "trained_model", model_version_id: modelId, source_digest: pin.source_digest });
  expect(screen.getByRole("region", { name: "模型初始化" })).toBeVisible();
  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("初始化权重"), "scratch");
  expect(screen.getByText("scratch")).toBeVisible();
  expect(screen.getByText(/仅加载所选模型的架构并随机初始化/)).toBeVisible();
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
  await user.selectOptions(screen.getByLabelText("初始化权重"), "");
  expect(screen.getByText("pretrained")).toBeVisible();
  expect(screen.getByLabelText("初始化权重")).toHaveValue("");
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
    { task: "detect", framework: "ultralytics" },
  ]);
});

it("filters project initialization candidates by task, framework and base model", async () => {
  const candidate = {
    id: modelId,
    name: "兼容候选",
    framework: "ultralytics",
    task: "detect",
    provenance: { base_model: { id: "yolo11n" } },
  };
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 4,
    items: [
      candidate,
      { ...candidate, id: "other-task", name: "分割候选", task: "segment" },
      { ...candidate, id: "other-framework", name: "其他框架", framework: "libreyolo" },
      {
        ...candidate,
        id: "other-model",
        name: "其他型号",
        provenance: { base_model: { id: "yolo11s" } },
      },
    ],
  } as never);
  mount();
  await screen.findByRole("option", { name: /兼容候选/ });
  expect(
    screen.queryByRole("option", { name: /分割候选|其他框架|其他型号/ }),
  ).not.toBeInTheDocument();
  expect(trainedModelsApi.list).toHaveBeenCalledWith("project", 0, expect.any(Object), "include", {
    task: "detect",
    framework: "ultralytics",
  });
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

it("qualifies segmentation weights and lists candidates only within the segmentation task", async () => {
  mount(
    { kind: "trained_model", model_version_id: modelId, source_digest: pin.source_digest },
    "segment",
  );
  await waitFor(() =>
    expect(trainingRunsApi.initializationSource).toHaveBeenCalledWith(
      "project",
      modelId,
      "ultralytics",
      "yolo11n-seg",
      "dataset",
      expect.any(Object),
    ),
  );
  expect(trainedModelsApi.list).toHaveBeenCalledWith("project", 0, expect.any(Object), "include", {
    task: "segment",
    framework: "ultralytics",
  });
  expect(screen.queryByText(/尚未取得项目权重初始化资格/)).not.toBeInTheDocument();
});
