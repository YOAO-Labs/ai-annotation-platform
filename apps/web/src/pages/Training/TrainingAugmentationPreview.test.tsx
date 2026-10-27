import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  trainingPreviewsApi,
  type TrainingPreviewCreate,
  type TrainingPreviewOut,
} from "@/api/trainingPreviews";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { setActiveClassesConfig } from "@/pages/Workbench/stage/colors";
import { TrainingAugmentationPreview } from "./TrainingAugmentationPreview";

vi.mock("@/api/trainingPreviews", () => ({
  trainingPreviewsApi: {
    capabilities: vi.fn(),
    create: vi.fn(),
    get: vi.fn(),
    cancel: vi.fn(),
    image: vi.fn(),
  },
}));
vi.mock("@/api/trainingDatasets", () => ({ trainingDatasetsApi: { sampleMedia: vi.fn() } }));
const clients: QueryClient[] = [];
const createUrl = vi.fn(() => "blob:preview");
const revokeUrl = vi.fn();
const props = {
  projectId: "project-1",
  binding: "draft:0:runtime:dataset-sha",
  epochs: 10,
  classes: ["car", "truck"],
  disabled: false,
  input: {
    dataset_id: "dataset-1",
    framework: "ultralytics" as TrainingPreviewCreate["framework"],
    base_model: "yolo11n",
    configuration: { schema_id: "schema", schema_digest: "a".repeat(64), values: { seed: 777 } },
  },
};
function outcome(
  body: TrainingPreviewCreate,
  status: TrainingPreviewOut["status"] = "succeeded",
): TrainingPreviewOut {
  return {
    id: "preview-1",
    project_id: props.projectId,
    request: body,
    input_digest: "b".repeat(64),
    config_digest: "c".repeat(64),
    configuration: {},
    async_job_id: "job-1",
    status,
    error_message: null,
    created_at: new Date().toISOString(),
    started_at: null,
    completed_at: null,
    expires_at: new Date(Date.now() + 86400000).toISOString(),
    report:
      status === "succeeded"
        ? {
            revision: 1,
            preview_id: "preview-1",
            input_digest: "b".repeat(64),
            config_digest: "c".repeat(64),
            seed: body.seed!,
            epoch: body.epoch!,
            mosaic_closed: false,
            native_parameters: { seed: 777, device: "cpu", workers: 0 },
            transform_parameters: { mosaic: 1 },
            policy: {},
            policy_digest: "d".repeat(64),
            runtime: {},
            pool_summary: { selected: 1 },
            source_pool: [
              {
                task_id: "source-1",
                labels: "0 0.5 0.5 0.2 0.2\n1 0.3 0.3 0.1 0.1\n",
                label_path: "labels/train/source.txt",
                image_path: "images/train/source.png",
                label_sha256: "e".repeat(64),
                sha256: "f".repeat(64),
                media_key: "owned",
                width: 16,
                height: 12,
                size_bytes: 128,
              },
            ],
            outputs: [
              {
                index: 0,
                image: "preview-0.png",
                sha256: "f".repeat(64),
                width: 128,
                height: 128,
                source_draws: ["source-1", "source-1"],
                sources_read: ["source-1", "source-1"],
                operations: [{ step: "clip", clipped: 2, filtered: 0 }],
                clipped: 2,
                filtered: 0,
                boxes: Array.from({ length: 601 }, (_, index) => ({
                  class_index: index % 2,
                  xywh: [0.5, 0.5, 0.2, 0.2],
                })),
              },
            ],
          }
        : null,
  };
}
function mount(initial = props) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const tree = (next = initial) => (
    <QueryClientProvider client={client}>
      <TrainingAugmentationPreview {...next} />
    </QueryClientProvider>
  );
  return { ...render(tree()), tree };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function generate() {
  const button = screen.getByRole("button", { name: "生成原生预览" });
  await waitFor(() => expect(button).toBeEnabled());
  await userEvent.setup().click(button);
}
beforeEach(() => {
  vi.resetAllMocks();
  createUrl.mockReturnValue("blob:preview");
  Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl });
  useAuthStore.getState().setAuth("session-a", { id: "user-a" } as never);
  vi.mocked(trainingPreviewsApi.capabilities).mockResolvedValue({
    qualified: true,
    online: true,
    busy: false,
    resource_id: "cpu",
    policy: {},
    policy_digest: "d".repeat(64),
    reason: null,
  });
  vi.mocked(trainingPreviewsApi.create).mockImplementation(async (_project, body) => outcome(body));
  vi.mocked(trainingPreviewsApi.image).mockResolvedValue(new Blob(["verified native PNG"]));
  vi.mocked(trainingDatasetsApi.sampleMedia).mockResolvedValue(new Blob(["verified source PNG"]));
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useAuthStore.getState().logout();
  setActiveClassesConfig(undefined);
});

describe("native augmentation preview ownership", () => {
  it("shows complete transformed/source boxes in class colors and frees authenticated Blob URLs", async () => {
    setActiveClassesConfig({ car: { color: "#010101" } });
    const view = mount();
    await generate();
    await screen.findByAltText("原生增强结果 1");
    const overlay = screen.getByRole("img", { name: "原生增强结果 1矩形框" });
    expect(overlay.querySelectorAll("rect")).toHaveLength(601);
    expect(overlay.querySelector("rect")).toHaveAttribute("x", "0.4");
    expect(overlay.querySelector("rect")?.getAttribute("stroke")).not.toBe("#010101");
    expect(
      new Set([...overlay.querySelectorAll("rect")].map((box) => box.getAttribute("stroke"))).size,
    ).toBe(2);
    expect(
      screen.getByRole("img", { name: "原始来源 1矩形框" }).querySelectorAll("rect"),
    ).toHaveLength(2);
    const signal = vi.mocked(trainingPreviewsApi.image).mock.calls[0][3]!.signal!;
    view.unmount();
    expect(signal.aborted).toBe(true);
    expect(revokeUrl).toHaveBeenCalledTimes(2);
  });
  it("invalidates immediately and ignores late create responses after a draft/runtime change", async () => {
    const late = deferred<TrainingPreviewOut>();
    vi.mocked(trainingPreviewsApi.create).mockReturnValue(late.promise);
    const view = mount();
    await generate();
    const body = vi.mocked(trainingPreviewsApi.create).mock.calls[0][1];
    const signal = vi.mocked(trainingPreviewsApi.create).mock.calls[0][2]!.signal!;
    view.rerender(view.tree({ ...props, binding: "draft:1:new-image" }));
    await act(async () => late.resolve(outcome(body)));
    expect(signal.aborted).toBe(true);
    expect(screen.queryByText(/预览已完成/)).not.toBeInTheDocument();
    expect(createUrl).not.toHaveBeenCalled();
  });
  it("isolates a same-user new token and ignores late errors from the old session", async () => {
    const late = deferred<TrainingPreviewOut>();
    vi.mocked(trainingPreviewsApi.create).mockReturnValue(late.promise);
    mount();
    await generate();
    act(() => useAuthStore.getState().setAuth("session-b", { id: "user-a" } as never));
    await act(async () => late.reject(new Error("old session error")));
    expect(screen.queryByText("old session error")).not.toBeInTheDocument();
    expect(screen.queryByText("正在提交…")).not.toBeInTheDocument();
  });
  it("retries the exact request and keeps preview controls separate from the training seed", async () => {
    vi.mocked(trainingPreviewsApi.create).mockRejectedValueOnce(
      new Error("temporary transport failure"),
    );
    mount();
    await generate();
    await userEvent.setup().click(await screen.findByRole("button", { name: "重试原请求" }));
    await screen.findByAltText("原生增强结果 1");
    const calls = vi.mocked(trainingPreviewsApi.create).mock.calls;
    expect(calls[0][1]).toEqual(calls[1][1]);
    expect(calls[0][1]).toMatchObject({
      seed: 42,
      epoch: 1,
      configuration: { values: { seed: 777 } },
    });
    fireEvent.change(screen.getByLabelText("预览种子"), { target: { value: "43" } });
    expect(screen.queryByAltText("原生增强结果 1")).not.toBeInTheDocument();
    await generate();
    await screen.findByAltText("原生增强结果 1");
    expect(calls[2][1].request_id).not.toBe(calls[0][1].request_id);
    expect(calls[2][1].seed).toBe(43);
    expect(props.input.configuration.values.seed).toBe(777);
  });
  it("ignores an older poll completion after cancellation", async () => {
    const late = deferred<TrainingPreviewOut>();
    vi.mocked(trainingPreviewsApi.create).mockImplementation(async (_project, body) =>
      outcome(body, "queued"),
    );
    vi.mocked(trainingPreviewsApi.get).mockReturnValue(late.promise);
    mount();
    await generate();
    await waitFor(() => expect(trainingPreviewsApi.get).toHaveBeenCalled(), { timeout: 2000 });
    const body = vi.mocked(trainingPreviewsApi.create).mock.calls[0][1];
    vi.mocked(trainingPreviewsApi.cancel).mockResolvedValue(outcome(body, "cancelled"));
    await userEvent.setup().click(screen.getByRole("button", { name: "取消预览" }));
    await screen.findByText(/预览已取消/);
    await act(async () => late.resolve(outcome(body)));
    expect(screen.queryByText(/预览已完成/)).not.toBeInTheDocument();
    expect(vi.mocked(trainingPreviewsApi.get).mock.calls[0][2]!.signal!.aborted).toBe(true);
  });
  it("aborts late media on an input switch and offers scoped decode retry", async () => {
    const late = deferred<Blob>();
    vi.mocked(trainingPreviewsApi.image).mockReturnValueOnce(late.promise);
    const view = mount();
    await generate();
    await screen.findByAltText("原始来源 1");
    view.rerender(view.tree({ ...props, binding: "dataset-changed" }));
    await act(async () => late.resolve(new Blob(["old output"])));
    expect(createUrl).toHaveBeenCalledTimes(1);
    vi.mocked(trainingPreviewsApi.image).mockResolvedValue(new Blob(["new output"]));
    await generate();
    fireEvent.error(await screen.findByAltText("原生增强结果 1"));
    await screen.findByText("浏览器无法解码预览图片，请重试");
    await userEvent.setup().click(screen.getByRole("button", { name: "重试图片" }));
    expect(await screen.findByAltText("原生增强结果 1")).toBeVisible();
  });
  it("rejects a response belonging to another frozen request and gates unsupported frameworks", async () => {
    vi.mocked(trainingPreviewsApi.create).mockImplementation(async (_project, body) =>
      outcome({ ...body, dataset_id: "foreign" }),
    );
    const view = mount();
    await generate();
    await screen.findByText("预览输入身份不符，请重新生成");
    expect(createUrl).not.toHaveBeenCalled();
    view.rerender(
      view.tree({ ...props, input: { ...props.input, framework: "libreyolo" } } as typeof props),
    );
    expect(screen.getByText(/LibreYOLO 尚未通过原生增强预览验证/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "生成原生预览" })).not.toBeInTheDocument();
  });
});
