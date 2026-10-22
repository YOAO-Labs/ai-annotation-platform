import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { activeLearningApi, type ActiveRound } from "@/api/activeLearning";
import { useAuthStore } from "@/stores/authStore";
import { ActiveLearningRoundPage } from "./ActiveLearningRound";
const maskPreview = vi.hoisted(() => vi.fn());
vi.mock("./ActiveLearningMaskPreview", () => ({
  ActiveLearningMaskPreview: (props: unknown) => {
    maskPreview(props);
    return <p>冻结候选实例掩码</p>;
  },
}));
vi.mock("@/api/activeLearning", () => ({
  activeLearningApi: {
    get: vi.fn(),
    report: vi.fn(),
    candidates: vi.fn(),
    decisions: vi.fn(),
    feedback: vi.fn(),
    decide: vi.fn(),
    recordFeedback: vi.fn(),
    cancel: vi.fn(),
    retry: vi.fn(),
    image: vi.fn(),
  },
}));
const clients: QueryClient[] = [];
const features = {
  priority: null,
  low_confidence: null,
  ambiguity: null,
  detections: 0,
  possible_truncation: false,
};
const samples: ActiveRound["spec"]["samples"] = Array.from({ length: 6 }, (_, ordinal) => ({
  ordinal,
  task_id: `t${ordinal}`,
  arm: ordinal < 3 ? "active" : "random_control",
  sha256: "a".repeat(64),
  width: 16,
  height: 12,
  group_task_ids: [`t${ordinal}`],
}));
let round: ActiveRound;
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/p1/learning/active-learning/r1"]}>
        <Routes>
          <Route
            path="/projects/:id/learning/active-learning/:roundId"
            element={<ActiveLearningRoundPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  useAuthStore.getState().setAuth("round-token", { id: "u1" } as never);
  round = {
    id: "r1",
    project_id: "p1",
    name: "道路选样",
    status: "succeeded",
    spec_digest: "s",
    inference_dispatches: 6,
    accepted_tasks: 0,
    current_attempt_id: "a1",
    error_message: null,
    error_code: null,
    created_at: "2026-10-16T03:00:00Z",
    spec: {
      round_id: "r1",
      candidate_count: 6,
      pool_size: 6,
      seed: 42,
      confidence: 0.1,
      nms_iou: 0.7,
      batch_size: 2,
      max_inference_dispatches: 12,
      max_review_tasks: 4,
      model_ref: { sha256: "w" },
      profile_digest: "profile",
      samples,
      expires_at: Date.now() / 1000 + 600,
    },
    attempts: [
      { id: "a1", ordinal: 1, status: "succeeded", has_report: true, error_message: null },
    ],
  };
  vi.mocked(activeLearningApi.get).mockImplementation(async () => round);
  vi.mocked(activeLearningApi.report).mockResolvedValue({
    round_id: "r1",
    attempt_id: "a1",
    spec_digest: "s",
    report_digest: "report",
    missing_signal_count: 6,
    inference_dispatches: 6,
    ranking: samples.map((sample) => ({
      ...sample,
      rank: (sample.ordinal % 3) + 1,
      recommended: sample.ordinal % 3 < 2,
      reason: sample.arm === "active" ? "random_exploration" : "random_control",
      features,
      evidence_digest: "e",
    })),
  } as never);
  vi.mocked(activeLearningApi.candidates).mockResolvedValue({
    items: samples.map((sample) => ({
      ordinal: sample.ordinal,
      task_id: sample.task_id,
      status: "succeeded",
      disposition: null,
      has_image: true,
      decision_id: null,
      evidence: { features, predictions: [] },
    })),
  });
  vi.mocked(activeLearningApi.decisions).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(activeLearningApi.feedback).mockResolvedValue({ items: [], total: 0 });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  sessionStorage.clear();
  vi.unstubAllGlobals();
});
it("explains original-mask ambiguity and sends only focused frozen evidence to the mask preview", async () => {
  round.spec = { ...round.spec, schema: 2, task: "segment", model_ref: { task: "segment" } };
  const maskFeatures = {
    ...features,
    priority: 0.8,
    low_confidence: 0.3,
    ambiguity: 0.8,
    detections: 1,
    minimum_instance_pixels: 8,
    total_instance_pixels_including_overlap: 8,
    overlap_run_visits: 12,
  };
  const predictions = [
    {
      index: 0,
      class_index: 1,
      native_class_index: 1,
      score: 0.7,
      rle: {
        encoding: "coco_rle" as const,
        size: [12, 16] as [number, number],
        counts: [1, 8, 183],
      },
      mask_sha256: "b".repeat(64),
      area_pixels: 8,
    },
  ];
  vi.mocked(activeLearningApi.report).mockResolvedValue({
    round_id: "r1",
    attempt_id: "a1",
    spec_digest: "s",
    report_digest: "report",
    missing_signal_count: 1,
    inference_dispatches: 6,
    ranking: [
      {
        ...samples[0],
        rank: 1,
        recommended: true,
        reason: "overlapping_classes",
        features: maskFeatures,
        evidence_digest: "e",
      },
    ],
  });
  vi.mocked(activeLearningApi.candidates).mockResolvedValue({
    items: [
      {
        ordinal: 0,
        task_id: "t0",
        status: "succeeded",
        disposition: null,
        decision_id: null,
        has_image: true,
        evidence: { features: maskFeatures, predictions },
      },
    ],
  });
  vi.mocked(activeLearningApi.image).mockResolvedValue(new Blob(["frozen"], { type: "image/png" }));
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = vi.fn(() => "blob:frozen-mask");
      static revokeObjectURL = vi.fn();
    },
  );
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "#1 · 跨类别实例掩码重叠" }));
  expect(await screen.findByText("冻结候选实例掩码")).toBeVisible();
  expect(screen.getByText(/跨类掩码 IoU 0.800/)).toBeVisible();
  expect(screen.getByText(/最小实例 8 像素.*实例像素合计（含重叠）8/)).toBeVisible();
  expect(screen.getByText(/不使用像素概率，也不是 AP/)).toBeVisible();
  expect(screen.queryByText("查看原生检测框")).not.toBeInTheDocument();
  expect(maskPreview).toHaveBeenLastCalledWith(
    expect.objectContaining({ predictions, sampleUrl: "blob:frozen-mask", width: 16, height: 12 }),
  );
  expect(activeLearningApi.image).toHaveBeenCalledWith("p1", "r1", 0, expect.any(Object));
  fireEvent.click(screen.getByRole("checkbox", { name: "选择主动建议 1" }));
  expect(screen.getByRole("button", { name: "接纳并创建人工批次" })).toBeEnabled();
});
it("rejects segmentation evidence advertised as the legacy detection schema", async () => {
  round.spec = { ...round.spec, schema: 1, task: "segment", model_ref: { task: "segment" } };
  mount();
  expect(await screen.findByText("选样轮次任务或分割规格不受支持")).toBeVisible();
  expect(screen.queryByRole("button", { name: "接纳并创建人工批次" })).not.toBeInTheDocument();
});
it("withholds candidate geometry that contradicts the segmentation task", async () => {
  round.spec = { ...round.spec, schema: 2, task: "segment", model_ref: { task: "segment" } };
  vi.mocked(activeLearningApi.candidates).mockResolvedValue({
    items: [
      {
        ordinal: 0,
        task_id: "t0",
        status: "succeeded",
        disposition: null,
        decision_id: null,
        has_image: true,
        evidence: { features, predictions: [{ class_index: 0, score: 0.7, xyxy: [0, 0, 1, 1] }] },
      },
    ],
  });
  mount();
  expect(await screen.findByText("候选几何证据与本轮任务不一致")).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "选择主动建议 1" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "接纳并创建人工批次" })).toBeDisabled();
});
it.each(["detect", "segment"] as const)(
  "accepts %s subsets with exact decision replay after lost acknowledgement",
  async (task) => {
    if (task === "segment") round.spec = { ...round.spec, schema: 2, task, model_ref: { task } };
    vi.mocked(activeLearningApi.decide)
      .mockRejectedValueOnce(new Error("决策回执丢失"))
      .mockImplementation(
        async (_project, _round, body) => ({ id: "d1", request_id: body.request_id }) as never,
      );
    mount();
    fireEvent.click(await screen.findByRole("checkbox", { name: "选择主动建议 1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "选择随机对照 1" }));
    fireEvent.click(screen.getByRole("button", { name: "接纳并创建人工批次" }));
    await screen.findByText("决策回执丢失");
    const first = vi.mocked(activeLearningApi.decide).mock.calls[0][2];
    expect(first.ordinals).toEqual([0, 3]);
    fireEvent.click(screen.getByRole("button", { name: "重试同一决策" }));
    await waitFor(() => expect(vi.mocked(activeLearningApi.decide).mock.calls).toHaveLength(2));
    expect(vi.mocked(activeLearningApi.decide).mock.calls[1][2]).toEqual(first);
  },
);
it("prevents accepting backup candidates while allowing explicit rejection", async () => {
  vi.mocked(activeLearningApi.decide).mockImplementation(
    async (_project, _round, body) => ({ id: "d1", request_id: body.request_id }) as never,
  );
  mount();
  fireEvent.click(await screen.findByRole("checkbox", { name: "选择主动建议 3" }));
  expect(screen.getByRole("button", { name: "接纳并创建人工批次" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "拒绝所选候选" }));
  await waitFor(() =>
    expect(activeLearningApi.decide).toHaveBeenCalledWith(
      "p1",
      "r1",
      expect.objectContaining({ action: "reject", ordinals: [2] }),
    ),
  );
});
it("shows factual unequal review and unknown correction/effort without efficiency claims", async () => {
  round.accepted_tasks = 2;
  vi.mocked(activeLearningApi.decisions).mockResolvedValue({
    items: [
      {
        id: "d1",
        action: "accept",
        accepted_batch_id: "b1",
        snapshot: {
          batch: { id: "b1", name: "人工复核", display_id: "B-0002" },
          tasks: [{ task_id: "t0", arm: "active", ordinal: 0 }],
        },
      },
    ],
  } as never);
  vi.mocked(activeLearningApi.feedback).mockResolvedValue({
    items: [
      {
        id: "f1",
        snapshot: {
          observed_at: "2026-10-16T03:00:00Z",
          arms: ["active", "random_control"].map((arm, index) => ({
            arm,
            accepted_tasks: 1,
            planned_tasks: 2,
            reviewed_tasks: index ? 0 : 1,
            reviewer_rejection_count: 0,
            correction_count: null,
            effort: {
              annotate: { recorded_ms: null, coverage: "unknown" },
              review: { recorded_ms: null, coverage: "unknown" },
            },
          })),
          evaluations: {
            comparable: false,
            reasons: ["尚无可比后续评估"],
            references: [],
            delta_second_minus_first: null,
          },
          limitations: ["完整工时未知"],
          efficiency_claim: false,
        },
      },
    ],
  } as never);
  mount();
  await screen.findByRole("table");
  expect(screen.getAllByText("未知").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText(/未建立效率收益/)).toBeInTheDocument();
  expect(screen.getByText("尚无可比后续评估")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "B-0002 人工复核" })).toHaveAttribute(
    "href",
    "/projects/p1/settings?section=batches",
  );
});
it("withholds ranking when the report identity differs", async () => {
  vi.mocked(activeLearningApi.report).mockResolvedValue({
    round_id: "foreign",
    attempt_id: "a1",
    spec_digest: "s",
    ranking: [],
  } as never);
  mount();
  await screen.findByText(/选样报告身份不符/);
  expect(screen.queryByRole("button", { name: "接纳并创建人工批次" })).not.toBeInTheDocument();
});
