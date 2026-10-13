/**
 * v0.8.5 · InviteUserModal 单测：open=false 不渲染 / 角色选项随 actor / 提交流 /
 * 错误提示 / 邀请生成后切到结果视图 + 复制 + 继续邀请。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useAuthStore } from "@/stores/authStore";

vi.mock("@/api/groups", () => ({ groupsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/api/projects", () => ({ projectsApi: { list: vi.fn().mockResolvedValue([]) } }));

const mockMutate = vi.fn();
const mockReset = vi.fn();
const mutationState: any = {
  isPending: false,
  isError: false,
  error: null,
  mutate: mockMutate,
  reset: mockReset,
};
const mockPushToast = vi.fn();
const emailMutationState = {
  isPending: false,
  mutate: vi.fn(),
};

vi.mock("@/hooks/useInvitation", () => ({
  useInviteUser: () => mutationState,
}));
vi.mock("@/hooks/useInvitations", () => ({
  useSendInvitationEmail: () => emailMutationState,
}));
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { InviteUserModal } from "./InviteUserModal";

function setRole(role: string | null) {
  useAuthStore.setState({
    user: role
      ? ({
          id: "u1",
          email: "u@x.com",
          name: "U",
          role,
          status: "online",
        } as any)
      : null,
  });
}

describe("InviteUserModal", () => {
  beforeEach(() => {
    mockMutate.mockReset();
    mockReset.mockReset();
    mockPushToast.mockReset();
    mutationState.isPending = false;
    mutationState.isError = false;
    mutationState.error = null;
    emailMutationState.isPending = false;
    emailMutationState.mutate.mockReset();
    setRole("super_admin");
  });

  it("open=false → 不渲染", async () => {
    await act(async () => {
      render(<InviteUserModal open={false} onClose={() => {}} />);
    });
    expect(screen.queryByText("邀请新成员")).not.toBeInTheDocument();
  });

  it("super_admin → 账号角色 select 含 4 个平台角色", async () => {
    setRole("super_admin");
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    const select = screen.getByDisplayValue(/超管|超级|管理员/) as HTMLSelectElement;
    expect(select.options.length).toBe(4);
  });

  it("project_admin → 只能邀请员工 / 观察者账号", async () => {
    setRole("project_admin");
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    const selects = document.querySelectorAll("select");
    expect(selects.length).toBe(1);
    expect((selects[0] as HTMLSelectElement).options.length).toBe(2);
  });

  it("employee → 角色 select 0 选项（虽允许打开但无可邀请角色）", async () => {
    setRole("employee");
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    const sel = document.querySelector("select") as HTMLSelectElement;
    expect(sel.options.length).toBe(0);
  });

  it("空 email 提交 → 不调用 invite.mutate", async () => {
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    const form = document.querySelector("form")!;
    fireEvent.submit(form);
    expect(mockMutate).not.toHaveBeenCalled();
  });

  it("有效填写后提交 → 调用 invite.mutate(规范化 email + 平台角色)", async () => {
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    const email = screen.getByPlaceholderText("newuser@your-org.com") as HTMLInputElement;
    fireEvent.change(email, { target: { value: "  Tom@Example.COM  " } });
    fireEvent.change(screen.getByDisplayValue(/超管|超级|管理员/), {
      target: { value: "employee" },
    });
    fireEvent.change(screen.getByPlaceholderText("例如：标注组A"), {
      target: { value: "  群组1  " },
    });
    fireEvent.click(screen.getByText("生成邀请链接"));
    expect(mockMutate).toHaveBeenCalledWith(
      { email: "tom@example.com", role: "employee", group_name: "群组1" },
      expect.any(Object),
    );
  });

  it("空 group_name → 入参不带 group_name 字段（undefined）", async () => {
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    fireEvent.change(screen.getByPlaceholderText("newuser@your-org.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("生成邀请链接"));
    const arg = mockMutate.mock.calls[0][0];
    expect(arg.group_name).toBeUndefined();
  });

  it("invite.isPending → 按钮显示「生成中...」+ disabled", async () => {
    mutationState.isPending = true;
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    const btn = screen.getByText("生成中...") as HTMLButtonElement;
    expect(btn).toBeInTheDocument();
    expect(btn.closest("button")?.disabled).toBe(true);
  });

  it("invite.isError → 显示错误信息", async () => {
    mutationState.isError = true;
    mutationState.error = new Error("邮箱已存在");
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    expect(screen.getByText("邮箱已存在")).toBeInTheDocument();
  });

  it("成功生成 → 切到结果视图，显示链接 + 复制按钮", async () => {
    mockMutate.mockImplementation((_args, opts) =>
      opts?.onSuccess?.({
        invite_url: "https://x.com/invite?t=abc",
        expires_at: new Date(Date.now() + 7 * 86400000).toISOString(),
      }),
    );
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    fireEvent.change(screen.getByPlaceholderText("newuser@your-org.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("生成邀请链接"));
    await waitFor(() => screen.getByText("邀请已生成"));
    expect(screen.getByDisplayValue("https://x.com/invite?t=abc")).toBeInTheDocument();
    expect(screen.getByText("复制")).toBeInTheDocument();
    expect(screen.getByText("继续邀请")).toBeInTheDocument();
    expect(screen.getByText("完成")).toBeInTheDocument();
  });

  it("点击复制 → 调用 clipboard 并 toast", async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn(async () => undefined) },
    });
    mockMutate.mockImplementation((_args, opts) =>
      opts?.onSuccess?.({
        invite_url: "https://x.com/invite?t=abc",
        expires_at: new Date(Date.now() + 86400000 * 5).toISOString(),
      }),
    );
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    fireEvent.change(screen.getByPlaceholderText("newuser@your-org.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("生成邀请链接"));
    await waitFor(() => screen.getByText("邀请已生成"));
    fireEvent.click(screen.getByText("复制"));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("https://x.com/invite?t=abc");
    });
    expect(mockPushToast).toHaveBeenCalledWith({
      msg: "邀请链接已复制",
      kind: "success",
    });
  });

  it("结果页明确点击发送邀请邮件 → 调用发送接口", async () => {
    mockMutate.mockImplementation((_args, opts) =>
      opts?.onSuccess?.({
        id: "inv-1",
        invite_url: "https://x.com/invite?t=abc",
        expires_at: new Date(Date.now() + 86400000).toISOString(),
      }),
    );
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    fireEvent.change(screen.getByPlaceholderText("newuser@your-org.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("生成邀请链接"));
    await waitFor(() => screen.getByText("邀请已生成"));
    fireEvent.click(screen.getByRole("button", { name: "发送邀请邮件" }));
    expect(emailMutationState.mutate).toHaveBeenCalledWith("inv-1", expect.any(Object));
  });

  it("点击「继续邀请」→ 切回表单视图", async () => {
    mockMutate.mockImplementation((_args, opts) =>
      opts?.onSuccess?.({
        invite_url: "https://x.com/invite?t=abc",
        expires_at: new Date(Date.now() + 86400000).toISOString(),
      }),
    );
    await act(async () => {
      render(<InviteUserModal open={true} onClose={() => {}} />);
    });
    fireEvent.change(screen.getByPlaceholderText("newuser@your-org.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("生成邀请链接"));
    await waitFor(() => screen.getByText("邀请已生成"));
    fireEvent.click(screen.getByText("继续邀请"));
    expect(screen.getByText("邀请新成员")).toBeInTheDocument();
  });
});
