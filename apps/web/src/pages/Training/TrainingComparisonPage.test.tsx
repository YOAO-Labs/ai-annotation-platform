import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  trainingRunsApi,
  type TrainingRunOut,
  type TrainingAttemptOut,
  type TrainingEvaluationOut,
} from "@/api/trainingRuns";
import { useAuthStore } from "@/stores/authStore";
import { TrainingComparisonPage } from "./TrainingComparisonPage";
import {
  comparisonPoints,
  comparisonMetricRanking,
  comparisonMetricValues,
  curveData,
  incompatibility,
  inputDifferences,
  parseComparison,
  type ComparisonExperiment,
} from "./trainingComparison";
import { metricLabel, trainingTaskLabel } from "./trainingPresentation";

vi.mock("@/api/trainingRuns", () => ({
  trainingRunsApi: { list: vi.fn(), get: vi.fn(), events: vi.fn(), evaluation: vi.fn() },
}));
vi.mock("./TrainingMetricChart", () => ({
  TrainingMetricChart: ({ title }: { title: string }) => <div role="img" aria-label={title} />,
}));
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const projectId = id(1),
  r1 = id(11),
  r2 = id(12),
  a1 = id(21),
  a2 = id(22),
  a3 = id(23);
const clients: QueryClient[] = [];
function run(runId: string): TrainingRunOut {
  return {
    id: runId,
    project_id: projectId,
    name: runId === r1 ? "路口基线" : "学习率实验",
    status: "succeeded",
    current_attempt_id: runId === r1 ? a1 : a2,
    dataset_id: id(31),
    creator_id: id(2),
    async_job_id: null,
    config_digest: "c".repeat(64),
    metrics: { epoch: 99, "metrics/mAP50": 0.99 },
    error_message: null,
    created_at: "2026-10-15T00:00:00Z",
    completed_at: "2026-10-15T00:01:00Z",
    config: {
      framework: "ultralytics",
      framework_version: "8.4.177",
      validation_digest: "v",
      classes: ["car"],
      base_model: { id: "yolo11n", sha256: "base" },
      parameters: { epochs: 3, lr0: 0.01 },
      configuration: {
        identity: { schema_id: "frozen-old-schema" },
        effective_values: { epochs: 3, lr0: runId === r1 ? 0.01 : 0.02, imgsz: 128, amp: false },
        origins: { lr0: "user" },
      },
    },
  };
}
function attempt(attemptId: string): TrainingAttemptOut {
  return {
    id: attemptId,
    ordinal: attemptId === a3 ? 2 : 1,
    status: "succeeded",
    resume_from_id: null,
    executor_id: "executor",
    resource_id: "gpu",
    runtime: { image_digest: "image" },
    artifacts: {},
    error_message: null,
    heartbeat_at: null,
    started_at: "2026-10-15T00:00:00Z",
    completed_at: "2026-10-15T00:01:00Z",
  };
}
function report(runId: string, attemptId: string): TrainingEvaluationOut {
  return {
    identity_status: "verified",
    reasons: [],
    compatibility_digest: "compatible",
    descriptor: {
      revision: 1,
      config_digest: "c".repeat(64),
      candidate: { weights_sha256: runId, selection: "native_best", best_epoch: null },
      benchmark: { validation_digest: "v", classes: ["car"], class_space: "frozen_native" },
      evaluator: {
        framework: "ultralytics",
        version: "8.4.177",
        task: "detection",
        implementation: "pinned",
        source_sha256: "e".repeat(64),
        options: { split: "val" },
      },
      semantics: {
        "metrics/mAP50": {
          unit: "fraction",
          direction: "higher",
          minimum: 0,
          maximum: 1,
          unavailable_below: 0,
        },
      },
    },
    summary: { "metrics/mAP50": runId === r1 ? 0.4 : 0.5 },
    per_class_available: false,
    per_class_total: 0,
    per_class: [],
    provenance: { run_id: runId, attempt_id: attemptId, weights_sha256: runId },
  };
}
function experiment(runId = r1): ComparisonExperiment {
  const runData = run(runId),
    attemptData = attempt(runData.current_attempt_id!);
  return {
    selection: { runId, attemptId: attemptData.id },
    run: runData,
    attempt: attemptData,
    attempts: [attemptData],
    events: [],
    report: report(runId, attemptData.id),
    loading: false,
  };
}
const both = `runs=${r1}:${a1},${r2}:${a2}`;
function Context() {
  const location = useLocation(),
    navigate = useNavigate();
  return (
    <>
      <p data-testid="location">{location.search}</p>
      <button onClick={() => navigate(-1)}>测试返回</button>
    </>
  );
}
function mount(search = both) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/${projectId}/learning/compare?${search}`]}>
        <Routes>
          <Route path="/projects/:id/learning/compare" element={<TrainingComparisonPage />} />
        </Routes>
        <Context />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("session-1", { id: id(2) } as never);
  vi.mocked(trainingRunsApi.list).mockResolvedValue({ total: 2, items: [run(r1), run(r2)] });
  vi.mocked(trainingRunsApi.get).mockImplementation(async (_p, runId) => ({
    run: run(runId),
    attempts: [attempt(runId === r1 ? a1 : a2)],
  }));
  vi.mocked(trainingRunsApi.events).mockImplementation(async (_p, _r, attemptId) => ({
    attempt_id: attemptId!,
    items: [{ sequence: 1, kind: "epoch", epoch: 3, metrics: { "metrics/mAP50": 0.2 } }],
  }));
  vi.mocked(trainingRunsApi.evaluation).mockImplementation(async (_p, runId, attemptId) =>
    report(runId, attemptId),
  );
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
});

describe("comparison evidence and URL", () => {
  it("keeps segmentation box and mask curves distinct and labels native fitness as a score", () => {
    const e = experiment();
    e.run!.config.configuration = { identity: { task: "image-instance-segmentation" } };
    e.events = [
      {
        sequence: 1,
        kind: "epoch",
        epoch: 1,
        metrics: {
          "metrics/mAP50(B)": 0.8,
          "metrics/mAP50(M)": 0.3,
          fitness: 1.1,
        },
      },
    ];
    expect(comparisonPoints(e)[0]).toEqual({
      epoch: 1,
      "metrics/mAP50(B)": 0.8,
      "metrics/mAP50(M)": 0.3,
      fitness: 1.1,
    });
    expect(curveData([e], "metrics/mAP50(M)", false)[0].series0).toBe(0.3);
    expect(metricLabel("metrics/mAP50(B)")).toBe("框 mAP50");
    expect(metricLabel("metrics/mAP50(M)")).toBe("掩码 mAP50");
    expect(metricLabel("fitness")).toBe("原生最佳权重选择分数");
    expect(trainingTaskLabel("image-instance-segmentation")).toBe("图片实例分割");
    expect(parseComparison(new URLSearchParams("metric=metrics/mAP50(M)")).metricExplicit).toBe(
      true,
    );
  });
  it("rejects duplicate experiments, malformed IDs, oversized lists and invalid references", () => {
    for (const search of [
      `runs=${r1}:${a1},${r1}:${a3}`,
      "runs=broken",
      `runs=${r1}:${a1}&reference=${r2}`,
      `runs=${Array.from({ length: 5 }, (_, i) => `${id(100 + i)}:${id(200 + i)}`).join(",")}`,
    ])
      expect(parseComparison(new URLSearchParams(search)).error).toBeTruthy();
    expect(parseComparison(new URLSearchParams(both)).selections).toHaveLength(2);
  });
  it("reads frozen effective values and selected resume initialization, without recomputing defaults", () => {
    const first = experiment(),
      second = experiment(r2);
    second.attempt!.resume_from_id = a3;
    second.attempts.push({
      ...attempt(a3),
      artifacts: { "checkpoint.pt": { role: "checkpoint", sha256: "resume-sha", epoch: 2 } },
    });
    const rows = inputDifferences([first, second], true);
    expect(rows.find((row) => row.key === "参数 · lr0")?.values).toEqual(["0.01", "0.02"]);
    expect(rows.find((row) => row.key === "参数 · lr0")?.origins).toEqual(["user", "user"]);
    expect(rows.find((row) => row.key === "续训检查点 SHA")?.values).toEqual([
      '"无"',
      '"resume-sha"',
    ]);
    expect(rows.some((row) => row.key === "基础权重 SHA")).toBe(false);
    delete second.run!.config.configuration;
    expect(
      inputDifferences([second], false).find((row) => row.key === "参数定义")?.values[0],
    ).toContain("其他默认值未知");
  });
  it("keeps real zero, drops negative aliases and breaks curves across absent epochs; smoothing never mutates events", () => {
    const e = experiment();
    e.events = [1, 2, 4].map((epoch, i) => ({
      sequence: i + 1,
      kind: "epoch",
      epoch,
      metrics: {
        "metrics/mAP50(B)": epoch === 1 ? 0 : epoch === 2 ? 0.5 : 0.8,
        "metrics/mAP_small": -1,
      },
    }));
    expect(comparisonPoints(e)[0]["metrics/mAP50"]).toBe(0);
    const raw = curveData([e], "metrics/mAP50", false),
      smooth = curveData([e], "metrics/mAP50", true);
    expect(raw.map((p) => p.series0)).toEqual([0, 0.5, null, 0.8]);
    expect(smooth.map((p) => p.series0)).toEqual([0, 0.2, null, 0.8]);
    expect(e.events[1].metrics["metrics/mAP50(B)"]).toBe(0.5);
  });
  it("does not qualify missing, legacy or incompatible evaluator evidence", () => {
    const a = report(r1, a1),
      b = report(r2, a2);
    expect(incompatibility(a, b)).toEqual([]);
    b.compatibility_digest = "other";
    b.descriptor!.evaluator.options = { conf: 0.2 };
    expect(incompatibility(a, b).join()).toContain("评估器");
    b.descriptor = null;
    b.compatibility_digest = null;
    expect(incompatibility(a, b).join()).toContain("缺少完整评估身份");
  });
});

it("separates latest epoch from final best-weight metrics, exposes safe deltas and restores URL view choices", async () => {
  mount();
  await screen.findByText("较最优 -10.0 个百分点");
  expect(await screen.findAllByText("0.2000")).toHaveLength(2);
  expect(screen.queryByText("0.9900")).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("tab", { name: "输入差异" }));
  expect(await screen.findByText("参数 · lr0")).toBeInTheDocument();
  expect(screen.getByTestId("location").textContent).toContain("view=inputs");
  await user.click(screen.getByRole("tab", { name: "训练曲线" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "曲线指标" }), "metrics/mAP50");
  await user.click(screen.getByRole("checkbox", { name: "平滑曲线" }));
  expect(screen.getByTestId("location").textContent).toContain("smoothing=ema");
  expect(await screen.findAllByText("0.2000")).toHaveLength(2);
  await user.click(screen.getByRole("button", { name: "测试返回" }));
  expect(screen.getByRole("checkbox", { name: "平滑曲线" })).not.toBeChecked();
});

it("highlights the optimum per metric independently for Best and Last, regardless of URL reference", async () => {
  vi.mocked(trainingRunsApi.evaluation).mockImplementation(async (_p, runId, attemptId) => ({
    ...report(runId, attemptId),
    summary: {
      "metrics/mAP50": runId === r1 ? 0.8 : 0.5,
      "metrics/precision": runId === r1 ? 0.4 : 0.9,
    },
  }));
  vi.mocked(trainingRunsApi.events).mockImplementation(async (_p, runId, attemptId) => ({
    attempt_id: attemptId!,
    items: [
      {
        sequence: 1,
        kind: "epoch",
        epoch: 3,
        metrics: {
          "metrics/mAP50(B)": runId === r1 ? 0.2 : 0.6,
          "metrics/precision(B)": runId === r1 ? 0.9 : 0.4,
        },
      },
    ],
  }));
  mount(`${both}&reference=${r2}`);
  const best = await screen.findByRole("region", { name: "Best 指标表" });
  const last = await screen.findByRole("region", { name: "Last 指标表" });
  await within(best).findByText("较最优 -30.0 个百分点");
  await within(last).findByText("较最优 -40.0 个百分点");
  for (const [table, metric, winner, winningValue, gap] of [
    [best, "mAP50", 0, "0.8000", "较最优 -30.0 个百分点"],
    [best, "Precision", 1, "0.9000", "较最优 -50.0 个百分点"],
    [last, "mAP50", 1, "0.6000", "较最优 -40.0 个百分点"],
    [last, "Precision", 0, "0.9000", "较最优 -50.0 个百分点"],
  ] as const) {
    const row = within(table).getByRole("row", { name: new RegExp(`^${metric} `) });
    const cells = within(row).getAllByRole("cell");
    expect(within(cells[winner]).getByText(winningValue)).toHaveClass(
      "font-semibold",
      "text-primary",
    );
    expect(within(cells[winner]).getByText("最优")).toBeInTheDocument();
    expect(within(cells[1 - winner]).getByText(gap)).toHaveClass("text-status-positive");
  }
  expect(within(last).getAllByText("Last · 第 3 轮")).toHaveLength(2);
  expect(
    within(best)
      .getAllByRole("rowheader")
      .map((row) => row.textContent),
  ).toEqual(
    within(last)
      .getAllByRole("rowheader")
      .map((row) => row.textContent),
  );
  expect(screen.queryByRole("combobox", { name: "参照实验" })).not.toBeInTheDocument();
});

it("highlights every tied optimum, keeps zero and missing values distinct, and never substitutes Best for Last", async () => {
  vi.mocked(trainingRunsApi.evaluation).mockImplementation(async (_p, runId, attemptId) => ({
    ...report(runId, attemptId),
    summary: { "metrics/mAP50": 0, "metrics/precision": runId === r1 ? null : 0.5 },
  }));
  vi.mocked(trainingRunsApi.events).mockImplementation(async (_p, _runId, attemptId) => ({
    attempt_id: attemptId!,
    items: [],
  }));
  mount();
  const best = await screen.findByRole("region", { name: "Best 指标表" });
  await within(best).findAllByText("并列最优");
  const tied = within(best).getByRole("row", { name: /^mAP50 / });
  expect(within(tied).getAllByText("0.0000")).toHaveLength(2);
  expect(within(tied).getAllByText("并列最优")).toHaveLength(2);
  const missing = within(best).getByRole("row", { name: /^Precision / });
  expect(within(missing).getByText("—")).toBeInTheDocument();
  expect(within(missing).getByText("0.5000")).toBeInTheDocument();
  expect(within(missing).queryByText(/最优/)).not.toBeInTheDocument();
  const last = await screen.findByRole("region", { name: "Last 指标表" });
  expect(within(last).getByText(/暂无 Last 轮次指标/)).toBeInTheDocument();
  expect(within(last).queryByText("0.5000")).not.toBeInTheDocument();
  expect(within(best).queryByText(/较最优 -0/)).not.toBeInTheDocument();
});

it("ranks compatible peers without letting an incompatible higher result become the baseline", () => {
  const first = experiment(),
    second = experiment(r2),
    other = experiment(id(13));
  other.report!.compatibility_digest = "other";
  other.report!.descriptor!.evaluator.options = { conf: 0.2 };
  other.report!.summary = { "metrics/mAP50": 0.99 };
  const ranking = comparisonMetricRanking([first, second, other], "best", "metrics/mAP50");
  expect(ranking[0]?.delta).toBeCloseTo(-0.1);
  expect(ranking[0]?.grouped).toBe(true);
  expect(ranking[1]?.best).toBe(true);
  expect(ranking[2]).toBeUndefined();
  second.run!.config.configuration = { effective_values: { imgsz: 640 } };
  first.events = second.events = [
    {
      sequence: 1,
      kind: "epoch",
      epoch: 3,
      metrics: { "metrics/mAP50(B)": 0.3 },
    },
  ];
  expect(comparisonMetricRanking([first, second], "best", "metrics/mAP50")[1]?.best).toBe(true);
  expect(comparisonMetricRanking([first, second], "last", "metrics/mAP50")).toEqual([
    undefined,
    undefined,
  ]);
});

it("keeps Last tied to the selected execution's latest epoch without carrying missing metrics forward", () => {
  const e = experiment();
  e.events = [
    {
      sequence: 1,
      kind: "epoch",
      epoch: 1,
      metrics: { "metrics/mAP50": 0.8, "metrics/recall": 0.4 },
    },
    {
      sequence: 2,
      kind: "epoch",
      epoch: 2,
      metrics: { "metrics/recall": 0, "metrics/precision": -1 },
    },
  ];
  expect(comparisonMetricValues(e, "last")).toEqual({ "metrics/recall": 0 });
  expect(comparisonMetricValues(e, "best")).toEqual({ "metrics/mAP50": 0.4 });
  e.eventsError = "执行记录读取失败";
  expect(comparisonMetricValues(e, "last")).toEqual({});
});

it("does not round a small nonzero deficit to negative zero", async () => {
  vi.mocked(trainingRunsApi.evaluation).mockImplementation(async (_p, runId, attemptId) => ({
    ...report(runId, attemptId),
    summary: { "metrics/mAP50": runId === r1 ? 0.5002 : 0.5 },
  }));
  mount();
  const best = await screen.findByRole("region", { name: "Best 指标表" });
  expect(await within(best).findByText("较最优 -0.02 个百分点")).toHaveClass(
    "text-status-positive",
  );
  expect(within(best).queryByText("较最优 -0.0 个百分点")).not.toBeInTheDocument();
});

it("retains fixed historical attempts and lets a missing attempt be explicitly replaced", async () => {
  vi.mocked(trainingRunsApi.get).mockImplementation(async (_p, runId) => ({
    run: { ...run(runId), current_attempt_id: runId === r1 ? a3 : a2 },
    attempts: runId === r1 ? [attempt(a3), attempt(a1)] : [attempt(a2)],
  }));
  mount(`runs=${r1}:${id(999)},${r2}:${a2}`);
  expect((await screen.findAllByText(/所选执行不存在/)).length).toBeGreaterThan(0);
  const select = screen.getByRole("combobox", { name: "路口基线的执行" });
  expect(select).toHaveValue(id(999));
  expect(trainingRunsApi.events).not.toHaveBeenCalledWith(
    projectId,
    r1,
    id(999),
    expect.anything(),
    expect.anything(),
  );
  fireEvent.change(select, { target: { value: a1 } });
  await waitFor(() =>
    expect(trainingRunsApi.evaluation).toHaveBeenCalledWith(
      projectId,
      r1,
      a1,
      0,
      expect.anything(),
      20,
    ),
  );
  expect(select).toHaveValue(a1);
  expect(screen.getByTestId("location").textContent).toContain(a1);
  expect(screen.getByTestId("location").textContent).not.toContain(a3);
});

it("keeps deleted or unreadable selections independent and disables unsafe deltas", async () => {
  vi.mocked(trainingRunsApi.get).mockImplementation(async (_p, runId) => {
    if (runId === r1) throw new Error("实验已删除");
    return { run: run(runId), attempts: [attempt(a2)] };
  });
  mount();
  expect((await screen.findAllByText("实验已删除")).length).toBeGreaterThan(0);
  await screen.findByText("0.5000");
  expect(screen.queryByText(/较(?:组内)?最优 -/)).not.toBeInTheDocument();
  expect(screen.getByTestId("location").textContent).toContain(a1);
});

it("retains selections across picker pages and prevents duplicate/max-count choices", async () => {
  vi.mocked(trainingRunsApi.list).mockImplementation(async (_p, offset = 0) => ({
    total: 22,
    items: offset ? [run(r2)] : [run(r1)],
  }));
  mount("");
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "加入比较：路口基线" }));
  await user.click(screen.getByRole("button", { name: "下一页实验" }));
  await user.click(await screen.findByRole("button", { name: "加入比较：学习率实验" }));
  await screen.findByText("较最优 -10.0 个百分点");
  expect(screen.getByRole("button", { name: "加入比较：学习率实验" })).toBeDisabled();
  expect(screen.getByTestId("location").textContent).toContain(a1);
  expect(screen.getByTestId("location").textContent).toContain(a2);
});

it("fences a delayed report when the token changes", async () => {
  let resolve!: (value: TrainingEvaluationOut) => void;
  let signal: AbortSignal | undefined;
  vi.mocked(trainingRunsApi.evaluation)
    .mockImplementationOnce((_p, _r, _a, _o, options) => {
      signal = options?.signal as AbortSignal;
      return new Promise((yes) => {
        resolve = yes;
      });
    })
    .mockImplementation(async (_p, runId, attemptId) => ({
      ...report(runId, attemptId),
      summary: { "metrics/mAP50": 0.1 },
    }));
  mount();
  await waitFor(() => expect(signal).toBeDefined());
  act(() => useAuthStore.getState().setAuth("session-2", { id: id(2) } as never));
  await waitFor(() => expect(signal!.aborted).toBe(true));
  await act(async () => resolve({ ...report(r1, a1), summary: { "metrics/mAP50": 0.99 } }));
  await waitFor(() => expect(screen.getAllByText("0.1000")).toHaveLength(2));
  expect(screen.queryByText("0.9900")).not.toBeInTheDocument();
});
