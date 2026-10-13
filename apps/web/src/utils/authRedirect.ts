import type { UserRole } from "@/types";

/**
 * 各角色登录后的默认首页,与 App.tsx 的 DefaultLandingRedirect 保持同源:
 * super_admin 落在「平台概览」(/overview),其余角色进 /dashboard。
 */
export function defaultHomePath(role: UserRole | undefined | null): string {
  return role === "super_admin" ? "/overview" : "/dashboard";
}

/**
 * 登录后不应作为返回目标的页面:认证流程自身(登录 / 注册 / 找回密码等)
 * 以及错误页(如 /unauthorized)。错误页展示的是「上一个会话」的拒绝提示,
 * 换账号登录后回到那里只会看到与新身份无关的固定文案。
 */
const NON_RETURNABLE_PATHS = new Set([
  "/login",
  "/register",
  "/forgot-password",
  "/reset-password",
  "/verify-email",
  "/unauthorized",
]);

/**
 * 校验登录后的返回目标:仅接受站内绝对路径,并排除认证流程页与错误页。
 * 合法时原样返回(保留 query / hash),不合法时返回 null,由调用方回落默认首页。
 */
export function sanitizeLoginRedirect(candidate: string | null | undefined): string | null {
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//")) return null;
  const { pathname, searchParams } = new URL(candidate, "http://localhost");
  if (NON_RETURNABLE_PATHS.has(pathname)) {
    // 邀请链接的「登录后确认加入」以 /register?token=… 作为 next,是既有账号
    // 登录后接受邀请的合法落点;仅放行带 token 的邀请注册页,其余认证流程页
    // (含无 token 的 /register)仍不作为返回目标。
    if (pathname !== "/register" || !searchParams.get("token")) return null;
  }
  return candidate;
}

/**
 * Issue #123 · 主动退出的粘性标记。clearLocal() 可能在 navigate() 生效前就触发
 * RequireAuth 重渲染(zustand 经 useSyncExternalStore 同步刷新),且 <Navigate>
 * 会在挂载期间的每次重渲染重复发起跳转 —— 任何一次读到 false 都会把当前页
 * (/unauthorized 或受限页)作为 from 写回历史。因此标记保持置位,直到认证恢复
 * (RequireAuth 渲染到已登录分支 / 登录成功)或登录页重新挂载才清除;
 * 之后真正的会话过期跳转恢复正常携带 from。
 */
let proactiveLogoutActive = false;

export function markProactiveLogout(): void {
  proactiveLogoutActive = true;
}

export function isProactiveLogout(): boolean {
  return proactiveLogoutActive;
}

export function clearProactiveLogout(): void {
  proactiveLogoutActive = false;
}
