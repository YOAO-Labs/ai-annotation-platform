import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { batchesApi } from "@/api/batches";
import { tasksApi } from "@/api/tasks";
import { asyncJobsApi } from "@/api/asyncJobs";
import { ProjectLearningPage } from "./ProjectLearningPage";

vi.mock("./TrainingDatasetInsights", () => ({
  TrainingDatasetInsights: ({ version }: { version: { name: string } }) => (
    <p>冻结详情 {version.name}</p>
  ),
}));

vi.mock("./ProjectTrainingPanel", () => ({
  ProjectTrainingPanel: ({
    onAttemptSelect,
  }: {
    onAttemptSelect: (id: string, runId: string) => void;
  }) => (
    <button onClick={() => onAttemptSelect("historical-attempt", "source-run")}>
      测试历史执行
    </button>
  ),
}));
vi.mock("./ProjectModelsPanel", () => ({ ProjectModelsPanel: () => null }));
vi.mock("./ProjectLearningPolicyPanel", () => ({ ProjectLearningPolicyPanel: () => null }));

vi.mock("@/hooks/useProjects", () => ({
  useProject: () => ({ data: { name: "路口车辆", data_type: "image" } }),
}));
vi.mock("@/api/batches", () => ({ batchesApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/api/asyncJobs", () => ({ asyncJobsApi: { cancel: vi.fn() } }));
vi.mock("@/api/tasks", () => ({
  tasksApi: { listByProject: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}));
vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: {
    list: vi.fn(),
    get: vi.fn(),
    getSummary: vi.fn(),
    preflight: vi.fn(),
    create: vi.fn(),
    retry: vi.fn(),
    download: vi.fn(),
    remove: vi.fn(),
  },
}));

const version = {
  id: "dataset-1",
  project_id: "project-1",
  name: "第一轮",
  status: "ready",
  progress_pct: 100,
  created_at: "2026-10-14T12:00:00Z",
  manifest: {},
  snapshot_digest: "a".repeat(64),
  error_message: null,
};
const report = {
  ready: true,
  digest: "b".repeat(64),
  classes: ["car"],
  summary: { positives: 3, negatives: 0, excluded: 0 },
  issues: [],
  excluded: [],
};
const clients: QueryClient[] = [];

function Location() {
  const location = useLocation();
  return (
    <p>
      {location.pathname}
      {location.search}
    </p>
  );
}
function mount(path = "/projects/project-1/learning?tab=data") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/projects/:id/learning"
            element={
              <>
                <ProjectLearningPage />
                <Location />
              </>
            }
          />
          <Route path="/projects/:id/learning/new" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(batchesApi.list).mockResolvedValue([]);
  vi.mocked(tasksApi.listByProject).mockResolvedValue({ items: [], total: 0 } as never);
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(trainingDatasetsApi.get).mockResolvedValue(version as never);
  vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue(version as never);
  vi.mocked(trainingDatasetsApi.preflight).mockResolvedValue(report);
  vi.mocked(trainingDatasetsApi.create).mockResolvedValue(version as never);
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

describe("project learning data versions", () => {
  it("redirects legacy creation links with controlled framework and return experiment context", async () => {
    mount("/projects/project-1/learning?tab=training&new=1&framework=libreyolo&run=run-1");
    expect(
      await screen.findByText(
        "/projects/project-1/learning/new?framework=libreyolo&return_run=run-1",
      ),
    ).toBeVisible();
    expect(trainingDatasetsApi.list).not.toHaveBeenCalled();
  });
  it("reuses the creation request after a lost response and identical recheck", async () => {
    vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
    const user = userEvent.setup();
    mount();
    await user.type(screen.getByLabelText("版本名称"), "车辆首轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await screen.findByText("连接中断");
    const first = vi.mocked(trainingDatasetsApi.create).mock.calls[0][1];
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingDatasetsApi.create).mock.calls[1][1]).toEqual(first);
  });
  it("requires a successful current preflight and a name before creating", async () => {
    const user = userEvent.setup();
    mount();
    expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
    await user.type(screen.getByLabelText("版本名称"), "车辆首轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.create).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          name: "车辆首轮",
          preflight_digest: report.digest,
          validation_fraction: 0.2,
          seed: 42,
          mode: "reviewed",
        }),
      ),
    );
    await waitFor(() => expect(screen.getByLabelText("版本名称")).toHaveValue(""));
  });

  it("invalidates the previous preflight when split settings change", async () => {
    const user = userEvent.setup();
    mount();
    await user.type(screen.getByLabelText("版本名称"), "第二轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.clear(screen.getByLabelText("划分随机种子"));
    await user.type(screen.getByLabelText("划分随机种子"), "99");
    expect(screen.queryByText("数据检查通过")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  });

  it("keeps explicit manager confirmation disabled without selected tasks", async () => {
    const user = userEvent.setup();
    mount();
    await user.selectOptions(screen.getByLabelText("标注纳入方式"), "manager_confirmed");
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    expect(await screen.findByText("已选择 0 个任务")).toBeVisible();
  });

  it("shows preflight failures and allows rechecking", async () => {
    vi.mocked(trainingDatasetsApi.preflight).mockRejectedValueOnce(new Error("素材不可用"));
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("素材不可用");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
  });

  it("restores a selected version outside the current list page without loading its heavy manifest", async () => {
    mount(
      "/projects/project-1/learning?tab=data&dataset=dataset-1&data_view=compare&data_baseline=older",
    );
    expect(await screen.findByText("冻结详情 第一轮")).toBeVisible();
    expect(trainingDatasetsApi.getSummary).toHaveBeenCalledWith(
      "project-1",
      "dataset-1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(trainingDatasetsApi.get).not.toHaveBeenCalled();
  });

  it("downloads through the authorized version endpoint", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [version as never], total: 1 });
    vi.mocked(trainingDatasetsApi.download).mockResolvedValue({
      url: "/minio/training/package.zip",
      sha256: "checksum",
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "下载训练包" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.download).toHaveBeenCalledWith("project-1", "dataset-1"),
    );
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
  });

  it("cancels the current build through its associated job", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
      items: [{ ...version, status: "running", async_job_id: "job-1" } as never],
      total: 1,
    });
    vi.mocked(asyncJobsApi.cancel).mockResolvedValue({ status: "cancelled", id: "job-1" });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "取消构建" }));
    await waitFor(() => expect(asyncJobsApi.cancel).toHaveBeenCalledWith("job-1"));
  });
  it("pins the owning run when selecting an attempt from the default experiment", async () => {
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=training");
    await user.click(await screen.findByRole("button", { name: "测试历史执行" }));
    expect(
      screen.getByText(/tab=training&attempt=historical-attempt&run=source-run/),
    ).toBeInTheDocument();
  });
});
