import { useEffect, type ReactNode } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useProject } from "@/hooks/useProjects";
import { useToastStore } from "@/components/ui/Toast";
import { ApiError } from "@/api/client";
import styles from "./RequireProjectMember.module.css";

interface Props {
  children: ReactNode;
}

export function RequireProjectMember({ children }: Props) {
  const { id } = useParams<{ id: string }>();
  const projectId = id ?? "";
  const { data, isLoading, isError, error } = useProject(projectId);
  const pushToast = useToastStore((s) => s.push);

  const denied =
    isError && error instanceof ApiError && (error.status === 403 || error.status === 404);

  useEffect(() => {
    if (denied) {
      pushToast({
        msg: error?.status === 404 ? "项目不存在或已被删除" : "你没有权限进入该项目",
        kind: "warning",
      });
    }
  }, [denied, error, pushToast]);

  if (!projectId) return <Navigate to="/dashboard" replace />;
  if (isLoading) {
    return <div className={styles.loading}>正在校验项目权限…</div>;
  }
  if (denied || !data) {
    return <Navigate to="/dashboard" replace />;
  }
  return <>{children}</>;
}
