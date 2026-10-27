import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { trainingHubApi } from "@/api/trainingHub";
import type { TrainingRunOut } from "@/api/trainingRuns";
import { TrainingPage } from "./TrainingPage";

vi.mock("@/api/trainingHub", () => ({
  trainingHubApi: { catalog: vi.fn(), projects: vi.fn(), runs: vi.fn() },
}));
const clients: QueryClient[] = [];
const catalog = {
  enabled: true,
  online: true,
  busy: false,
  resource_id: "node/GPU-1",
  executor_id: "container",
  last_seen_at: null,
  items: ["ultralytics", "libreyolo"].map((framework) => ({
    framework,
    version: "1.0",
    image_digest: "sha256:fixed",
    compatible: true,
    base_models: [{ id: "base", sha256: "fixed", filename: "base.pt" }],
    capabilities: {
      task_types: ["image-detection"],
      resume: true,
      artifact_download: true,
      model_registration: framework === "ultralytics",
    },
  })),
};
const projects = [
  {
    id: "ready",
    name: "有数据的项目",
    display_id: "P-1",
    data_type: "image",
    status: "in_progress",
    can_prepare_dataset: true,
    preparation_reason: null,
    ready_dataset_count: 1,
    active_run_count: 0,
  },
  {
    id: "prepare",
    name: "待准备项目",
    display_id: "P-2",
    data_type: "image",
    status: "in_progress",
    can_prepare_dataset: true,
    preparation_reason: null,
    ready_dataset_count: 0,
    active_run_count: 0,
  },
];
const run: TrainingRunOut = {
  id: "experiment",
  project_id: "ready",
  name: "缺陷检测 · 基线实验",
  status: "training",
  task: "detect",
  config: { framework: "ultralytics", parameters: { epochs: 20 } },
  metrics: { epoch: 6 },
  created_at: "2026-10-16T02:00:00Z",
  completed_at: null,
  async_job_id: null,
  config_digest: "digest",
  creator_id: null,
  current_attempt_id: null,
  dataset_id: "dataset",
  error_message: null,
};
function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}
function mount(url = "/training") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <TrainingPage />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(trainingHubApi.catalog).mockResolvedValue(catalog as never);
  vi.mocked(trainingHubApi.projects).mockResolvedValue({ total: 2, items: projects } as never);
  vi.mocked(trainingHubApi.runs).mockResolvedValue({
    total: 0,
    items: [],
    counts: { active: 0, queued: 0, running: 0, succeeded: 2, failed: 0, cancelled: 0 },
  });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("training entry", () => {
  it("keeps the readable resource node and copies the complete GPU resource identity", async () => {
    const resourceId = "node-west/GPU-11111111-2222-4333-8444-555555555555";
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({
      ...catalog,
      resource_id: resourceId,
    } as never);
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    mount("/training?tab=warehouses");
    const executor = await screen.findByRole("region", { name: "训练执行器" });
    expect(within(executor).getByText("node-west")).toBeVisible();
    expect(executor).not.toHaveTextContent("GPU-11111111");
    await user.click(within(executor).getByRole("button", { name: "复制训练资源 ID" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(resourceId));
    expect(screen.getByTestId("location")).toHaveTextContent("/training?tab=warehouses");
  });

  it("keeps unconfigured training resources explicit without a copy action", async () => {
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({ ...catalog, resource_id: null } as never);
    mount("/training?tab=warehouses");
    const executor = await screen.findByRole("region", { name: "训练执行器" });
    expect(within(executor).getByText("尚未配置")).toBeVisible();
    expect(
      within(executor).queryByRole("button", { name: "复制训练资源 ID" }),
    ).not.toBeInTheDocument();
  });

  it("shows warehouses and links to projects without starting experiment creation", async () => {
    mount();
    expect(await screen.findByRole("link", { name: "有数据的项目" })).toHaveAttribute(
      "href",
      "/projects/ready/learning?tab=training",
    );
    expect(screen.getByRole("link", { name: "准备数据" })).toHaveAttribute(
      "href",
      "/projects/prepare/learning?tab=data",
    );
    const warehouse = screen.getByRole("heading", { name: "LibreYOLO" }).closest("article")!;
    expect(within(warehouse).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /基础模型/, hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByText("base")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "新建训练" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "进入项目" })[0]).toHaveAttribute(
      "href",
      "/projects/ready/learning?tab=training",
    );
    expect(screen.getByText(/训练仓库共用同一 GPU/)).toBeVisible();
  });
  it("groups model sizes in one row per family and task without losing any model", async () => {
    const models = ["yolov8", "yolo11", "yolo26"].flatMap((family) =>
      ["n", "s", "m", "l", "x"].flatMap((size) =>
        ["detect", "segment"].map((task) => ({
          id: `${family}${size}${task === "segment" ? "-seg" : ""}`,
          filename: `${family}${size}.pt`,
          sha256: "fixed",
          model_family: family,
          model_size: size,
          task,
        })),
      ),
    );
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        {
          ...catalog.items[0],
          base_models: models,
          parameter_schemas: ["yolov8", "yolo11", "yolo26"].flatMap((family) =>
            ["detect", "segment"].map((task) => ({
              schema_id: `${family}:${task}`,
              model_family: family,
              task,
              field_count: 114,
              editable_count: task === "detect" ? 38 : 26,
              compatible: task === "detect",
            })),
          ),
        },
      ],
    } as never);
    mount("/training?tab=warehouses");
    const warehouse = (await screen.findByRole("heading", { name: "Ultralytics" })).closest(
      "article",
    )!;
    expect(within(warehouse).getByText("30 个 · 6 组")).toBeVisible();
    expect(within(warehouse).getAllByRole("list")).toHaveLength(1);
    expect(within(warehouse).queryByText("Ultralytics 技术信息")).not.toBeInTheDocument();
    const list = within(warehouse).getByRole("list", { name: "Ultralytics 基础模型" });
    expect(list).toBeVisible();
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(6);
    for (const row of rows) {
      expect(within(row).getAllByRole("button", { name: /基础权重哈希/ })).toHaveLength(5);
      expect(within(row).getByText(/114 个原生参数/)).toBeVisible();
    }
    expect(rows[0]).toHaveTextContent("YOLOv8图片目标检测");
    expect(rows[1]).toHaveTextContent("YOLOv8图片实例分割");
    expect(within(list).getByLabelText("yolo26x-seg · 权重下载")).toBeVisible();
    expect(within(list).getAllByText("支持模型回流")).toHaveLength(3);
    expect(within(list).getAllByText("权重下载")).toHaveLength(3);
    expect(within(list).getAllByText("已兼容")).toHaveLength(3);
    expect(within(list).getAllByText("执行器需更新")).toHaveLength(3);
  });
  it("keeps per-size registration differences visible within a shared model row", async () => {
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        {
          ...catalog.items[0],
          base_models: [true, false].map((registration, index) => ({
            id: `yolo11${index === 0 ? "n" : "s"}`,
            filename: "model.pt",
            sha256: "fixed",
            model_size: index === 0 ? "n" : "s",
            capabilities: { model_registration: registration },
          })),
        },
      ],
    } as never);
    mount("/training?tab=warehouses");
    const list = await screen.findByRole("list", { name: "Ultralytics 基础模型" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByLabelText("yolo11n · 支持模型回流")).toHaveTextContent("n可回流");
    expect(within(list).getByLabelText("yolo11s · 权重下载")).toHaveTextContent("s下载");
  });
  it("shows an explicit empty model catalog", async () => {
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({
      ...catalog,
      items: [{ ...catalog.items[0], base_models: [] }],
    } as never);
    mount("/training?tab=warehouses");
    expect(await screen.findByText("尚未配置基础模型")).toBeVisible();
    expect(screen.queryByLabelText(/Ultralytics 基础模型/)).not.toBeInTheDocument();
  });
  it("preserves parameter contracts when no base weights are configured", async () => {
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        {
          ...catalog.items[0],
          base_models: [],
          parameter_schemas: [
            {
              schema_id: "schema-only",
              model_family: "yolo11",
              task: "detect",
              field_count: 114,
              editable_count: 38,
            },
          ],
        },
      ],
    } as never);
    mount("/training?tab=warehouses");
    const list = await screen.findByRole("list", { name: "Ultralytics 基础模型" });
    expect(within(list).getByText("尚未配置基础模型")).toBeVisible();
    expect(within(list).getByText(/114 个原生参数 \/ 38 个可编辑/)).toBeVisible();
    expect(within(list).getByText("待执行器确认")).toBeVisible();
    expect(within(list).queryByRole("button")).not.toBeInTheDocument();
  });
  it("separates queue, history and environment details with restorable URL tabs", async () => {
    const user = userEvent.setup();
    mount("/training?tab=queue&state=finished&framework=libreyolo");
    await waitFor(() =>
      expect(trainingHubApi.runs).toHaveBeenCalledWith("finished", 0, "libreyolo", 20),
    );
    expect(screen.queryByRole("heading", { name: "可训练项目" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "训练仓库" }));
    expect(await screen.findByRole("region", { name: "训练执行器" })).toHaveTextContent(
      "container",
    );
    expect(screen.queryByRole("group", { name: "筛选训练状态" })).not.toBeInTheDocument();
  });
  it("shows catalog failures with retry instead of claiming availability", async () => {
    vi.mocked(trainingHubApi.catalog).mockRejectedValue(new Error("无法读取仓库"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取仓库");
    expect(screen.queryByText("共享 GPU 空闲")).not.toBeInTheDocument();
    expect(screen.getByText("执行器状态读取失败")).toBeVisible();
    expect(screen.queryByText("训练未启用")).not.toBeInTheDocument();
  });
  it("returns to a valid page when a polled queue shrinks", async () => {
    let first = true;
    const result = {
      total: 1,
      items: [
        {
          run: {
            id: "run",
            project_id: "ready",
            name: "仍在排队",
            status: "queued",
            config: { framework: "ultralytics", parameters: { epochs: 10 } },
            metrics: {},
          },
          project_name: "有数据的项目",
          project_display_id: "P-1",
        },
      ],
      counts: { active: 1, queued: 1, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
    };
    vi.mocked(trainingHubApi.runs).mockImplementation(async (_state, offset) => {
      if (offset === 0 && first) {
        first = false;
        return { ...result, total: 21 } as never;
      }
      return offset === 20 ? ({ ...result, items: [] } as never) : (result as never);
    });
    const user = userEvent.setup();
    mount("/training?tab=queue");
    await user.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() => expect(trainingHubApi.runs).toHaveBeenCalledWith("active", 20, "", 20));
    await waitFor(() => expect(trainingHubApi.runs).toHaveBeenLastCalledWith("active", 0, "", 20));
    expect(await screen.findByRole("link", { name: /仍在排队/ })).toBeVisible();
  });
  it("shows loading feedback without claiming the queue is empty", async () => {
    vi.mocked(trainingHubApi.runs).mockReturnValue(new Promise(() => {}));
    mount("/training?tab=queue");
    expect(
      await within(screen.getByRole("tabpanel", { name: "队列与历史" })).findByText(
        "正在加载训练队列…",
      ),
    ).toHaveTextContent("正在加载训练队列");
    expect(screen.queryByText("当前没有进行中的训练")).not.toBeInTheDocument();
  });
  it("retries a failed queue request and restores experiment links and real progress", async () => {
    vi.mocked(trainingHubApi.runs)
      .mockRejectedValueOnce(new Error("训练队列读取失败"))
      .mockResolvedValue({
        total: 1,
        items: [{ run, project_name: "有数据的项目", project_display_id: "P-1" }],
        counts: { active: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
      });
    const user = userEvent.setup();
    mount("/training?tab=queue");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("训练队列读取失败");
    await user.click(within(alert).getByRole("button", { name: "重试" }));
    const link = await screen.findByRole("link", { name: /缺陷检测 · 基线实验/ });
    expect(link).toHaveAttribute("href", "/projects/ready/learning?tab=training&run=experiment");
    expect(within(link).getByText("图片目标检测")).toBeVisible();
    expect(within(link).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "6");
    expect(within(link).getByRole("progressbar")).toHaveAttribute("aria-valuemax", "20");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
  it("shows queued scheduling instead of a zero progress bar and respects the frozen task", async () => {
    vi.mocked(trainingHubApi.runs).mockResolvedValue({
      total: 1,
      items: [
        {
          run: { ...run, task: "segment", status: "queued", metrics: {} },
          project_name: "有数据的项目",
          project_display_id: "P-1",
        },
      ],
      counts: { active: 1, queued: 1, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
    });
    mount("/training?tab=queue");
    const link = await screen.findByRole("link", { name: /缺陷检测 · 基线实验/ });
    expect(within(link).getByText("图片实例分割")).toBeVisible();
    expect(within(link).getByText("等待执行器调度")).toBeVisible();
    expect(within(link).queryByRole("progressbar")).not.toBeInTheDocument();
  });
  it("resets both queue filters and keeps the selected view in the URL", async () => {
    const user = userEvent.setup();
    mount("/training?tab=queue&state=failed&framework=libreyolo");
    await user.click(await screen.findByRole("button", { name: "重置筛选" }));
    await waitFor(() => expect(trainingHubApi.runs).toHaveBeenLastCalledWith("all", 0, "", 20));
    expect(screen.getByRole("combobox", { name: "筛选训练器" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "全部实验" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByTestId("location")).toHaveTextContent("/training?tab=queue&state=all");
  });
  it("starts queue pagination over after changing the trainer", async () => {
    vi.mocked(trainingHubApi.runs).mockResolvedValue({
      total: 21,
      items: [{ run, project_name: "有数据的项目", project_display_id: "P-1" }],
      counts: { active: 21, queued: 20, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    });
    const user = userEvent.setup();
    mount("/training?tab=queue");
    await user.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() => expect(trainingHubApi.runs).toHaveBeenLastCalledWith("active", 20, "", 20));
    await user.selectOptions(screen.getByRole("combobox", { name: "筛选训练器" }), "libreyolo");
    await waitFor(() =>
      expect(trainingHubApi.runs).toHaveBeenLastCalledWith("active", 0, "libreyolo", 20),
    );
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });
  it("clears an unsuccessful project search without leaving the training entry", async () => {
    vi.mocked(trainingHubApi.projects).mockImplementation(
      async (search) => ({ total: search ? 0 : 2, items: search ? [] : projects }) as never,
    );
    const user = userEvent.setup();
    mount();
    await user.type(screen.getByRole("textbox", { name: "搜索训练项目" }), "不存在");
    await user.click(await screen.findByRole("button", { name: "清除搜索" }));
    expect(screen.getByRole("textbox", { name: "搜索训练项目" })).toHaveValue("");
    expect(await screen.findByRole("link", { name: "有数据的项目" })).toBeVisible();
  });
  it("copies image and base-weight hashes directly from the unified warehouse catalog", async () => {
    vi.mocked(trainingHubApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        {
          ...catalog.items[0],
          base_models: ["n", "s"].map((size) => ({
            id: `yolo11${size}`,
            model_size: size,
            filename: `yolo11${size}.pt`,
            sha256: `sha256:weight-${size}`,
          })),
        },
      ],
    } as never);
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    mount("/training?tab=warehouses");
    const heading = await screen.findByRole("heading", { name: "Ultralytics" });
    const warehouse = heading.closest("article")!;
    const copyHash = within(warehouse).getByRole("button", {
      name: "复制容器镜像摘要",
    });
    expect(copyHash).toBeVisible();
    expect(within(warehouse).queryByText("sha256:fixed")).not.toBeInTheDocument();
    await user.click(copyHash);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("sha256:fixed"));
    const model = within(warehouse).getByRole("button", { name: "复制 yolo11s 基础权重哈希" });
    act(() => model.focus());
    expect(model).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("sha256:weight-s"));
    await user.click(within(warehouse).getByRole("button", { name: "复制 yolo11n 基础权重哈希" }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("sha256:weight-n"));
  });
  it("uses the selected row count for server offsets and starts at page one after a size change", async () => {
    vi.mocked(trainingHubApi.projects).mockResolvedValue({ items: projects, total: 65 } as never);
    const user = userEvent.setup();
    mount();
    const size = await screen.findByRole("combobox", { name: "每页项目数" });
    await waitFor(() => expect(size).toBeEnabled());
    await user.selectOptions(size, "10");
    await waitFor(() => expect(trainingHubApi.projects).toHaveBeenLastCalledWith("", 0, 10));
    await waitFor(() => expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(trainingHubApi.projects).toHaveBeenLastCalledWith("", 10, 10));
    await waitFor(() => expect(size).toBeEnabled());
    await user.selectOptions(size, "50");
    await waitFor(() => expect(trainingHubApi.projects).toHaveBeenLastCalledWith("", 0, 50));
    expect(
      within(screen.getByRole("navigation", { name: "项目分页" })).getByRole("status"),
    ).toHaveTextContent("第 1 / 2 页");
  });
});
