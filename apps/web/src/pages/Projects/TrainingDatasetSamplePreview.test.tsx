import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { insightSample, insightBox } from "@/test/trainingDatasetInsightsFixtures";
import { TrainingDatasetSamplePreview } from "./TrainingDatasetSamplePreview";

vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: { sample: vi.fn(), sampleMedia: vi.fn() },
}));
vi.mock("./FrozenInstanceMaskPreview", () => ({
  FrozenInstanceMaskPreview: () => <div role="img" aria-label="冻结实例掩码像素预览" />,
}));
const clients: QueryClient[] = [];
const observed = vi.fn();
const close = vi.fn();
const createUrl = vi.fn(() => "blob:owned-preview");
const revokeUrl = vi.fn();
function mount(sample = insightSample) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const tree = (next = sample) => (
    <QueryClientProvider client={client}>
      <TrainingDatasetSamplePreview
        projectId="project-1"
        sample={next}
        onClose={close}
        onObserved={observed}
      />
    </QueryClientProvider>
  );
  return { ...render(tree()), update: tree };
}
beforeEach(() => {
  vi.resetAllMocks();
  createUrl.mockReturnValue("blob:owned-preview");
  vi.stubGlobal(
    "URL",
    Object.assign(globalThis.URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl }),
  );
  useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
  vi.mocked(trainingDatasetsApi.sample).mockImplementation(
    async (_projectId, versionId, taskId) => ({
      sample: { ...insightSample, version_id: versionId, task_id: taskId },
      labels: [insightBox],
      total: 1,
      offset: 0,
      limit: 20,
    }),
  );
  vi.mocked(trainingDatasetsApi.sampleMedia).mockResolvedValue(
    new Blob(["verified-preview"], { type: "image/png" }),
  );
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
  useAuthStore.getState().logout();
  vi.unstubAllGlobals();
});

describe("immutable sample preview", () => {
  it("shows native polygon vertices, area and source version without deriving a rectangle", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
      sample: insightSample,
      labels: [
        {
          type: "polygon",
          class_index: 0,
          class_name: "冻结车辆",
          points: [
            [0.1, 0.2],
            [0.8, 0.2],
            [0.4, 0.7],
          ],
          area: 0.175,
          annotation_id: "annotation-123",
          annotation_version: 7,
        },
      ],
      total: 1,
      offset: 0,
      limit: 20,
    });
    mount();
    await screen.findByAltText("冻结样本图片");
    const overlay = await screen.findByRole("img", { name: "冻结多边形" });
    expect(overlay.querySelector("polygon")).toHaveAttribute("points", "0.1,0.2 0.8,0.2 0.4,0.7");
    expect(overlay.querySelector("rect")).toBeNull();
    expect(screen.getByText("3 个顶点")).toBeVisible();
    expect(screen.getByRole("button", { name: "复制冻结标注 ID" })).toBeVisible();
    expect(screen.getByText(/17\.50%/)).toHaveTextContent("标注版本 7");
    await user.click(screen.getByRole("button", { name: "复制冻结标注 ID" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("annotation-123"));
    await user.click(screen.getByLabelText("显示本页冻结多边形"));
    expect(screen.queryByRole("img", { name: "冻结多边形" })).not.toBeInTheDocument();
  });
  it("uses authenticated preview bytes and frozen normalized coordinates, toggles overlay and revokes its URL", async () => {
    const user = userEvent.setup();
    const mounted = mount();
    expect(await screen.findByAltText("冻结样本图片")).toHaveAttribute("src", "blob:owned-preview");
    expect(await screen.findByText("冻结车辆")).toBeVisible();
    const rectangle = screen.getByRole("img", { name: "冻结矩形框" }).querySelector("rect")!;
    expect(Number(rectangle.getAttribute("x"))).toBeCloseTo(0.4);
    expect(Number(rectangle.getAttribute("y"))).toBeCloseTo(0.2);
    expect(rectangle).toHaveAttribute("width", "0.2");
    await user.click(screen.getByLabelText("显示本页冻结框"));
    expect(screen.queryByRole("img", { name: "冻结矩形框" })).not.toBeInTheDocument();
    expect(observed).toHaveBeenCalledOnce();
    const signal = vi.mocked(trainingDatasetsApi.sampleMedia).mock.calls[0][3]!.signal!;
    mounted.unmount();
    expect(signal.aborted).toBe(true);
    expect(revokeUrl).toHaveBeenCalledWith("blob:owned-preview");
  });
  it("aborts a closed preview and ignores late media without creating an object URL", async () => {
    let resolve!: (blob: Blob) => void;
    vi.mocked(trainingDatasetsApi.sampleMedia).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const mounted = mount();
    const signal = vi.mocked(trainingDatasetsApi.sampleMedia).mock.calls[0][3]!.signal!;
    mounted.unmount();
    await act(async () => resolve(new Blob(["late"])));
    expect(signal.aborted).toBe(true);
    expect(createUrl).not.toHaveBeenCalled();
    expect(observed).not.toHaveBeenCalled();
  });
  it("rejects a frozen detail response from another member instead of displaying its labels", async () => {
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
      sample: { ...insightSample, task_id: "foreign-member" },
      labels: [{ ...insightBox, class_name: "其他成员类别" }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("冻结标注响应身份与所选成员不一致");
    expect(screen.queryByText("其他成员类别")).not.toBeInTheDocument();
  });
  it("retires old detail and media after batched A to B to A session changes", async () => {
    let resolveMedia!: (blob: Blob) => void;
    let resolveDetail!: (value: Awaited<ReturnType<typeof trainingDatasetsApi.sample>>) => void;
    vi.mocked(trainingDatasetsApi.sampleMedia).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveMedia = resolve;
        }),
    );
    vi.mocked(trainingDatasetsApi.sample).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveDetail = resolve;
        }),
    );
    mount();
    const retiredMediaSignal = vi.mocked(trainingDatasetsApi.sampleMedia).mock.calls[0][3]!.signal!;
    const retiredDetailSignal = vi.mocked(trainingDatasetsApi.sample).mock.calls[0][4]!.signal!;
    act(() => {
      useAuthStore.getState().setAuth("session-b", { id: "user-b" } as MeResponse);
      useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
    });
    expect(await screen.findByText("冻结车辆")).toBeVisible();
    expect(await screen.findByAltText("冻结样本图片")).toBeVisible();
    expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledTimes(2);
    await act(async () => {
      resolveMedia(new Blob(["retired A session"]));
      resolveDetail({
        sample: insightSample,
        labels: [{ ...insightBox, class_name: "退役会话类别" }],
        total: 1,
        offset: 0,
        limit: 20,
      });
    });
    expect(retiredMediaSignal.aborted).toBe(true);
    expect(retiredDetailSignal.aborted).toBe(true);
    expect(createUrl).toHaveBeenCalledTimes(1);
    expect(observed).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("退役会话类别")).not.toBeInTheDocument();
  });
  it("resets labels/media on sample and session switches, including another tab's credential change", async () => {
    let resolve!: (blob: Blob) => void;
    vi.mocked(trainingDatasetsApi.sampleMedia).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const mounted = mount();
    localStorage.setItem("token", "other-tab-token");
    await act(async () => resolve(new Blob(["old account"])));
    expect(createUrl).not.toHaveBeenCalled();
    act(() => useAuthStore.getState().setAuth("session-b", { id: "user-b" } as MeResponse));
    expect(await screen.findByAltText("冻结样本图片")).toBeVisible();
    mounted.rerender(mounted.update({ ...insightSample, task_id: "new-task-123" }));
    expect(await screen.findByAltText("冻结样本图片")).toBeVisible();
    expect(revokeUrl).toHaveBeenCalledWith("blob:owned-preview");
    expect(trainingDatasetsApi.sample).toHaveBeenLastCalledWith(
      "project-1",
      insightSample.version_id,
      "new-task-123",
      0,
      expect.anything(),
      20,
    );
  });
  it("shows storage/decode failures and retries without substituting source media", async () => {
    const user = userEvent.setup();
    vi.mocked(trainingDatasetsApi.sampleMedia).mockRejectedValueOnce(
      new Error("冻结媒体摘要不匹配"),
    );
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("冻结媒体摘要不匹配");
    expect(screen.queryByRole("img", { name: /冻结样本/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试冻结图片" }));
    const image = await screen.findByAltText("冻结样本图片");
    fireEvent.error(image);
    expect(screen.getByRole("alert")).toHaveTextContent("浏览器无法解码冻结图片预览");
    expect(screen.queryByAltText("冻结样本图片")).not.toBeInTheDocument();
    expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledTimes(2);
  });
  it("keeps missing labels unknown, distinguishes confirmed negatives and paginates many frozen boxes", async () => {
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
      sample: { ...insightSample, issue: "missing_snapshot" },
      labels: [],
      total: null,
      offset: 0,
      limit: 20,
    });
    const mounted = mount();
    expect(await screen.findByText(/不能将此样本视为空标注/)).toBeVisible();
    expect(screen.queryByText("此样本已明确确认为无目标。")).not.toBeInTheDocument();
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
      sample: { ...insightSample, task_id: "negative", explicit_negative: true },
      labels: [],
      total: 0,
      offset: 0,
      limit: 20,
    });
    mounted.rerender(mounted.update({ ...insightSample, task_id: "negative" }));
    expect(await screen.findByText("此样本已明确确认为无目标。")).toBeVisible();
    vi.mocked(trainingDatasetsApi.sample).mockImplementation(
      async (_projectId, versionId, taskId) => ({
        sample: { ...insightSample, version_id: versionId, task_id: taskId },
        labels: [insightBox],
        total: 501,
        offset: 0,
        limit: 20,
      }),
    );
    mounted.rerender(mounted.update({ ...insightSample, task_id: "many-boxes" }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "下一页标注" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.sample).toHaveBeenLastCalledWith(
        "project-1",
        insightSample.version_id,
        "many-boxes",
        20,
        expect.anything(),
        20,
      ),
    );
  });
  it("paginates labels and current-page overlays with the selected real limit without reloading media", async () => {
    const user = userEvent.setup();
    vi.mocked(trainingDatasetsApi.sample).mockImplementation(
      async (_project, versionId, taskId, offset = 0, _options, limit = 20) => ({
        sample: { ...insightSample, version_id: versionId, task_id: taskId },
        labels: Array.from({ length: Math.min(limit, 101 - offset) }, (_, index) => ({
          ...insightBox,
          class_name: `类别 ${offset + index}`,
        })),
        total: 101,
        offset,
        limit,
      }),
    );
    mount();
    await screen.findByText("类别 19");
    expect(screen.queryByText("类别 20")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页标注" }));
    await screen.findByText("类别 20");
    await user.selectOptions(screen.getByRole("combobox", { name: "每页冻结标注数" }), "50");
    await screen.findByText("类别 49");
    expect(trainingDatasetsApi.sample).toHaveBeenLastCalledWith(
      "project-1",
      insightSample.version_id,
      insightSample.task_id,
      0,
      expect.anything(),
      50,
    );
    expect(screen.getByRole("button", { name: "上一页标注" })).toBeDisabled();
    expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "冻结矩形框" }).querySelectorAll("rect")).toHaveLength(
      50,
    );
  });
});

it("shows frozen instance mask metadata and never derives a polygon or box overlay", async () => {
  vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
    sample: insightSample,
    labels: [
      {
        type: "instance_mask",
        annotation_id: "frozen-annotation",
        annotation_version: 4,
        class_index: 0,
        class_name: "冻结掩码车辆",
        area: 0.125,
        area_pixels: 100,
        bbox_xyxy: [10, 20, 30, 40],
        pixel_sha256: "a".repeat(64),
        size: [80, 100],
      },
    ],
    total: 1,
    offset: 0,
    limit: 20,
  });
  const user = userEvent.setup();
  mount();
  expect(await screen.findByRole("img", { name: "冻结实例掩码像素预览" })).toBeVisible();
  expect(screen.getByText("100 px · [10, 20, 30, 40]")).toBeVisible();
  expect(screen.getByText(/12.50%/)).toHaveTextContent("标注版本 4");
  expect(screen.getByRole("button", { name: "复制冻结掩码像素摘要" })).toBeVisible();
  expect(screen.queryByRole("img", { name: "冻结矩形框" })).not.toBeInTheDocument();
  await user.click(screen.getByLabelText("显示本页冻结实例掩码"));
  expect(screen.queryByRole("img", { name: "冻结实例掩码像素预览" })).not.toBeInTheDocument();
});
