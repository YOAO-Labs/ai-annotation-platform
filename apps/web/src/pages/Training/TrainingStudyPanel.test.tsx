import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import {
  trainingStudiesApi,
  type TrainingStudy,
  type TrainingStudyCreate as StudyBody,
} from "@/api/trainingStudies";
import type { TrainingRunCreate } from "@/api/trainingRuns";
import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useAuthStore } from "@/stores/authStore";
import { TrainingStudyCreate } from "./TrainingStudyCreate";
import { TrainingStudyPanel } from "./TrainingStudyPanel";

vi.mock("@/api/trainingStudies", () => ({
  trainingStudiesApi: {
    preview: vi.fn(),
    create: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    cancel: vi.fn(),
  },
}));
vi.mock("@/components/ui/decisionDialog", () => ({ confirmDialog: vi.fn() }));
const sha = "a".repeat(64);
const base: TrainingRunCreate = {
  name: "Pilot",
  dataset_id: crypto.randomUUID(),
  framework: "ultralytics",
  base_model: "yolo11n",
  request_id: crypto.randomUUID(),
  configuration: { schema_id: "schema", schema_digest: sha, values: { epochs: 2 } },
  preflight_digest: sha,
};
const schema = { fields: [{ key: "lr0", status: "editable" }] } as TrainingParameterSchema;
const onCreated = vi.fn();
const beforeStart = vi.fn(async () => true);
const clients: QueryClient[] = [];
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { resolve, promise };
}
function study(id = "study"): TrainingStudy {
  const trials = [1, 2].map((ordinal) => ({
    id: `trial-${ordinal}`,
    ordinal,
    values: { lr0: ordinal / 100 },
    config_digest: sha,
    config: {},
  }));
  return {
    id,
    project_id: "project",
    name: id,
    status: "active",
    scheduling: true,
    message: "按序放行",
    plan_digest: sha,
    plan: {
      study_id: id,
      policy_digest: sha,
      objective: "metrics/mAP50-95",
      direction: "maximize",
      planned_epochs: 4,
      budgets: { max_trials: 2, max_planned_epochs: 4, max_native_seconds: 20 },
      trials,
    },
    accounting: {
      consumed_seconds: 2,
      reserved_seconds: 0,
      remaining_seconds: 18,
      overrun_seconds: 0,
      unknown_attempt_ids: [],
    },
    trials: trials.map((trial) => ({
      ...trial,
      planned_epochs: 2,
      run_id: `run-${trial.ordinal}`,
      status: "succeeded",
      actual_epochs: 1,
      completion: "early_stopped",
      queue_seconds: 3,
      lease_seconds: 4,
      native_seconds: 1,
      native_time_unknown: false,
      result_error: null,
      result: {
        value: trial.ordinal / 10,
        compatibility_digest: `identity-${trial.ordinal}`,
        attempt_id: `attempt-${trial.ordinal}`,
        report_sha256: sha,
        weights_sha256: sha,
      },
    })),
    rankings: trials.map((trial) => ({
      compatibility_digest: `identity-${trial.ordinal}`,
      trial_ids: [trial.id],
    })),
  };
}
function Create({ initial }: { initial?: StudyBody }) {
  const [pending, setPending] = useState(initial);
  return (
    <TrainingStudyCreate
      projectId="project"
      schema={schema}
      base={base}
      pending={pending}
      onBusy={() => undefined}
      beforeStart={beforeStart}
      persist={(body) => {
        setPending(body);
        return true;
      }}
      onCreated={onCreated}
      onDiscard={() => setPending(undefined)}
    />
  );
}
function mount(content = <TrainingStudyPanel projectId="project" />, path = "/?study=study") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{content}</MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("study-token", { id: "study-owner" } as never);
  beforeStart.mockResolvedValue(true);
  vi.mocked(trainingStudiesApi.list).mockResolvedValue({
    total: 1,
    items: [
      {
        id: "study",
        name: "study",
        status: "active",
        scheduling: true,
        message: null,
        trial_count: 2,
      },
    ],
  });
  vi.mocked(trainingStudiesApi.get).mockResolvedValue(study());
  vi.mocked(trainingStudiesApi.preview).mockResolvedValue({
    plan: study().plan,
    preview_digest: sha,
  });
  vi.mocked(trainingStudiesApi.create).mockResolvedValue(study());
  vi.mocked(trainingStudiesApi.cancel).mockResolvedValue({
    ...study(),
    scheduling: false,
    status: "cancelled",
  });
  vi.mocked(confirmDialog).mockResolvedValue(true);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});
function budgets() {
  fireEvent.change(screen.getByLabelText("最多试验数"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("计划总轮数上限"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("原生计算秒数上限"), { target: { value: "20" } });
}

it("requires explicit budgets and invalidates a complete preview after edits", async () => {
  const user = userEvent.setup();
  mount(<Create />);
  await user.click(screen.getByRole("button", { name: "预览全部组合与预算" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("明确填写");
  expect(trainingStudiesApi.preview).not.toHaveBeenCalled();
  budgets();
  await user.click(screen.getByRole("button", { name: "预览全部组合与预算" }));
  expect(await screen.findByLabelText("搜索计划预览")).toHaveTextContent("共 2 个试验");
  expect(screen.getByRole("button", { name: "开始参数搜索" })).toBeEnabled();
  fireEvent.change(screen.getByLabelText("原生计算秒数上限"), { target: { value: "21" } });
  expect(screen.queryByLabelText("搜索计划预览")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始参数搜索" })).toBeDisabled();
});

it("retains a lost start request and retries the identical frozen body", async () => {
  const user = userEvent.setup();
  mount(<Create />);
  budgets();
  await user.click(screen.getByRole("button", { name: "预览全部组合与预算" }));
  await screen.findByLabelText("搜索计划预览");
  vi.mocked(trainingStudiesApi.create).mockRejectedValueOnce(new Error("响应中断"));
  await user.click(screen.getByRole("button", { name: "开始参数搜索" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("响应中断");
  expect(screen.getByLabelText("参数网格")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "重试并确认搜索创建" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith("study"));
  expect(vi.mocked(trainingStudiesApi.create).mock.calls[1]).toEqual(
    vi.mocked(trainingStudiesApi.create).mock.calls[0],
  );
  expect(vi.mocked(trainingStudiesApi.create).mock.calls[0][1].preview_digest).toBe(sha);
});

it("discards a preview response after the authenticated session changes", async () => {
  const waiting = deferred<Awaited<ReturnType<typeof trainingStudiesApi.preview>>>();
  vi.mocked(trainingStudiesApi.preview).mockReturnValueOnce(waiting.promise);
  const user = userEvent.setup();
  mount(<Create />);
  budgets();
  await user.click(screen.getByRole("button", { name: "预览全部组合与预算" }));
  await waitFor(() => expect(trainingStudiesApi.preview).toHaveBeenCalled());
  await act(async () => {
    useAuthStore.getState().setAuth("other-token", { id: "other-owner" } as never);
    waiting.resolve({ plan: study().plan, preview_digest: sha });
  });
  expect(screen.queryByLabelText("搜索计划预览")).not.toBeInTheDocument();
  expect(trainingStudiesApi.create).not.toHaveBeenCalled();
});

it("keeps incompatible evaluation groups separate and displays unknown accounting", async () => {
  const data = study();
  data.accounting.unknown_attempt_ids = ["lost-attempt"];
  data.trials[0].native_time_unknown = true;
  vi.mocked(trainingStudiesApi.get).mockResolvedValue(data);
  mount();
  await screen.findByLabelText("搜索详情");
  expect(screen.getByText("兼容评估组 1 · 组内排名")).toBeInTheDocument();
  expect(screen.getByText("兼容评估组 2 · 组内排名")).toBeInTheDocument();
  expect(screen.getByText(/不能按零结算/)).toBeInTheDocument();
  expect(screen.getByText(/原生提前停止.*原生时间 未知/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /比较组内/ })).not.toBeInTheDocument();
});

it("cancels the selected study and refreshes its ordinary children", async () => {
  const user = userEvent.setup();
  mount();
  await screen.findByLabelText("搜索详情");
  await user.click(screen.getByRole("button", { name: "停止搜索" }));
  await waitFor(() => expect(trainingStudiesApi.cancel).toHaveBeenCalledWith("project", "study"));
  expect(confirmDialog).toHaveBeenCalled();
});

it("does not cancel an old selection after its confirmation resolves", async () => {
  const confirmation = deferred<boolean>();
  vi.mocked(confirmDialog).mockReturnValueOnce(confirmation.promise);
  vi.mocked(trainingStudiesApi.list).mockResolvedValue({
    total: 2,
    items: [
      {
        id: "study",
        name: "study",
        status: "active",
        scheduling: true,
        message: null,
        trial_count: 2,
      },
      {
        id: "second",
        name: "second",
        status: "active",
        scheduling: true,
        message: null,
        trial_count: 2,
      },
    ],
  });
  vi.mocked(trainingStudiesApi.get).mockImplementation(async (_project, id) => study(id));
  const user = userEvent.setup();
  mount();
  await screen.findByLabelText("搜索详情");
  await user.click(screen.getByRole("button", { name: "停止搜索" }));
  await user.click(screen.getByRole("button", { name: "second · 进行中" }));
  await act(async () => confirmation.resolve(true));
  expect(trainingStudiesApi.cancel).not.toHaveBeenCalled();
});
