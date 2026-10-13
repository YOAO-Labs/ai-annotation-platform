import { useEffect, type ReactNode } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useProjectAccess } from "@/hooks/useProjectAccess";
import { useToastStore } from "@/components/ui/Toast";
import { ApiError } from "@/api/client";
import type { ProjectCapability } from "@/api/projects";
import styles from "./RequireProjectMember.module.css";

interface Props {
  /** Capability/capabilities the route requires before it may mount. */
  capability: ProjectCapability | ProjectCapability[];
  children: ReactNode;
  /** Where to send a denied/errored request.  Defaults to the dashboard. */
  redirectTo?: string;
}

/**
 * Project-scoped route guard.  While access is unresolved (or failed) it never
 * mounts the children, so an editable Workbench cannot appear before the
 * server-authoritative capability is known.  A capability denial redirects with
 * a toast instead of rendering a broken editor.
 */
export function RequireProjectAccess({ capability, children, redirectTo = "/dashboard" }: Props) {
  const { id } = useParams<{ id: string }>();
  const projectId = id ?? "";
  const required = Array.isArray(capability) ? capability : [capability];
  const { isLoading, isError, error, hasCapability } = useProjectAccess(projectId);
  const pushToast = useToastStore((s) => s.push);

  const notFound =
    isError && error instanceof ApiError && (error.status === 403 || error.status === 404);
  const allowed = hasCapability(...required);
  const denied = !isLoading && !isError && !allowed;

  useEffect(() => {
    if (notFound) {
      pushToast({
        msg: error?.status === 404 ? "项目不存在或已被删除" : "你没有权限进入该项目",
        kind: "warning",
      });
    } else if (isError) {
      pushToast({ msg: "无法校验项目权限，请稍后重试", kind: "error" });
    } else if (denied) {
      pushToast({ msg: "当前账号缺少该操作所需的项目权限", kind: "warning" });
    }
  }, [notFound, isError, denied, error, pushToast]);

  if (!projectId) return <Navigate to={redirectTo} replace />;
  if (isLoading) {
    return <div className={styles.loading}>正在校验项目权限…</div>;
  }
  if (isError || denied) {
    return <Navigate to={redirectTo} replace />;
  }
  return <>{children}</>;
}
