import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  AuditArchiveBindingHistory,
  AvatarBindingCandidate,
  AvatarBindingHistory,
  BugAttachmentBindingHistory,
  StorageBindingOut,
} from "@/api/generated/types.gen";
import { server } from "@/mocks/server";
import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";
import { AvatarBindingChangeButton } from "./AvatarBindingChangeButton";
import { StorageBindingChangeButton } from "./StorageBindingChangeButton";

const removed = {
  location_id: "00000000-0000-4000-8000-000000000007",
  name: "旧候选",
  config_revision: 7,
};
const replacement = {
  location_id: "00000000-0000-4000-8000-000000000009",
  name: "新候选",
  config_revision: 9,
};

beforeEach(() => {
  resetUnexpectedApiRequests();
  seedAuthUser(createTestUser({ role: "super_admin" }));
});
afterEach(() => {
  cleanup();
  resetAuthUser();
  expectNoUnexpectedApiRequests();
});

function serveBinding(
  purpose: "avatars" | "bug-reports" | "audit-archive",
  changePolicy: StorageBindingOut["change_policy"],
  history: AvatarBindingHistory | BugAttachmentBindingHistory | AuditArchiveBindingHistory,
  canApply = true,
) {
  const route = `*/api/v1/storage-locations/bindings/${purpose}`;
  const binding: StorageBindingOut = {
    purpose,
    change_policy: changePolicy,
    location_id: "00000000-0000-4000-8000-000000000001",
    generation: 2,
    location_config_revision: 3,
    bucket: "current",
    state: "configured",
    read_policy: "consumer_owned_compatibility",
    write_policy: "current_binding",
  };
  const reviews: unknown[] = [];
  const applications: unknown[] = [];
  const onApplied = vi.fn().mockResolvedValue(undefined);
  server.use(
    http.get(`${route}/candidates`, () => HttpResponse.json({ items: [replacement], total: 1 })),
    http.post(`${route}/change-preview`, async ({ request }) => {
      reviews.push(await request.json());
      return HttpResponse.json({
        purpose,
        previous: binding,
        target: {
          ...replacement,
          bucket: "next",
          key_prefix: `managed/${purpose}/`,
          connection_revision: 4,
        },
        history,
        capabilities: { conditional_write: true, full_read: true, delete: true },
        can_apply: canApply,
        measured_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 300_000).toISOString(),
        impact_token: `${purpose}-reviewed`,
      });
    }),
    http.patch(route, async ({ request }) => {
      applications.push(await request.json());
      return HttpResponse.json({
        ...binding,
        location_id: replacement.location_id,
        generation: 3,
        location_config_revision: replacement.config_revision,
        bucket: "next",
      });
    }),
  );

  function open(label: string) {
    renderWithProviders(
      purpose === "avatars" ? (
        <AvatarBindingChangeButton binding={binding} onApplied={onApplied} />
      ) : (
        <StorageBindingChangeButton purpose={purpose} binding={binding} onApplied={onApplied} />
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: `切换${label}位置` }));
    const dialog = within(screen.getByRole("dialog", { name: `切换${label}存储位置` }));
    async function requestPreview(target: AvatarBindingCandidate) {
      await dialog.findByRole("option", { name: target.name });
      fireEvent.change(dialog.getByRole("combobox", { name: "目标存储位置" }), {
        target: { value: target.location_id },
      });
      fireEvent.click(dialog.getByRole("button", { name: "验证并预览" }));
    }
    return { dialog, requestPreview };
  }
  return { route, reviews, applications, onApplied, open };
}

it.each([
  {
    purpose: "avatars",
    label: "头像",
    policy: "reviewed_avatar_switch",
    missingCode: "avatar_binding_target_missing",
    history: { pending: 2, ready: 4, retired: 3, unlocated_users: 0 },
    historyText: /已登记头像：待完成 2 · 可用 4 · 待清理 3/,
  },
  {
    purpose: "bug-reports",
    label: "报告附件",
    policy: "reviewed_bug_attachment_switch",
    missingCode: "bug_attachment_binding_target_missing",
    history: { pending: 2, ready: 4, retired: 3 },
    historyText: /已登记报告附件：待完成 2 · 可用 4 · 待清理 3/,
  },
  {
    purpose: "audit-archive",
    label: "审计归档",
    policy: "reviewed_audit_archive_switch",
    missingCode: "audit_archive_binding_target_missing",
    history: { pending: 2, ready: 4 },
    historyText: /已登记审计归档：待完成 2 · 可用 4。.*归档永久保留，不执行物理清理。/,
  },
] as const)(
  "$purpose recovers from a deleted target and applies only the reviewed replacement",
  async ({ purpose, label, policy, missingCode, history, historyText }) => {
    const { route, reviews, applications, onApplied, open } = serveBinding(
      purpose,
      policy,
      history,
    );
    server.use(
      http.get(`${route}/candidates`, () => HttpResponse.json({ items: [removed], total: 1 }), {
        once: true,
      }),
      http.post(
        `${route}/change-preview`,
        async ({ request }) => {
          reviews.push(await request.json());
          return HttpResponse.json({ detail: { code: missingCode } }, { status: 404 });
        },
        { once: true },
      ),
    );

    const { dialog, requestPreview } = open(label);
    await requestPreview(removed);
    expect(await dialog.findByRole("alert")).toHaveTextContent("目标位置已不存在");
    const select = dialog.getByRole("combobox", { name: "目标存储位置" });
    expect(select).toHaveValue("");
    expect(select).toBeEnabled();
    expect(dialog.getByRole("button", { name: "确认切换" })).toBeDisabled();

    fireEvent.click(dialog.getByRole("button", { name: "刷新位置" }));
    await requestPreview(replacement);
    const review = await dialog.findByRole("region", { name: `${label}绑定变更预览` });
    expect(within(review).getByText(replacement.name)).toBeVisible();
    expect(within(review).getByText(historyText)).toBeVisible();
    expect(dialog.queryByRole("option", { name: removed.name })).not.toBeInTheDocument();
    expect(reviews).toEqual([
      {
        expected_generation: 2,
        target_location_id: removed.location_id,
        target_config_revision: 7,
      },
      {
        expected_generation: 2,
        target_location_id: replacement.location_id,
        target_config_revision: 9,
      },
    ]);
    expect(applications).toEqual([]);
    const confirm = dialog.getByRole("button", { name: "确认切换" });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(applications).toEqual([
      {
        expected_generation: 2,
        target_location_id: replacement.location_id,
        target_config_revision: 9,
        impact_token: `${purpose}-reviewed`,
      },
    ]);
  },
);

it("blocks avatar switching when historical uploads have no saved location", async () => {
  const history = { pending: 2, ready: 4, retired: 3, unlocated_users: 5 };
  const { open, applications, onApplied } = serveBinding(
    "avatars",
    "reviewed_avatar_switch",
    history,
    false,
  );
  const { dialog, requestPreview } = open("头像");
  await requestPreview(replacement);
  const review = await dialog.findByRole("region", { name: "头像绑定变更预览" });
  const alert = within(review).getByRole("alert");
  expect(alert).toHaveTextContent("5 位用户的历史上传头像缺少存储定位");
  expect(alert).toHaveTextContent("重新上传头像或改用预设头像");
  const confirm = dialog.getByRole("button", { name: "确认切换" });
  expect(confirm).toBeDisabled();
  fireEvent.click(confirm);
  expect(applications).toEqual([]);
  expect(onApplied).not.toHaveBeenCalled();
});

it("hides the reviewed target and disables the dialog after candidate access is revoked", async () => {
  const { route, open, applications } = serveBinding(
    "bug-reports",
    "reviewed_bug_attachment_switch",
    { pending: 2, ready: 4, retired: 3 },
  );
  const { dialog, requestPreview } = open("报告附件");
  await requestPreview(replacement);
  const review = await dialog.findByRole("region", { name: "报告附件绑定变更预览" });
  expect(dialog.getByRole("button", { name: "确认切换" })).toBeEnabled();
  server.use(
    http.get(`${route}/candidates`, () =>
      HttpResponse.json({ detail: "Forbidden" }, { status: 403 }),
    ),
  );
  fireEvent.click(dialog.getByRole("button", { name: "刷新位置" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("存储管理权限已变化");
  expect(review).not.toBeInTheDocument();
  expect(dialog.queryByRole("option", { name: replacement.name })).not.toBeInTheDocument();
  const confirm = dialog.getByRole("button", { name: "确认切换" });
  expect(confirm).toBeDisabled();
  fireEvent.click(confirm);
  expect(applications).toEqual([]);
  fireEvent.click(dialog.getByRole("button", { name: "取消" }));
  expect(await screen.findByRole("button", { name: "切换报告附件位置" })).toBeDisabled();
});
