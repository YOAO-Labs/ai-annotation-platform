import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { bindAuthQueryCache } from "@/stores/authQueryCache";
import { RegisterPage } from "./RegisterPage";

const mocks = vi.hoisted(() => ({ resolve: vi.fn(), register: vi.fn() }));
vi.mock("@/api/invitations", () => ({ invitationsApi: mocks }));
let dispose = () => {};
let client: QueryClient;
beforeEach(() => {
  localStorage.clear();
  useAuthStore.setState({ token: null, user: null });
  vi.clearAllMocks();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  dispose = bindAuthQueryCache(client);
});
afterEach(() => {
  cleanup();
  dispose();
  client.clear();
});

it("stops resolving a consumed invitation before login clears the anonymous cache", async () => {
  mocks.resolve.mockResolvedValue({
    email: "new@example.test",
    role: "employee",
    project_id: "p1",
    project_name: "Road QA",
    expires_at: "2030-01-01",
  });
  mocks.register.mockImplementation(async () => {
    mocks.resolve.mockRejectedValue(new Error("邀请已使用"));
    return {
      access_token: "registered-token",
      user: { id: "new", email: "new@example.test", role: "employee" },
      acceptance: {
        project_id: "p1",
        project_name: "Road QA",
        next_action: "wait_for_allocation",
        next_action_label: "等待分派",
        responsible_person_name: "Manager",
        active_batch_count: 0,
      },
    };
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/register?token=invite-token"]}>
        <RegisterPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.change(await screen.findByPlaceholderText("如何在平台中称呼你"), {
    target: { value: "New member" },
  });
  for (const input of document.querySelectorAll('input[type="password"]')) {
    fireEvent.change(input, { target: { value: "Test1234" } });
  }
  fireEvent.click(screen.getByRole("button", { name: "完成注册并登录" }));
  expect(await screen.findByRole("heading", { name: "邀请已完成" })).toBeInTheDocument();
  await act(async () => {
    await Promise.resolve();
  });
  expect(useAuthStore.getState().user?.id).toBe("new");
  expect(mocks.resolve).toHaveBeenCalledTimes(1);
  expect(screen.getByText("等待分派")).toBeInTheDocument();
});
