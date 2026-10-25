import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
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
import { createTestQueryClient } from "@/test/queryClient";
import { StorageBindingChangeButton } from "./StorageBindingChangeButton";

const auth = vi.hoisted(() => ({ revision: 1 }));
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
vi.mock("@/hooks/useSessionGuard", async (original) => ({
  ...(await original<typeof import("@/hooks/useSessionGuard")>()),
  useSessionRevision: () => auth.revision,
}));
const binding: StorageBindingOut = {
  purpose: "bug-reports",
  location_id: "old-location",
  generation: 2,
  location_config_revision: 3,
  bucket: "old-avatar-bucket",
  state: "configured",
  read_policy: "consumer_owned_compatibility",
  write_policy: "current_binding",
  change_policy: "reviewed_bug_attachment_switch",
};
const target = { location_id: "new-location", name: "新报告附件存储", config_revision: 7 };
function reviewed(
  overrides: Partial<BugAttachmentBindingPreview> = {},
): BugAttachmentBindingPreview {
  return {
    purpose: "bug-reports",
    previous: binding,
    target: {
      ...target,
      connection_revision: 4,
      bucket: "new-avatar-bucket",
      key_prefix: "managed/avatars/",
    },
    history: { pending: 2, ready: 4, retired: 3 },
    capabilities: { conditional_write: true, full_read: true, delete: true },
    can_apply: true,
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    impact_token: "reviewed-avatar-switch",
    ...overrides,
  };
}
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  let currentBinding = binding;
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const tree = () => (
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="bug-reports"
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
async function open() {
  fireEvent.click(screen.getByRole("button", { name: "切换报告附件位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换报告附件存储位置" });
  await within(dialog).findByRole("option", { name: target.name });
  return dialog;
}
async function preview() {
  const dialog = await open();
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  await within(dialog).findByRole("region", { name: "报告附件绑定变更预览" });
  return dialog;
}
beforeEach(() => {
  vi.resetAllMocks();
  auth.revision = 1;
  api.bindingCandidates.mockResolvedValue({ items: [target], total: 21 });
  api.previewBinding.mockImplementation(async () => reviewed());
  api.applyBinding.mockResolvedValue({
    ...binding,
    location_id: target.location_id,
    generation: 3,
  });
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
  vi.useRealTimers();
});

it("loads candidates only on opening and keeps label and muted hint outside the selected value", async () => {
  mount();
  expect(api.bindingCandidates).not.toHaveBeenCalled();
  const dialog = await open();
  expect(api.bindingCandidates).toHaveBeenCalledWith("bug-reports", 0, expect.any(AbortSignal), 20);
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveValue("");
  const hint = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(hint).toHaveClass("text-xs", "text-muted-foreground");
  expect(hint).toHaveTextContent("实际写入、完整读回并删除");
  expect(within(select).getByRole("option", { name: target.name })).toHaveTextContent(target.name);
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
    impact_token: "reviewed-avatar-switch",
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
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: "other" },
  });
  await act(async () => complete(reviewed()));
  expect(
    within(dialog).queryByRole("region", { name: "报告附件绑定变更预览" }),
  ).not.toBeInTheDocument();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it.each(["generation", "location", "revision", "session"])(
  "retires a review after %s changes",
  async (transition) => {
    const view = mount();
    await preview();
    if (transition === "generation") view.refresh({ ...binding, generation: 3 });
    else if (transition === "location") view.refresh({ ...binding, location_id: "changed" });
    else if (transition === "revision") view.refresh({ ...binding, location_config_revision: 4 });
    else {
      auth.revision++;
      view.refresh();
    }
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.applyBinding).not.toHaveBeenCalled();
  },
);

it("invalidates a review if candidate refresh reports a new target revision", async () => {
  const view = mount();
  await preview();
  api.bindingCandidates.mockResolvedValueOnce({
    items: [{ ...target, config_revision: 8 }],
    total: 1,
  });
  await act(async () =>
    view.client.invalidateQueries({ queryKey: ["bug-attachment-binding-candidates"] }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: "报告附件绑定变更预览" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("alert")).toHaveTextContent("目标位置配置已变化");
  expect(screen.getByRole("button", { name: "确认切换" })).toBeDisabled();
});

it("hides retained evidence when candidate access is revoked", async () => {
  const view = mount();
  await preview();
  api.bindingCandidates.mockRejectedValueOnce(Object.assign(new Error("revoked"), { status: 403 }));
  await act(async () =>
    view.client.invalidateQueries({ queryKey: ["bug-attachment-binding-candidates"] }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: "报告附件绑定变更预览" })).not.toBeInTheDocument(),
  );
  expect(screen.queryByRole("option", { name: target.name })).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("存储管理权限已变化");
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
  auth.revision++;
  view.refresh();
  await act(async () => complete());
  expect(pushToast).not.toHaveBeenCalled();
  expect(view.onApplied).not.toHaveBeenCalled();
});

it("rejects a response describing a different target instead of opening its confirmation", async () => {
  api.previewBinding.mockResolvedValueOnce(
    reviewed({ target: { ...reviewed().target, location_id: "another-target" } }),
  );
  mount();
  const dialog = await open();
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(
    within(dialog).queryByRole("region", { name: "报告附件绑定变更预览" }),
  ).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
});

it("settles an in-flight apply after target refresh without trapping the dialog", async () => {
  let reject!: (error: unknown) => void;
  api.applyBinding.mockReturnValueOnce(
    new Promise((_resolve, fail) => {
      reject = fail;
    }),
  );
  const view = mount();
  await preview();
  fireEvent.click(screen.getByRole("button", { name: "确认切换" }));
  api.bindingCandidates.mockResolvedValueOnce({
    items: [{ ...target, config_revision: 8 }],
    total: 1,
  });
  await act(async () =>
    view.client.invalidateQueries({ queryKey: ["bug-attachment-binding-candidates"] }),
  );
  await act(async () =>
    reject(new ApiError(409, "Conflict", { code: "storage_binding_preview_changed" })),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(view.onApplied).not.toHaveBeenCalled();
});

it("permits another target after the selected candidate is deleted", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(404, "Not found", { code: "avatar_binding_target_missing" }),
  );
  mount();
  const dialog = await open();
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标位置已不存在");
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveValue("");
  expect(select).toBeEnabled();
  expect(within(dialog).queryByText("存储管理权限已变化，请刷新后重试。")).not.toBeInTheDocument();
  api.bindingCandidates.mockResolvedValueOnce({
    items: [{ ...target, location_id: "other", name: "另一位置" }],
    total: 1,
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "刷新位置" }));
  await within(dialog).findByRole("option", { name: "另一位置" });
  fireEvent.change(select, { target: { value: "other" } });
  expect(within(dialog).getByRole("button", { name: "验证并预览" })).toBeEnabled();
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
function mountExport() {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="export"
        binding={exportReview().previous}
        onApplied={onApplied}
      />
    </QueryClientProvider>,
  );
  return onApplied;
}
async function openExport() {
  fireEvent.click(screen.getByRole("button", { name: "切换导出位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换导出存储位置" });
  await within(dialog).findByRole("option", { name: target.name });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  return dialog;
}
it("reviews both export owners and listing capabilities before confirming the export route", async () => {
  api.previewBinding.mockResolvedValueOnce(exportReview());
  const onApplied = mountExport();
  const dialog = await openExport();
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
  expect(within(dialog).getByText(/外部端点须供浏览器及下载脚本访问/)).toHaveClass(
    "text-muted-foreground",
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "确认切换" }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(api.applyBinding).toHaveBeenCalledWith("export", {
    expected_generation: 2,
    target_location_id: target.location_id,
    target_config_revision: 7,
    impact_token: exportReview().impact_token,
  });
});
it("rejects another purpose's preview for an export switch", async () => {
  api.previewBinding.mockResolvedValueOnce(reviewed());
  mountExport();
  const dialog = await openExport();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(within(dialog).queryByRole("region")).not.toBeInTheDocument();
});
it("resets an export target that disappears during verification", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(404, "Not found", { code: "export_binding_target_missing" }),
  );
  mountExport();
  const dialog = await openExport();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标位置已不存在");
  expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
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
function mountImport() {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="import"
        binding={importReview().previous}
        onApplied={onApplied}
      />
    </QueryClientProvider>,
  );
  return onApplied;
}
async function openImport() {
  fireEvent.click(screen.getByRole("button", { name: "切换导入暂存位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换导入暂存位置" });
  await within(dialog).findByRole("option", { name: target.name });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  return dialog;
}
it("reviews all import owners without promising staging deletion and applies the import route", async () => {
  api.previewBinding.mockResolvedValueOnce(importReview());
  const onApplied = mountImport();
  const dialog = await openImport();
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
  expect(within(dialog).getByText(/外部端点须供浏览器与模型服务访问/)).toHaveClass(
    "text-xs",
    "text-muted-foreground",
  );
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
it("rejects a preview belonging to another purpose for import", async () => {
  api.previewBinding.mockResolvedValueOnce(exportReview());
  mountImport();
  const dialog = await openImport();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});
it("explains the input version requirement when an import probe rejects it", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(409, "Unavailable", { code: "import_storage_versioning_required" }),
  );
  mountImport();
  const dialog = await openImport();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("启用目标桶版本控制");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});
it("clears an import target that disappeared before its probe completed", async () => {
  api.previewBinding.mockRejectedValueOnce(
    new ApiError(404, "Missing", { code: "import_binding_target_missing" }),
  );
  mountImport();
  const dialog = await openImport();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标位置已不存在");
  expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
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
function mountDataset() {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="datasets"
        binding={datasetReview().previous}
        onApplied={onApplied}
      />
    </QueryClientProvider>,
  );
  return onApplied;
}
async function openDataset() {
  fireEvent.click(screen.getByRole("button", { name: "切换数据集原件位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换数据集原件存储位置" });
  await within(dialog).findByRole("option", { name: target.name });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  return dialog;
}

it("reviews dataset allocation and saved owners before explicitly switching new originals", async () => {
  api.previewBinding.mockResolvedValueOnce(datasetReview());
  const onApplied = mountDataset();
  const dialog = await openDataset();
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
  const hint = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(hint).toHaveClass("text-xs", "text-muted-foreground");
  expect(hint).toHaveTextContent("空文件和分段上传的条件写入");
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

it("rejects another purpose's preview when switching dataset originals", async () => {
  api.previewBinding.mockResolvedValueOnce(importReview());
  mountDataset();
  const dialog = await openDataset();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it.each([
  ["dataset_binding_target_missing", "目标位置已不存在"],
  ["dataset_storage_conditions_unsupported", "目标不支持条件写入"],
  ["dataset_storage_unavailable", "检查连接、读写权限及分段上传能力"],
])("keeps dataset confirmation disabled after %s", async (code, message) => {
  api.previewBinding.mockRejectedValueOnce(new ApiError(409, "Unavailable", { code }));
  mountDataset();
  const dialog = await openDataset();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
  if (code === "dataset_binding_target_missing")
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

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
function mountMediaCache() {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="media-cache"
        binding={mediaCacheReview().previous}
        onApplied={onApplied}
      />
    </QueryClientProvider>,
  );
  return onApplied;
}
async function openMediaCache() {
  fireEvent.click(screen.getByRole("button", { name: "切换媒体缓存位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换媒体缓存存储位置" });
  await within(dialog).findByRole("option", { name: target.name });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  return dialog;
}
it("reviews six separate media-cache history counts and switches only new allocations", async () => {
  api.previewBinding.mockResolvedValueOnce(mediaCacheReview());
  const onApplied = mountMediaCache();
  const dialog = await openMediaCache();
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
  const hint = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(hint).toHaveClass("text-xs", "text-muted-foreground");
  expect(hint).toHaveTextContent("条件写入、完整读回、准确删除和前缀列举");
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
it("rejects another purpose's preview for a media-cache switch", async () => {
  api.previewBinding.mockResolvedValueOnce(datasetReview());
  mountMediaCache();
  const dialog = await openMediaCache();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});
it.each([
  ["media_cache_binding_target_missing", "目标位置已不存在"],
  ["media_cache_storage_conditions_unsupported", "目标不支持条件写入"],
  ["media_cache_storage_unavailable", "条件写入、读取、删除和前缀列举权限"],
  ["media_cache_storage_probe_cleanup_unproven", "验证操作尚未确认收尾"],
  ["media_cache_storage_binding_unproven", "用途绑定或目标配置无法确认"],
])("keeps media-cache confirmation disabled after %s", async (code, message) => {
  api.previewBinding.mockRejectedValueOnce(new ApiError(409, "Unavailable", { code }));
  mountMediaCache();
  const dialog = await openMediaCache();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
  if (code === "media_cache_binding_target_missing")
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

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
function mountAnnotations() {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="annotations"
        binding={annotationsReview().previous}
        onApplied={onApplied}
      />
    </QueryClientProvider>,
  );
  return onApplied;
}
async function openAnnotations() {
  fireEvent.click(screen.getByRole("button", { name: "切换标注文件位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换标注文件存储位置" });
  await within(dialog).findByRole("option", { name: target.name });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  return dialog;
}

it("reviews annotations allocation and saved owners before explicitly switching new originals", async () => {
  api.previewBinding.mockResolvedValueOnce(annotationsReview());
  const onApplied = mountAnnotations();
  const dialog = await openAnnotations();
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
  const hint = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(hint).toHaveClass("text-xs", "text-muted-foreground");
  expect(hint).toHaveTextContent("空文件和分段上传的条件写入");
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

it("rejects another purpose's preview when switching annotations originals", async () => {
  api.previewBinding.mockResolvedValueOnce(importReview());
  mountAnnotations();
  const dialog = await openAnnotations();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it.each([
  ["annotations_binding_target_missing", "目标位置已不存在"],
  ["annotations_storage_conditions_unsupported", "目标不支持条件写入"],
  ["annotations_storage_unavailable", "检查连接、读写权限及分段上传能力"],
])("keeps annotations confirmation disabled after %s", async (code, message) => {
  api.previewBinding.mockRejectedValueOnce(new ApiError(409, "Unavailable", { code }));
  mountAnnotations();
  const dialog = await openAnnotations();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
  if (code === "annotations_binding_target_missing")
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

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
function mountTraining() {
  const client = createTestQueryClient();
  clients.push(client);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <StorageBindingChangeButton
        purpose="training"
        binding={trainingReview().previous}
        onApplied={onApplied}
      />
    </QueryClientProvider>,
  );
  return onApplied;
}
async function openTraining() {
  fireEvent.click(screen.getByRole("button", { name: "切换训练文件位置" }));
  const dialog = screen.getByRole("dialog", { name: "切换训练文件存储位置" });
  await within(dialog).findByRole("option", { name: target.name });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: target.location_id },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  return dialog;
}

it("reviews training allocation and saved owners before explicitly switching new originals", async () => {
  api.previewBinding.mockResolvedValueOnce(trainingReview());
  const onApplied = mountTraining();
  const dialog = await openTraining();
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
  expect(within(dialog).getByText(/主动学习轮次 5 · 删除意图 6/)).toBeInTheDocument();
  expect(within(dialog).getByText(/不相加作为对象总数/)).toBeInTheDocument();
  expect(within(dialog).getByText(/普通写入通过/)).toBeInTheDocument();
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  const hint = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(hint).toHaveClass("text-xs", "text-muted-foreground");
  expect(hint).toHaveTextContent("普通写入、非空分段上传");
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

it("rejects another purpose's preview when switching training originals", async () => {
  api.previewBinding.mockResolvedValueOnce(importReview());
  mountTraining();
  const dialog = await openTraining();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("用途绑定或目标配置已变化");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});

it.each([
  ["training_binding_target_missing", "目标位置已不存在"],
  ["storage_operation_unsettled", "验证操作尚未确认收尾"],
  ["training_storage_unavailable", "检查连接、普通写入、分段上传"],
])("keeps training confirmation disabled after %s", async (code, message) => {
  api.previewBinding.mockRejectedValueOnce(new ApiError(409, "Unavailable", { code }));
  mountTraining();
  const dialog = await openTraining();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
  if (code === "training_binding_target_missing")
    expect(within(dialog).getByRole("combobox", { name: "目标存储位置" })).toHaveValue("");
  expect(within(dialog).getByRole("button", { name: "确认切换" })).toBeDisabled();
  expect(api.applyBinding).not.toHaveBeenCalled();
});
