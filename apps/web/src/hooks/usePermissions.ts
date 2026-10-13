import { useAuthStore } from "@/stores/authStore";
import { ROLE_PAGE_ACCESS, ROLE_PERMISSIONS, type Permission } from "@/constants/permissions";
import type { PlatformRole, PageKey } from "@/types";

/**
 * Platform-level authorization only.  Use {@link useProjectAccess} for anything
 * scoped to a project (management, export, annotation/review capability).
 */
export function usePermissions() {
  const user = useAuthStore((s) => s.user);
  const role = (user?.role ?? "viewer") as PlatformRole;

  const canAccessPage = (page: PageKey): boolean => ROLE_PAGE_ACCESS[role]?.includes(page) ?? false;

  const hasPermission = (perm: Permission): boolean => {
    if (role === "super_admin") return true;
    return ROLE_PERMISSIONS[role]?.includes(perm) ?? false;
  };

  const hasAnyPermission = (...perms: Permission[]): boolean => {
    if (role === "super_admin") return true;
    return perms.some((p) => ROLE_PERMISSIONS[role]?.includes(p));
  };

  return {
    role,
    isEmployee: role === "employee",
    isManager: role === "super_admin" || role === "project_admin",
    canAccessPage,
    hasPermission,
    hasAnyPermission,
    allowedPages: ROLE_PAGE_ACCESS[role] ?? [],
  };
}
