import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import type { RoleImpactPreview, UserResponse } from "@/api/users";
import { EditUserModal } from "./EditUserModal";
const api = vi.hoisted(() => ({ previewRoleChange: vi.fn(), changeRole: vi.fn() }));
vi.mock("@/api/users", () => ({ usersApi: api }));
vi.mock("@/hooks/useGroups", () => ({ useGroups: () => ({ data: [] }) }));
const user = {
  id: "a",
  name: "A",
  email: "a@test.local",
  role: "employee",
  group_id: null,
} as UserResponse;
const preview: RoleImpactPreview = {
  user_id: "a",
  email: user.email,
  current_role: "employee",
  requested_role: "employee",
  can_change: true,
  blockers: [],
  projects: [],
  other_project_count: 2,
  warnings: ["平台角色对所有项目生效"],
  assigned_batch_count: 0,
  assigned_task_count: 0,
  review_task_count: 0,
};
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("token", {
    id: "admin",
    name: "Admin",
    email: "admin@test.local",
    role: "super_admin",
    status: "active",
    group_name: null,
    created_at: "2026-09-09",
  });
});
function wrap(userData: UserResponse) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <EditUserModal open user={userData} onClose={vi.fn()} />
    </QueryClientProvider>
  );
}
it("discards a late preview for a previous target", async () => {
  let resolve!: (value: RoleImpactPreview) => void;
  api.previewRoleChange
    .mockReturnValueOnce(
      new Promise<RoleImpactPreview>((done) => {
        resolve = done;
      }),
    )
    .mockResolvedValueOnce({
      ...preview,
      user_id: "b",
      warnings: ["B 的角色影响"],
      other_project_count: 0,
    });
  const { rerender } = render(wrap(user));
  await waitFor(() => expect(api.previewRoleChange).toHaveBeenCalledWith("a", "employee"));
  rerender(wrap({ ...user, id: "b", name: "B" }));
  await screen.findByText("B 的角色影响");
  await act(async () => {
    resolve({ ...preview, warnings: ["过期的 A 影响"] });
  });
  expect(screen.queryByText("过期的 A 影响")).not.toBeInTheDocument();
  expect(screen.getByText("B 的角色影响")).toBeVisible();
});
it("shows anonymous cross-project impact and waits for the selected role preview before saving", async () => {
  let resolve!: (value: RoleImpactPreview) => void;
  api.previewRoleChange.mockResolvedValueOnce(preview).mockReturnValueOnce(
    new Promise<RoleImpactPreview>((done) => {
      resolve = done;
    }),
  );
  api.changeRole.mockResolvedValue({ ...user, role: "viewer" });
  render(wrap(user));
  await screen.findByText(/另涉及 2 个管理范围外/);
  fireEvent.change(screen.getByLabelText("角色"), { target: { value: "viewer" } });
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  await waitFor(() => expect(api.previewRoleChange).toHaveBeenCalledTimes(2));
  expect(api.previewRoleChange.mock.calls).toEqual([
    ["a", "employee"],
    ["a", "viewer"],
  ]);
  expect(api.changeRole).not.toHaveBeenCalled();
  await act(async () => {
    resolve({ ...preview, requested_role: "viewer" });
  });
  expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
  expect(api.changeRole).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(api.changeRole).toHaveBeenCalledWith("a", "viewer"));
});

it("hides platform-role editing from project administrators and points them to project member management", async () => {
  useAuthStore.getState().setAuth("token", {
    id: "pa",
    name: "PA",
    email: "pa@test.local",
    role: "project_admin",
    status: "active",
    group_name: null,
    created_at: "2026-09-09",
  });
  render(wrap(user));
  expect(api.previewRoleChange).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("角色")).not.toBeInTheDocument();
  expect(screen.getByText(/平台角色由超级管理员管理/)).toBeVisible();
  expect(screen.getByLabelText("数据组")).toBeEnabled();
});

it("disables self role changes without querying role impact", () => {
  render(wrap({ ...user, id: "admin", role: "super_admin", is_active: true }));
  expect(screen.getByRole("combobox", { name: /^角色/ })).toBeDisabled();
  expect(screen.queryByText("角色影响预览")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /删除/ })).not.toBeInTheDocument();
  expect(api.previewRoleChange).not.toHaveBeenCalled();
  expect(api.changeRole).not.toHaveBeenCalled();
});

it("shows a write rejection even when the role preview allowed the change", async () => {
  api.previewRoleChange.mockImplementation(async (_id: string, role: string) => ({
    ...preview,
    requested_role: role,
  }));
  api.changeRole.mockRejectedValue(new Error("项目授权已变化，请重新预览"));
  render(wrap(user));
  await screen.findByText(/另涉及 2 个管理范围外/);
  fireEvent.change(screen.getByLabelText("角色"), { target: { value: "viewer" } });
  const save = screen.getByRole("button", { name: "保存" });
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(save);
  expect(await screen.findByText("项目授权已变化，请重新预览")).toBeVisible();
  expect(screen.getByRole("dialog", { name: "编辑成员 · A" })).toBeVisible();
  expect(api.changeRole).toHaveBeenCalledTimes(1);
  expect(api.changeRole).toHaveBeenCalledWith("a", "viewer");
});
