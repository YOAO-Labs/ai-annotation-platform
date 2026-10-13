import { useAuthStore } from "@/stores/authStore";
import type { ProjectResponse } from "@/api/projects";

/** Only platform administrators can manage a project through ownership. */
export function useIsProjectOwner(project?: ProjectResponse | null): boolean {
  const user = useAuthStore((s) => s.user);
  if (!user || !project) return false;
  if (user.role === "super_admin") return true;
  return user.role === "project_admin" && project.owner_id === user.id;
}
