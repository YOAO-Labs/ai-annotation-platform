import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import type { StorageFaultOut } from "@/api/generated/types.gen";
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
const page = (items = [fault], total = items.length) => ({
  items,
  total,
  counts: { active: 1, recovered: 2 },
  observation_only: true,
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
  api.faults.mockResolvedValue(page([{ ...fault, source_name: "新位置来源" }]));
  view.refresh("next-location");
  await screen.findByText("新位置来源 · 原件读取");
  await act(async () => {
    finish(page());
  });
  expect(screen.queryByText("车辆图片 · 原件读取")).not.toBeInTheDocument();
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
