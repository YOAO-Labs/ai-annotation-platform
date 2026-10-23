import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { DatasetLifecyclePreview, DatasetResponse } from "@/api/datasets";
import { DatasetLifecycleAction } from "./DatasetLifecycleAction";
const auth = vi.hoisted(() => ({ revision: 1 }));
const api = vi.hoisted(() => ({ previewLifecycle: vi.fn(), setLifecycle: vi.fn() }));
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/api/datasets", () => ({ datasetsApi: api }));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (select: (state: { push: typeof toast }) => unknown) => select({ push: toast }),
}));
vi.mock("@/hooks/useSessionGuard", async (original) => ({
  ...(await original<typeof import("@/hooks/useSessionGuard")>()),
  useSessionRevision: () => auth.revision,
}));
const dataset = {
  id: "dataset",
  name: "保留目录",
  lifecycle_revision: 2,
  archived_at: null,
} as DatasetResponse;
function reviewed(overrides: Partial<DatasetLifecyclePreview> = {}): DatasetLifecyclePreview {
  return {
    dataset_id: dataset.id,
    lifecycle_revision: 2,
    state: "active",
    operation: "archive",
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
    impact_token: "review-token",
    frozen_coverage: "recorded_dataset_membership_only",
    counts: {
      items: 5,
      sources: 2,
      projects: 1,
      tasks: 7,
      annotations: 9,
      active_scans: 1,
      active_registrations: 2,
      enabled_schedules: 3,
      pending_jobs: 4,
      ready_lineage_versions: 1,
      unfinished_lineage_versions: 2,
    },
    ...overrides,
  };
}
function mount() {
  let value = dataset;
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const tree = () => <DatasetLifecycleAction dataset={value} onApplied={onApplied} />;
  const view = render(tree());
  return {
    onApplied,
    update: (next = value) => {
      value = next;
      view.rerender(tree());
    },
  };
}
async function preview() {
  fireEvent.click(screen.getByRole("button", { name: "归档数据集" }));
  return screen.findByRole("dialog", { name: "归档数据集前确认" });
}
beforeEach(() => {
  vi.clearAllMocks();
  auth.revision = 1;
  api.previewLifecycle.mockResolvedValue(reviewed());
  api.setLifecycle.mockResolvedValue({
    dataset_id: dataset.id,
    lifecycle_revision: 3,
    state: "archived",
    archived_at: new Date().toISOString(),
    archived_by: "owner",
  });
});
afterEach(() => {
  vi.useRealTimers();
});
it("previews the exact operation and preserved dependency scope without applying", async () => {
  mount();
  const dialog = await preview();
  expect(api.previewLifecycle).toHaveBeenCalledWith("dataset", {
    operation: "archive",
    expected_revision: 2,
  });
  expect(api.setLifecycle).not.toHaveBeenCalled();
  expect(dialog).toHaveTextContent("5 个样本 · 2 个来源");
  expect(dialog).toHaveTextContent("缺少证据的历史未计入");
  expect(dialog).toHaveTextContent("不会删除或改写源文件");
});
it("cancels without applying and restores trigger focus", async () => {
  mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(api.setLifecycle).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "归档数据集" })).toHaveFocus();
});
it("applies the reviewed token once and reports successful application", async () => {
  const view = mount();
  const dialog = await preview();
  const confirm = within(dialog).getByRole("button", { name: "确认归档数据集" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  await waitFor(() => expect(view.onApplied).toHaveBeenCalledTimes(1));
  expect(api.setLifecycle).toHaveBeenCalledTimes(1);
  expect(api.setLifecycle).toHaveBeenCalledWith("dataset", {
    operation: "archive",
    expected_revision: 2,
    impact_token: "review-token",
  });
  expect(toast).toHaveBeenCalledWith({ msg: "数据集已归档", kind: "success" });
});
it("requires a new review after a changed dependency rejection", async () => {
  mount();
  api.setLifecycle.mockRejectedValueOnce(new Error("关系已变化"));
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认归档数据集" }));
  await screen.findByText("关系已变化");
  expect(within(dialog).getByRole("button", { name: "确认归档数据集" })).toBeDisabled();
  api.previewLifecycle.mockResolvedValue(reviewed({ impact_token: "new-review" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "重新预览" }));
  await waitFor(() =>
    expect(within(dialog).getByRole("button", { name: "确认归档数据集" })).toBeEnabled(),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "确认归档数据集" }));
  await waitFor(() =>
    expect(api.setLifecycle).toHaveBeenLastCalledWith(
      "dataset",
      expect.objectContaining({ impact_token: "new-review" }),
    ),
  );
});
it("expires an open review and blocks application", async () => {
  vi.useFakeTimers();
  mount();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "归档数据集" })));
  expect(screen.getByRole("button", { name: "确认归档数据集" })).toBeEnabled();
  await act(async () => vi.advanceTimersByTimeAsync(300000));
  expect(screen.getByRole("button", { name: "确认归档数据集" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("预览已过期");
  expect(api.setLifecycle).not.toHaveBeenCalled();
});

it.each(["dataset", "revision", "session"])(
  "drops a late preview after %s changes",
  async (change) => {
    let resolve!: (data: DatasetLifecyclePreview) => void;
    api.previewLifecycle.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const view = mount();
    fireEvent.click(screen.getByRole("button", { name: "归档数据集" }));
    if (change === "session") {
      auth.revision += 1;
      view.update();
    } else
      view.update({
        ...dataset,
        ...(change === "dataset" ? { id: "new-dataset" } : { lifecycle_revision: 3 }),
      });
    await act(async () => resolve(reviewed()));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.setLifecycle).not.toHaveBeenCalled();
  },
);
it("does not acknowledge an old session's pending application", async () => {
  let resolve!: (value: unknown) => void;
  api.setLifecycle.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const view = mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认归档数据集" }));
  auth.revision += 1;
  view.update();
  await act(async () =>
    resolve({ dataset_id: dataset.id, lifecycle_revision: 3, state: "archived" }),
  );
  expect(view.onApplied).not.toHaveBeenCalled();
  expect(toast).not.toHaveBeenCalled();
});
it("hides a denied review and does not reuse the old confirmation", async () => {
  mount();
  const dialog = await preview();
  api.previewLifecycle.mockRejectedValueOnce(
    Object.assign(new Error("Forbidden"), { status: 403 }),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "重新预览" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "归档数据集" })).toBeDisabled();
  expect(api.setLifecycle).not.toHaveBeenCalled();
});
it.each([{ dataset_id: "other" }, { expires_at: "not-a-time" }, { operation: "restore" as const }])(
  "rejects mismatched or malformed preview %j",
  async (overrides) => {
    mount();
    api.previewLifecycle.mockResolvedValue(reviewed(overrides));
    fireEvent.click(screen.getByRole("button", { name: "归档数据集" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.setLifecycle).not.toHaveBeenCalled();
  },
);
it("restores only lifecycle and explains that revoked rights and jobs remain stopped", async () => {
  const view = mount();
  view.update({ ...dataset, archived_at: new Date().toISOString(), lifecycle_revision: 4 });
  api.previewLifecycle.mockResolvedValue(
    reviewed({ operation: "restore", state: "archived", lifecycle_revision: 4 }),
  );
  api.setLifecycle.mockResolvedValue({
    dataset_id: dataset.id,
    lifecycle_revision: 5,
    state: "active",
    archived_at: null,
    archived_by: null,
  });
  fireEvent.click(screen.getByRole("button", { name: "恢复数据集" }));
  const dialog = await screen.findByRole("dialog", { name: "恢复数据集前确认" });
  expect(dialog).toHaveTextContent("不恢复已撤销的人员权限");
  expect(dialog).toHaveTextContent("不自动启用来源、同步计划或重启归档前的后台任务");
  fireEvent.click(within(dialog).getByRole("button", { name: "确认恢复数据集" }));
  await waitFor(() => expect(view.onApplied).toHaveBeenCalledTimes(1));
  expect(api.setLifecycle).toHaveBeenCalledWith(
    dataset.id,
    expect.objectContaining({ operation: "restore", expected_revision: 4 }),
  );
});
it("reports saved lifecycle separately if refreshing its owner fails", async () => {
  const view = mount();
  view.onApplied.mockRejectedValueOnce(new Error("刷新断开"));
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认归档数据集" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("操作已提交，但刷新失败");
  expect(api.setLifecycle).toHaveBeenCalledTimes(1);
});
