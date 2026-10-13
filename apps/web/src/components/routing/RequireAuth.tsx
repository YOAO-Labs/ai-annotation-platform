import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { clearProactiveLogout, isProactiveLogout } from "@/utils/authRedirect";

export function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.token);
  const location = useLocation();

  if (!token) {
    // Issue #123 · 主动退出后回到 /login 时不携带 state.from,避免把当前页
    // (可能是 /unauthorized 或他人受限页)留给下一个账号登录后回跳;
    // 会话过期 / 未登录访问仍保留 from,登录后返回原业务页。
    // 标记在挂载期间保持置位:<Navigate> 每次重渲染都会重新跳转,
    // 中途读到 false 会把 from 写回历史。
    const proactive = isProactiveLogout();
    return <Navigate to="/login" replace state={proactive ? undefined : { from: location }} />;
  }
  // 恢复认证即退出"主动退出"窗口,之后的会话过期跳转恢复正常携带 from。
  clearProactiveLogout();
  return <>{children}</>;
}
