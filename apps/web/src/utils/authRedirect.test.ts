/** Issue #123 · 登录返回目标的角色默认首页与错误页过滤。 */
import { describe, expect, it } from "vitest";
import { defaultHomePath, sanitizeLoginRedirect } from "./authRedirect";

describe("defaultHomePath", () => {
  it("super_admin 落在平台概览 /overview", () => {
    expect(defaultHomePath("super_admin")).toBe("/overview");
  });

  it("其余平台角色与未加载完身份时落在 /dashboard", () => {
    expect(defaultHomePath("project_admin")).toBe("/dashboard");
    expect(defaultHomePath("employee")).toBe("/dashboard");
    expect(defaultHomePath("viewer")).toBe("/dashboard");
    expect(defaultHomePath(undefined)).toBe("/dashboard");
    expect(defaultHomePath(null)).toBe("/dashboard");
  });
});

describe("sanitizeLoginRedirect", () => {
  it("保留站内业务路径,含 query 与 hash", () => {
    expect(sanitizeLoginRedirect("/review")).toBe("/review");
    expect(sanitizeLoginRedirect("/projects/1/annotate?task=9")).toBe(
      "/projects/1/annotate?task=9",
    );
    expect(sanitizeLoginRedirect("/datasets#recent")).toBe("/datasets#recent");
  });

  it("拒绝空值、外部地址与协议相对地址", () => {
    expect(sanitizeLoginRedirect(null)).toBeNull();
    expect(sanitizeLoginRedirect(undefined)).toBeNull();
    expect(sanitizeLoginRedirect("")).toBeNull();
    expect(sanitizeLoginRedirect("https://evil.example.com")).toBeNull();
    expect(sanitizeLoginRedirect("//evil.example.com")).toBeNull();
    expect(sanitizeLoginRedirect("dashboard")).toBeNull();
  });

  it("拒绝认证流程页", () => {
    expect(sanitizeLoginRedirect("/login")).toBeNull();
    expect(sanitizeLoginRedirect("/register")).toBeNull();
    expect(sanitizeLoginRedirect("/forgot-password")).toBeNull();
    expect(sanitizeLoginRedirect("/reset-password")).toBeNull();
    expect(sanitizeLoginRedirect("/verify-email")).toBeNull();
  });

  it("保留带 token 的邀请注册页,登录后仍能确认加入", () => {
    expect(sanitizeLoginRedirect("/register?token=invite-token")).toBe(
      "/register?token=invite-token",
    );
    // 无 token 的注册页仍不作为返回目标(现有账号误入注册页不应成为落点)。
    expect(sanitizeLoginRedirect("/register?token=")).toBeNull();
    expect(sanitizeLoginRedirect("/register?foo=bar")).toBeNull();
  });

  it("拒绝错误页 /unauthorized", () => {
    expect(sanitizeLoginRedirect("/unauthorized")).toBeNull();
    expect(sanitizeLoginRedirect("/unauthorized?from=/users")).toBeNull();
  });

  it("前缀相近的业务路径不受误伤", () => {
    expect(sanitizeLoginRedirect("/unauthorized-feedback")).toBe("/unauthorized-feedback");
  });
});
