import { QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/mocks/server";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { createTestQueryClient } from "@/test/queryClient";
import { renderWithProviders } from "@/test/renderWithProviders";
import {
  DEFAULT_PROJECTS,
  DEFAULT_USERS,
  INACTIVE_USER,
  createUser,
  installUsersApi,
} from "@/test/usersApi";

vi.mock("@/components/users/BulkInviteModal", () => ({
  BulkInviteModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="bulk-invite-modal">BulkInviteModal</div> : null,
}));
vi.mock("@/components/users/BulkGroupAssignmentModal", () => ({
  BulkGroupAssignmentModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="bulk-group-modal">BulkGroupAssignmentModal</div> : null,
}));
vi.mock("@/components/users/GroupManageModal", () => ({
  GroupManageModal: () => null,
}));
vi.mock("@/components/users/OffboardingDialog", () => ({
  OffboardingDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="offboarding-dialog">OffboardingDialog</div> : null,
  ReactivateDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="reactivate-dialog">ReactivateDialog</div> : null,
}));

const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: Object.assign(
      <T,>(sel: (s: { push: typeof mockPushToast }) => T) => sel({ push: mockPushToast }),
      { getState: () => ({ push: mockPushToast }) },
    ),
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

const createObjectURL = vi.fn((_blob: Blob) => "blob:mock-export");
const downloads: HTMLAnchorElement[] = [];
function onDownload(event: MouseEvent) {
  if (event.target instanceof HTMLAnchorElement && event.target.download) {
    downloads.push(event.target);
    event.preventDefault();
  }
}

beforeAll(() => {
  document.addEventListener("click", onDownload);
});
afterAll(() => {
  document.removeEventListener("click", onDownload);
});

beforeEach(() => {
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = vi.fn();
    },
  );
  downloads.length = 0;
  createObjectURL.mockClear();
  mockPushToast.mockReset();
  onlineManager.setOnline(true);
  seedAuthUser(createTestUser({ id: ACTOR_ID, role: "super_admin" }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
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

  it("项目管理员打开带全局范围的邀请记录，只查询、统计和导出自己的邀请", async () => {
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    installUsersApi();
    const reads: URL[] = [];
    const own = {
      id: "own-invite",
      email: "sample+own@example.org",
      role: "employee",
      status: "pending",
      invited_by: ACTOR_ID,
      expires_at: "2027-01-01T00:00:00Z",
    };
    const other = {
      ...own,
      id: "other-invite",
      email: "sample+other@example.org",
      invited_by: "another-admin",
    };
    server.use(
      http.get("*/api/v1/invitations/:action", ({ request, params }) => {
        const url = new URL(request.url);
        reads.push(url);
        const items = url.searchParams.get("scope") === "me" ? [own] : [own, other];
        if (params.action === "query") {
          return HttpResponse.json({
            items,
            total: items.length,
            page: 1,
            pages: 1,
            page_size: 20,
          });
        }
        if (params.action === "stats") {
          return HttpResponse.json({
            total: items.length,
            pending: items.length,
            accepted: 0,
            expired: 0,
            revoked: 0,
          });
        }
        if (params.action === "export")
          return HttpResponse.text(items.map((row) => row.email).join("\n"));
        return new HttpResponse(null, { status: 404 });
      }),
    );
    renderUsers("/users?q=Alice&invite_q=sample&invite_scope=all");
    await screen.findByText("Alice");
    fireEvent.click(screen.getByRole("button", { name: "邀请记录" }));
    expect(await screen.findByText(own.email)).toBeVisible();
    expect(reads.filter((url) => url.searchParams.get("scope") !== "me").map(String)).toEqual([]);
    expect(screen.queryByText(other.email)).not.toBeInTheDocument();
    expect(await screen.findByText("筛选结果 1")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "导出筛选结果" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "导出筛选结果" })).toBeEnabled());
    expect(downloads.map((anchor) => anchor.download)).toEqual(["invitations.csv"]);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(await createObjectURL.mock.calls[0][0].text()).toBe(own.email);
    expect([...new Set(reads.map((url) => url.pathname))].sort()).toEqual([
      "/api/v1/invitations/export",
      "/api/v1/invitations/query",
      "/api/v1/invitations/stats",
    ]);
    for (const url of reads) {
      expect(url.searchParams.get("scope"), url.href).toBe("me");
      expect(url.searchParams.get("search"), url.href).toBe("sample");
    }
    expect(new URLSearchParams(locationSearch()).get("q")).toBe("Alice");
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

  it("从成员页生成项目邀请：创建和复制不发信，明确点击邮件后只发送一次", async () => {
    const actor = userEvent.setup();
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    const target = {
      ...DEFAULT_PROJECTS[0],
      id: "project-2",
      name: "Review project",
      display_id: "PRJ-002",
    };
    installUsersApi({ projects: [...DEFAULT_PROJECTS, target] });
    const id = "created-invite";
    const link = "https://app.example.test/invite?t=review";
    const email = "tom+review@example.org";
    const writes: { path: string; body: unknown }[] = [];
    const create = {
      path: "/api/v1/users/invite",
      body: {
        email,
        role: "employee",
        project_id: target.id,
        project_member_role: "reviewer",
        group_name: "质检组",
      },
    };
    const mail = { path: `/api/v1/invitations/${id}/send-email`, body: {} };
    server.use(
      http.post(`*${create.path}`, async ({ request }) => {
        writes.push({ path: new URL(request.url).pathname, body: await request.json() });
        return HttpResponse.json(
          {
            id,
            token: "review",
            invite_url: link,
            expires_at: "2027-01-01T00:00:00Z",
            project_id: target.id,
            project_name: target.name,
            project_member_role: "reviewer",
          },
          { status: 201 },
        );
      }),
      http.post(`*${mail.path}`, async ({ request }) => {
        writes.push({ path: new URL(request.url).pathname, body: await request.json() });
        return HttpResponse.json({
          ok: true,
          invitation_id: id,
          email,
          invite_url: link,
          message: "sent",
        });
      }),
    );
    renderUsers();
    await screen.findByText("Alice");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: /邀请成员/ }));
    await screen.findByText(target.display_id, { selector: "option" });
    fireEvent.change(screen.getByPlaceholderText("newuser@your-org.com"), {
      target: { value: " Tom+Review@Example.ORG " },
    });
    fireEvent.change(screen.getByPlaceholderText("输入或选择项目名称"), {
      target: { value: target.name },
    });
    fireEvent.change(screen.getByLabelText(/^项目职责/), { target: { value: "reviewer" } });
    fireEvent.change(screen.getByPlaceholderText("例如：标注组A"), {
      target: { value: "  质检组  " },
    });
    await actor.click(screen.getByRole("button", { name: "生成邀请链接" }));
    expect(await screen.findByDisplayValue(link)).toBeVisible();
    expect(writes).toEqual([create]);
    await actor.click(screen.getByRole("button", { name: "复制" }));
    expect(await navigator.clipboard.readText()).toBe(link);
    expect(mockPushToast).toHaveBeenCalledWith(expect.objectContaining({ msg: "邀请链接已复制" }));
    expect(writes).toEqual([create]);
    await actor.click(screen.getByRole("button", { name: "发送邀请邮件" }));
    await waitFor(() =>
      expect(mockPushToast).toHaveBeenCalledWith(
        expect.objectContaining({ msg: "邀请邮件已发送" }),
      ),
    );
    expect(writes).toEqual([create, mail]);
    await actor.click(screen.getByRole("button", { name: "继续邀请" }));
    expect(screen.getByRole("button", { name: "生成邀请链接" })).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("取消删除不发送 DELETE，成员仍在列表中", async () => {
    const captured = installUsersApi();
    renderUsers();
    await screen.findByText("Alice");
    fireEvent.click(screen.getAllByTitle("删除账号")[0]);
    expect(screen.getByText(/确认删除以下账号/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByText(/确认删除以下账号/)).not.toBeInTheDocument();
    expect(captured.deletes).toEqual([]);
    expect(screen.getByText("Alice")).toBeVisible();
  });

  it("确认删除 → 发出 DELETE /users/:id、成功 toast 并关闭弹窗", async () => {
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    const captured = installUsersApi({
      users: [{ ...DEFAULT_USERS[0], is_managed: true, is_lifecycle_managed: true }],
    });
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

  it("保留删除确认，显示提交时服务端返回的权限变化", async () => {
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    const captured = installUsersApi({
      deleteStatus: 403,
      deleteBody: { detail: "目标现在持有其他项目的任务" },
    });
    renderUsers();
    await screen.findByText("Alice");
    fireEvent.click(screen.getAllByTitle("删除账号")[0]);
    const dialog = screen.getByRole("dialog", { name: "删除账号确认" });
    expect(captured.deletes).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认删除" }));
    expect(await within(dialog).findByText("目标现在持有其他项目的任务")).toBeVisible();
    expect(captured.deletes).toEqual(["u1"]);
    expect(screen.getByRole("dialog", { name: "删除账号确认" })).toBeVisible();
    expect(mockPushToast).not.toHaveBeenCalledWith(expect.objectContaining({ kind: "success" }));
  });

  it("本人行不能打开编辑成员或删除账号", async () => {
    installUsersApi({
      users: [createUser({ id: ACTOR_ID, name: "本人", role: "super_admin", is_managed: true })],
    });
    renderUsers();
    const row = await screen.findByRole("row", { name: /本人/ });
    expect(within(row).getByTitle("不能修改自己")).toBeDisabled();
    expect(within(row).queryByTitle("编辑成员")).not.toBeInTheDocument();
    expect(within(row).queryByTitle("删除账号")).not.toBeInTheDocument();
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

    expect(screen.getAllByTitle("编辑成员")).toHaveLength(3);
    expect(screen.getAllByTitle("删除账号")).toHaveLength(3);

    expect(screen.getByTitle("仅可查看：超级管理员账号")).toBeDisabled();
    expect(screen.getAllByTitle(/编辑成员|仅可查看/)).toHaveLength(4);
  });

  it("跨项目行 is_lifecycle_managed=false：保留账号操作，隐藏离职/删除入口", async () => {
    seedAuthUser(createTestUser({ id: ACTOR_ID, role: "project_admin" }));
    const crossProjectUser = createUser({
      id: "u6",
      name: "Cross Project",
      email: "cross@example.com",
      is_managed: true,
      is_lifecycle_managed: false,
    });
    const captured = installUsersApi({
      users: [crossProjectUser],
      groups: [
        {
          id: "g1",
          name: "测试数据组",
          description: null,
          member_count: 1,
          created_at: "2026-01-01",
        },
      ],
    });
    const groupWrites: unknown[] = [];
    server.use(
      http.patch("*/api/v1/users/u6/group", async ({ request }) => {
        groupWrites.push(await request.json());
        crossProjectUser.group_id = "g1";
        crossProjectUser.group_name = "测试数据组";
        return HttpResponse.json(crossProjectUser);
      }),
    );
    renderUsers();

    expect(await screen.findByText("Cross Project")).toBeInTheDocument();
    expect(screen.getByTitle("编辑成员")).toBeInTheDocument();
    expect(screen.getByTitle("重置密码")).toBeInTheDocument();
    expect(screen.queryByTitle("离职处理")).not.toBeInTheDocument();
    expect(screen.queryByTitle("删除账号")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTitle("编辑成员"));
    const dialog = screen.getByRole("dialog", { name: "编辑成员 · Cross Project" });
    expect(within(dialog).getByDisplayValue(crossProjectUser.email)).toHaveAttribute("readonly");
    expect(within(dialog).getByText(/平台角色由超级管理员管理/)).toBeVisible();
    const group = within(dialog).getByRole("combobox", { name: "数据组" });
    expect(within(dialog).getAllByRole("combobox")).toEqual([group]);
    expect(within(dialog).queryByRole("button", { name: /删除/ })).not.toBeInTheDocument();

    await userEvent.selectOptions(
      group,
      within(dialog).getByRole("option", { name: "测试数据组" }),
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    const row = screen.getByRole("row", { name: /Cross Project/ });
    expect(await within(row).findByText("测试数据组")).toBeVisible();
    expect(groupWrites).toEqual([{ group_id: "g1" }]);
    expect(captured.deletes).toEqual([]);
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
      expect(query.get("page_size")).toBe("20");
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

  it("changes the server page size and returns to page one", async () => {
    const captured = installUsersApi({ total: 1000, pages: 50 });
    renderUsers("/users?page=2");
    await screen.findByText("Alice");
    fireEvent.change(screen.getByLabelText("每页成员数"), { target: { value: "50" } });
    await waitFor(() => {
      const query = capturedQuery(captured.pageQueries);
      expect(query.get("page")).toBe("1");
      expect(query.get("page_size")).toBe("50");
    });
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
      expect(query.get("page_size")).toBe("20");
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
