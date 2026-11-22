import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { clearProactiveLogout, isProactiveLogout } from "@/utils/authRedirect";

export function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.token);
  const location = useLocation();

  if (!token) {
    const proactive = isProactiveLogout();
    return <Navigate to="/login" replace state={proactive ? undefined : { from: location }} />;
  }
  clearProactiveLogout();
  return <>{children}</>;
}
