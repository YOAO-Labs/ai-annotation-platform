import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  BugAttachmentBindingPreview,
  ExportBindingPreview,
  ImportBindingPreview,
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
