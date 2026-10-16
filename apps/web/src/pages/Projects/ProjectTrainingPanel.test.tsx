import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { trainingRunsApi } from "@/api/trainingRuns";
import { ProjectTrainingPanel } from "./ProjectTrainingPanel";
import { trainingObservabilityApi } from "@/api/trainingObservability";
import { useAuthStore } from "@/stores/authStore";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { act } from "@testing-library/react";

vi.mock("@/api/trainingObservability", () => ({
  trainingObservabilityApi: { telemetry: vi.fn(), logs: vi.fn(), recovery: vi.fn() },
}));
vi.mock("@/components/ui/decisionDialog", () => ({ confirmDialog: vi.fn() }));

vi.mock("@/api/trainingRuns", () => ({
  trainingRunsApi: {
    catalog: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    events: vi.fn(),
    preflight: vi.fn(),
    create: vi.fn(),
    cancel: vi.fn(),
    resume: vi.fn(),
    remove: vi.fn(),
    download: vi.fn(),
  },
}));
const clients: QueryClient[] = [];
const run = {
  id: "run",
  project_id: "project",
  name: "车辆训练",
  status: "training",
  metrics: { epoch: 2 },
  config: { framework: "ultralytics", parameters: { epochs: 10 } },
  created_at: "2026-10-14T12:00:00Z",
  current_attempt_id: "attempt",
};
function Location() {
  return <p>{useLocation().pathname}</p>;
}
function mount(initialAttemptId = "") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProjectTrainingPanel
          projectId="project"
          initialRunId={initialAttemptId ? "run" : ""}
          initialAttemptId={initialAttemptId}
        />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("token-a", { id: "owner" } as never);
  const page = {
    attempt_id: "attempt",
    stream_id: null,
    earliest_cursor: null,
    latest_cursor: 0,
    next_cursor: 0,
    has_more: false,
    gaps: [],
    summary: null,
    items: [],
    metric_definitions: {},
  };
  vi.mocked(trainingObservabilityApi.telemetry).mockResolvedValue(page);
  vi.mocked(trainingObservabilityApi.logs).mockResolvedValue(page);
  vi.mocked(trainingObservabilityApi.recovery).mockResolvedValue({
    attempt_id: "attempt",
    eligible: false,
    reasons: ["unfinished_checkpoint_missing"],
    executor_online: true,
    runtime_compatible: true,
    checkpoint_epoch: null,
  });
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    enabled: true,
    online: true,
    busy: false,
    resource_id: "gpu",
    items: [
      {
        framework: "ultralytics",
        version: "8.4.177",
        image_digest: "sha256:fixed",
        base_models: [{ id: "yolo11n", sha256: "base", filename: "yolo11n.pt" }],
        capabilities: {
          tasks: ["detect"],
          resume: true,
          artifact_download: true,
          model_registration: true,
        },
      },
    ],
  });
  vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(trainingRunsApi.preflight).mockResolvedValue({ config: {}, config_digest: "frozen" });
  vi.mocked(trainingRunsApi.create).mockResolvedValue(run as never);
  vi.mocked(trainingRunsApi.get).mockResolvedValue({ run: run as never, attempts: [] });
  vi.mocked(trainingRunsApi.events).mockResolvedValue({ attempt_id: "attempt", items: [] });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useAuthStore.getState().logout();
  localStorage.clear();
});
describe("managed project training", () => {
  it("starts with metrics and keeps execution resources accessible in their own view", async () => {
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [run as never], total: 1 });
    const user = userEvent.setup();
    mount();
    expect(await screen.findByRole("tab", { name: "指标曲线" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByRole("region", { name: "所选执行资源与耗时" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "资源与耗时" }));
    expect(await screen.findByRole("region", { name: "所选执行资源与耗时" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "指标曲线" }));
    expect(screen.queryByRole("region", { name: "所选执行资源与耗时" })).not.toBeInTheDocument();
    expect(trainingRunsApi.cancel).not.toHaveBeenCalled();
  });
  it("filters only the current experiment page and selects the requested run without a write", async () => {
    const other = { ...run, id: "other", name: "行人分割实验", task: "segment" };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [run, other] as never, total: 2 });
    vi.mocked(trainingRunsApi.get).mockImplementation(
      async (_project, id) => ({ run: id === "other" ? other : run, attempts: [] }) as never,
    );
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByRole("textbox", { name: "搜索本页实验" }), "分割");
    const list = screen.getByRole("complementary", { name: "实验列表" });
    expect(within(list).queryByRole("button", { name: "车辆训练" })).not.toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "行人分割实验" }));
    expect(await screen.findByRole("heading", { name: "行人分割实验" })).toBeVisible();
    expect(within(list).getByRole("button", { name: "行人分割实验" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(trainingRunsApi.get).toHaveBeenLastCalledWith(
      "project",
      "other",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(trainingRunsApi.create).not.toHaveBeenCalled();
  });
  it("keeps an experiment request in loading feedback without announcing an empty project", async () => {
    vi.mocked(trainingRunsApi.list).mockReturnValue(new Promise(() => {}));
    mount();
    expect(await screen.findByText("正在加载训练实验…")).toBeInTheDocument();
    expect(screen.queryByText("创建你的第一个训练实验")).not.toBeInTheDocument();
  });
  it("opens the independent project creation page", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "新建实验" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "新建实验" }));
    expect(screen.getByText("/projects/project/learning/new")).toBeVisible();
  });
  it("requests cancellation without displaying terminal cancellation early", async () => {
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [run as never], total: 1 });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "停止训练" }));
    await waitFor(() => expect(trainingRunsApi.cancel).toHaveBeenCalledWith("project", "run"));
    expect(screen.queryByText("已取消")).not.toBeInTheDocument();
  });
  it("offers resume only for the current attempt with an incomplete checkpoint", async () => {
    vi.mocked(trainingObservabilityApi.recovery).mockResolvedValue({
      attempt_id: "attempt",
      eligible: true,
      reasons: [],
      executor_online: false,
      runtime_compatible: true,
      checkpoint_epoch: 2,
    });
    const failed = { ...run, status: "failed" };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [failed as never], total: 1 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: failed as never,
      attempts: [
        {
          id: "attempt",
          ordinal: 1,
          status: "failed",
          artifacts: { "checkpoint-0002.pt": { role: "checkpoint", epoch: 2 } },
        } as never,
      ],
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "车辆训练" }));
    await user.click(await screen.findByRole("button", { name: "从检查点续训" }));
    await waitFor(() => expect(trainingRunsApi.resume).toHaveBeenCalledWith("project", "run"));
  });

  it("ignores a delayed delete confirmation after same-user token replacement without a rerender", async () => {
    const failed = { ...run, status: "failed" };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [failed as never], total: 1 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({ run: failed as never, attempts: [] });
    let confirm!: (value: boolean) => void;
    vi.mocked(confirmDialog).mockImplementation(
      () =>
        new Promise((resolve) => {
          confirm = resolve;
        }),
    );
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("tab", { name: "产物与记录" }));
    await user.click(screen.getByRole("button", { name: "删除实验" }));
    const invalidation = vi.spyOn(clients[0], "invalidateQueries");
    useAuthStore.getState().setToken("token-b");
    await act(async () => confirm(true));
    expect(trainingRunsApi.remove).not.toHaveBeenCalled();
    expect(invalidation).not.toHaveBeenCalled();
  });

  it("ignores delayed log download when another tab replaces shared credentials first", async () => {
    const failed = { ...run, status: "failed" };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [failed as never], total: 1 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: failed as never,
      attempts: [
        {
          id: "attempt",
          ordinal: 1,
          status: "failed",
          artifacts: { "train.log": { role: "log" } },
        } as never,
      ],
    });
    let resolve!: (value: { url: string; sha256: string; size_bytes: number }) => void;
    vi.mocked(trainingRunsApi.download).mockImplementation(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("tab", { name: "产物与记录" }));
    await user.click(screen.getByRole("button", { name: /下载脱敏训练日志/ }));
    localStorage.setItem("token", "another-tab-token");
    await act(async () =>
      resolve({ url: "https://storage/redacted", sha256: "safe", size_bytes: 4 }),
    );
    expect(click).not.toHaveBeenCalled();
    click.mockRestore();
  });

  it("rejects recovery eligibility for a different current attempt and explains OOM without changing parameters", async () => {
    const failed = { ...run, status: "failed", config: { ...run.config, configuration: {} } };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [failed as never], total: 1 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: failed as never,
      attempts: [
        {
          id: "attempt",
          ordinal: 1,
          status: "failed",
          artifacts: {},
          failure: { code: "cuda_oom", diagnostic: "safe" },
        } as never,
      ],
    });
    vi.mocked(trainingObservabilityApi.recovery).mockResolvedValue({
      attempt_id: "old",
      eligible: true,
      reasons: [],
      executor_online: true,
      runtime_compatible: true,
      checkpoint_epoch: 2,
    });
    mount();
    const link = await screen.findByRole("link", { name: /检查 batch \/ imgsz/ });
    expect(link).toHaveAttribute(
      "href",
      "/projects/project/learning/new?clone_run=run&clone_attempt=attempt&return_run=run",
    );
    expect(screen.queryByRole("button", { name: "从检查点续训" })).not.toBeInTheDocument();
    expect(trainingRunsApi.resume).not.toHaveBeenCalled();
  });

  it("rejects a delete confirmation after switching attempts away and back", async () => {
    const failed = { ...run, status: "failed" };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [failed as never], total: 1 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: failed as never,
      attempts: [
        { id: "attempt", ordinal: 2, status: "failed", artifacts: {} } as never,
        { id: "old", ordinal: 1, status: "failed", artifacts: {} } as never,
      ],
    });
    vi.mocked(trainingRunsApi.events).mockImplementation(async (_p, _r, attempt) => ({
      attempt_id: attempt!,
      items: [],
    }));
    vi.mocked(trainingObservabilityApi.telemetry).mockImplementation(async (_p, _r, attempt) => ({
      attempt_id: attempt,
      stream_id: null,
      earliest_cursor: null,
      latest_cursor: 0,
      next_cursor: 0,
      has_more: false,
      gaps: [],
      summary: null,
      items: [],
      metric_definitions: {},
    }));
    let confirm!: (value: boolean) => void;
    vi.mocked(confirmDialog).mockImplementation(
      () =>
        new Promise((resolve) => {
          confirm = resolve;
        }),
    );
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("tab", { name: "产物与记录" }));
    await user.click(screen.getByRole("button", { name: "删除实验" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "查看执行记录" }), "old");
    await user.selectOptions(screen.getByRole("combobox", { name: "查看执行记录" }), "attempt");
    await act(async () => confirm(true));
    expect(trainingRunsApi.remove).not.toHaveBeenCalled();
  });
  it("opens an explicit historical attempt and never falls back for a missing attempt", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: run as never,
      attempts: [
        { id: "attempt", ordinal: 2, status: "training", artifacts: {} } as never,
        { id: "old", ordinal: 1, status: "failed", artifacts: {} } as never,
      ],
    });
    vi.mocked(trainingRunsApi.events).mockImplementation(async (_p, _r, id) => ({
      attempt_id: id!,
      items: [],
    }));
    vi.mocked(trainingObservabilityApi.telemetry).mockImplementation(async (_p, _r, id) => ({
      attempt_id: id,
      items: [],
      stream_id: null,
      latest_cursor: 0,
      earliest_cursor: null,
      next_cursor: 0,
      has_more: false,
      gaps: [],
      summary: null,
      metric_definitions: {},
    }));
    const mounted = mount("old");
    expect(await screen.findByRole("combobox", { name: "查看执行记录" })).toHaveValue("old");
    mounted.unmount();
    mount("missing");
    await screen.findByRole("alert");
    expect(screen.getByText(/指定的执行尝试不存在/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "查看执行记录" })).not.toBeInTheDocument();
  });
});
