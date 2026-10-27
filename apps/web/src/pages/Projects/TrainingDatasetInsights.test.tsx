import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import {
  insightVersion,
  insightOverview,
  insightComparison,
  insightClass,
  insightSample,
} from "@/test/trainingDatasetInsightsFixtures";
import { TrainingDatasetInsights } from "./TrainingDatasetInsights";

vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: {
    insights: vi.fn(),
    classes: vi.fn(),
    list: vi.fn(),
    getSummary: vi.fn(),
    compare: vi.fn(),
    samples: vi.fn(),
    get: vi.fn(),
  },
}));
vi.mock("./TrainingDatasetSamplePreview", () => ({
  TrainingDatasetSamplePreview: ({
    sample,
    onClose,
  }: {
    sample: typeof insightSample;
    onClose: () => void;
  }) => (
    <div role="dialog">
      预览版本 {sample.version_id}
      <button onClick={onClose}>关闭预览</button>
    </div>
  ),
}));
vi.mock("./TrainingDatasetSampleComparison", () => ({
  TrainingDatasetSampleComparison: ({ contentSha256 }: { contentSha256: string }) => (
    <div role="dialog">前后版本 {contentSha256}</div>
  ),
}));
const clients: QueryClient[] = [];
function RouteState() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="url">{location.search}</output>
      <button onClick={() => navigate(-1)}>测试返回</button>
      <button onClick={() => navigate(1)}>测试前进</button>
    </>
  );
}
function mount(search = "", props = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const tree = (next = props) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/learning?dataset=dataset-new${search}`]}>
        <RouteState />
        <TrainingDatasetInsights
          projectId="project-1"
          version={insightVersion}
          onClose={vi.fn()}
          {...next}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...render(tree()), update: (next: object) => tree(next) };
}
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
  vi.mocked(trainingDatasetsApi.insights).mockResolvedValue(insightOverview);
  vi.mocked(trainingDatasetsApi.classes).mockResolvedValue({
    items: [insightClass],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
    items: [{ ...insightVersion, id: "dataset-old", name: "上一轮" }],
    total: 21,
  });
  vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue({
    ...insightVersion,
    id: "dataset-old",
    name: "上一轮",
  });
  vi.mocked(trainingDatasetsApi.compare).mockResolvedValue(insightComparison);
  vi.mocked(trainingDatasetsApi.samples).mockResolvedValue({
    source_version_id: insightVersion.id,
    items: [insightSample],
    total: 26,
    offset: 0,
    limit: 20,
  });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
  useAuthStore.getState().logout();
});

describe("frozen training data insights", () => {
  it("keeps source changes separate and sends the selected source filter", async () => {
    const user = userEvent.setup();
    vi.mocked(trainingDatasetsApi.compare).mockResolvedValue({
      ...insightComparison,
      source_comparison_status: "complete",
      counts: { ...insightComparison.counts, source_relation_changed: 1 },
    });
    mount("&data_view=compare&data_baseline=dataset-old");
    const sources = await screen.findByRole("region", { name: "冻结来源比较" });
    expect(within(sources).getByText(/来源变化可以与训练语义未变化同时成立/)).toBeVisible();
    await user.click(within(sources).getByRole("button", { name: "来源关系变化 · 1" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.samples).toHaveBeenCalledWith(
        "project-1",
        "dataset-new",
        expect.objectContaining({
          change: "source_relation_changed",
          baseline_id: "dataset-old",
          offset: 0,
        }),
        expect.anything(),
      ),
    );
    expect(screen.getByTestId("url")).toHaveTextContent("data_change=source_relation_changed");
  });
  it("preserves a requested self baseline instead of displaying another version", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
      items: [insightVersion, { ...insightVersion, id: "dataset-old" }],
      total: 2,
    });
    mount(`&data_view=compare&data_baseline=${insightVersion.id}`);
    const select = await screen.findByLabelText("选择比较基线");
    await waitFor(() => expect(trainingDatasetsApi.list).toHaveBeenCalled());
    expect(select).toHaveValue(insightVersion.id);
    expect(select).toHaveDisplayValue(`${insightVersion.name}（不能作为自身基线）`);
  });
  it("clears the baseline and dependent filters while preserving the selected data version", async () => {
    const user = userEvent.setup();
    mount(
      "&data_view=compare&data_baseline=dataset-old&data_class=0&data_page=2&data_change=removed",
    );
    const clear = await screen.findByRole("button", { name: "清除比较基线" });
    await user.click(clear);
    expect(screen.getByLabelText("选择比较基线")).toHaveValue("");
    const query = new URLSearchParams(screen.getByTestId("url").textContent!);
    expect(query.get("dataset")).toBe("dataset-new");
    expect(query.get("data_baseline")).toBeNull();
    expect(query.get("data_class")).toBeNull();
    expect(query.get("data_page")).toBe("0");
    expect(query.get("data_change")).toBe("all");
  });
  it("retires paired preview state when a view changes so browser history cannot revive it", async () => {
    const user = userEvent.setup();
    mount("&data_view=samples&data_baseline=dataset-old&data_change=labels_changed");
    await user.click(await screen.findByRole("button", { name: "前后对照" }));
    expect(screen.getByRole("dialog")).toHaveTextContent(insightSample.sha256!);
    await user.click(screen.getByRole("button", { name: "版本比较" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "测试返回" }));
    await screen.findByRole("button", { name: "前后对照" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("distinguishes images and instances, keeps missing historical evidence explicit, and loads manifests lazily", async () => {
    const user = userEvent.setup();
    mount();
    expect(await screen.findByText("类别分布")).toBeVisible();
    await user.click(screen.getByText("统计口径与冻结证据"));
    expect(
      screen.getByText("历史排除记录未保留，不能用当前任务状态补算当时的排除原因。"),
    ).toBeVisible();
    const distribution = screen.getByRole("region", { name: "冻结类别分布" });
    await user.click(within(distribution).getByText("完整类别明细"));
    expect(within(distribution).getByRole("columnheader", { name: "训练图片" })).toBeVisible();
    expect(within(distribution).getByRole("columnheader", { name: "训练实例" })).toBeVisible();
    expect(trainingDatasetsApi.get).not.toHaveBeenCalled();
    expect(trainingDatasetsApi.samples).not.toHaveBeenCalled();
  });
  it("drills into class/split filters before server pagination and preserves URL history", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByText("完整类别明细"));
    await user.click(await screen.findByRole("button", { name: "查看 冻结车辆 训练图片" }));
    await screen.findByRole("button", { name: "预览冻结样本" });
    expect(trainingDatasetsApi.samples).toHaveBeenLastCalledWith(
      "project-1",
      insightVersion.id,
      expect.objectContaining({ class_index: 0, split: "train", offset: 0, limit: 20 }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    await user.click(screen.getByRole("button", { name: "下一页样本" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.samples).toHaveBeenLastCalledWith(
        "project-1",
        insightVersion.id,
        expect.objectContaining({ offset: 20, class_index: 0 }),
        expect.anything(),
      ),
    );
    await user.click(screen.getByRole("button", { name: "测试返回" }));
    await waitFor(() => expect(screen.getByTestId("url")).toHaveTextContent("data_page=0"));
  });
  it("restores an older selected baseline independently of its list page and uses baseline media for removals", async () => {
    const user = userEvent.setup();
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [], total: 21 });
    vi.mocked(trainingDatasetsApi.samples).mockResolvedValue({
      source_version_id: "dataset-old",
      items: [{ ...insightSample, version_id: "dataset-old" }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    mount("&data_view=compare&data_baseline=dataset-old");
    expect(await screen.findByText("包含重复次数的评估基准：已变化")).toBeVisible();
    expect(screen.getByLabelText("选择比较基线")).toHaveDisplayValue("上一轮");
    await user.click(screen.getByRole("button", { name: "下一页基线" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        20,
        "ready",
        expect.anything(),
        { task: "detect" },
        20,
      ),
    );
    await user.click(screen.getByRole("button", { name: /移除内容/ }));
    await user.click(await screen.findByRole("button", { name: "预览冻结样本" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("预览版本 dataset-old");
    expect(trainingDatasetsApi.classes).toHaveBeenCalledWith(
      "project-1",
      "dataset-old",
      expect.anything(),
      expect.anything(),
    );
  });
  it("closes preview when filters change and does not reuse stale sample rows", async () => {
    const user = userEvent.setup();
    mount("&data_view=samples");
    await user.click(await screen.findByRole("button", { name: "预览冻结样本" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "关闭预览" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "预览冻结样本" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "预览冻结样本" }));
    vi.mocked(trainingDatasetsApi.samples).mockResolvedValue({
      source_version_id: insightVersion.id,
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });
    await user.selectOptions(screen.getByLabelText("样本类型"), "negative");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText("没有符合当前条件的冻结样本。")).toBeVisible();
  });
  it("displays explicit partial counts, analysis failures and unavailable labels", async () => {
    vi.mocked(trainingDatasetsApi.insights).mockResolvedValue({
      ...insightOverview,
      status: "partial",
      observations: { unknown_labels: 2 },
      dimensions: { unknown: 1 },
    });
    mount();
    expect(await screen.findByText(/标注未知 2 张，尺寸未知 1 张/)).toBeVisible();
    cleanup();
    clients.splice(0).forEach((c) => c.clear());
    vi.mocked(trainingDatasetsApi.insights).mockRejectedValue(new Error("冻结记录缺失"));
    mount();
    expect(await screen.findByText(/训练包下载入口仍独立可用/)).toBeVisible();
  });
  it("does not fetch analysis for unfinished versions or invalid numeric URL filters", async () => {
    mount("", { version: { ...insightVersion, status: "running" } });
    expect(screen.getByText(/版本构建完成后/)).toBeVisible();
    expect(trainingDatasetsApi.insights).not.toHaveBeenCalled();
    cleanup();
    clients.splice(0).forEach((c) => c.clear());
    mount("&data_view=samples&data_page=999999999999999999999&data_class=999999999999999999999");
    expect(screen.getByText(/URL 中的类别或页码无效/)).toBeVisible();
    expect(trainingDatasetsApi.samples).not.toHaveBeenCalled();
    expect(
      vi.mocked(trainingDatasetsApi.classes).mock.calls.every((call) => call[2]?.limit !== 1),
    ).toBe(true);
  });
  it("aborts old version/account requests and discards their late results", async () => {
    let resolve!: (value: typeof insightOverview) => void;
    vi.mocked(trainingDatasetsApi.insights).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const mounted = mount();
    const signal = vi.mocked(trainingDatasetsApi.insights).mock.calls[0][2]!.signal!;
    mounted.rerender(
      mounted.update({ version: { ...insightVersion, id: "dataset-other", name: "新版本" } }),
    );
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolve({ ...insightOverview, observations: { unknown_labels: 999 }, status: "partial" });
    });
    expect(screen.queryByText(/标注未知 999/)).not.toBeInTheDocument();
    const before = vi.mocked(trainingDatasetsApi.insights).mock.calls.length;
    act(() => useAuthStore.getState().setAuth("session-b", { id: "user-b" } as MeResponse));
    await waitFor(() => expect(trainingDatasetsApi.insights).toHaveBeenCalledTimes(before + 1));
    expect(screen.getByRole("heading", { name: "新版本" })).toBeVisible();
  });
  it("clamps a stale sample page after the server collection shrinks", async () => {
    mount("&data_view=samples&data_page=8");
    await waitFor(() =>
      expect(trainingDatasetsApi.samples).toHaveBeenLastCalledWith(
        "project-1",
        insightVersion.id,
        expect.objectContaining({ limit: 20, offset: 20 }),
        expect.anything(),
      ),
    );
    expect(screen.getByTestId("url")).toHaveTextContent("data_page=1");
    expect(screen.getByRole("button", { name: "下一页样本" })).toBeDisabled();
  });
  it("restores sample page size from the URL and resets the real server offset when resizing", async () => {
    const user = userEvent.setup();
    vi.mocked(trainingDatasetsApi.samples).mockResolvedValue({
      source_version_id: insightVersion.id,
      items: [insightSample],
      total: 201,
      offset: 50,
      limit: 50,
    });
    mount("&data_view=samples&data_page=1&data_page_size=50");
    await screen.findByRole("button", { name: "预览冻结样本" });
    expect(trainingDatasetsApi.samples).toHaveBeenLastCalledWith(
      "project-1",
      insightVersion.id,
      expect.objectContaining({ limit: 50, offset: 50 }),
      expect.anything(),
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "每页样本数" }), "100");
    await waitFor(() =>
      expect(trainingDatasetsApi.samples).toHaveBeenLastCalledWith(
        "project-1",
        insightVersion.id,
        expect.objectContaining({ limit: 100, offset: 0 }),
        expect.anything(),
      ),
    );
    expect(screen.getByTestId("url")).toHaveTextContent("data_page_size=100");
    expect(screen.getByTestId("url")).toHaveTextContent("data_page=0");
  });
  it("uses the class page size for both server pagination and URL state", async () => {
    const user = userEvent.setup();
    vi.mocked(trainingDatasetsApi.classes).mockResolvedValue({
      items: [insightClass],
      total: 101,
      offset: 0,
      limit: 20,
    });
    mount();
    await screen.findByRole("combobox", { name: "每页类别数" });
    await user.selectOptions(screen.getByRole("combobox", { name: "每页类别数" }), "50");
    await waitFor(() =>
      expect(trainingDatasetsApi.classes).toHaveBeenLastCalledWith(
        "project-1",
        insightVersion.id,
        expect.objectContaining({ limit: 50, offset: 0 }),
        expect.anything(),
      ),
    );
    await user.click(screen.getByRole("button", { name: "下一页类别" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.classes).toHaveBeenLastCalledWith(
        "project-1",
        insightVersion.id,
        expect.objectContaining({ limit: 50, offset: 50 }),
        expect.anything(),
      ),
    );
  });
});
