import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  BugAttachmentBindingPreview,
  ExportBindingPreview,
  ImportBindingPreview,
  DatasetBindingPreview,
  MediaCacheBindingPreview,
  AnnotationsBindingPreview,
  TrainingBindingPreview,
  StorageBindingOut,
} from "@/api/generated/types.gen";
import { ApiError } from "@/api/client";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { createTestQueryClient } from "@/test/queryClient";
import { StorageBindingChangeButton } from "./StorageBindingChangeButton";

type Purpose = ComponentProps<typeof StorageBindingChangeButton>["purpose"];

const pushToast = vi.hoisted(() => vi.fn());
const api = vi.hoisted(() => ({
  bindingCandidates: vi.fn(),
  previewBinding: vi.fn(),
  applyBinding: vi.fn(),
}));
vi.mock("@/api/storage", () => ({ storageApi: api }));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (select: (state: { push: typeof pushToast }) => unknown) =>
    select({ push: pushToast }),
}));
const binding: StorageBindingOut = {
  purpose: "bug-reports",
  location_id: "old-location",
  generation: 2,
  location_config_revision: 3,
  bucket: "old-bug-attachments-bucket",
  state: "configured",
  read_policy: "consumer_owned_compatibility",
  write_policy: "current_binding",
  change_policy: "reviewed_bug_attachment_switch",
};
const target = { location_id: "new-location", name: "新存储位置", config_revision: 7 };
function reviewed(
  overrides: Partial<BugAttachmentBindingPreview> = {},
): BugAttachmentBindingPreview {
  return {
    purpose: "bug-reports",
    previous: binding,
    target: {
      ...target,
      connection_revision: 4,
      bucket: "new-bug-attachments-bucket",
      key_prefix: "managed/bug-reports/",
    },
    history: { pending: 2, ready: 4, retired: 3 },
    capabilities: { conditional_write: true, full_read: true, delete: true },
    can_apply: true,
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    impact_token: "reviewed-bug-attachment-switch",
    ...overrides,
  };
}
const clients: QueryClient[] = [];
function mount(currentBinding = binding, purpose: Purpose = "bug-reports") {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const tree = () => (
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose={purpose}
        binding={currentBinding}
        onApplied={onApplied}
      />
    </QueryClientProvider>
  );
  const view = render(tree());
  return {
    client,
    onApplied,
    refresh: (next = currentBinding) => {
      currentBinding = next;
      view.rerender(tree());
    },
  };
}
async function open(label = "报告附件", dialogName = `切换${label}存储位置`) {
  fireEvent.click(screen.getByRole("button", { name: `切换${label}位置` }));
  const dialog = screen.getByRole("dialog", { name: dialogName });
  await within(dialog).findByRole("option", { name: target.name });
  return dialog;
}
function requestPreview(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
}
async function preview() {
  const dialog = await open();
  requestPreview(dialog);
  await within(dialog).findByRole("region", { name: "报告附件绑定变更预览" });
  return dialog;
}
async function previewPurpose(
  purpose: Purpose,
  currentBinding: StorageBindingOut,
  label: string,
  dialogName?: string,
) {
  const { onApplied } = mount(currentBinding, purpose);
  const dialog = await open(label, dialogName);
  await act(async () => requestPreview(dialog));
  return { dialog, onApplied };
}
beforeEach(() => {
  vi.resetAllMocks();
  seedAuthUser(createTestUser({ role: "super_admin" }));
  api.bindingCandidates.mockResolvedValue({ items: [target], total: 21 });
  api.previewBinding.mockImplementation(async () => reviewed());
  api.applyBinding.mockResolvedValue({
    ...binding,
    location_id: target.location_id,
    generation: 3,
  });
});
afterEach(() => {
  cleanup();
  resetAuthUser();
  clients.splice(0).forEach((client) => client.clear());
  vi.useRealTimers();
});

it("loads candidates only on opening and exposes the probe hint as a description, not an option", async () => {
  mount();
  expect(api.bindingCandidates).not.toHaveBeenCalled();
  const dialog = await open();
  expect(api.bindingCandidates).toHaveBeenCalledWith("bug-reports", 0, expect.any(AbortSignal), 20);
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveValue("");
  expect(select).toHaveAccessibleDescription(/实际写入、完整读回并删除/);
  expect(within(select).queryByText(/验证将/)).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
});

it("previews fixed mapping facts and applies only after explicit confirmation", async () => {
  const view = mount();
  const dialog = await preview();
  expect(api.previewBinding).toHaveBeenCalledWith("bug-reports", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(api.applyBinding).not.toHaveBeenCalled();
  expect(within(dialog).getByText(/待完成 2 · 可用 4 · 待清理 3/)).toBeInTheDocument();
  expect(within(dialog).getByText(/条件写入通过 · 完整读回通过 · 删除通过/)).toBeInTheDocument();
  expect(within(dialog).getByText(/用途代次 2 → 3/)).toBeInTheDocument();
  expect(within(dialog).getByText(/位置修订 7 · 连接修订 4/)).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "确认切换" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  await waitFor(() => expect(view.onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledTimes(1);
  expect(api.applyBinding).toHaveBeenCalledWith("bug-reports", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: "reviewed-bug-attachment-switch",
  });
  expect(pushToast).toHaveBeenCalledTimes(1);
});

it("preserves the selected candidate across server pages and page sizes", async () => {
  mount();
  const dialog = await open();
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  fireEvent.change(select, { target: { value: target.location_id } });
  api.bindingCandidates.mockResolvedValueOnce({
    items: [{ ...target, location_id: "other", name: "另一位置" }],
    total: 21,
  });
  const pagination = within(dialog).getByRole("navigation", { name: "候选位置分页" });
  fireEvent.click(within(pagination).getByRole("button", { name: "下一页" }));
  await within(dialog).findByRole("option", { name: "另一位置" });
  expect(api.bindingCandidates).toHaveBeenLastCalledWith(
    "bug-reports",
    20,
    expect.any(AbortSignal),
    20,
  );
  expect(select).toHaveValue(target.location_id);
  expect(within(select).getByRole("option", { name: target.name })).toBeInTheDocument();
  fireEvent.change(within(dialog).getByRole("combobox", { name: "每页候选位置数" }), {
    target: { value: "10" },
  });
  await waitFor(() =>
    expect(api.bindingCandidates).toHaveBeenLastCalledWith(
      "bug-reports",
      0,
      expect.any(AbortSignal),
      10,
    ),
  );
});

it("dismisses the dialog without applying and restores trigger focus", async () => {
  mount();
  const dialog = await preview();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "切换报告附件位置" })).toHaveFocus();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it("expires the preview and requires another remote verification", async () => {
  mount();
  await open();
  vi.useFakeTimers();
  fireEvent.change(screen.getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "验证并预览" })));
  expect(screen.getByRole("button", { name: "确认切换" })).toBeEnabled();
  await act(async () => vi.advanceTimersByTimeAsync(300_000));
  expect(screen.getByText("预览已过期，请重新预览。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it("restores focus to the refreshed trigger after the applied binding changes", async () => {
  const view = mount();
  view.onApplied.mockImplementation(async () => {
    view.refresh({ ...binding, location_id: target.location_id, generation: 3 });
  });
  await preview();
  fireEvent.click(screen.getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "切换报告附件位置" })).toHaveFocus(),
  );
});

it("drops an old review when the selected target changes", async () => {
  api.bindingCandidates.mockResolvedValue({
    items: [target, { ...target, location_id: "other", name: "另一位置" }],
    total: 2,
  });
  mount();
  const dialog = await preview();
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: "other" },
  });
  expect(
    within(dialog).queryByRole("region", { name: "报告附件绑定变更预览" }),
  ).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it("retires a pending old target response after selection changes", async () => {
  let complete!: (value: BugAttachmentBindingPreview) => void;
  api.bindingCandidates.mockResolvedValue({
    items: [target, { ...target, location_id: "other", name: "另一位置" }],
    total: 2,
  });
  api.previewBinding.mockReturnValueOnce(
    new Promise<BugAttachmentBindingPreview>((resolve) => {
      complete = resolve;
    }),
  );
  mount();
  const dialog = await open();
  requestPreview(dialog);
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: "other" },
  });
  await act(async () => complete(reviewed()));
  expect(
    within(dialog).queryByRole("region", { name: "报告附件绑定变更预览" }),
  ).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it.each([
  ["generation", { generation: 3 }],
  ["location", { location_id: "changed" }],
  ["revision", { location_config_revision: 4 }],
] as const)("retires a review after %s changes", async (_field, change) => {
  const view = mount();
  await preview();
  view.refresh({ ...binding, ...change });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it("retires a review after the session changes", async () => {
  mount();
  await preview();
  act(() => {
    seedAuthUser(createTestUser({ id: "another-admin", role: "super_admin" }));
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it("invalidates a review if candidate refresh reports a new target revision", async () => {
  mount();
  const dialog = await preview();
  api.bindingCandidates.mockResolvedValueOnce({
    items: [{ ...target, config_revision: 8 }],
    total: 1,
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "刷新位置" }));
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: "报告附件绑定变更预览" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("alert")).toHaveTextContent("目标位置配置已变化");
  expect(screen.getByRole("button", { name: "确认切换" })).toBeDisabled();
});

it("invalidates a rejected apply and permits a new reviewed token", async () => {
  api.applyBinding.mockRejectedValueOnce(
    new ApiError(409, "Conflict", { code: "storage_binding_preview_changed" }),
  );
  mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "用途绑定、目标配置或影响关系已变化",
  );
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  api.previewBinding.mockResolvedValueOnce(reviewed({ impact_token: "fresh" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "重新预览" }));
  await waitFor(() =>
    expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeEnabled(),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(api.applyBinding).toHaveBeenCalledTimes(2));
  expect(api.applyBinding.mock.calls[1][1].impact_token).toBe("fresh");
});

it("disables confirmation when the backend rejects the reviewed token", async () => {
  api.applyBinding.mockRejectedValueOnce(
    new ApiError(409, "Conflict", { code: "storage_binding_preview_invalid" }),
  );
  const { onApplied } = mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("确认凭证无效");
  const confirm = within(dialog).getByRole("button", { name: "确认切换" });
  expect(confirm).toBeDisabled();
  fireEvent.click(confirm);
  expect(api.applyBinding).toHaveBeenCalledTimes(1);
  expect(onApplied).not.toHaveBeenCalled();
  expect(pushToast).not.toHaveBeenCalled();
});

it("does not acknowledge an application completed for an old session", async () => {
  let complete!: () => void;
  api.applyBinding.mockReturnValueOnce(
    new Promise<void>((resolve) => {
      complete = resolve;
    }),
  );
  const view = mount();
  await preview();
  fireEvent.click(screen.getByRole("button", { name: "确认切换" }));
  act(() => resetAuthUser());
  await act(async () => complete());
  expect(pushToast).not.toHaveBeenCalled();
  expect(view.onApplied).not.toHaveBeenCalled();
});

it.each([
  ["another target", { target: { ...reviewed().target, location_id: "another-target" } }],
  ["another purpose", { purpose: "export" }],
  ["a previous binding for another purpose", { previous: { ...binding, purpose: "export" } }],
  ["an invalid expiry", { expires_at: "not-a-timestamp" }],
] as const)("rejects a preview with %s without opening confirmation", async (_case, patch) => {
  api.previewBinding.mockResolvedValueOnce({ ...reviewed(), ...patch });
  mount();
  const dialog = await open();
  requestPreview(dialog);
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).queryByRole("region")).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it("settles an in-flight apply after target refresh without trapping the dialog", async () => {
  let reject!: (error: unknown) => void;
  api.applyBinding.mockReturnValueOnce(
    new Promise((_resolve, fail) => {
      reject = fail;
    }),
  );
  const view = mount();
  const dialog = await preview();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeDisabled();
  api.bindingCandidates.mockResolvedValueOnce({
    items: [{ ...target, name: "刷新后的存储位置", config_revision: 8 }],
    total: 1,
  });
  await act(async () => view.client.invalidateQueries());
  await within(dialog).findByRole("option", { name: "刷新后的存储位置" });
  await act(async () =>
    reject(new ApiError(409, "Conflict", { code: "storage_binding_preview_changed" })),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(view.onApplied).not.toHaveBeenCalled();
});

function exportReview(): ExportBindingPreview {
  return {
    ...reviewed(),
    purpose: "export",
    previous: { ...binding, purpose: "export", change_policy: "reviewed_export_switch" },
    history: {
      zip_pending: 1,
      zip_ready: 2,
      zip_retired: 3,
      media_building: 4,
      media_ready: 5,
      media_retiring: 6,
      media_deleted: 7,
    },
    capabilities: {
      conditional_write: true,
      full_read: true,
      delete: true,
      list: true,
      list_versions: true,
    },
  };
}
it("reviews both export owners and listing capabilities before confirming the export route", async () => {
  const review = exportReview();
  api.previewBinding.mockResolvedValueOnce(review);
  const { dialog, onApplied } = await previewPurpose("export", review.previous, "导出");
  await within(dialog).findByRole("region", { name: "导出绑定变更预览" });
  expect(api.bindingCandidates).toHaveBeenCalledWith("export", 0, expect.any(AbortSignal), 20);
  expect(api.previewBinding).toHaveBeenCalledWith("export", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(within(dialog).getByText(/ZIP：待完成 1 · 可用 2 · 待清理 3/)).toBeInTheDocument();
  expect(
    within(dialog).getByText(/独立媒体包：生成中 4 · 可用 5 · 清理中 6 · 已清理 7/),
  ).toBeInTheDocument();
  expect(within(dialog).getByText(/配套 ZIP 使用媒体包分配时的位置/)).toBeInTheDocument();
  expect(within(dialog).getByText(/对象列举通过 · 版本列举通过/)).toBeInTheDocument();
  expect(
    within(dialog).getByRole("combobox", { name: "目标存储位置" }),
  ).toHaveAccessibleDescription(/外部端点须供浏览器及下载脚本访问/);
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("export", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: review.impact_token,
  });
});
it("resets an export target that disappears during verification", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(404, "Not found", { code: "export_binding_target_missing" }),
  );
  const { dialog } = await previewPurpose("export", exportReview().previous, "导出");
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标位置已不存在");
  expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

function importReview(): ImportBindingPreview {
  return {
    ...reviewed(),
    purpose: "import",
    previous: {
      ...binding,
      purpose: "import",
      bucket: "imports-a",
      change_policy: "reviewed_import_switch",
    },
    history: {
      lease_active: 1,
      lease_cleanup_pending: 2,
      lease_deleted: 3,
      creation_staging: 4,
      mask_uploads: 5,
      mask_batches: 6,
    },
    capabilities: {
      conditional_write: true,
      full_read: true,
      delete: true,
      list: true,
      list_versions: true,
    },
    impact_token: "reviewed-import-switch",
  };
}
it("reviews all import owners without promising staging deletion and applies the import route", async () => {
  const review = importReview();
  api.previewBinding.mockResolvedValueOnce(review);
  const { dialog, onApplied } = await previewPurpose(
    "import",
    review.previous,
    "导入暂存",
    "切换导入暂存位置",
  );
  await within(dialog).findByRole("region", { name: "导入暂存绑定变更预览" });
  expect(api.bindingCandidates).toHaveBeenCalledWith("import", 0, expect.any(AbortSignal), 20);
  expect(api.previewBinding).toHaveBeenCalledWith("import", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(
    within(dialog).getByText(/预标临时输入：活动 1 · 待清理 2 · 已清理 3/),
  ).toBeInTheDocument();
  expect(within(dialog).getByText("直传暂存：已分配 4")).toBeInTheDocument();
  expect(within(dialog).getByText(/格式导入：已分配上传 5 · 批次 6/)).toBeInTheDocument();
  expect(within(dialog).getByText(/直传和格式导入暂存不自动删除/)).toBeInTheDocument();
  expect(within(dialog).getByText(/最终原件位置不变/)).toBeInTheDocument();
  expect(
    within(dialog).getByRole("combobox", { name: "目标存储位置" }),
  ).toHaveAccessibleDescription(/外部端点须供浏览器与模型服务访问/);
  expect(api.applyBinding).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("import", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: "reviewed-import-switch",
  });
});
it("explains the input version requirement when an import probe rejects it", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(409, "Unavailable", { code: "import_storage_versioning_required" }),
  );
  const { dialog } = await previewPurpose(
    "import",
    importReview().previous,
    "导入暂存",
    "切换导入暂存位置",
  );
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("启用目标桶版本控制");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});
it("clears an import target that disappeared before its probe completed", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(404, "Missing", { code: "import_binding_target_missing" }),
  );
  const { dialog } = await previewPurpose(
    "import",
    importReview().previous,
    "导入暂存",
    "切换导入暂存位置",
  );
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标位置已不存在");
  expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

function datasetReview(): DatasetBindingPreview {
  return {
    ...reviewed(),
    purpose: "datasets",
    previous: {
      ...binding,
      purpose: "datasets",
      bucket: "datasets-a",
      change_policy: "reviewed_dataset_switch",
    },
    target: {
      ...target,
      connection_revision: 4,
      bucket: "datasets-b",
      key_prefix: "company/_aap_managed/datasets/",
    },
    history: {
      asset_locations: 1,
      creation_allocations: 2,
      auxiliary_originals: 3,
      retirements: 4,
    },
    capabilities: {
      conditional_write: true,
      full_read: true,
      delete: true,
      empty_put: true,
      multipart: true,
    },
    impact_token: "reviewed-dataset-switch",
  };
}
it("reviews dataset allocation and saved owners before explicitly switching new originals", async () => {
  const review = datasetReview();
  api.previewBinding.mockResolvedValueOnce(review);
  const { dialog, onApplied } = await previewPurpose("datasets", review.previous, "数据集原件");
  await within(dialog).findByRole("region", { name: "数据集原件绑定变更预览" });
  expect(api.bindingCandidates).toHaveBeenCalledWith("datasets", 0, expect.any(AbortSignal), 20);
  expect(api.previewBinding).toHaveBeenCalledWith("datasets", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(within(dialog).getByText(/仅后续新分配的数据集原件及辅助原件/)).toHaveTextContent(
    "已有原件和在途上传保留原位置，数据集改名不改变文件路径",
  );
  expect(within(dialog).getByText(/原件位置记录 1 · 写入分配 2/)).toBeInTheDocument();
  expect(within(dialog).getByText(/辅助原件 3 · 退役记录 4/)).toBeInTheDocument();
  expect(within(dialog).getByText(/切换不会搬迁文件/)).toBeInTheDocument();
  expect(within(dialog).getByText(/空文件通过 · 分段上传通过/)).toBeInTheDocument();
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveAccessibleDescription(/空文件和分段上传的条件写入/);
  expect(within(select).queryByText(/条件写入/)).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("datasets", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: "reviewed-dataset-switch",
  });
  expect(pushToast).toHaveBeenCalledWith({
    msg: "数据集原件绑定已更新，后续新分配使用新位置",
    kind: "success",
  });
});
it.each([
  ["dataset_binding_target_missing", 404, "目标位置已不存在", ""],
  ["dataset_storage_conditions_unsupported", 409, "目标不支持条件写入", target.location_id],
  ["dataset_storage_unavailable", 503, "检查连接、读写权限及分段上传能力", target.location_id],
] as const)(
  "keeps dataset confirmation disabled after %s",
  async (code, status, message, selected) => {
    api.previewBinding.mockRejectedValueOnce(new ApiError(status, "Unavailable", { code }));
    const { dialog } = await previewPurpose("datasets", datasetReview().previous, "数据集原件");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue(selected);
    expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
    expect(api.applyBinding).not.toHaveBeenCalled();
  },
);

function mediaCacheReview(): MediaCacheBindingPreview {
  return {
    ...reviewed(),
    purpose: "media-cache",
    previous: {
      ...binding,
      purpose: "media-cache",
      bucket: "cache-a",
      change_policy: "reviewed_media_cache_switch",
    },
    target: {
      ...target,
      name: "媒体缓存目标",
      connection_revision: 4,
      bucket: "cache-b",
      key_prefix: "company/_aap_managed/media-cache/",
    },
    history: {
      directory_previews: 1,
      original_cache_entries: 2,
      image_pyramid_outputs: 3,
      video_derivative_outputs: 4,
      thumbnail_outputs: 5,
      legacy_thumbnails: 6,
    },
    capabilities: { conditional_write: true, full_read: true, delete: true, list: true },
    impact_token: "reviewed-media-cache-switch",
  };
}
it("reviews six separate media-cache history counts and switches only new allocations", async () => {
  const review = mediaCacheReview();
  api.previewBinding.mockResolvedValueOnce(review);
  const { dialog, onApplied } = await previewPurpose("media-cache", review.previous, "媒体缓存");
  await within(dialog).findByRole("region", { name: "媒体缓存绑定变更预览" });
  expect(api.bindingCandidates).toHaveBeenCalledWith("media-cache", 0, expect.any(AbortSignal), 20);
  expect(api.previewBinding).toHaveBeenCalledWith("media-cache", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(within(dialog).getByText(/仅后续新分配的原图缓存/)).toHaveTextContent(
    "已有及在途输出保留原位置，切换不会搬迁或清空缓存",
  );
  expect(within(dialog).getByText("目录预览 1 · 原图缓存 2")).toBeInTheDocument();
  expect(within(dialog).getByText("图片金字塔 3 · 视频派生 4")).toBeInTheDocument();
  expect(within(dialog).getByText("缩略图输出 5 · 旧缩略图 6")).toBeInTheDocument();
  expect(within(dialog).getByText(/不同类型的责任记录，不相加作为对象总数/)).toBeInTheDocument();
  expect(within(dialog).getByText(/前缀列举通过/)).toBeInTheDocument();
  expect(within(dialog).queryByText(/空文件通过|分段上传通过|版本列举/)).not.toBeInTheDocument();
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveAccessibleDescription(/条件写入、完整读回、准确删除和前缀列举/);
  expect(within(select).queryByText(/条件写入|前缀列举/)).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("media-cache", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: "reviewed-media-cache-switch",
  });
  expect(pushToast).toHaveBeenCalledWith({
    msg: "媒体缓存绑定已更新，后续新分配使用新位置",
    kind: "success",
  });
});
it.each([
  ["media_cache_binding_target_missing", 404, "目标位置已不存在", ""],
  ["media_cache_storage_conditions_unsupported", 409, "目标不支持条件写入", target.location_id],
  [
    "media_cache_storage_unavailable",
    503,
    "条件写入、读取、删除和前缀列举权限",
    target.location_id,
  ],
  ["media_cache_storage_probe_cleanup_unproven", 409, "验证操作尚未确认收尾", target.location_id],
  ["media_cache_storage_binding_unproven", 409, "用途绑定或目标配置无法确认", target.location_id],
] as const)(
  "keeps media-cache confirmation disabled after %s",
  async (code, status, message, selected) => {
    api.previewBinding.mockRejectedValueOnce(new ApiError(status, "Unavailable", { code }));
    const { dialog } = await previewPurpose("media-cache", mediaCacheReview().previous, "媒体缓存");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue(selected);
    expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
    expect(api.applyBinding).not.toHaveBeenCalled();
  },
);

function annotationsReview(): AnnotationsBindingPreview {
  return {
    ...reviewed(),
    purpose: "annotations",
    previous: {
      ...binding,
      purpose: "annotations",
      bucket: "annotationss-a",
      change_policy: "reviewed_annotations_switch",
    },
    target: {
      ...target,
      connection_revision: 4,
      bucket: "annotationss-b",
      key_prefix: "company/_aap_managed/annotationss/",
    },
    history: {
      task_originals: 1,
      creation_allocations: 2,
      raster_mask_objects: 3,
      comment_attachments: 4,
      guide_assets: 5,
    },
    capabilities: {
      conditional_write: true,
      full_read: true,
      delete: true,
      empty_put: true,
      multipart: true,
    },
    impact_token: "reviewed-annotations-switch",
  };
}
it("reviews saved annotation owners and probes before switching new annotation files", async () => {
  const review = annotationsReview();
  api.previewBinding.mockResolvedValueOnce(review);
  const { dialog, onApplied } = await previewPurpose("annotations", review.previous, "标注文件");
  await within(dialog).findByRole("region", { name: "标注文件绑定变更预览" });
  expect(api.bindingCandidates).toHaveBeenCalledWith("annotations", 0, expect.any(AbortSignal), 20);
  expect(api.previewBinding).toHaveBeenCalledWith("annotations", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(within(dialog).getByText(/仅后续新分配的独立任务原件/)).toHaveTextContent(
    "已有及在途对象保留原位置，逻辑键、发布状态与期限不变",
  );
  expect(within(dialog).getByText(/任务原件 1 · 任务写入分配 2/)).toBeInTheDocument();
  expect(within(dialog).getByText(/RLE 对象 3 · 评论附件 4 · 指南图片 5/)).toBeInTheDocument();
  expect(within(dialog).getByText(/不相加作为对象总数/)).toBeInTheDocument();
  expect(within(dialog).getByText(/空文件通过 · 分段上传通过/)).toBeInTheDocument();
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveAccessibleDescription(/空文件和分段上传的条件写入/);
  expect(within(select).queryByText(/条件写入/)).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("annotations", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: "reviewed-annotations-switch",
  });
  expect(pushToast).toHaveBeenCalledWith({
    msg: "标注文件绑定已更新，后续新分配使用新位置",
    kind: "success",
  });
});
it.each([
  ["annotations_binding_target_missing", 404, "目标位置已不存在", ""],
  ["annotations_storage_conditions_unsupported", 409, "目标不支持条件写入", target.location_id],
  ["annotations_storage_unavailable", 503, "检查连接、读写权限及分段上传能力", target.location_id],
] as const)(
  "keeps annotations confirmation disabled after %s",
  async (code, status, message, selected) => {
    api.previewBinding.mockRejectedValueOnce(new ApiError(status, "Unavailable", { code }));
    const { dialog } = await previewPurpose(
      "annotations",
      annotationsReview().previous,
      "标注文件",
    );
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue(selected);
    expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
    expect(api.applyBinding).not.toHaveBeenCalled();
  },
);

function trainingReview(): TrainingBindingPreview {
  return {
    ...reviewed(),
    purpose: "training",
    previous: {
      ...binding,
      purpose: "training",
      bucket: "trainings-a",
      change_policy: "reviewed_training_switch",
    },
    target: {
      ...target,
      connection_revision: 4,
      bucket: "trainings-b",
      key_prefix: "company/_aap_managed/trainings/",
    },
    history: {
      training_versions: 1,
      training_runs: 2,
      training_previews: 3,
      sample_evaluations: 4,
      active_learning_rounds: 5,
      deletion_intents: 6,
    },
    capabilities: {
      write: true,
      list: true,
      full_read: true,
      delete: true,
      multipart: true,
    },
    impact_token: "reviewed-training-switch",
  };
}
it("reviews saved training owners and probes before switching new training files", async () => {
  const review = trainingReview();
  api.previewBinding.mockResolvedValueOnce(review);
  const { dialog, onApplied } = await previewPurpose("training", review.previous, "训练文件");
  await within(dialog).findByRole("region", { name: "训练文件绑定变更预览" });
  expect(api.bindingCandidates).toHaveBeenCalledWith("training", 0, expect.any(AbortSignal), 20);
  expect(api.previewBinding).toHaveBeenCalledWith("training", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
  });
  expect(within(dialog).getByText(/仅后续新分配的训练版本/)).toHaveTextContent(
    "已有及在途文件保留原位置，重试、续训及清理沿保存地址，内容和首次期限不变",
  );
  expect(within(dialog).getByText(/训练版本 1 · 训练实验 2/)).toBeInTheDocument();
  expect(within(dialog).getByText(/训练预览 3 · 逐样本评估 4/)).toBeInTheDocument();
  expect(within(dialog).getByText(/主动学习轮次 5 · 删除意图 6/)).toBeInTheDocument();
  expect(within(dialog).getByText(/不相加作为对象总数/)).toBeInTheDocument();
  expect(within(dialog).getByText(/普通写入通过/)).toBeInTheDocument();
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveAccessibleDescription(/普通写入、非空分段上传/);
  expect(within(select).queryByText(/条件写入/)).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("training", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: "reviewed-training-switch",
  });
  expect(pushToast).toHaveBeenCalledWith({
    msg: "训练文件绑定已更新，后续新分配使用新位置",
    kind: "success",
  });
});
it.each([
  ["training_binding_target_missing", 404, "目标位置已不存在", ""],
  ["storage_operation_unsettled", 503, "验证操作尚未确认收尾", target.location_id],
  ["training_storage_unavailable", 503, "检查连接、普通写入、分段上传", target.location_id],
] as const)(
  "keeps training confirmation disabled after %s",
  async (code, status, message, selected) => {
    api.previewBinding.mockRejectedValueOnce(new ApiError(status, "Unavailable", { code }));
    const { dialog } = await previewPurpose("training", trainingReview().previous, "训练文件");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue(selected);
    expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
    expect(api.applyBinding).not.toHaveBeenCalled();
  },
);
