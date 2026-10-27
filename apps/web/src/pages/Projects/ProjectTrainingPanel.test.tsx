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
    downloadContent: vi.fn(),
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
function mount(initialAttemptId = "", mode: "list" | "detail" = "detail", search = "") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/project/learning${search}`]}>
        <ProjectTrainingPanel
          projectId="project"
          mode={mode}
          initialRunId={mode === "detail" ? "run" : ""}
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
  vi.unstubAllGlobals();
});
describe("managed project training", () => {
  it.each(["detect", "segment"])(
    "pairs %s training performance and losses with validation charts",
    async (task) => {
      vi.mocked(trainingRunsApi.get).mockResolvedValue({
        run: { ...run, task },
        attempts: [],
      } as never);
      vi.mocked(trainingRunsApi.events).mockResolvedValue({
        attempt_id: "attempt",
        items: [
          {
            sequence: 1,
            kind: "epoch",
            epoch: 1,
            metrics: {
              "train/box_loss": 1.2,
              "train/dfl_loss": 0.8,
              "train/cls_loss": 0,
              ...(task === "segment" ? { "train/seg_loss": 2.3, "train/sem_loss": 0 } : {}),
              "train/metrics/precision(B)": 0.7,
              "train/metrics/recall(B)": 0.6,
              "train/metrics/mAP50(B)": 0.8,
              "train/metrics/mAP50-95(B)": 0.6,
              ...(task === "segment" ? { "train/metrics/mAP50-95(M)": 0.4 } : {}),
              "metrics/mAP50-95(B)": 0.5,
              "val/box_loss": 1.9,
              ...(task === "segment" ? { "val/seg_loss": 2.9, "val/sem_loss": 0.1 } : {}),
            },
          },
        ],
      });
      mount();
      const performance = await screen.findByRole("img", { name: "训练表现，1 轮上报数据" });
      const charts = screen.getAllByRole("region", { name: /损失|表现/ });
      expect(charts.map((chart) => chart.getAttribute("aria-label"))).toEqual([
        "训练表现",
        "训练损失",
        "验证表现",
        "验证损失",
      ]);
      expect(charts[0]).toContainElement(performance);
      expect(within(charts[0]).getByText("训练框 Precision")).toBeVisible();
      expect(within(charts[0]).getByText("训练框 Recall")).toBeVisible();
      expect(within(charts[0]).getByText("训练框 mAP50–95")).toBeVisible();
      expect(within(charts[1]).getByText("训练框回归损失")).toBeVisible();
      expect(within(charts[1]).getByText("训练 DFL 损失")).toBeVisible();
      expect(within(charts[1]).getByText("训练分类损失")).toBeVisible();
      expect(within(charts[3]).queryByText("训练框回归损失")).not.toBeInTheDocument();
      expect(within(charts[3]).getByText("验证框回归损失")).toBeVisible();
      if (task === "segment") {
        expect(within(charts[0]).getByText("训练掩码 mAP50–95")).toBeVisible();
        expect(within(charts[1]).getByText("掩码训练损失")).toBeVisible();
        expect(within(charts[1]).getByText("语义训练损失")).toBeVisible();
        expect(within(charts[3]).getByText("语义验证损失")).toBeVisible();
        expect(within(charts[3]).queryByText("掩码训练损失")).not.toBeInTheDocument();
      }
      await userEvent.click(screen.getByText("逐轮指标表 · 1 轮"));
      expect(screen.getAllByRole("cell", { name: "0.0000" })).toHaveLength(
        task === "segment" ? 2 : 1,
      );
    },
  );
  it("keeps historical training loss charts empty when only validation losses were recorded", async () => {
    vi.mocked(trainingRunsApi.events).mockResolvedValue({
      attempt_id: "attempt",
      items: [{ sequence: 1, kind: "epoch", epoch: 1, metrics: { "val/box_loss": 1.9 } }],
    });
    mount();
    expect(await screen.findByRole("img", { name: "验证损失，1 轮上报数据" })).toBeVisible();
    expect(screen.getByText("此执行尚未上报训练损失")).toBeVisible();
    expect(screen.getByText("此执行尚未上报训练集表现")).toBeVisible();
    expect(screen.queryByRole("img", { name: /训练.*损失/ })).not.toBeInTheDocument();
  });
  it("preserves LibreYOLO total loss without inventing training components", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { ...run, config: { ...run.config, framework: "libreyolo" } },
      attempts: [],
    } as never);
    vi.mocked(trainingRunsApi.events).mockResolvedValue({
      attempt_id: "attempt",
      items: [{ sequence: 1, kind: "epoch", epoch: 1, metrics: { "train/loss": 0 } }],
    });
    mount();
    expect(await screen.findByRole("img", { name: "训练损失，1 轮上报数据" })).toBeVisible();
    expect(screen.getByText(/仅上报训练总损失，未提供训练集表现/)).toBeVisible();
    expect(screen.getByText("此执行尚未上报验证损失")).toBeVisible();
  });
  it("links a qualified successful execution to model registration with its exact source", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { ...run, status: "succeeded" },
      registration_eligibility: { qualified: true, reason: null, qualification: {} },
      attempts: [
        { id: "attempt", ordinal: 2, status: "succeeded", artifacts: {} },
        { id: "older", ordinal: 1, status: "succeeded", artifacts: {} },
      ],
    } as never);
    mount("older");
    expect(await screen.findByRole("link", { name: "回流到项目模型" })).toHaveAttribute(
      "href",
      "/projects/project/learning?tab=models&model_task=detect&register_run=run&register_attempt=older",
    );
  });
  it("explains why a successful training result cannot return as a project model", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { ...run, status: "succeeded" },
      registration_eligibility: { qualified: false, reason: "该模型家族尚未支持回流" },
      attempts: [{ id: "attempt", ordinal: 1, status: "succeeded", artifacts: {} }],
    } as never);
    mount();
    expect(await screen.findByText(/该模型家族尚未支持回流/)).toBeVisible();
    expect(screen.queryByRole("link", { name: "回流到项目模型" })).not.toBeInTheDocument();
  });
  it("disables return and deletion for an execution already referenced by a project model", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { ...run, status: "succeeded" },
      registration_eligibility: { qualified: true, reason: null },
      registered_models: [{ id: "model", attempt_id: "attempt", name: "车辆模型" }],
      attempts: [{ id: "attempt", ordinal: 1, status: "succeeded", artifacts: {} }],
    } as never);
    const user = userEvent.setup();
    mount();
    expect(await screen.findByRole("button", { name: "已回流到项目模型" })).toBeDisabled();
    expect(screen.queryByRole("link", { name: "回流到项目模型" })).not.toBeInTheDocument();
    expect(screen.getByText(/本次执行已添加为项目模型“车辆模型”/)).toBeVisible();
    const deletion = screen.getByRole("button", { name: "删除实验" });
    expect(deletion).toBeDisabled();
    expect(deletion).toHaveAccessibleDescription(/实验已被项目模型引用/);
    await user.click(deletion);
    expect(confirmDialog).not.toHaveBeenCalled();
    expect(trainingRunsApi.remove).not.toHaveBeenCalled();
  });
  it("keeps a different successful execution available for return", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { ...run, status: "succeeded" },
      registration_eligibility: { qualified: true, reason: null },
      registered_models: [{ id: "model", attempt_id: "older", name: "旧模型" }],
      attempts: [
        { id: "attempt", ordinal: 2, status: "succeeded", artifacts: {} },
        { id: "older", ordinal: 1, status: "succeeded", artifacts: {} },
      ],
    } as never);
    const user = userEvent.setup();
    mount();
    expect(await screen.findByRole("link", { name: "回流到项目模型" })).toHaveAttribute(
      "href",
      expect.stringContaining("register_attempt=attempt"),
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "查看执行记录" }), "older");
    expect(await screen.findByRole("button", { name: "已回流到项目模型" })).toBeDisabled();
    await user.selectOptions(screen.getByRole("combobox", { name: "查看执行记录" }), "attempt");
    expect(await screen.findByRole("link", { name: "回流到项目模型" })).toBeVisible();
  });
  it("does not offer model registration for an unfinished execution", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run,
      registration_eligibility: { qualified: true, reason: null },
      attempts: [{ id: "attempt", ordinal: 1, status: "training", artifacts: {} }],
    } as never);
    mount();
    await screen.findByRole("article", { name: "训练实验详情" });
    expect(screen.queryByRole("link", { name: "回流到项目模型" })).not.toBeInTheDocument();
  });
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
  it("filters before pagination and links directly to independent details without loading a monitor", async () => {
    const other = { ...run, id: "other", name: "行人分割实验", task: "segment" };
    vi.mocked(trainingRunsApi.list).mockImplementation(
      async (_project, _offset, _options, filters) => ({
        items: (filters?.q ? [other] : [run, other]) as never,
        total: filters?.q ? 21 : 22,
      }),
    );
    vi.mocked(trainingRunsApi.get).mockImplementation(
      async (_project, id) => ({ run: id === "other" ? other : run, attempts: [] }) as never,
    );
    const user = userEvent.setup();
    mount("", "list", "?run_page=1");
    await screen.findByRole("link", { name: "车辆训练" });
    await user.type(screen.getByRole("textbox", { name: "搜索实验名称或基础权重" }), "分割");
    await user.selectOptions(screen.getByRole("combobox", { name: "实验状态筛选" }), "succeeded");
    await waitFor(() =>
      expect(trainingRunsApi.list).toHaveBeenLastCalledWith(
        "project",
        0,
        expect.anything(),
        expect.objectContaining({ q: "分割", status: "succeeded" }),
        20,
      ),
    );
    const list = screen.getByRole("table", { name: "训练实验列表" });
    await waitFor(() =>
      expect(within(list).queryByRole("link", { name: "车辆训练" })).not.toBeInTheDocument(),
    );
    const link = within(list).getByRole("link", { name: "行人分割实验" });
    expect(link).toHaveAttribute(
      "href",
      "/projects/project/learning/experiments/other?run_q=%E5%88%86%E5%89%B2&run_status=succeeded",
    );
    expect(screen.queryByRole("tab", { name: "实验详情" })).not.toBeInTheDocument();
    expect(trainingRunsApi.get).not.toHaveBeenCalled();
    expect(trainingRunsApi.events).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "下一页实验" }));
    await waitFor(() =>
      expect(trainingRunsApi.list).toHaveBeenLastCalledWith(
        "project",
        20,
        expect.anything(),
        expect.objectContaining({ q: "分割", status: "succeeded" }),
        20,
      ),
    );
    expect(trainingRunsApi.create).not.toHaveBeenCalled();
  });
  it("keeps an experiment request in loading feedback without announcing an empty project", async () => {
    vi.mocked(trainingRunsApi.list).mockReturnValue(new Promise(() => {}));
    mount("", "list");
    expect(await screen.findByText("正在加载训练实验…")).toBeInTheDocument();
    expect(screen.queryByText("创建你的第一个训练实验")).not.toBeInTheDocument();
  });
  it("keeps rows and filter focus stable while background polling is pending", async () => {
    vi.mocked(trainingRunsApi.list).mockResolvedValueOnce({ items: [run as never], total: 1 });
    let resolve!: (value: Awaited<ReturnType<typeof trainingRunsApi.list>>) => void;
    vi.mocked(trainingRunsApi.list).mockImplementation(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    mount("", "list");
    const link = await screen.findByRole("link", { name: "车辆训练" });
    const input = screen.getByRole("textbox", { name: "搜索实验名称或基础权重" });
    input.focus();
    await act(async () => {
      void clients[clients.length - 1].refetchQueries({ queryKey: ["training-runs"] });
    });
    await waitFor(() => expect(trainingRunsApi.list).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("正在加载训练实验…")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "车辆训练" })).toBe(link);
    expect(input).toHaveFocus();
    await act(async () =>
      resolve({ items: [{ ...run, metrics: { epoch: 3 } } as never], total: 1 }),
    );
    expect(await screen.findByText("3 / 10 轮")).toBeVisible();
    expect(screen.getByRole("link", { name: "车辆训练" })).toBe(link);
  });
  it("opens the independent project creation page", async () => {
    const user = userEvent.setup();
    mount("", "list");
    await waitFor(() => expect(screen.getByRole("button", { name: "新建实验" })).toBeEnabled());
    expect(screen.queryByRole("button", { name: "删除实验" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "新建实验" }));
    expect(screen.getByText("/projects/project/learning/new")).toBeVisible();
  });
  it("replaces creation with a visible disabled delete entry while training", async () => {
    mount();
    await screen.findByRole("article", { name: "训练实验详情" });
    expect(screen.queryByRole("button", { name: "新建实验" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除实验" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "删除实验" })).toHaveAccessibleDescription(
      "实验尚未结束，请先停止训练。",
    );
  });
  it("deletes a terminal experiment from its header after confirmation and returns to the list", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { ...run, status: "failed" },
      attempts: [],
      registered_models: [],
    } as never);
    vi.mocked(confirmDialog).mockResolvedValueOnce(true);
    const user = userEvent.setup();
    mount();
    await screen.findByRole("article", { name: "训练实验详情" });
    expect(screen.queryByRole("button", { name: "新建实验" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "指标曲线" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("button", { name: "删除实验" }));
    await waitFor(() => expect(trainingRunsApi.remove).toHaveBeenCalledWith("project", "run"));
    expect(confirmDialog).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "删除实验“车辆训练”",
        tone: "danger",
      }),
    );
    expect(screen.getByText("/projects/project/learning")).toBeVisible();
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

  it("downloads authenticated redacted content and releases its Blob URL", async () => {
    const failed = { ...run, status: "failed" };
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
    vi.mocked(trainingRunsApi.download).mockResolvedValue({
      url: "/projects/project/training-runs/run/attempts/attempt/artifacts/train.log/content",
      requires_authentication: true,
      sha256: "redacted",
      size_bytes: 4,
    });
    const blob = new Blob(["safe"]);
    vi.mocked(trainingRunsApi.downloadContent).mockResolvedValue(blob);
    const create = vi.fn(() => "blob:redacted");
    const revoke = vi.fn();
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = create;
        static revokeObjectURL = revoke;
      },
    );
    const clicked: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this.href);
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("tab", { name: "产物与记录" }));
    await user.click(screen.getByRole("button", { name: /下载脱敏训练日志/ }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:redacted"));
    expect(trainingRunsApi.downloadContent).toHaveBeenCalledWith(
      "project",
      "run",
      "attempt",
      "train.log",
    );
    expect(create).toHaveBeenCalledWith(blob);
    expect(clicked).toEqual(["blob:redacted"]);
    click.mockRestore();
  });

  it("ignores authenticated log bytes received after the account changes", async () => {
    const failed = { ...run, status: "failed" };
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
    vi.mocked(trainingRunsApi.download).mockResolvedValue({
      url: "/authenticated-content",
      requires_authentication: true,
      sha256: "redacted",
      size_bytes: 4,
    });
    let resolve!: (blob: Blob) => void;
    vi.mocked(trainingRunsApi.downloadContent).mockImplementation(
      () => new Promise((yes) => (resolve = yes)),
    );
    const create = vi.fn();
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = create;
      },
    );
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("tab", { name: "产物与记录" }));
    await user.click(screen.getByRole("button", { name: /下载脱敏训练日志/ }));
    await waitFor(() => expect(trainingRunsApi.downloadContent).toHaveBeenCalledOnce());
    await act(async () => useAuthStore.getState().setToken("token-b"));
    await act(async () => resolve(new Blob(["safe"])));
    expect(create).not.toHaveBeenCalled();
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
