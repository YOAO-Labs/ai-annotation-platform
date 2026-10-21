import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  sampleEvaluationsApi,
  type EvaluationComparison,
  type EvaluationReport,
  type EvaluationSampleResult,
  type EvaluationSourceOut,
  type SampleEvaluationOut,
} from "@/api/sampleEvaluations";
import { useAuthStore } from "@/stores/authStore";
import { SampleEvaluationPage } from "./SampleEvaluationPage";
import { parseEvaluationState, sourceWorkbenchUrl } from "./sampleEvaluation";
import { useTrainingViewOwner } from "./trainingViewOwner";

vi.mock("@/api/sampleEvaluations", () => ({
  sampleEvaluationsApi: {
    list: vi.fn(),
    get: vi.fn(),
    report: vi.fn(),
    samples: vi.fn(),
    image: vi.fn(),
    source: vi.fn(),
    compare: vi.fn(),
    cancel: vi.fn(),
    retry: vi.fn(),
    remove: vi.fn(),
  },
}));
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const project = id(1),
  evaluation = id(2),
  second = id(3),
  attempt = id(4),
  historical = id(5),
  nextAttempt = id(6);
const hash = "a".repeat(64);
const clients: QueryClient[] = [];
const sample = (ordinal = 0): EvaluationSampleResult => ({
  ordinal,
  task_id: id(100 + ordinal),
  sha256: hash,
  label_sha256: hash,
  width: 300,
  height: 200,
  status: "succeeded",
  truth: [{ index: 0, class_index: 0, xyxy: [0.1, 0.1, 0.8, 0.8] }],
  predictions: [
    { index: 0, class_index: 0, native_class_index: 0, score: 0.9, xyxy: [0.1, 0.1, 0.8, 0.8] },
  ],
  matching: {
    tp: 1,
    fp: 0,
    fn: 0,
    precision: 1,
    recall: 1,
    f1: 1,
    matches: [{ prediction_index: 0, truth_index: 0, iou: 1 }],
    false_positive_indices: [],
    false_negative_indices: [],
    excluded_prediction_indices: [],
    diagnostics: [],
  },
  request_id: id(200 + ordinal),
  derived_sha256: hash,
  produced_attempt_id: attempt,
});
const aggregate = {
  tp: 13,
  fp: 0,
  fn: 0,
  precision: 1,
  recall: 1,
  f1: 1,
  samples: 13,
  per_class: [
    { class_index: 0, name: "车辆", tp: 13, fp: 0, fn: 0, precision: 1, recall: 1, f1: 1 },
  ],
  diagnostics: { confusion: 0, localization: 0 },
  confusion: [],
};
function run(evaluationId = evaluation, currentAttempt = attempt): SampleEvaluationOut {
  return {
    id: evaluationId,
    project_id: project,
    name: evaluationId === evaluation ? "冻结路口评估" : "第二候选评估",
    status: "succeeded",
    dataset_id: id(7),
    model_version_id: id(8),
    deployment_id: id(9),
    request_id: id(10),
    spec_digest: hash,
    benchmark_digest: hash,
    comparison_digest: hash,
    async_job_id: null,
    current_attempt_id: currentAttempt,
    progress: { succeeded: 13 },
    error_code: null,
    error_message: null,
    created_at: "2026-10-15T00:00:00Z",
    completed_at: "2026-10-15T00:01:00Z",
    deletion_requested_at: null,
    attempts: [currentAttempt, historical].map((value, i) => ({
      id: value,
      ordinal: i ? 1 : 2,
      generation: i ? 1 : 2,
      status: i ? "cancelled" : "succeeded",
      report: { sha256: hash },
      completed_at: null,
      started_at: null,
      heartbeat_at: null,
      error_code: null,
      error_message: null,
    })),
    spec: {
      schema: 1,
      evaluation_id: evaluationId,
      model_ref: { sha256: hash },
      dataset_id: id(7),
      snapshot_digest: hash,
      manifest_digest: hash,
      package_sha256: hash,
      benchmark_digest: hash,
      comparison_digest: hash,
      classes: ["车辆"],
      native_to_benchmark: [0],
      role: "validation_selection",
      held_out_confirmed: false,
      confidence: 0.25,
      matching_iou: 0.5,
      nms_iou: 0.7,
      evaluator: {},
      preprocessing: {},
      profile_digest: hash,
      profile: {
        schema: 1,
        implementation: "aap-ultralytics-detection",
        revision: 1,
        effective_device: "cpu",
        ultralytics_version: "8.4.177",
        torch_version: "2",
        pillow_version: "12",
        source_digest: hash,
        profile_digest: hash,
        storage_identity: id(20),
        options: {},
      },
      issued_at: 1,
      expires_at: 9999999999,
      samples: Array.from({ length: 13 }, (_, ordinal) => ({
        ...sample(ordinal),
        split: "val",
        size_bytes: 1024,
        explicit_negative: false,
      })),
    },
  };
}
function report(evaluationId = evaluation, attemptId = attempt): EvaluationReport {
  return {
    schema: 1,
    evaluation_id: evaluationId,
    attempt_id: attemptId,
    spec_digest: hash,
    benchmark_digest: hash,
    comparison_digest: hash,
    model_ref: {},
    evaluator: {},
    status: "succeeded",
    complete: true,
    total_samples: 13,
    succeeded_samples: 13,
    failed_samples: 0,
    pending_samples: 0,
    aggregate,
    samples: Array.from({ length: 13 }, (_, n) => sample(n)),
  };
}
function comparison(): EvaluationComparison {
  return {
    compatible: true,
    reasons: [],
    candidates: [evaluation, second].map((value) => ({
      evaluation_id: value,
      attempt_id: attempt,
      name: run(value).name,
      status: "succeeded",
      model_ref: {},
      spec_digest: hash,
      comparison_digest: hash,
      aggregate,
    })),
    delta_right_minus_left: { tp: 0, fp: 0, fn: 0, precision: 0, recall: 0, f1: 0 },
    sample_pairs: Array.from({ length: 13 }, (_, n) => ({
      left_ordinal: n,
      right_ordinal: 12 - n,
    })),
  };
}
function source(overrides: Partial<EvaluationSourceOut> = {}): EvaluationSourceOut {
  return {
    evaluation_id: evaluation,
    attempt_id: attempt,
    ordinal: 0,
    checked_at: "2026-10-15T00:02:00Z",
    source: "found",
    task_id: id(100),
    project_id: project,
    batch_id: id(30),
    media: "same",
    labels: "changed",
    current_sha256: hash,
    current_label_sha256: "b".repeat(64),
    current_width: 300,
    current_height: 200,
    task_status: "in_progress",
    reviewed: false,
    negative_confirmed: false,
    reason: null,
    ...overrides,
  };
}
function Context() {
  const location = useLocation(),
    navigate = useNavigate();
  return (
    <>
      <output data-testid="location">
        {location.pathname}
        {location.search}
      </output>
      <button onClick={() => navigate(-1)}>测试后退</button>
      <button
        onClick={() =>
          navigate(`/projects/${id(40)}/learning/evaluations/${id(41)}?attempt=${id(42)}`)
        }
      >
        测试切换项目
      </button>
    </>
  );
}
function mount(query = `attempt=${attempt}&sample=0`) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[`/projects/${project}/learning/evaluations/${evaluation}?${query}`]}
      >
        <Routes>
          <Route
            path="/projects/:id/learning/evaluations/:evaluationId"
            element={<SampleEvaluationPage />}
          />
          <Route path="/projects/:id/annotate" element={<p>当前任务工作台</p>} />
        </Routes>
        <Context />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}
const originalCreate = URL.createObjectURL,
  originalRevoke = URL.revokeObjectURL;
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("session-a", { id: id(90) } as never);
  URL.createObjectURL = vi.fn().mockReturnValue("blob:frozen");
  URL.revokeObjectURL = vi.fn();
  vi.mocked(sampleEvaluationsApi.list).mockResolvedValue({ total: 2, items: [run(), run(second)] });
  vi.mocked(sampleEvaluationsApi.get).mockImplementation(async (_p, value) => run(value));
  vi.mocked(sampleEvaluationsApi.report).mockImplementation(async (_p, value, attemptId) =>
    report(value, attemptId),
  );
  vi.mocked(sampleEvaluationsApi.samples).mockImplementation(
    async (_p, value, attemptId, filters) => ({
      evaluation_id: value,
      attempt_id: attemptId,
      spec_digest: hash,
      report_sha256: hash,
      total: 13,
      offset: filters.offset ?? 0,
      limit: filters.limit ?? 20,
      items: report(value, attemptId).samples.slice(
        filters.offset ?? 0,
        (filters.offset ?? 0) + (filters.limit ?? 20),
      ),
    }),
  );
  vi.mocked(sampleEvaluationsApi.image).mockResolvedValue(
    new Blob(["image"], { type: "image/png" }),
  );
  vi.mocked(sampleEvaluationsApi.source).mockResolvedValue(source());
  vi.mocked(sampleEvaluationsApi.compare).mockResolvedValue(comparison());
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
});

describe("frozen evaluation evidence navigation", () => {
  it("shows original-pixel mask metrics, matched Dice and area groups separately from FN", async () => {
    const pixel = {
      index: 0,
      class_index: 0,
      rle: { encoding: "coco_rle" as const, size: [3, 3] as [number, number], counts: [0, 9] },
      area_pixels: 9,
      mask_sha256: hash,
    };
    const maskSample: EvaluationSampleResult = {
      ...sample(),
      width: 3,
      height: 3,
      truth: [
        { ...pixel, annotation_id: id(101), annotation_version: 1, rasterizer: "native-rle-1" },
      ],
      predictions: [{ ...pixel, native_class_index: 0, score: 0.9 }],
      matching: {
        ...sample().matching!,
        diagnostics: [],
        matches: [{ prediction_index: 0, truth_index: 0, iou: 1, dice: 1 }],
      },
    };
    const maskAggregate = {
      ...aggregate,
      matched_mean_iou: 1,
      matched_mean_dice: 1,
      area_groups: ["small", "medium", "large"].map((group) => ({
        group: group as "small" | "medium" | "large",
        tp: group === "small" ? 1 : 0,
        fp: 0,
        fn: 0,
        precision: group === "small" ? 1 : null,
        recall: group === "small" ? 1 : null,
        f1: group === "small" ? 1 : null,
      })),
    };
    const maskRun = run();
    maskRun.spec.schema = 2;
    maskRun.spec.model_ref.task = "segment";
    vi.mocked(sampleEvaluationsApi.get).mockResolvedValue(maskRun);
    vi.mocked(sampleEvaluationsApi.report).mockResolvedValue({
      ...report(),
      schema: 2,
      samples: [maskSample],
      aggregate: maskAggregate,
    });
    vi.mocked(sampleEvaluationsApi.samples).mockResolvedValue({
      evaluation_id: evaluation,
      attempt_id: attempt,
      spec_digest: hash,
      report_sha256: hash,
      total: 1,
      offset: 0,
      limit: 12,
      items: [maskSample],
    });
    vi.mocked(sampleEvaluationsApi.image).mockReturnValue(new Promise(() => {}));
    mount();
    expect(
      await screen.findByRole("table", { name: "按原图掩码像素面积统计" }),
    ).toBeInTheDocument();
    expect(screen.getByText("已匹配 Dice")).toBeInTheDocument();
    expect(screen.getByText(/TP → 真值 #0，Mask IoU 1.000，Dice 1.000/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "真值掩码，蓝色" })).toBeInTheDocument();
    expect(screen.getAllByText(/漏检另计 FN/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/不是 AP\/mAP/).length).toBeGreaterThan(0);
  });
  it("rejects malformed, duplicate and reversed URL filters without silently replacing them", () => {
    for (const query of [
      "attempt=bad",
      `attempt=${attempt}&attempt=${historical}`,
      "sample=32",
      "page=-1",
      "class=NaN",
      "confidence_min=0.9&confidence_max=0.2",
      "error=unknown",
      "confidence_min=",
    ])
      expect(parseEvaluationState(new URLSearchParams(query)).error).toBeTruthy();
    expect(
      parseEvaluationState(new URLSearchParams(`attempt=${attempt}&page=1&error=false_negative`)),
    ).toMatchObject({
      attemptId: attempt,
      page: 1,
      error: "",
      filters: { offset: 20, limit: 20, error: "false_negative" },
    });
    expect(
      sourceWorkbenchUrl(
        source({ source: "deleted", task_id: null }),
        project,
        id(100),
        "/history",
      ),
    ).toBeNull();
    expect(
      sourceWorkbenchUrl(source({ project_id: id(77) }), project, id(100), "/history"),
    ).toBeNull();
    const url = sourceWorkbenchUrl(source(), project, id(100), "/history?attempt=fixed")!;
    expect(url).toContain(`task=${id(100)}`);
    expect(url).toContain("returnTo=");
    expect(url).not.toContain("annotationId");
  });

  it("puts the first member of a filtered page in the URL and aligns the second candidate to that member", async () => {
    mount(`attempt=${attempt}&page=1&page_size=10&second=${second}`);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("sample=10"));
    expect(await screen.findByRole("heading", { name: "样本 11" })).toBeInTheDocument();
    const aligned = await screen.findByRole("link", { name: /同一冻结成员/ });
    expect(aligned.getAttribute("href")).toContain("sample=2");
    expect(sampleEvaluationsApi.samples).toHaveBeenCalledWith(
      project,
      evaluation,
      attempt,
      expect.objectContaining({ offset: 10, limit: 10 }),
      { signal: expect.any(AbortSignal) },
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "预测，虚线" }));
    expect(document.querySelectorAll('[data-layer="prediction"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-layer="truth"]')).toHaveLength(1);
  });

  it("never substitutes an unknown attempt and rejects a report returned for another identity", async () => {
    const view = mount(`attempt=${id(88)}`);
    expect(await screen.findByText(/所选尝试不属于此评估/)).toBeInTheDocument();
    expect(sampleEvaluationsApi.report).not.toHaveBeenCalled();
    view.unmount();
    vi.mocked(sampleEvaluationsApi.report).mockResolvedValue(report(second));
    mount();
    expect(await screen.findByText(/评估报告身份不符/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "逐样本误差" })).not.toBeInTheDocument();
  });

  it("shows current evidence changes before opening the exact current task with a return link", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "核对当前标注任务" }));
    expect(await screen.findByText(/当前任务与冻结证据不同/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开当前任务" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(`task=${id(100)}`),
    );
    expect(screen.getByTestId("location").textContent).toContain("returnTo=");
    expect(screen.getByTestId("location").textContent).not.toContain("annotationId");
  });

  it("does not open deleted sources or let a closed dialog's late source response reopen it", async () => {
    let resolve!: (data: EvaluationSourceOut) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(sampleEvaluationsApi.source)
      .mockImplementationOnce((_p, _e, _a, _o, options) => {
        signal = options?.signal as AbortSignal;
        return new Promise((yes) => {
          resolve = yes;
        });
      })
      .mockResolvedValue(source({ source: "deleted", task_id: null, project_id: null }));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "核对当前标注任务" }));
    await waitFor(() => expect(signal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "核对当前标注任务" })).toHaveFocus(),
    );
    fireEvent.click(screen.getByRole("button", { name: "核对当前标注任务" }));
    expect(await screen.findByText(/源任务已删除或移出项目/)).toBeInTheDocument();
    await act(async () => resolve(source()));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByRole("button", { name: "打开当前任务" })).not.toBeInTheDocument();
  });

  it("fences a batched account ABA before React commits, aborts old image fetches, and revokes blob URLs", async () => {
    const owner = renderHook(() => useTrainingViewOwner(project, "view"));
    const oldAssert = owner.result.current.assert;
    act(() => {
      useAuthStore.getState().setAuth("session-b", { id: id(91) } as never);
      useAuthStore.getState().setAuth("session-a", { id: id(90) } as never);
    });
    expect(() => oldAssert()).toThrow(/会话已变化/);
    expect(() => owner.result.current.assert()).not.toThrow();
    let resolve!: (blob: Blob) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(sampleEvaluationsApi.image).mockImplementationOnce((_p, _e, _a, _o, options) => {
      signal = options?.signal as AbortSignal;
      return new Promise((yes) => {
        resolve = yes;
      });
    });
    const view = mount();
    await waitFor(() => expect(signal).toBeDefined());
    act(() => {
      useAuthStore.getState().setAuth("session-b", { id: id(91) } as never);
      useAuthStore.getState().setAuth("session-a", { id: id(90) } as never);
    });
    await screen.findByRole("img", { name: "冻结样本 1" });
    await act(async () => resolve(new Blob(["old"])));
    expect(signal?.aborted).toBe(true);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:frozen");
  });

  it("preserves a later attempt selection through a late retry result, including URL ABA", async () => {
    let resolve!: (data: SampleEvaluationOut) => void;
    let current = { ...run(), status: "failed" as const };
    vi.mocked(sampleEvaluationsApi.get).mockImplementation(async () => current);
    vi.mocked(sampleEvaluationsApi.retry).mockImplementation(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "恢复未完成成员" }));
    await waitFor(() => expect(sampleEvaluationsApi.retry).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByRole("combobox", { name: "查看执行尝试" }), {
      target: { value: historical },
    });
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(`attempt=${historical}`),
    );
    fireEvent.change(screen.getByRole("combobox", { name: "查看执行尝试" }), {
      target: { value: attempt },
    });
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(`attempt=${attempt}`),
    );
    current = { ...run(evaluation, nextAttempt), status: "failed" };
    await act(async () => resolve(current));
    await waitFor(() => expect(sampleEvaluationsApi.get).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("location")).toHaveTextContent(`attempt=${attempt}`);
    expect(screen.getByTestId("location").textContent).not.toContain(`attempt=${nextAttempt}`);
  });

  it("hides cached comparison deltas when the second candidate loses access", async () => {
    const view = mount(`attempt=${attempt}&sample=0&second=${second}`);
    expect(await screen.findByRole("columnheader", { name: "第二减当前" })).toBeInTheDocument();
    vi.mocked(sampleEvaluationsApi.get).mockImplementation(async (_p, value) => {
      if (value === second) throw new Error("候选访问已撤销");
      return run(value);
    });
    await act(async () => {
      await view.client.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "sample-evaluation" && query.queryKey.includes(second),
      });
    });
    expect(await screen.findByText(/第二候选不可用/)).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "第二减当前" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /同一冻结成员/ })).not.toBeInTheDocument();
  });
  it("cancels candidate comparison without changing the current attempt or sample", async () => {
    mount(`attempt=${attempt}&sample=0&second=${second}`);
    expect(await screen.findByRole("columnheader", { name: "第二减当前" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消评估比较" }));
    expect(screen.getByLabelText("第二个明确评估")).toHaveValue("");
    const query = new URLSearchParams(screen.getByTestId("location").textContent!.split("?")[1]);
    expect(query.get("second")).toBeNull();
    expect(query.get("attempt")).toBe(attempt);
    expect(query.get("sample")).toBe("0");
    expect(screen.queryByRole("columnheader", { name: "第二减当前" })).not.toBeInTheDocument();
  });
  it("keeps an invalid self comparison visible as the requested selection", async () => {
    mount(`attempt=${attempt}&second=${evaluation}`);
    await screen.findByText("须选择两个不同的评估。");
    expect(screen.getByLabelText("第二个明确评估")).toHaveValue(evaluation);
    expect(screen.getByLabelText("第二个明确评估")).toHaveDisplayValue(
      "冻结路口评估（不能与自身比较）",
    );
  });
  it("returns to the exact lineage route after opening a sample report", async () => {
    const target = `/projects/${project}/learning/lineage/evaluation/${evaluation}?relation=evaluations&cursor=owned`;
    mount(`attempt=${attempt}&returnTo=${encodeURIComponent(target)}`);
    expect(await screen.findByRole("link", { name: "返回来源与使用记录" })).toHaveAttribute(
      "href",
      target,
    );
  });
});
