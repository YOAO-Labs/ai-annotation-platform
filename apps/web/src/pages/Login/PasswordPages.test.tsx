/**
 * ForgotPasswordPage + ResetPasswordPage 单测：表单提交 / 成功态切换 /
 * 错误处理 / 不匹配校验 / 缺 token 重定向。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const mockPublicPost = vi.fn();

vi.mock("@/api/client", () => ({
  apiClient: {
    publicPost: (...args: any[]) => mockPublicPost(...args),
  },
}));

import { ForgotPasswordPage } from "./ForgotPasswordPage";
import { ResetPasswordPage } from "./ResetPasswordPage";

describe("ForgotPasswordPage", () => {
  beforeEach(() => {
    mockPublicPost.mockReset();
  });

  function renderUI() {
    return render(
      <MemoryRouter>
        <ForgotPasswordPage />
      </MemoryRouter>,
    );
  }

  it("初始 → 渲染表单", () => {
    renderUI();
    expect(screen.getByPlaceholderText("your@company.com")).toBeInTheDocument();
    expect(screen.getByText("发送重置链接")).toBeInTheDocument();
  });

  it("空 email 提交 → 不调用 API", () => {
    renderUI();
    fireEvent.submit(document.querySelector("form")!);
    expect(mockPublicPost).not.toHaveBeenCalled();
  });

  it("提交成功 → 显示「已发送」文案", async () => {
    mockPublicPost.mockResolvedValue(undefined);
    renderUI();
    fireEvent.change(screen.getByPlaceholderText("your@company.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("发送重置链接"));
    await waitFor(() => screen.getByText(/如果该邮箱已注册，您将收到一封包含重置链接的邮件/));
    // payload 增加 captcha_token（VITE_TURNSTILE_SITE_KEY 缺省时为 null）
    expect(mockPublicPost).toHaveBeenCalledWith("/auth/forgot-password", {
      email: "x@y.com",
      captcha_token: null,
    });
  });

  it("API 失败 → 显示错误文案", async () => {
    mockPublicPost.mockRejectedValue(new Error("net err"));
    renderUI();
    fireEvent.change(screen.getByPlaceholderText("your@company.com"), {
      target: { value: "x@y.com" },
    });
    fireEvent.click(screen.getByText("发送重置链接"));
    await waitFor(() => screen.getByText("请求失败，请稍后重试"));
  });
});

describe("ResetPasswordPage", () => {
  beforeEach(() => {
    mockPublicPost.mockReset();
  });

  function renderUI(initial = "/reset?token=abc") {
    return render(
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/reset" element={<ResetPasswordPage />} />
          <Route path="/login" element={<div>LOGIN</div>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("缺 token → 重定向 /login", () => {
    renderUI("/reset");
    expect(screen.getByText("LOGIN")).toBeInTheDocument();
  });

  it("有 token → 渲染表单", () => {
    renderUI();
    const pwdInputs = document.querySelectorAll('input[type="password"]');
    expect(pwdInputs.length).toBe(2);
    expect(screen.getByRole("button", { name: "重置密码" })).toBeInTheDocument();
  });

  it("两次密码不一致 → 显示提示 + 按钮 disabled", () => {
    renderUI();
    const pwds = document.querySelectorAll(
      'input[type="password"]',
    ) as NodeListOf<HTMLInputElement>;
    fireEvent.change(pwds[0], { target: { value: "Abcdef12" } });
    fireEvent.change(pwds[1], { target: { value: "Different1" } });
    expect(screen.getByText("两次密码不一致")).toBeInTheDocument();
    const btn = screen
      .getByRole("button", { name: "重置密码" })
      .closest("button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("密码强度不符合服务端规则 → 显示规则并禁用按钮", () => {
    renderUI();
    const pwds = document.querySelectorAll(
      'input[type="password"]',
    ) as NodeListOf<HTMLInputElement>;
    fireEvent.change(pwds[0], { target: { value: "abc12345" } });
    fireEvent.change(pwds[1], { target: { value: "abc12345" } });
    expect(screen.getByText("✗ 含大写字母")).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "重置密码" });
    expect(btn).toBeDisabled();
  });

  it("提交成功 → 显示完成文案", async () => {
    mockPublicPost.mockResolvedValue(undefined);
    renderUI();
    const pwds = document.querySelectorAll(
      'input[type="password"]',
    ) as NodeListOf<HTMLInputElement>;
    fireEvent.change(pwds[0], { target: { value: "Abcdef12" } });
    fireEvent.change(pwds[1], { target: { value: "Abcdef12" } });
    fireEvent.click(screen.getByRole("button", { name: "重置密码" }));
    await waitFor(() => screen.getByText("密码已重置，请使用新密码登录。"));
    expect(mockPublicPost).toHaveBeenCalledWith("/auth/reset-password", {
      token: "abc",
      new_password: "Abcdef12",
    });
  });

  it("API 失败带 message → 显示该 message", async () => {
    mockPublicPost.mockRejectedValue(new Error("token expired"));
    renderUI();
    const pwds = document.querySelectorAll(
      'input[type="password"]',
    ) as NodeListOf<HTMLInputElement>;
    fireEvent.change(pwds[0], { target: { value: "Abcdef12" } });
    fireEvent.change(pwds[1], { target: { value: "Abcdef12" } });
    fireEvent.click(screen.getByRole("button", { name: "重置密码" }));
    await waitFor(() => screen.getByText("token expired"));
  });
});
