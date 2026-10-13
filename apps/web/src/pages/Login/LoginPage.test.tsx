/**
 * v0.8.5 · LoginPage 单测：已登录跳转 / 表单提交 / 错误提示 / loading 态 /
 * eye toggle / 注册入口可见性；Issue #123 · 登录返回目标过滤错误页与角色默认首页。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { ApiError } from "@/api/client";

const mockLogin: any = { isPending: false, isError: false, error: null, mutate: vi.fn() };
const mockRegStatus: any = { data: { open_registration_enabled: true } };
const mockResend: any = { isPending: false, mutate: vi.fn() };

vi.mock("@/hooks/useAuth", () => ({
  useLogin: () => mockLogin,
}));
vi.mock("@/hooks/useInvitation", () => ({
  useRegistrationStatus: () => mockRegStatus,
  useResendVerification: () => mockResend,
}));

import { LoginPage } from "./LoginPage";

function renderUI(initial = "/login") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/dashboard" element={<div>DASHBOARD</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null });
    mockLogin.isPending = false;
    mockLogin.isError = false;
    mockLogin.error = null;
    mockLogin.mutate = vi.fn();
    mockRegStatus.data = { open_registration_enabled: true };
  });

  it("已登录 → 跳 /dashboard", () => {
    useAuthStore.setState({ token: "t", user: null });
    renderUI();
    expect(screen.getByText("DASHBOARD")).toBeInTheDocument();
  });

  it("管理员临时密码登录后 → 进入设置完成改密", () => {
    useAuthStore.setState({
      token: "t",
      user: { password_admin_reset_at: "2026-09-09T00:00:00Z" } as never,
    });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/settings" element={<div>SETTINGS</div>} />
          <Route path="/dashboard" element={<div>DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("SETTINGS")).toBeInTheDocument();
  });

  it("未登录 → 渲染登录表单", () => {
    renderUI();
    expect(screen.getByPlaceholderText("输入账号或邮箱")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("••••••••")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
  });

  it("空 email/password 提交 → 不调用 mutate", () => {
    renderUI();
    const form = document.querySelector("form")!;
    fireEvent.submit(form);
    expect(mockLogin.mutate).not.toHaveBeenCalled();
  });

  it("有效填写 → 调用 login.mutate", () => {
    renderUI();
    fireEvent.change(screen.getByPlaceholderText("输入账号或邮箱"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByPlaceholderText("••••••••"), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(mockLogin.mutate).toHaveBeenCalledWith(
      expect.objectContaining({ email: "admin", password: "secret" }),
      expect.any(Object),
    );
  });

  it("login.isPending → 按钮显示「登录中...」+ disabled", () => {
    mockLogin.isPending = true;
    renderUI();
    const btn = screen.getByText("登录中...") as HTMLButtonElement;
    expect(btn.closest("button")?.disabled).toBe(true);
  });

  it("login.isError 默认文案", () => {
    mockLogin.isError = true;
    mockLogin.error = null;
    renderUI();
    expect(screen.getByText("登录失败，请检查账号密码")).toBeInTheDocument();
  });

  it("login.isError + 自定义 message", () => {
    mockLogin.isError = true;
    mockLogin.error = new Error("账号已被禁用");
    renderUI();
    expect(screen.getByText("账号已被禁用")).toBeInTheDocument();
  });

  it("email_not_verified → 显示重发入口，点击调 resend.mutate", () => {
    mockLogin.isError = true;
    mockLogin.error = new ApiError(400, "邮箱未验证", { code: "email_not_verified" });
    renderUI();
    fireEvent.change(screen.getByPlaceholderText("输入账号或邮箱"), {
      target: { value: "unverified@test.com" },
    });
    const btn = screen.getByText("重新发送验证邮件");
    fireEvent.click(btn);
    expect(mockResend.mutate).toHaveBeenCalledWith("unverified@test.com", expect.any(Object));
  });

  it("eye toggle 切换密码可见性", () => {
    renderUI();
    const pwd = screen.getByPlaceholderText("••••••••") as HTMLInputElement;
    expect(pwd.type).toBe("password");
    // 找到 toggle 按钮（type=button 且不含文字 "登录"）
    const buttons = document.querySelectorAll("button");
    const eyeBtn = Array.from(buttons).find((b) => b.getAttribute("type") === "button")!;
    fireEvent.click(eyeBtn);
    expect(pwd.type).toBe("text");
  });

  it("open_registration_enabled=true → 显示「立即注册」链接", () => {
    renderUI();
    expect(screen.getByText("没有账号？立即注册")).toBeInTheDocument();
  });

  it("open_registration_enabled=false → 不显示「立即注册」链接", () => {
    mockRegStatus.data = { open_registration_enabled: false };
    renderUI();
    expect(screen.queryByText("没有账号？立即注册")).not.toBeInTheDocument();
  });

  it("「忘记密码？」链接始终显示", () => {
    renderUI();
    expect(screen.getByText("忘记密码？")).toBeInTheDocument();
  });

  it("location state from → 登录后回跳到来源路径", () => {
    useAuthStore.setState({ token: "t", user: null });
    render(
      <MemoryRouter
        initialEntries={[{ pathname: "/login", state: { from: { pathname: "/projects" } } }]}
      >
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/projects" element={<div>PROJECTS</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("PROJECTS")).toBeInTheDocument();
  });

  // Issue #123 · 错误页不能成为登录后的返回目标;无效目标回落角色默认首页。
  it("来源是 /unauthorized → 回落 /dashboard,不再回错误页", () => {
    useAuthStore.setState({ token: "t", user: null });
    render(
      <MemoryRouter
        initialEntries={[{ pathname: "/login", state: { from: { pathname: "/unauthorized" } } }]}
      >
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/dashboard" element={<div>DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("DASHBOARD")).toBeInTheDocument();
  });

  it("超级管理员无有效返回目标时默认落到 /overview", () => {
    useAuthStore.setState({
      token: "t",
      user: { id: "1", role: "super_admin" } as never,
    });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/overview" element={<div>OVERVIEW</div>} />
          <Route path="/dashboard" element={<div>DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("OVERVIEW")).toBeInTheDocument();
  });

  it("超级管理员来源是 /unauthorized → 回落 /overview 而非错误页", () => {
    useAuthStore.setState({
      token: "t",
      user: { id: "1", role: "super_admin" } as never,
    });
    render(
      <MemoryRouter
        initialEntries={[{ pathname: "/login", state: { from: { pathname: "/unauthorized" } } }]}
      >
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/overview" element={<div>OVERVIEW</div>} />
          <Route path="/unauthorized" element={<div>UNAUTHORIZED</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("OVERVIEW")).toBeInTheDocument();
  });

  it("?next=/unauthorized → 被过滤,回落 /dashboard", () => {
    useAuthStore.setState({ token: "t", user: null });
    render(
      <MemoryRouter initialEntries={["/login?next=%2Funauthorized"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/dashboard" element={<div>DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("DASHBOARD")).toBeInTheDocument();
  });

  it("?next= 合法业务路径 → 保留返回能力", () => {
    useAuthStore.setState({ token: "t", user: null });
    render(
      <MemoryRouter initialEntries={["/login?next=%2Fdatasets"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/datasets" element={<div>DATASETS</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("DATASETS")).toBeInTheDocument();
  });
});
