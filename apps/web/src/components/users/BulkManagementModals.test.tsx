import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { useAuthStore } from "@/stores/authStore";
import type { UserResponse } from "@/api/users";
import { BulkInviteModal } from "./BulkInviteModal";
import { BulkGroupAssignmentModal } from "./BulkGroupAssignmentModal";

const api = vi.hoisted(() => ({
  previewBulkInvite: vi.fn(),
  bulkInvite: vi.fn(),
  previewBulkGroup: vi.fn(),
  bulkGroup: vi.fn(),
}));
vi.mock("@/api/users", () => ({ usersApi: api }));
vi.mock("@/hooks/useProjects", () => ({
  useProjects: () => ({ data: [{ id: "p1", name: "Target project" }] }),
}));
vi.mock("@/hooks/useGroups", () => ({
  useGroups: () => ({ data: [{ id: "g1", name: "Target group" }] }),
}));
function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
        })
      }
    >
      {children}
    </QueryClientProvider>
  );
}
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("token", {
    id: "admin",
    email: "admin@test.local",
    name: "Admin",
    role: "super_admin",
    status: "active",
    group_name: null,
    created_at: "2026-09-09",
  });
});

it("previews invitations on the server and preserves successes when retrying only failed items", async () => {
  api.previewBulkInvite.mockResolvedValue({
    preview: true,
    succeeded: 2,
    failed: 1,
    items: [
      { index: 0, email: "a@test.local", ok: true, retryable: false },
      { index: 1, email: "b@test.local", ok: true, retryable: false },
      { index: 2, email: "bad", ok: false, retryable: false, error: "邮箱格式不正确" },
    ],
  });
  api.bulkInvite
    .mockResolvedValueOnce({
      succeeded: 1,
      failed: 1,
      items: [
        {
          index: 0,
          email: "a@test.local",
          ok: true,
          retryable: false,
          invite_url: "https://example.test/a",
        },
        { index: 1, email: "b@test.local", ok: false, retryable: true, error: "请重试" },
      ],
    })
    .mockResolvedValueOnce({
      succeeded: 1,
      failed: 0,
      items: [
        {
          index: 0,
          email: "b@test.local",
          ok: true,
          retryable: false,
          invite_url: "https://example.test/b",
        },
      ],
    });
  render(<BulkInviteModal open onClose={vi.fn()} />, { wrapper });
  fireEvent.change(screen.getByLabelText("邮箱清单"), {
    target: { value: "A@test.local\nb@test.local\nbad\na@test.local" },
  });
  fireEvent.change(screen.getByLabelText("目标项目"), { target: { value: "p1" } });
  fireEvent.click(screen.getByRole("button", { name: "预览邀请" }));
  await screen.findByText("邮箱格式不正确");
  expect(api.bulkInvite).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认邀请 2 人" }));
  await screen.findByRole("button", { name: "仅重试失败项" });
  expect(api.bulkInvite.mock.calls[0][0]).toEqual([
    { email: "a@test.local", role: "employee", project_id: "p1", project_member_role: "annotator" },
    { email: "b@test.local", role: "employee", project_id: "p1", project_member_role: "annotator" },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
  await screen.findByDisplayValue("https://example.test/b");
  expect(screen.getByDisplayValue("https://example.test/a")).toBeVisible();
  expect(screen.getByText("邮箱格式不正确")).toBeVisible();
  expect(api.bulkInvite.mock.calls[1][0]).toEqual([
    { email: "b@test.local", role: "employee", project_id: "p1", project_member_role: "annotator" },
  ]);
  expect(screen.getByText("成功 2 条 · 失败 1 条")).toBeVisible();
});

it("keeps blocked members and earlier successes in the group result after retry", async () => {
  api.previewBulkGroup.mockResolvedValue({
    group_id: "g1",
    group_name: "Target",
    applicable: 2,
    blocked: 1,
    items: [
      { user_id: "a", ok: true, name: "A" },
      { user_id: "b", ok: true, name: "B" },
      { user_id: "c", ok: false, error: "不在管理范围内" },
    ],
  });
  api.bulkGroup
    .mockResolvedValueOnce({
      succeeded: 1,
      failed: 1,
      items: [
        { user_id: "a", ok: true, retryable: false },
        { user_id: "b", ok: false, retryable: true },
      ],
    })
    .mockResolvedValueOnce({
      succeeded: 1,
      failed: 0,
      items: [{ user_id: "b", ok: true, retryable: false }],
    });
  render(
    <BulkGroupAssignmentModal
      open
      users={[{ id: "a" }, { id: "b" }, { id: "c" }] as UserResponse[]}
      groups={[
        { id: "g1", name: "Target", description: "", member_count: 0, created_at: "2026-09-09" },
      ]}
      onClose={vi.fn()}
    />,
    { wrapper },
  );
  fireEvent.change(screen.getByLabelText("目标数据组"), { target: { value: "g1" } });
  fireEvent.click(screen.getByRole("button", { name: "预览变更" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认变更 2 人" }));
  fireEvent.click(await screen.findByRole("button", { name: "仅重试失败项" }));
  await waitFor(() => expect(screen.getByText("成功 2 条 · 失败 1 条")).toBeVisible());
  expect(api.bulkGroup.mock.calls[1][0]).toEqual({ user_ids: ["b"], group_id: "g1" });
  expect(screen.getByText("不在管理范围内")).toBeVisible();
});

it("normalizes the project role when the account role switches to viewer", async () => {
  api.previewBulkInvite.mockResolvedValue({
    preview: true,
    succeeded: 1,
    failed: 0,
    items: [{ index: 0, email: "v@test.local", ok: true, retryable: false }],
  });
  render(<BulkInviteModal open onClose={vi.fn()} />, { wrapper });
  fireEvent.change(screen.getByLabelText("邮箱清单"), {
    target: { value: "v@test.local" },
  });
  // Pick a project first: 项目职责 defaults to 标注员 and becomes visible.
  fireEvent.change(screen.getByLabelText("目标项目"), { target: { value: "p1" } });
  expect(screen.getByLabelText("项目职责")).toHaveValue("annotator");
  // Switch the account role to viewer: the stale annotator must not be sent.
  fireEvent.change(screen.getByLabelText("默认账号角色"), { target: { value: "viewer" } });
  expect(screen.getByLabelText("项目职责")).toHaveValue("viewer");
  fireEvent.click(screen.getByRole("button", { name: "预览邀请" }));
  await waitFor(() => expect(api.previewBulkInvite).toHaveBeenCalled());
  expect(api.previewBulkInvite.mock.calls[0][0]).toEqual([
    {
      email: "v@test.local",
      role: "viewer",
      project_id: "p1",
      project_member_role: "viewer",
    },
  ]);
});
