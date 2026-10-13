/**
 * UsersPage — URL-driven member list, filters/paging, export, delete and
 * permission/lifecycle branches.
 *
 * The page's data dependencies run for real against MSW at the HTTP boundary
 * (`installUsersApi`), with a fresh QueryClient, the real query hooks,
 * permission table and seeded auth store per test. Only the heavy modal
 * components and the sonner-backed toast store stay stubbed as narrow
 * page-wiring stand-ins (the page's own contract is routing/URL/filters/query
 * shaping, not each modal's body).
 */
import { QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { createTestQueryClient } from "@/test/queryClient";
import { renderWithProviders } from "@/test/renderWithProviders";
import { DEFAULT_USERS, INACTIVE_USER, createUser, installUsersApi } from "@/test/usersApi";

// Heavy modal bodies are deliberate stand-ins: this suite protects the UsersPage
// routing/filter/query/export/delete contract, not each modal's own flow. They
// keep their own suites.
vi.mock("@/components/users/InviteUserModal", () => ({
  InviteUserModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="invite-modal">InviteModal</div> : null,
}));
vi.mock("@/components/users/BulkInviteModal", () => ({
  BulkInviteModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="bulk-invite-modal">BulkInviteModal</div> : null,
}));
vi.mock("@/components/users/BulkGroupAssignmentModal", () => ({
  BulkGroupAssignmentModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="bulk-group-modal">BulkGroupAssignmentModal</div> : null,
}));
vi.mock("@/components/users/EditUserModal", () => ({
  EditUserModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="edit-modal">EditModal</div> : null,
}));
vi.mock("@/components/users/GroupManageModal", () => ({
  GroupManageModal: () => null,
}));
vi.mock("@/components/users/InvitationListPanel", () => ({
  InvitationListPanel: () => <div data-testid="invitation-panel">InvitationPanel</div>,
}));
vi.mock("@/components/users/OffboardingDialog", () => ({
  OffboardingDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="offboarding-dialog">OffboardingDialog</div> : null,
  ReactivateDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="reactivate-dialog">ReactivateDialog</div> : null,
}));

// The real store delegates to sonner and holds no toast list, so a narrow spy on
// `push` is the page's observable contract without rendering the portal.
const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: typeof mockPushToast }) => T) =>
      sel({ push: mockPushToast }),
  };
});

import { UsersPage } from "./UsersPage";

const ACTOR_ID = "me-id";

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location-search">{location.search}</output>
      <button aria-label="浏览器后退" onClick={() => navigate(-1)} />
      <button aria-label="浏览器前进" onClick={() => navigate(1)} />
    </>
  );
}

function renderUsers(initialEntries: string | string[] = "/users") {
  const entries = Array.isArray(initialEntries) ? initialEntries : [initialEntries];
  return renderWithProviders(<UsersPage />, {
    initialEntries: entries,
    children: <LocationProbe />,
  });
}

/**
 * `renderWithProviders` uses MemoryRouter's default initial index (the last
 * entry); one regression needs an explicit first-entry start, so this variant
 * composes the same fixtures with `initialIndex`.
 */
function renderUsersAt(initialEntries: string[], initialIndex: number) {
  const queryClient = createTestQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries} initialIndex={initialIndex}>
        <LocationProbe />
        <UsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

const locationSearch = () => screen.getByTestId("location-search").textContent ?? "";
const capturedQuery = (queries: URLSearchParams[]) => queries[queries.length - 1];

beforeAll(() => {
  // jsdom does not implement object URLs; the page's CSV download needs them.
  URL.createObjectURL = (() => "blob:mock-export") as typeof URL.createObjectURL;
  URL.revokeObjectURL = (() => undefined) as typeof URL.revokeObjectURL;
});

beforeEach(() => {
  resetUnexpectedApiRequests();
  mockPushToast.mockReset();
  onlineManager.setOnline(true);
  seedAuthUser(createTestUser({ id: ACTOR_ID, role: "super_admin" }));
});

afterEach(() => {
  vi.useRealTimers();
  expectNoUnexpectedApiRequests();
  onlineManager.setOnline(true);
  resetAuthUser();
});

describe("UsersPage", () => {
  it("渲染页面标题与成员表格", async () => {
    installUsersApi();
    renderUsers();
    expect(screen.getByText("用户与权限")).toBeInTheDocument();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
  });

  it("isLoading=true → 显示「加载中...」", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    installUsersApi({ queryGate: gate });
    renderUsers();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
    release();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
  });

  it("resumes search while a browser-navigation draft is still debouncing", async () => {
    const captured = installUsersApi({ total: 1000, pages: 40 });
    const view = renderUsers([
      "/users?q=restored&page=3&status=all&role=annotator&keep=yes",
      "/users?q=old&status=all&role=annotator&keep=yes",
    ]);
    try {
      // Let the initial URL-driven query settle with real timers before narrowly
      // freezing time for the debounce boundary; freezing time freezes MSW.
      await waitFor(() => expect(captured.pageQueries.length).toBeGreaterThan(0));
      fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
      const input = screen.getByPlaceholderText(/搜索姓名或邮箱/);
      expect(input).toHaveValue("restored");

      vi.useFakeTimers();
      fireEvent.change(input, { target: { value: "Alice" } });
      fireEvent.change(screen.getByLabelText("账号状态"), { target: { value: "inactive" } });
      await act(async () => vi.advanceTimersByTime(249));
      expect(new URLSearchParams(locationSearch()).get("q")).toBe("restored");
      await act(async () => vi.advanceTimersByTime(1));
      vi.useRealTimers();

      await waitFor(() => {
        const afterDebounce = new URLSearchParams(locationSearch());
        expect(afterDebounce.get("q")).toBe("Alice");
        expect(afterDebounce.get("page")).toBeNull();
        expect(afterDebounce.get("status")).toBe("inactive");
        expect(afterDebounce.get("role")).toBe("annotator");
        expect(afterDebounce.get("keep")).toBe("yes");
      });
      await waitFor(() => expect(capturedQuery(captured.pageQueries).get("search")).toBe("Alice"));

      fireEvent.change(input, { target: { value: "Alice updated" } });
      await waitFor(() =>
        expect(new URLSearchParams(locationSearch()).get("q")).toBe("Alice updated"),
      );
      await waitFor(() =>
        expect(capturedQuery(captured.statsQueries).get("search")).toBe("Alice updated"),
      );
    } finally {
      vi.useRealTimers();
      view.unmount();
    }
  });

  it("搜索框过滤：输入 'Alice' 后只显示 Alice", async () => {
    const captured = installUsersApi();
    renderUsers();
    // Wait for the unfiltered page before typing so the assertion is about the
    // debounced server query, not the initial empty frame.
    expect(await screen.findByText("Bob")).toBeInTheDocument();
    const searchInput = screen.getByPlaceholderText(/搜索姓名或邮箱/);
    fireEvent.change(searchInput, { target: { value: "Alice" } });
    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeInTheDocument();
      expect(screen.queryByText("Bob")).not.toBeInTheDocument();
    });
    await waitFor(() => expect(capturedQuery(captured.pageQueries).get("search")).toBe("Alice"));
  });

  it("点击「角色」tab → 显示角色卡片", () => {
    installUsersApi();
    renderUsers();
    const roleTab = screen.getAllByRole("button").find((b) => b.textContent?.includes("角色"));
    fireEvent.click(roleTab!);
    expect(screen.getAllByText("超级管理员").length).toBeGreaterThan(0);
  });

  it("点击「邀请记录」tab → 渲染 InvitationListPanel", () => {
    installUsersApi();
    renderUsers();
    const invTab = screen.getAllByRole("button").find((b) => b.textContent?.includes("邀请记录"));
    fireEvent.click(invTab!);
    expect(screen.getByTestId("invitation-panel")).toBeInTheDocument();
  });

  it("点击「导出名单」→ 以当前筛选请求 CSV + toast 成功", async () => {
    const captured = installUsersApi();
    renderUsers();
    await screen.findByText("Alice");
    fireEvent.click(screen.getByRole("button", { name: /导出名单/ }));
    await waitFor(() => expect(captured.exports).toHaveLength(1));
    const exported = capturedQuery(captured.exports);
    expect(exported.get("format")).toBe("csv");
    expect(exported.get("status")).toBe("active");
    await waitFor(() =>
      expect(mockPushToast).toHaveBeenCalledWith(expect.objectContaining({ kind: "success" })),
    );
  });

  it("点击「邀请成员」→ 显示 InviteModal", () => {
    installUsersApi();
    renderUsers();
    fireEvent.click(screen.getByRole("button", { name: /邀请成员/ }));
    expect(screen.getByTestId("invite-modal")).toBeInTheDocument();
  });

  it("点击删除按钮 → 显示确认弹窗", async () => {
    installUsersApi();
    renderUsers();
    await screen.findByText("Alice");
    fireEvent.click(screen.getAllByTitle("删除账号")[0]);
    expect(screen.getByText(/确认删除以下账号/)).toBeInTheDocument();
  });

  it("删除确认弹窗 → 点取消关闭弹窗", async () => {
    installUsersApi();
    renderUsers();
    await screen.findByText("Alice");
    fireEvent.click(screen.getAllByTitle("删除账号")[0]);
    expect(screen.getByText(/确认删除以下账号/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByText(/确认删除以下账号/)).not.toBeInTheDocument();
  });

  it("确认删除 → 发出 DELETE /users/:id、成功 toast 并关闭弹窗", async () => {
    const captured = installUsersApi();
    renderUsers();
    await screen.findByText("Alice");
    fireEvent.click(screen.getAllByTitle("删除账号")[0]);
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(captured.deletes).toEqual(["u1"]));
    await waitFor(() =>
      expect(mockPushToast).toHaveBeenCalledWith(
        expect.objectContaining({ kind: "success", msg: expect.stringContaining("Alice") }),
      ),
    );
    expect(screen.queryByText(/确认删除以下账号/)).not.toBeInTheDocument();
  });

  it("用户列表 403 → 显示无权查看文案（真实 ApiError 状态映射）", async () => {
    installUsersApi({ queryStatus: 403 });
    renderUsers();
    expect(await screen.findByText("无权查看用户列表")).toBeInTheDocument();
    expect(screen.getByText(/没有权限查看用户列表/)).toBeInTheDocument();
  });

  it("数据组 tab 空态 → 显示暂无数据组提示", () => {
    installUsersApi({ groups: [] });
    renderUsers();
    const groupTab = screen.getAllByRole("button").find((b) => b.textContent?.includes("数据组"));
    fireEvent.click(groupTab!);
    expect(screen.getByText(/暂无数据组/)).toBeInTheDocument();
  });

  it("账号状态筛选显示已停用账号，并保留继续交接与恢复入口", async () => {
    installUsersApi({ users: [INACTIVE_USER] });
    renderUsers();
    fireEvent.change(screen.getByLabelText("账号状态"), { target: { value: "inactive" } });
    expect(await screen.findByText("Emergency Bob")).toBeInTheDocument();
    expect(screen.getByText("紧急停用")).toBeInTheDocument();
    expect(screen.getByTitle("继续交接")).toBeInTheDocument();
    expect(screen.getByTitle("恢复账号")).toBeInTheDocument();
    expect(screen.queryByTitle("删除账号")).not.toBeInTheDocument();
  });

  it("project_admin 可见并直接操作未分配标注员，超管行只读", async () => {
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    installUsersApi({
      users: [
        ...DEFAULT_USERS.map((user) => ({ ...user, is_managed: true })),
        createUser({
          id: "u4",
          name: "Free Annotator",
          email: "free@example.com",
          is_managed: true,
        }),
        createUser({
          id: "u5",
          name: "Root",
          email: "root@example.com",
          role: "super_admin",
          status: "online",
          is_managed: false,
        }),
      ],
    });
    renderUsers();

    expect(await screen.findByText("Free Annotator")).toBeInTheDocument();
    expect(screen.getByText("Root")).toBeInTheDocument();

    // Owned members plus the unassigned annotator keep their full actions.
    expect(screen.getAllByTitle("编辑成员")).toHaveLength(3);
    expect(screen.getAllByTitle("删除账号")).toHaveLength(3);

    // Super-admin rows are read-only.
    expect(screen.getByTitle("仅可查看：超级管理员账号")).toBeDisabled();
    expect(screen.getAllByTitle(/编辑成员|仅可查看/)).toHaveLength(4);
  });

  it("跨项目行 is_lifecycle_managed=false：保留账号操作，隐藏离职/删除入口", async () => {
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    installUsersApi({
      users: [
        createUser({
          id: "u6",
          name: "Cross Project",
          email: "cross@example.com",
          is_managed: true,
          is_lifecycle_managed: false,
        }),
      ],
    });
    renderUsers();

    expect(await screen.findByText("Cross Project")).toBeInTheDocument();
    // Account-level actions stay.
    expect(screen.getByTitle("编辑成员")).toBeInTheDocument();
    expect(screen.getByTitle("重置密码")).toBeInTheDocument();
    // Lifecycle writes are hidden so a click cannot 403.
    expect(screen.queryByTitle("离职处理")).not.toBeInTheDocument();
    expect(screen.queryByTitle("删除账号")).not.toBeInTheDocument();
  });

  it("初次离线且没有用户数据时显示等待网络恢复，而不是空列表", () => {
    onlineManager.setOnline(false);
    installUsersApi();
    renderUsers();
    expect(screen.getByText("暂时离线，等待网络恢复")).toBeInTheDocument();
    expect(screen.getByText(/网络恢复后会自动继续加载用户列表/)).toBeInTheDocument();
    expect(screen.queryByText(/暂无启用账号/)).not.toBeInTheDocument();
  });

  it("已有用户数据但请求暂停时保留旧数据并显示离线提示", async () => {
    installUsersApi();
    const { queryClient } = renderUsers();
    expect(await screen.findByText("Alice")).toBeInTheDocument();

    onlineManager.setOnline(false);
    void queryClient.invalidateQueries({ queryKey: ["users", "page"] });

    expect(await screen.findByText(/当前离线，正在等待网络恢复/)).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
  });

  it("paginates 1000 members and passes the same project and role filters to stats and export", async () => {
    const captured = installUsersApi({ total: 1000, pages: 40 });
    renderUsers();
    await screen.findByText("Alice");

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      const query = capturedQuery(captured.pageQueries);
      expect(query.get("page")).toBe("2");
      expect(query.get("page_size")).toBe("25");
    });

    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    fireEvent.change(screen.getByLabelText("项目筛选"), { target: { value: "project-1" } });
    fireEvent.change(screen.getByLabelText("角色筛选"), { target: { value: "employee" } });

    await waitFor(() => {
      const query = capturedQuery(captured.pageQueries);
      expect(query.get("project_id")).toBe("project-1");
      expect(query.get("role")).toBe("employee");
      expect(query.get("page")).toBe("1");
    });
    await waitFor(() => {
      const stats = capturedQuery(captured.statsQueries);
      expect(stats.get("project_id")).toBe("project-1");
      expect(stats.get("role")).toBe("employee");
      expect(stats.get("status")).toBe("active");
    });

    fireEvent.click(screen.getByRole("button", { name: /导出名单/ }));
    await waitFor(() => expect(captured.exports).toHaveLength(1));
    const exported = capturedQuery(captured.exports);
    expect(exported.get("project_id")).toBe("project-1");
    expect(exported.get("role")).toBe("employee");
    expect(exported.get("status")).toBe("active");
  });

  it("clears selected members when filters change and preserves them across pages", async () => {
    installUsersApi({ total: 1000, pages: 40 });
    renderUsers("/users?page=2");
    await screen.findByText("Alice");

    fireEvent.click(screen.getByLabelText("选择 Alice"));
    expect(screen.getByText(/已选择 1 名成员/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    fireEvent.change(screen.getByLabelText("角色筛选"), { target: { value: "employee" } });
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("?role=employee"),
    );
    expect(screen.queryByText(/已选择 1 名成员/)).not.toBeInTheDocument();
    // Let the role-filtered page settle before paging again.
    await screen.findByText("Alice");

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("?role=employee&page=2"),
    );
    await screen.findByText("Alice");
    fireEvent.click(screen.getByLabelText("选择 Alice"));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("?role=employee&page=3"),
    );
    expect(screen.getByText(/已选择 1 名成员/)).toBeInTheDocument();
  });

  it("rehydrates filter state on browser back and forward", async () => {
    const captured = installUsersApi({
      total: 1000,
      pages: 40,
      // The rehydrated URL filters to an inactive account, so the row the test
      // selects must match that server filter.
      users: [
        createUser({ id: "u1", name: "Alice", email: "alice@example.com", is_active: false }),
        createUser({ id: "u2", name: "Bob", email: "bob@example.com" }),
      ],
    });
    renderUsers(["/users", "/users?status=inactive&page=2&q=Alice&project_id=project-1"]);
    await screen.findByText("Alice");

    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    expect(screen.getByLabelText("账号状态")).toHaveValue("inactive");
    expect(screen.getByPlaceholderText(/搜索姓名或邮箱/)).toHaveValue("Alice");
    expect(screen.getByLabelText("项目筛选")).toHaveValue("project-1");
    fireEvent.click(screen.getByLabelText("选择 Alice"));
    expect(screen.getByText(/已选择 1 名成员/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() => {
      expect(screen.getByLabelText("账号状态")).toHaveValue("active");
      expect(screen.getByTestId("location-search")).toHaveTextContent(/^$/);
    });
    expect(screen.queryByText(/已选择 1 名成员/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "浏览器前进" }));
    await waitFor(() => {
      expect(screen.getByLabelText("账号状态")).toHaveValue("inactive");
      expect(screen.getByPlaceholderText(/搜索姓名或邮箱/)).toHaveValue("Alice");
      expect(screen.getByLabelText("项目筛选")).toHaveValue("project-1");
      expect(screen.getByTestId("location-search")).toHaveTextContent(
        "?status=inactive&page=2&q=Alice&project_id=project-1",
      );
    });
    await waitFor(() => {
      const query = capturedQuery(captured.pageQueries);
      expect(query.get("status")).toBe("inactive");
      expect(query.get("project_id")).toBe("project-1");
      expect(query.get("search")).toBe("Alice");
      expect(query.get("page")).toBe("2");
      expect(query.get("page_size")).toBe("25");
    });
  });

  it("records discrete member filter changes as browser history entries", async () => {
    installUsersApi();
    renderUsersAt(["/users", "/users?status=inactive"], 0);
    await screen.findByText("Alice");

    fireEvent.change(screen.getByLabelText("账号状态"), { target: { value: "inactive" } });
    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("?status=inactive");
    });

    fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent(/^$/);
      expect(screen.getByLabelText("账号状态")).toHaveValue("active");
    });
  });
});
