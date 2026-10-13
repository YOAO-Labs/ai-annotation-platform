import type { UserRole, PlatformRole, ProjectRole, ProjectStatus, TaskStatus } from "@/types";

/**
 * Account/platform role labels.  `employee` is the only global staff identity;
 * its annotation/review duties are project-scoped ({@link PROJECT_ROLE_LABELS}).
 */
export const ROLE_LABELS: Record<PlatformRole, string> = {
  super_admin: "超级管理员",
  project_admin: "项目管理员",
  employee: "员工",
  viewer: "观察者",
};

export const ROLE_DESC: Record<PlatformRole, string> = {
  super_admin: "全局权限，可访问所有功能、审计日志和系统设置",
  project_admin: "创建并管理项目，邀请成员，查看报表，配置 AI 模型",
  employee: "在所属项目中执行标注或质检工作",
  viewer: "只读浏览项目数据与标注结果",
};

/** Project responsibility labels (the 项目职责 dimension). */
export const PROJECT_ROLE_LABELS: Record<ProjectRole, string> = {
  annotator: "标注员",
  reviewer: "质检员",
  viewer: "观察者",
};

export const PROJECT_ROLE_DESC: Record<ProjectRole, string> = {
  annotator: "执行标注任务，提交质检",
  reviewer: "质检复核，判定通过 / 驳回，导出样本",
  viewer: "只读浏览本项目数据与标注结果",
};

export const PROJECT_ROLES: ProjectRole[] = ["annotator", "reviewer", "viewer"];

/** @deprecated Historical display helper for pre-migration records. */
export function roleLabel(role: UserRole | string | null | undefined): string {
  if (!role) return "";
  return ROLE_LABELS[role as PlatformRole] ?? PROJECT_ROLE_LABELS[role as ProjectRole] ?? role;
}

export const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  in_progress: "进行中",
  completed: "已完成",
  pending_review: "待审核",
  archived: "已归档",
};

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  uploading: "上传中",
  pending: "待标注",
  in_progress: "标注中",
  rejected: "待重做",
  completed: "已完成",
  review: "待审核",
};
