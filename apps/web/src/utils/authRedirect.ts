import type { UserRole } from "@/types";

export function defaultHomePath(role: UserRole | undefined | null): string {
  return role === "super_admin" ? "/overview" : "/dashboard";
}

const NON_RETURNABLE_PATHS = new Set([
  "/login",
  "/register",
  "/forgot-password",
  "/reset-password",
  "/verify-email",
  "/unauthorized",
]);

export function sanitizeLoginRedirect(candidate: string | null | undefined): string | null {
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//")) return null;
  const { pathname, searchParams } = new URL(candidate, "http://localhost");
  if (NON_RETURNABLE_PATHS.has(pathname)) {
    if (pathname !== "/register" || !searchParams.get("token")) return null;
  }
  return candidate;
}

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
