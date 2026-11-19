import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AvatarBindingCandidate, StorageBindingOut } from "@/api/generated/types.gen";
import { server } from "@/mocks/server";
import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";
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

describe.each([
  [
    "avatars",
    "头像",
    "reviewed_avatar_switch",
    "avatar_binding_target_missing",
    { pending: 0, ready: 1, retired: 0, unlocated_users: 0 },
  ],
  [
    "bug-reports",
    "报告附件",
    "reviewed_bug_attachment_switch",
    "bug_attachment_binding_target_missing",
    { pending: 0, ready: 1, retired: 0 },
  ],
  [
    "audit-archive",
    "审计归档",
    "reviewed_audit_archive_switch",
    "audit_archive_binding_target_missing",
    { pending: 0, ready: 1 },
  ],
] as const)("%s binding", (purpose, label, policy, missingCode, history) => {
  const route = `*/api/v1/storage-locations/bindings/${purpose}`;
  const binding: StorageBindingOut = {
    purpose,
    change_policy: policy,
    location_id: "00000000-0000-4000-8000-000000000001",
    generation: 2,
    location_config_revision: 3,
    bucket: "current",
    state: "configured",
    read_policy: "consumer_owned_compatibility",
    write_policy: "current_binding",
  };
  let requests: unknown[];

  beforeEach(() => {
    requests = [];
    server.use(
      http.get(`${route}/candidates`, () => HttpResponse.json({ items: [replacement], total: 1 })),
      http.post(`${route}/change-preview`, async ({ request }) => {
        requests.push(await request.json());
        return HttpResponse.json({
          purpose,
          previous: binding,
          target: {
            ...replacement,
            bucket: "next",
            key_prefix: "managed/",
            connection_revision: 4,
          },
          history,
          capabilities: { conditional_write: true, full_read: true, delete: true },
          can_apply: true,
          measured_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 300_000).toISOString(),
          impact_token: "reviewed-replacement",
        });
      }),
    );
  });

  function open() {
    renderWithProviders(
      <StorageBindingChangeButton purpose={purpose} binding={binding} onApplied={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: `切换${label}位置` }));
    const dialog = within(screen.getByRole("dialog", { name: `切换${label}存储位置` }));
    async function preview(target: AvatarBindingCandidate) {
      await dialog.findByRole("option", { name: target.name });
      fireEvent.change(dialog.getByRole("combobox", { name: "目标存储位置" }), {
        target: { value: target.location_id },
      });
      fireEvent.click(dialog.getByRole("button", { name: "验证并预览" }));
    }
    return { dialog, preview };
  }

  it("recovers from a deleted target by reviewing the replacement at its own revision", async () => {
    server.use(
      http.get(`${route}/candidates`, () => HttpResponse.json({ items: [removed], total: 1 }), {
        once: true,
      }),
      http.post(
        `${route}/change-preview`,
        async ({ request }) => {
          requests.push(await request.json());
          return HttpResponse.json({ detail: { code: missingCode } }, { status: 404 });
        },
        { once: true },
      ),
    );
    const { dialog, preview } = open();
    await preview(removed);
    expect(await dialog.findByRole("alert")).toHaveTextContent("目标位置已不存在");
    const select = dialog.getByRole("combobox", { name: "目标存储位置" });
    expect(select).toHaveValue("");
    expect(select).toBeEnabled();
    expect(dialog.getByRole("button", { name: "确认切换" })).toBeDisabled();

    fireEvent.click(dialog.getByRole("button", { name: "刷新位置" }));
    await preview(replacement);
    const review = await dialog.findByRole("region", { name: `${label}绑定变更预览` });
    expect(within(review).getByText(replacement.name)).toBeVisible();
    expect(dialog.queryByRole("option", { name: removed.name })).not.toBeInTheDocument();
    expect(dialog.getByRole("button", { name: "确认切换" })).toBeEnabled();
    expect(requests).toEqual([
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
  });

  it("hides the reviewed target and prevents confirmation when refreshing candidates is forbidden", async () => {
    const { dialog, preview } = open();
    await preview(replacement);
    const review = await dialog.findByRole("region", { name: `${label}绑定变更预览` });
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
    expect(dialog.getByRole("button", { name: "确认切换" })).toBeDisabled();
  });
});
