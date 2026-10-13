import type { UserRole, PageKey } from "@/types";

/**
 * Platform-level page reach.  Project-scoped actions (management, export,
 * assignment) are additionally gated by {@link useProjectAccess} capabilities,
 * not by this table.
 */
export const ROLE_PAGE_ACCESS: Record<UserRole, PageKey[]> = {
  super_admin: [
    "dashboard",
    "annotate",
    "review",
    "users",
    "datasets",
    "storage",
    "ai-pre",
    "model-market",
    "training",
    "audit",
    "bugs",
    "settings",
    "project-templates",
    "admin-people",
    "admin-analytics",
    "admin-health",
    "my-performance",
  ],
  project_admin: [
    "dashboard",
    "annotate",
    "review",
    "users",
    "datasets",
    "storage",
    "ai-pre",
    "model-market",
    "training",
    "bugs",
    "settings",
    "project-templates",
    "admin-people",
    "my-performance",
  ],
  // v0.25.x · employees can reach both work entry points; the server filters the
  // actual work by project membership.  No fabricated global current role.
  employee: ["dashboard", "annotate", "review", "datasets", "settings", "my-performance"],
  viewer: ["dashboard", "datasets", "settings"],
};

export type Permission =
  | "project.create"
  | "project.edit"
  | "project.delete"
  | "project.transfer"
  | "project.export"
  | "task.assign"
  | "task.annotate"
  | "task.review"
  | "task.approve"
  | "task.reject"
  | "user.list"
  | "user.invite"
  | "user.edit-role"
  | "user.export"
  | "group.manage"
  | "invitation.manage"
  | "dataset.create"
  | "dataset.delete"
  | "dataset.link"
  | "storage.manage"
  | "audit.view"
  | "settings.edit"
  | "ai.trigger"
  | "ml-backend.manage";

const ALL_PERMISSIONS: Permission[] = [
  "project.create",
  "project.edit",
  "project.delete",
  "project.transfer",
  "project.export",
  "task.assign",
  "task.annotate",
  "task.review",
  "task.approve",
  "task.reject",
  "user.list",
  "user.invite",
  "user.edit-role",
  "user.export",
  "group.manage",
  "invitation.manage",
  "dataset.create",
  "dataset.delete",
  "dataset.link",
  "storage.manage",
  "audit.view",
  "settings.edit",
  "ai.trigger",
  "ml-backend.manage",
];

export const ROLE_PERMISSIONS: Record<UserRole, Permission[]> = {
  super_admin: ALL_PERMISSIONS,
  project_admin: [
    "project.create",
    "project.edit",
    "project.export",
    "task.assign",
    "task.annotate",
    "task.review",
    "task.approve",
    "task.reject",
    "user.list",
    "user.invite",
    "user.export",
    "group.manage",
    "invitation.manage",
    "dataset.create",
    "dataset.delete",
    "dataset.link",
    "ai.trigger",
    "ml-backend.manage",
  ],
  // Employees may both annotate and review; each capability is only meaningful
  // inside a project whose membership grants it (server authoritative).
  employee: ["task.annotate", "task.review", "task.approve", "task.reject"],
  viewer: [],
};

export const PERMISSION_LABELS: Record<Permission, string> = {
  "project.create": "创建项目",
  "project.edit": "编辑项目",
  "project.delete": "删除项目",
  "project.transfer": "转让项目",
  "project.export": "导出项目",
  "task.assign": "分配任务",
  "task.annotate": "执行标注",
  "task.review": "审核任务",
  "task.approve": "通过任务",
  "task.reject": "驳回任务",
  "user.list": "查看用户",
  "user.invite": "邀请用户",
  "user.edit-role": "修改角色",
  "user.export": "导出用户",
  "group.manage": "管理数据组",
  "invitation.manage": "管理邀请",
  "dataset.create": "创建数据集",
  "dataset.delete": "删除数据集",
  "dataset.link": "关联项目",
  "storage.manage": "管理存储",
  "audit.view": "查看审计",
  "settings.edit": "系统设置",
  "ai.trigger": "触发 AI",
  "ml-backend.manage": "管理 ML 模型",
};

export const PERMISSION_GROUPS: Array<{ key: string; title: string; perms: Permission[] }> = [
  {
    key: "project",
    title: "项目",
    perms: [
      "project.create",
      "project.edit",
      "project.delete",
      "project.transfer",
      "project.export",
    ],
  },
  {
    key: "task",
    title: "任务",
    perms: ["task.assign", "task.annotate", "task.review", "task.approve", "task.reject"],
  },
  {
    key: "user",
    title: "用户 / 数据组",
    perms: [
      "user.list",
      "user.invite",
      "user.edit-role",
      "user.export",
      "group.manage",
      "invitation.manage",
    ],
  },
  {
    key: "dataset",
    title: "数据集 / 存储",
    perms: ["dataset.create", "dataset.delete", "dataset.link", "storage.manage"],
  },
  {
    key: "ai",
    title: "AI / 审计 / 设置",
    perms: ["ai.trigger", "ml-backend.manage", "audit.view", "settings.edit"],
  },
];
