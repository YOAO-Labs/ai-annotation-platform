import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import type { StorageFaultOut, StorageObservationRates } from "@/api/generated/types.gen";
import { StorageFaultsPanel } from "./StorageFaultsPanel";

const owner = vi.hoisted(() => ({ role: "super_admin", revision: 1, session: "session" }));
const api = vi.hoisted(() => ({ faults: vi.fn() }));
vi.mock("@/api/storage", () => ({ storageApi: api }));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: owner.role }) }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionRevision: () => owner.revision }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (value: unknown) => unknown) =>
    selector({ user: { id: "actor" }, sessionId: owner.session }),
}));

const fault: StorageFaultOut = {
  id: "fault",
  location_id: "location",
  source_id: "source",
  dataset_id: "dataset",
  item_id: null,
  asset_id: null,
  task_id: null,
  run_id: "run",
  job_id: "job",
  registration_id: null,
  source_name: "车辆图片",
  node_id: "node",
  operation: "reference_read",
  category: "root_unavailable",
  state: "active",
  first_failed_at: "2026-10-25T01:00:00Z",
  last_failed_at: "2026-10-25T01:01:00Z",
  recovered_at: null,
  failure_count: 3,
  scope_status: "current",
  responsible_role: "platform_admin",
  responsible_user_id: null,
};
const rates = (): StorageObservationRates => ({
  measured_at: "2026-10-26T02:30:00Z",
  window_start: "2026-10-26T00:00:00Z",
  window_end: "2026-10-26T02:30:00Z",
  timezone: "UTC",
  unit: "attempt",
  coverage: "recorded_attempts_only",
  original_read: { success_count: 7, failure_count: 1, total_count: 8, failure_rate: 0.125 },
  preview_generate: { success_count: 1, failure_count: 3, total_count: 4, failure_rate: 0.75 },
});
const page = (items = [fault], total = items.length) => ({
  items,
  total,
  counts: { active: 1, recovered: 2 },
  observation_only: true,
  rates: rates(),
});
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
beforeEach(() => {
  vi.clearAllMocks();
  owner.role = "super_admin";
  owner.revision = 1;
  owner.session = "session";
  api.faults.mockResolvedValue(page());
});

function mount(locationId = "location") {
  const client = createTestQueryClient();
  clients.push(client);
  const onJob = vi.fn();
  const tree = (id: string) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <StorageFaultsPanel locationId={id} returnTo="/datasets?page=2" onJob={onJob} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(locationId));
  return { client, onJob, refresh: (id = locationId) => view.rerender(tree(id)) };
}

it("shows retained failure count, scope and responsible role without claiming live health", async () => {
  mount();
  await screen.findByText("根目录不可用");
  expect(screen.getByText("车辆图片 · 原件读取")).toBeInTheDocument();
  expect(screen.getByText(/已接收失败观测 3 次/)).toHaveTextContent("保存的观测范围匹配");
  expect(screen.getByText(/处理方：平台管理员/)).toHaveTextContent("未指定个人");
  expect(screen.getByText(/没有记录不代表实时健康/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "未恢复", pressed: true })).toBeInTheDocument();
  expect(api.faults).toHaveBeenCalledWith("location", "active", 0, expect.any(AbortSignal));
});

it("pages the selected status and resets pagination when the status changes", async () => {
  api.faults.mockResolvedValue(page([fault], 21));
  mount();
  await screen.findByText("根目录不可用");
  fireEvent.click(screen.getByRole("button", { name: "下一页故障" }));
  await waitFor(() =>
    expect(api.faults).toHaveBeenCalledWith("location", "active", 20, expect.any(AbortSignal)),
  );
  const recovered = { ...fault, state: "recovered" as const, recovered_at: "2026-10-25T01:02:00Z" };
  api.faults.mockResolvedValue(page([recovered]));
  fireEvent.click(screen.getByRole("button", { name: "已恢复" }));
  await screen.findByText("恢复观测");
  expect(api.faults).toHaveBeenLastCalledWith("location", "recovered", 0, expect.any(AbortSignal));
  expect(screen.getByText("未恢复 1 · 已恢复 2")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "上一页故障" })).toBeDisabled();
  expect(screen.getByRole("group", { name: "原图不可读率" })).toHaveTextContent("12.5%");
  expect(screen.getByRole("group", { name: "预览失败率" })).toHaveTextContent(
    "成功 1 · 失败 3 · 分母 4",
  );
});

it("preserves source return navigation and opens the existing job with its focus trigger", async () => {
  const view = mount();
  await screen.findByText("根目录不可用");
  expect(screen.getByRole("link", { name: "查看来源" })).toHaveAttribute(
    "href",
    "/datasets/dataset?tab=sources&source=source&return=%2Fdatasets%3Fpage%3D2",
  );
  expect(screen.getByRole("link", { name: "查看故障转换记录" })).toHaveAttribute(
    "href",
    "/audit?target_type=storage_location&detail_key=fault_id&detail_value=fault",
  );
  const trigger = screen.getByRole("button", { name: "故障任务详情" });
  fireEvent.click(trigger);
  expect(view.onJob).toHaveBeenCalledWith("job", trigger);
});

it("hides retained evidence when the current scope is revoked", async () => {
  const { client } = mount();
  await screen.findByText("根目录不可用");
  api.faults.mockRejectedValue({ status: 403 });
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["storage-faults"] });
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("无权查看");
  expect(screen.queryByText("车辆图片 · 原件读取")).not.toBeInTheDocument();
  expect(screen.queryByText("未恢复 1 · 已恢复 2")).not.toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "原图不可读率" })).not.toBeInTheDocument();
});

it("keeps prior observations clearly labelled after a transient query failure", async () => {
  const { client } = mount();
  await screen.findByText("根目录不可用");
  api.faults.mockRejectedValue({ status: 503 });
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["storage-faults"] });
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("以下保留上次结果");
  expect(screen.getByText("车辆图片 · 原件读取")).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "原图不可读率" })).toHaveTextContent("12.5%");
  expect(screen.getByText("服务器观测于 2026-10-26 02:30:00 UTC。")).toBeInTheDocument();
});

it("does not show a late response from another location", async () => {
  let finish!: (value: ReturnType<typeof page>) => void;
  api.faults.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const view = mount();
  await waitFor(() => expect(api.faults).toHaveBeenCalledTimes(1));
  const next = page([{ ...fault, source_name: "新位置来源" }]);
  next.rates.original_read = {
    success_count: 2,
    failure_count: 0,
    total_count: 2,
    failure_rate: 0,
  };
  api.faults.mockResolvedValue(next);
  view.refresh("next-location");
  await screen.findByText("新位置来源 · 原件读取");
  await act(async () => {
    finish(page());
  });
  expect(screen.queryByText("车辆图片 · 原件读取")).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "原图不可读率" })).toHaveTextContent(
    "成功 2 · 失败 0 · 分母 2",
  );
  expect(screen.queryByText("12.5%")).not.toBeInTheDocument();
});

it("resets the query owner on session and role changes", async () => {
  const view = mount();
  await screen.findByText("根目录不可用");
  owner.session = "next-session";
  owner.revision++;
  owner.role = "project_admin";
  api.faults.mockResolvedValue(page([]));
  view.refresh();
  await screen.findByText("暂无未恢复的故障观测。");
  expect(screen.queryByText("车辆图片 · 原件读取")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "查看故障转换记录" })).not.toBeInTheDocument();
});

it("uses historical and unknown evidence labels independently of recovery state", async () => {
  api.faults.mockResolvedValue(
    page([
      { ...fault, scope_status: "historical" },
      {
        ...fault,
        id: "other",
        source_name: "未确认来源",
        scope_status: "unknown",
        category: "preview_failed",
        responsible_role: "dataset_admin",
      },
    ]),
  );
  owner.role = "project_admin";
  mount();
  await screen.findByText(/历史配置或内容范围/);
  expect(screen.getByText(/当前范围尚未确认/)).toBeInTheDocument();
  expect(screen.getByText(/处理方：数据集管理员/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "查看故障转换记录" })).not.toBeInTheDocument();
});

it("does not query or display platform observations for non-managers", () => {
  owner.role = "annotator";
  mount();
  expect(screen.queryByRole("region", { name: "来源故障观测" })).not.toBeInTheDocument();
  expect(api.faults).not.toHaveBeenCalled();
});

it("separates the two attempt rates, their denominators and the server UTC window from fault state", async () => {
  mount();
  const original = await screen.findByRole("group", { name: "原图不可读率" });
  expect(original).toHaveTextContent("12.5%");
  expect(original).toHaveTextContent("成功 7 · 失败 1 · 分母 8");
  const preview = screen.getByRole("group", { name: "预览失败率" });
  expect(preview).toHaveTextContent("75%");
  expect(preview).toHaveTextContent("成功 1 · 失败 3 · 分母 4");
  expect(
    screen.getByText(/当日 UTC 窗口：2026-10-26 00:00:00 至 2026-10-26 02:30:00/),
  ).toBeInTheDocument();
  expect(screen.getByText("服务器观测于 2026-10-26 02:30:00 UTC。")).toBeInTheDocument();
  expect(screen.getByText(/未记录调用的覆盖未知/)).toHaveTextContent(
    "窗口可能包含旧配置，不代表当前健康",
  );
  expect(screen.getByText(/已接收失败观测 3 次/)).toBeInTheDocument();
});

it("shows zero percent only for recorded successful attempts with a positive denominator", async () => {
  const value = page();
  value.rates.original_read = {
    success_count: 4,
    failure_count: 0,
    total_count: 4,
    failure_rate: 0,
  };
  value.rates.preview_generate = {
    success_count: 0,
    failure_count: 0,
    total_count: 0,
    failure_rate: null,
  };
  api.faults.mockResolvedValue(value);
  mount();
  const original = await screen.findByRole("group", { name: "原图不可读率" });
  expect(within(original).getByText("0%")).toBeInTheDocument();
  const preview = screen.getByRole("group", { name: "预览失败率" });
  expect(preview).toHaveTextContent("暂无可计算观测");
  expect(preview).toHaveTextContent("成功 0 · 失败 0 · 分母 0");
  expect(within(preview).queryByText("0%")).not.toBeInTheDocument();
});

it("does not round a recorded rare failure or rare success to an absolute zero or hundred percent", async () => {
  const value = page();
  value.rates.original_read = {
    success_count: 99_999,
    failure_count: 1,
    total_count: 100_000,
    failure_rate: 0.00001,
  };
  value.rates.preview_generate = {
    success_count: 1,
    failure_count: 99_999,
    total_count: 100_000,
    failure_rate: 0.99999,
  };
  api.faults.mockResolvedValue(value);
  mount();
  expect(await screen.findByRole("group", { name: "原图不可读率" })).toHaveTextContent("<0.01%");
  expect(screen.getByRole("group", { name: "预览失败率" })).toHaveTextContent(">99.99%");
});

it.each([
  ["old response", undefined],
  ["null observation", null],
  ["invalid timestamp", { ...rates(), measured_at: "invalid" }],
  ["invalid calendar date", { ...rates(), measured_at: "2026-02-30T02:30:00Z" }],
  ["reversed window", { ...rates(), window_start: "2026-10-27T00:00:00Z" }],
  ["non-midnight start", { ...rates(), window_start: "2026-10-26T01:00:00Z" }],
  ["cross-day window", { ...rates(), window_start: "2026-10-25T00:00:00Z" }],
  ["inconsistent server time", { ...rates(), window_end: "2026-10-26T01:00:00Z" }],
  ["unknown coverage", { ...rates(), coverage: "all_requests" }],
  ["unknown unit", { ...rates(), unit: "file" }],
  ["unknown timezone", { ...rates(), timezone: "Asia/Shanghai" }],
])(
  "keeps both rates unknown for %s without hiding the existing fault list",
  async (_label, value) => {
    api.faults.mockResolvedValue({ ...page(), rates: value });
    mount();
    await screen.findByText("根目录不可用");
    for (const label of ["原图不可读率", "预览失败率"]) {
      const row = screen.getByRole("group", { name: label });
      expect(row).toHaveTextContent("未知");
      expect(row).not.toHaveTextContent(/%|分母/);
    }
    expect(screen.getByText(/窗口与比率观测未知/)).toBeInTheDocument();
  },
);

it.each([
  ["negative count", { success_count: -1, failure_count: 1, total_count: 0, failure_rate: null }],
  [
    "fractional count",
    { success_count: 0.5, failure_count: 0.5, total_count: 1, failure_rate: 0.5 },
  ],
  [
    "unsafe count",
    {
      success_count: Number.MAX_SAFE_INTEGER + 1,
      failure_count: 0,
      total_count: Number.MAX_SAFE_INTEGER + 1,
      failure_rate: 0,
    },
  ],
  [
    "inconsistent denominator",
    { success_count: 7, failure_count: 1, total_count: 9, failure_rate: 1 / 9 },
  ],
  [
    "inconsistent ratio",
    { success_count: 7, failure_count: 1, total_count: 8, failure_rate: 0.75 },
  ],
  [
    "missing positive-denominator rate",
    { success_count: 7, failure_count: 1, total_count: 8, failure_rate: null },
  ],
  [
    "zero-denominator ratio",
    { success_count: 0, failure_count: 0, total_count: 0, failure_rate: 0 },
  ],
  [
    "nonfinite ratio",
    { success_count: 7, failure_count: 1, total_count: 8, failure_rate: Number.NaN },
  ],
  [
    "nonzero ratio without failures",
    { success_count: 4, failure_count: 0, total_count: 4, failure_rate: 1e-10 },
  ],
])(
  "does not turn %s into a rate while retaining the other valid metric",
  async (_label, counts) => {
    api.faults.mockResolvedValue({ ...page(), rates: { ...rates(), original_read: counts } });
    mount();
    const original = await screen.findByRole("group", { name: "原图不可读率" });
    expect(original).toHaveTextContent("未知");
    expect(original).not.toHaveTextContent(/%|分母|NaN|Infinity/);
    expect(screen.getByRole("group", { name: "预览失败率" })).toHaveTextContent("75%");
  },
);

it("refreshes the saved attempt counts and server window even when the percentage stays unchanged", async () => {
  mount();
  await screen.findByText("根目录不可用");
  const next = page();
  next.rates.original_read = {
    success_count: 14,
    failure_count: 2,
    total_count: 16,
    failure_rate: 0.125,
  };
  next.rates.measured_at = next.rates.window_end = "2026-10-26T02:31:00Z";
  api.faults.mockResolvedValue(next);
  fireEvent.click(screen.getByRole("button", { name: "刷新故障观测" }));
  await waitFor(() =>
    expect(screen.getByRole("group", { name: "原图不可读率" })).toHaveTextContent(
      "成功 14 · 失败 2 · 分母 16",
    ),
  );
  expect(screen.getByRole("group", { name: "原图不可读率" })).toHaveTextContent("12.5%");
  expect(screen.getByText("服务器观测于 2026-10-26 02:31:00 UTC。")).toBeInTheDocument();
});

it("does not publish late attempt rates after the session owner changes", async () => {
  let finish!: (value: ReturnType<typeof page>) => void;
  api.faults.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const view = mount();
  await waitFor(() => expect(api.faults).toHaveBeenCalledTimes(1));
  const next = page([]);
  next.rates.original_read = {
    success_count: 4,
    failure_count: 0,
    total_count: 4,
    failure_rate: 0,
  };
  owner.session = "next-session";
  owner.revision++;
  owner.role = "project_admin";
  api.faults.mockResolvedValue(next);
  view.refresh();
  await screen.findByText("暂无未恢复的故障观测。");
  await act(async () => {
    finish(page());
  });
  expect(screen.queryByText("12.5%")).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "原图不可读率" })).toHaveTextContent(
    "成功 4 · 失败 0 · 分母 4",
  );
});
