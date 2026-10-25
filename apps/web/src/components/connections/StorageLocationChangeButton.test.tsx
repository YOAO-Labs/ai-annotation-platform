import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  StorageLocationChangePreview,
  StorageLocationOut,
  StorageLocationUpdate,
} from "@/api/generated/types.gen";
import { StorageLocationChangeButton } from "./StorageLocationChangeButton";

const auth = vi.hoisted(() => ({ revision: 1 }));
const pushToast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (select: (state: { push: typeof pushToast }) => unknown) =>
    select({ push: pushToast }),
}));
const api = vi.hoisted(() => ({ post: vi.fn(), patch: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  apiClient: api,
}));
vi.mock("@/hooks/useSessionGuard", async (original) => ({
  ...(await original<typeof import("@/hooks/useSessionGuard")>()),
  useSessionRevision: () => auth.revision,
}));

const location: StorageLocationOut = {
  id: "location",
  name: "验收目录",
  kind: "filesystem",
  config_revision: 4,
  enabled: true,
  declared_capabilities: { reference: true },
};
const candidate: StorageLocationUpdate = {
  expected_revision: 4,
  scope: "restricted",
  use_user_ids: ["new-manager"],
};
function reviewed(
  overrides: Partial<StorageLocationChangePreview> = {},
): StorageLocationChangePreview {
  return {
    location_id: location.id,
    config_revision: 4,
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    impact_token: "reviewed-candidate",
    changes: [
      { field: "scope", before: "global", after: "restricted" },
      { field: "use_user_ids", before: ["old-manager"], after: ["new-manager"] },
    ],
    impact: {
      location_id: location.id,
      config_revision: 4,
      measured_at: new Date().toISOString(),
      scope: "stored_dependencies",
      frozen_coverage: "recorded_location_lineage_only",
      input_coverage: "current_task_links_only",
      execution_permission: false,
      counts: {
        sources: 23,
        enabled_sources: 21,
        unregistered_sources: 1,
        items: 5,
        reference_items: 3,
        datasets: 2,
        projects: 4,
        tasks: 7,
        annotations: 11,
        active_scans: 1,
        active_registrations: 2,
        enabled_schedules: 1,
        ready_previews: 3,
        failed_previews: 1,
        preview_bytes: 1024,
        cache_entries: 2,
        cache_bytes: 2048,
        active_cache_read_leases: 1,
        active_input_leases: 2,
        cleanup_input_leases: 1,
        ready_lineage_versions: 1,
        unfinished_lineage_versions: 1,
        target_records: 0,
        target_annotations: 0,
        target_datasets: 0,
        target_training: 0,
        target_media_cache: 0,
        target_audit_archive: 0,
        target_import: 0,
        target_export: 0,
        target_avatars: 0,
        target_bug_reports: 0,
        target_related_datasets: 0,
        target_related_projects: 0,
        target_related_tasks: 0,
        target_versions: 0,
        target_runs: 0,
        current_bindings: 0,
        unlocated_targets: 0,
      },
    },
    ...overrides,
  };
}
function mount() {
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const onDenied = vi.fn();
  let currentLocation = location;
  let payload = candidate;
  const tree = () => (
    <StorageLocationChangeButton
      location={currentLocation}
      payload={payload}
      label="预览变更"
      onApplied={onApplied}
      onDenied={onDenied}
    />
  );
  const view = render(tree());
  return {
    onApplied,
    onDenied,
    refresh: (nextPayload = payload, nextLocation = currentLocation) => {
      payload = nextPayload;
      currentLocation = nextLocation;
      view.rerender(tree());
    },
  };
}
async function preview() {
  fireEvent.click(screen.getByRole("button", { name: "预览变更" }));
  return screen.findByRole("dialog", { name: "确认位置配置变更" });
}
beforeEach(() => {
  vi.resetAllMocks();
  auth.revision = 1;
  api.post.mockImplementation(async () => reviewed());
  api.patch.mockResolvedValue(location);
});
afterEach(() => vi.useRealTimers());

it("reviews the exact candidate and record coverage without submitting before confirmation", async () => {
  mount();
  const dialog = await preview();
  expect(api.post).toHaveBeenCalledWith("/storage-locations/location/change-preview", candidate);
  expect(api.patch).not.toHaveBeenCalled();
  expect(within(dialog).getByText("所有数据管理员")).toBeInTheDocument();
  expect(within(dialog).getByText("仅指定的数据管理员")).toBeInTheDocument();
  expect(within(dialog).getByText(/新增 1 人，撤回 1 人/)).toBeInTheDocument();
  expect(within(dialog).getByText(/23 个来源.*7 个任务.*11 条标注/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByText("查看完整影响盘点与统计范围", { selector: "summary" }));
  expect(within(dialog).getByText(/托管和缺少位置血缘的历史未计入/)).toBeInTheDocument();
  expect(within(dialog).getByText(/零数量不能证明无引用/)).toBeInTheDocument();
});

it("cancels a review without applying and restores the trigger focus", async () => {
  mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "返回修改" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument(),
  );
  expect(api.patch).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "预览变更" })).toHaveFocus();
});

it("applies only the reviewed token and candidate and reports successful application once", async () => {
  const view = mount();
  const dialog = await preview();
  const confirm = within(dialog).getByRole("button", { name: "确认保存位置配置" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  await waitFor(() => expect(view.onApplied).toHaveBeenCalledTimes(1));
  expect(pushToast).toHaveBeenCalledWith({ msg: "位置配置已保存", kind: "success" });
  expect(api.patch).toHaveBeenCalledTimes(1);
  expect(api.patch).toHaveBeenCalledWith("/storage-locations/location", {
    ...candidate,
    impact_token: "reviewed-candidate",
  });
});

it("requires a fresh review after a rejected apply and sends its new token", async () => {
  mount();
  api.patch.mockRejectedValueOnce(Object.assign(new Error("关系已变化"), { status: 409 }));
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认保存位置配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("关系已变化");
  expect(screen.getByRole("button", { name: "确认保存位置配置" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("确认已失效");
  api.post.mockResolvedValueOnce(reviewed({ impact_token: "new-review" }));
  fireEvent.click(screen.getByRole("button", { name: "重新预览" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认保存位置配置" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认保存位置配置" }));
  await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
  expect(api.patch.mock.calls[1]).toEqual([
    "/storage-locations/location",
    { ...candidate, impact_token: "new-review" },
  ]);
});

it("expires the review while the dialog is open and blocks application", async () => {
  vi.useFakeTimers();
  mount();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "预览变更" })));
  expect(screen.getByRole("button", { name: "确认保存位置配置" })).toBeEnabled();
  await act(async () => vi.advanceTimersByTimeAsync(300_000));
  expect(screen.getByRole("status")).toHaveTextContent("预览已过期");
  fireEvent.click(screen.getByRole("button", { name: "确认保存位置配置" }));
  expect(api.patch).not.toHaveBeenCalled();
});

it("drops a pending review when the candidate changes", async () => {
  let complete!: (value: StorageLocationChangePreview) => void;
  api.post.mockReturnValueOnce(
    new Promise<StorageLocationChangePreview>((resolve) => {
      complete = resolve;
    }),
  );
  const view = mount();
  fireEvent.click(screen.getByRole("button", { name: "预览变更" }));
  view.refresh({ ...candidate, scope: "global" });
  await act(async () => complete(reviewed()));
  expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
  expect(api.patch).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "预览变更" })).toBeEnabled();
});

it.each(["location", "revision", "session"])(
  "retires an open confirmation after a %s transition",
  async (transition) => {
    const view = mount();
    await preview();
    if (transition === "location") view.refresh(candidate, { ...location, id: "another" });
    else if (transition === "revision")
      view.refresh({ ...candidate, expected_revision: 5 }, { ...location, config_revision: 5 });
    else {
      auth.revision += 1;
      view.refresh();
    }
    expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
    expect(api.patch).not.toHaveBeenCalled();
  },
);

it("does not acknowledge an old session's pending application", async () => {
  let complete!: (value: StorageLocationOut) => void;
  api.patch.mockReturnValueOnce(
    new Promise<StorageLocationOut>((resolve) => {
      complete = resolve;
    }),
  );
  const view = mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认保存位置配置" }));
  auth.revision += 1;
  view.refresh();
  await act(async () => complete(location));
  expect(view.onApplied).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
});

it("removes reviewed evidence and reports denial if preview permission is revoked", async () => {
  const view = mount();
  await preview();
  api.post.mockRejectedValueOnce(Object.assign(new Error("访问权限已撤销"), { status: 403 }));
  fireEvent.click(screen.getByRole("button", { name: "重新预览" }));
  await waitFor(() => expect(view.onDenied).toHaveBeenCalledOnce());
  expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
  expect(screen.queryByText(/23 个来源/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "预览变更" })).toBeDisabled();
  expect(api.patch).not.toHaveBeenCalled();
});

it("rejects a response for another location instead of offering its token", async () => {
  api.post.mockResolvedValueOnce(reviewed({ location_id: "another" }));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "预览变更" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("位置配置已变化");
  expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
  expect(api.patch).not.toHaveBeenCalled();
});

it("rejects a review with an invalid expiry without offering confirmation", async () => {
  api.post.mockResolvedValueOnce(reviewed({ expires_at: "invalid-date" }));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "预览变更" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
  expect(api.patch).not.toHaveBeenCalled();
});

it("reports saved configuration separately when the follow-up refresh fails", async () => {
  const view = mount();
  view.onApplied.mockRejectedValueOnce(new Error("刷新服务暂不可用"));
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认保存位置配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/已保存.*刷新/);
  expect(api.patch).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("dialog", { name: "确认位置配置变更" })).not.toBeInTheDocument();
  expect(screen.queryByText("确认已失效，请重新预览后再保存。")).not.toBeInTheDocument();
});
