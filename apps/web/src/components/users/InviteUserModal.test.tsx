import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, expect, it, vi } from "vitest";
import { server } from "@/mocks/server";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";
import { installUsersApi } from "@/test/usersApi";
import { InviteUserModal } from "./InviteUserModal";

afterEach(() => {
  cleanup();
  resetAuthUser();
});

function open(role: string) {
  seedAuthUser(createTestUser({ role }));
  installUsersApi();
  renderWithProviders(<InviteUserModal open onClose={vi.fn()} />);
}

it.each([
  ["super_admin", ["employee", "project_admin", "super_admin", "viewer"]],
  ["project_admin", ["employee", "viewer"]],
  ["employee", []],
  ["viewer", []],
] as const)("%s 只能选择其可邀请的平台角色", (actor, allowed) => {
  open(actor);
  const role = screen.getByLabelText(/^账号角色/) as HTMLSelectElement;
  expect(Array.from(role.options, (option) => option.value).sort()).toEqual(allowed);
});

it("无项目邀请不能空提交或重复提交；邮箱冲突后保留输入并允许改正", async () => {
  const attempts: unknown[] = [];
  const conflict = new TransformStream<Uint8Array>();
  const writer = conflict.writable.getWriter();
  const link = "https://app.example.test/invite?t=corrected";
  server.use(
    http.post("*/api/v1/users/invite", async ({ request }) => {
      attempts.push(await request.json());
      if (attempts.length === 1)
        return new HttpResponse(conflict.readable, {
          status: 409,
          headers: { "Content-Type": "application/json" },
        });
      return HttpResponse.json(
        { id: "inv-2", token: "corrected", invite_url: link, expires_at: "2027-01-01T00:00:00Z" },
        { status: 201 },
      );
    }),
  );
  open("project_admin");
  const email = screen.getByPlaceholderText("newuser@your-org.com");
  const submit = screen.getByRole("button", { name: "生成邀请链接" });
  fireEvent.click(submit);
  expect(email).toBeInvalid();
  fireEvent.change(screen.getByLabelText(/^账号角色/), { target: { value: "viewer" } });
  fireEvent.change(email, { target: { value: "old@example.org" } });
  fireEvent.click(submit);
  await waitFor(() => expect(submit).toBeDisabled());
  fireEvent.click(submit);
  await writer.write(new TextEncoder().encode(JSON.stringify({ detail: "邮箱已存在" })));
  await writer.close();
  expect(await screen.findByText("邮箱已存在")).toBeVisible();
  expect(email).toHaveValue("old@example.org");
  expect(submit).toBeEnabled();
  fireEvent.change(email, { target: { value: "new@example.org" } });
  fireEvent.click(submit);
  expect(await screen.findByDisplayValue(link)).toBeVisible();
  expect(attempts).toEqual([
    { email: "old@example.org", role: "viewer" },
    { email: "new@example.org", role: "viewer" },
  ]);
});
