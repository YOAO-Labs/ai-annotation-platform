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
  vi.mocked(trainingDatasetsApi.sample).mockResolvedValue({
    sample: insightSample,
    labels: [insightBox],
    total: 1,
    offset: 0,
    limit: 500,
  });
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
  it("uses authenticated preview bytes and frozen normalized coordinates, toggles overlay and revokes its URL", async () => {
    const user = userEvent.setup();
    const mounted = mount();
    expect(await screen.findByAltText("冻结样本 task-123")).toHaveAttribute(
      "src",
      "blob:owned-preview",
    );
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
    expect(await screen.findByAltText("冻结样本 task-123")).toBeVisible();
    mounted.rerender(mounted.update({ ...insightSample, task_id: "new-task-123" }));
    expect(await screen.findByAltText("冻结样本 new-task")).toBeVisible();
    expect(revokeUrl).toHaveBeenCalledWith("blob:owned-preview");
    expect(trainingDatasetsApi.sample).toHaveBeenLastCalledWith(
      "project-1",
      insightSample.version_id,
      "new-task-123",
      0,
      expect.anything(),
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
    const image = await screen.findByAltText("冻结样本 task-123");
    fireEvent.error(image);
    expect(screen.getByRole("alert")).toHaveTextContent("浏览器无法解码冻结图片预览");
    expect(screen.queryByAltText("冻结样本 task-123")).not.toBeInTheDocument();
    expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledTimes(2);
  });
  it("keeps missing labels unknown, distinguishes confirmed negatives and paginates many frozen boxes", async () => {
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
      sample: { ...insightSample, issue: "missing_snapshot" },
      labels: [],
      total: null,
      offset: 0,
      limit: 500,
    });
    const mounted = mount();
    expect(await screen.findByText(/不能将此样本视为空标注/)).toBeVisible();
    expect(screen.queryByText("此样本已明确确认为无目标。")).not.toBeInTheDocument();
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValueOnce({
      sample: { ...insightSample, explicit_negative: true },
      labels: [],
      total: 0,
      offset: 0,
      limit: 500,
    });
    mounted.rerender(mounted.update({ ...insightSample, task_id: "negative" }));
    expect(await screen.findByText("此样本已明确确认为无目标。")).toBeVisible();
    vi.mocked(trainingDatasetsApi.sample).mockResolvedValue({
      sample: insightSample,
      labels: [insightBox],
      total: 501,
      offset: 0,
      limit: 500,
    });
    mounted.rerender(mounted.update({ ...insightSample, task_id: "many-boxes" }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "下一页标注" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.sample).toHaveBeenLastCalledWith(
        "project-1",
        insightSample.version_id,
        "many-boxes",
        500,
        expect.anything(),
      ),
    );
  });
});
