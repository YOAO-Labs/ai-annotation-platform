import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { FilterTrigger } from "@/components/filters/FilterTrigger";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Modal } from "@/components/ui/Modal";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { StatCard } from "@/components/ui/StatCard";
import { SearchInput } from "@/components/ui/SearchInput";
import { TabRow } from "@/components/ui/TabRow";
import { useToastStore } from "@/components/ui/Toast";
import { useUsers, useUserPage, useDeleteUser, useUsersStats } from "@/hooks/useUsers";
import { useProjects } from "@/hooks/useProjects";
import { useGroups } from "@/hooks/useGroups";
import { usePermissions } from "@/hooks/usePermissions";
import { useAuthStore } from "@/stores/authStore";
import { ROLE_LABELS, ROLE_DESC } from "@/constants/roles";
import {
  ROLE_PERMISSIONS,
  PERMISSION_LABELS,
  PERMISSION_GROUPS,
  type Permission,
} from "@/constants/permissions";
import { Can } from "@/components/guards/Can";
import { InviteUserModal } from "@/components/users/InviteUserModal";
import { BulkInviteModal } from "@/components/users/BulkInviteModal";
import { BulkGroupAssignmentModal } from "@/components/users/BulkGroupAssignmentModal";
import { EditUserModal } from "@/components/users/EditUserModal";
import { GroupManageModal } from "@/components/users/GroupManageModal";
import { InvitationListPanel } from "@/components/users/InvitationListPanel";
import { OffboardingDialog, ReactivateDialog } from "@/components/users/OffboardingDialog";
import { usersApi, type UserResponse } from "@/api/users";
import { ApiError } from "@/api/client";
import type { UserRole } from "@/types";
import { PageContainer } from "@/components/layout/PageContainer";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { USERS_URL_DEFAULTS, USERS_URL_KEYS, usersUrlCodec } from "./usersUrlState";

// actor.role × target.role → 可点"编辑"（即可改平台角色或可删）。
// 平台角色编辑仅 super_admin（后端 AUTH-04）；项目管理员的生命周期路径仅
// 接受 employee 目标，观察者行不提供编辑/离职等入口。
const EDITABLE_TARGET_ROLES_BY_ACTOR: Record<UserRole, UserRole[]> = {
  super_admin: ["super_admin", "project_admin", "employee", "viewer"],
  project_admin: ["employee"],
  employee: [],
  viewer: [],
};

const ROLE_COLORS: Record<string, "accent" | "ai" | "warning" | "success" | "outline" | "danger"> =
  {
    super_admin: "danger",
    project_admin: "accent",
    employee: "ai",
    viewer: "success",
  };

const STATUS_LABEL: Record<string, string> = {
  online: "在线",
  offline: "离线",
  busy: "忙碌",
};

const STATUS_COLORS: Record<string, "success" | "warning" | "outline"> = {
  在线: "success",
  忙碌: "warning",
  离线: "outline",
};

const USER_STATUS_FILTER_LABELS = {
  active: "启用账号",
  inactive: "已停用",
  all: "全部账号",
} as const;

const DISABLED_KIND_LABELS: Record<string, string> = {
  suspended: "停用（可恢复）",
  emergency_suspended: "紧急停用",
  deleted: "已删除",
  historical_unknown: "历史未知状态",
};

const REACTIVATABLE_KINDS = new Set(["suspended", "emergency_suspended"]);

// 表头单元 / 主表数据单元
const TH_CLASS =
  "border-b border-border bg-muted px-3 py-2.5 text-left text-xs font-medium text-muted-foreground whitespace-nowrap";
const TD_CLASS = "border-b border-border p-3 align-middle";
// 弹窗内成员摘要卡 / 选择框基线
const SELECT_BASE =
  "appearance-none rounded-md border border-border bg-card text-foreground [font:inherit] outline-none";
const SUMMARY_CARD_CLASS =
  "flex items-center gap-2.5 rounded-md border border-border bg-muted px-3 py-2.5";

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString("zh-CN");
}

function formatDateTime(iso: string | null | undefined) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

export function UsersPage() {
  const ownerId = useAuthStore((state) => state.user?.id);
  return <UsersPageContent key={ownerId} />;
}

function UsersPageContent() {
  const urlState = useUrlFilterState({
    codec: usersUrlCodec,
    defaults: USERS_URL_DEFAULTS,
    ownedKeys: USERS_URL_KEYS,
  });
  const { state: filters, issues, patch } = urlState;
  const [queryDraft, setQueryDraft] = useState(filters.q);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const syncingQueryDraft = useRef(false);
  const lastUrlQuery = useRef(filters.q);
  const debouncedQuery = useDebouncedValue(queryDraft, 250);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [bulkInviteOpen, setBulkInviteOpen] = useState(false);
  const [bulkGroupOpen, setBulkGroupOpen] = useState(false);
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([]);
  const [selectedUsersById, setSelectedUsersById] = useState<Record<string, UserResponse>>({});
  const lastMemberFilters = useRef({
    q: filters.q,
    status: filters.status,
    role: filters.role,
    projectId: filters.projectId,
    groupId: filters.groupId,
  });
  const pageSize = 25;
  const [editing, setEditing] = useState<UserResponse | null>(null);
  const [deleting, setDeleting] = useState<UserResponse | null>(null);
  const [resettingPwd, setResettingPwd] = useState<UserResponse | null>(null);
  const [tempPwdResult, setTempPwdResult] = useState<{
    user: UserResponse;
    password: string;
  } | null>(null);
  const [pwdResetSubmitting, setPwdResetSubmitting] = useState(false);
  /** 后端 409 返回的待转交任务详情（pending_task_count / locked_task_count / sample_task_ids）。 */
  const [transferStage, setTransferStage] = useState<{
    pending: number;
    locked: number;
    sample: string[];
  } | null>(null);
  const [transferToId, setTransferToId] = useState<string>("");
  const [offboardingUser, setOffboardingUser] = useState<UserResponse | null>(null);
  const [reactivatingUser, setReactivatingUser] = useState<UserResponse | null>(null);
  const [manageGroupsOpen, setManageGroupsOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const clearSelection = useCallback(() => {
    setSelectedUserIds([]);
    setSelectedUsersById({});
  }, []);
  const tab = filters.tab;
  const userStatus = filters.status;
  const selectedRole = filters.role || "全部";
  const projectFilter = filters.projectId;
  const groupFilter = filters.groupId;
  const page = filters.page;

  useEffect(() => {
    const previous = lastMemberFilters.current;
    const changed =
      previous.q !== filters.q ||
      previous.status !== filters.status ||
      previous.role !== filters.role ||
      previous.projectId !== filters.projectId ||
      previous.groupId !== filters.groupId;
    lastMemberFilters.current = {
      q: filters.q,
      status: filters.status,
      role: filters.role,
      projectId: filters.projectId,
      groupId: filters.groupId,
    };
    if (changed) clearSelection();
  }, [clearSelection, filters.groupId, filters.projectId, filters.q, filters.role, filters.status]);

  useEffect(() => {
    if (lastUrlQuery.current === filters.q) return;
    lastUrlQuery.current = filters.q;
    if (queryDraft !== filters.q) {
      syncingQueryDraft.current = true;
      setQueryDraft(filters.q);
    }
  }, [filters.q, queryDraft]);

  useEffect(() => {
    if (syncingQueryDraft.current) {
      if (debouncedQuery === filters.q) syncingQueryDraft.current = false;
      return;
    }
    const nextQuery = debouncedQuery.trim();
    if (nextQuery !== queryDraft.trim()) return;
    if (nextQuery === filters.q) return;
    clearSelection();
    patch({ q: nextQuery, page: 1 }, { replace: true });
  }, [clearSelection, debouncedQuery, filters.q, patch, queryDraft]);

  const memberParams = useMemo(
    () => ({
      status: userStatus,
      role: filters.role || undefined,
      project_id: projectFilter || undefined,
      group_id: groupFilter || undefined,
      search: filters.q || undefined,
    }),
    [filters.q, filters.role, groupFilter, projectFilter, userStatus],
  );
  const pushToast = useToastStore((s) => s.push);
  const deleteUser = useDeleteUser();
  const navigate = useNavigate();
  const { role: actorRole, hasPermission } = usePermissions();
  const me = useAuthStore((s) => s.user);
  const editableTargets = EDITABLE_TARGET_ROLES_BY_ACTOR[actorRole] ?? [];
  const canManageGroups = hasPermission("group.manage");
  const canViewAudit = hasPermission("audit.view");

  const {
    data: pageData,
    isLoading,
    isError: usersError,
    error: usersQueryError,
    refetch: refetchUsers,
    isFetching: usersFetching,
    fetchStatus: usersFetchStatus,
  } = useUserPage({
    ...memberParams,
    page,
    page_size: pageSize,
  });
  const { data: groupsData = [] } = useGroups();
  const { data: usersStats } = useUsersStats(memberParams);
  const usersPaused = usersFetchStatus === "paused";

  const allUsers = pageData?.items ?? [];
  const filtered = allUsers;
  const pageMeta = pageData;
  const { data: projects = [] } = useProjects();
  const { data: transferUsers = [] } = useUsers(
    { status: "active" },
    !!deleting && !!transferStage,
  );
  const selectedUsers = useMemo(() => Object.values(selectedUsersById), [selectedUsersById]);
  const pageUserIds = filtered.map((user) => user.id);
  const allPageSelected =
    pageUserIds.length > 0 && pageUserIds.every((id) => selectedUserIds.includes(id));

  const userQueryStatus =
    usersQueryError instanceof ApiError
      ? usersQueryError.status
      : usersQueryError && typeof usersQueryError === "object" && "status" in usersQueryError
        ? Number((usersQueryError as { status?: unknown }).status)
        : undefined;
  const userQueryTitle =
    userQueryStatus === 403
      ? "无权查看用户列表"
      : userQueryStatus !== undefined && userQueryStatus >= 500
        ? "服务器暂时不可用"
        : "用户列表加载失败";
  const userQueryErrorCopy =
    userQueryStatus === 403
      ? "没有权限查看用户列表，请联系管理员。"
      : userQueryStatus !== undefined && userQueryStatus >= 500
        ? "服务器暂时不可用，请稍后重试。"
        : usersQueryError instanceof Error
          ? usersQueryError.message
          : "加载用户列表失败，请重试。";

  const handleExport = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      await usersApi.exportUsers("csv", memberParams);
      pushToast({ msg: "已导出名单 CSV", kind: "success" });
    } catch (err) {
      pushToast({
        msg: "导出失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
    } finally {
      setExporting(false);
    }
  };

  const roleKeys = Object.keys(ROLE_PERMISSIONS) as Array<keyof typeof ROLE_PERMISSIONS>;

  const tabLabels: Array<["members" | "roles" | "groups" | "invitations", string]> = [
    ["members", `成员 (${usersStats?.total ?? pageMeta?.total ?? allUsers.length})`],
    ["roles", `角色 (${roleKeys.length})`],
    ["groups", `数据组 (${groupsData.length})`],
    ["invitations", "邀请记录"],
  ];
  const activeLabel = tabLabels.find(([k]) => k === tab)?.[1] ?? tabLabels[0][1];

  return (
    <PageContainer>
      <div className="mb-5 flex items-end justify-between gap-6">
        <div>
          <h1 className="mb-1 text-xl font-semibold">用户与权限</h1>
          <p className="text-sm text-muted-foreground">管理团队成员、角色权限与数据组分配</p>
          {!!issues.length && (
            <div role="alert" className="mt-1 text-xs text-status-caution">
              URL 用户筛选无法完整恢复，已使用安全默认值。
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <Can permission="user.export">
            <Button onClick={handleExport} disabled={exporting}>
              <Icon name="download" size={13} />
              {exporting ? "导出中…" : "导出名单"}
            </Button>
          </Can>
          <Can permission="user.invite">
            <div className="flex gap-2">
              <Button onClick={() => setBulkInviteOpen(true)}>
                <Icon name="users" size={13} />
                批量邀请
              </Button>
              <Button variant="primary" onClick={() => setInviteOpen(true)}>
                <Icon name="plus" size={13} />
                邀请成员
              </Button>
            </div>
          </Can>
        </div>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
        <StatCard
          icon="users"
          label="团队成员"
          value={usersStats?.total ?? pageMeta?.total ?? allUsers.length}
          hint={userStatus === "active" ? "当前筛选" : "同筛选范围"}
        />
        <StatCard icon="shield" label="平台角色" value={roleKeys.length} hint="内置角色" />
        <StatCard icon="folder" label="数据组" value={groupsData.length} hint="可分配" />
        <StatCard
          icon="activity"
          label="本周活跃"
          value={usersStats?.weekly_active ?? "—"}
          hint={usersStats ? `在线 ${usersStats.online}` : "近 7 日"}
        />
      </div>

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <TabRow
            tabs={tabLabels.map(([, l]) => l)}
            active={activeLabel}
            onChange={(t) => {
              const found = tabLabels.find(([, l]) => l === t);
              if (found) patch({ tab: found[0] }, { replace: false });
            }}
          />
          {tab === "members" && (
            <div className="flex flex-wrap items-center justify-end gap-2">
              <FilterGroup label="账号" hideLabel compact>
                <FilterSelect
                  aria-label="账号状态"
                  value={userStatus}
                  onChange={(e) => {
                    clearSelection();
                    patch(
                      { status: e.target.value as "active" | "inactive" | "all", page: 1 },
                      { replace: false },
                    );
                  }}
                >
                  {Object.entries(USER_STATUS_FILTER_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </FilterSelect>
              </FilterGroup>
              <SearchInput
                placeholder="搜索姓名或邮箱..."
                value={queryDraft}
                onChange={(value) => {
                  syncingQueryDraft.current = false;
                  setQueryDraft(value);
                }}
                width={240}
              />
              <FilterPanel
                open={filtersOpen}
                onOpenChange={setFiltersOpen}
                trigger={
                  <FilterTrigger
                    count={[projectFilter, filters.role, groupFilter].filter(Boolean).length}
                    countLabel="附加筛选条件（不含账号状态）"
                  />
                }
                title="成员筛选"
                description="即时生效 · 按项目、角色和数据组筛选成员。"
                footer={
                  <div className="flex items-center justify-between gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        clearSelection();
                        patch(
                          { projectId: "", role: "", groupId: "", page: 1 },
                          { replace: false },
                        );
                      }}
                    >
                      清除附加条件
                    </Button>
                    <Button size="sm" onClick={() => setFiltersOpen(false)}>
                      完成
                    </Button>
                  </div>
                }
              >
                <div className="grid gap-3">
                  <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
                    项目
                    <FilterSelect
                      aria-label="项目筛选"
                      value={projectFilter}
                      onChange={(event) => {
                        clearSelection();
                        patch({ projectId: event.target.value, page: 1 }, { replace: false });
                      }}
                      className="w-full"
                    >
                      <option value="">全部项目</option>
                      {projects.map((project) => (
                        <option key={project.id} value={project.id}>
                          {project.name}
                        </option>
                      ))}
                    </FilterSelect>
                  </label>
                  <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
                    角色
                    <FilterSelect
                      aria-label="角色筛选"
                      value={selectedRole}
                      onChange={(e) => {
                        clearSelection();
                        patch(
                          { role: e.target.value === "全部" ? "" : e.target.value, page: 1 },
                          { replace: false },
                        );
                      }}
                      className="w-full"
                    >
                      <option>全部</option>
                      {roleKeys.map((r) => (
                        <option key={r} value={r}>
                          {ROLE_LABELS[r] ?? r}
                        </option>
                      ))}
                    </FilterSelect>
                  </label>
                  <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
                    数据组
                    <FilterSelect
                      aria-label="数据组筛选"
                      value={groupFilter}
                      onChange={(event) => {
                        clearSelection();
                        patch({ groupId: event.target.value, page: 1 }, { replace: false });
                      }}
                      className="w-full"
                    >
                      <option value="">全部数据组</option>
                      {groupsData.map((group) => (
                        <option key={group.id} value={group.id}>
                          {group.name}
                        </option>
                      ))}
                    </FilterSelect>
                  </label>
                </div>
              </FilterPanel>
            </div>
          )}
          {tab === "groups" && (
            <Can permission="group.manage">
              <Button onClick={() => setManageGroupsOpen(true)}>
                <Icon name="settings" size={12} /> 管理数据组
              </Button>
            </Can>
          )}
        </div>

        {tab === "members" && (projectFilter || filters.role || groupFilter) && (
          <div
            className="flex flex-wrap gap-1.5 border-b border-border px-4 py-2"
            role="group"
            aria-label="已应用的成员筛选"
          >
            {projectFilter && (
              <ActiveFilterChip
                label="项目"
                value={projects.find((project) => project.id === projectFilter)?.name ?? "指定项目"}
                onClick={() => setFiltersOpen(true)}
                onRemove={() => {
                  clearSelection();
                  patch({ projectId: "", page: 1 }, { replace: false });
                }}
              />
            )}
            {filters.role && (
              <ActiveFilterChip
                label="角色"
                value={ROLE_LABELS[filters.role as UserRole] ?? filters.role}
                onClick={() => setFiltersOpen(true)}
                onRemove={() => {
                  clearSelection();
                  patch({ role: "", page: 1 }, { replace: false });
                }}
              />
            )}
            {groupFilter && (
              <ActiveFilterChip
                label="数据组"
                value={groupsData.find((group) => group.id === groupFilter)?.name ?? "指定数据组"}
                onClick={() => setFiltersOpen(true)}
                onRemove={() => {
                  clearSelection();
                  patch({ groupId: "", page: 1 }, { replace: false });
                }}
              />
            )}
          </div>
        )}

        {tab === "members" && (
          <div>
            {selectedUserIds.length > 0 && (
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/40 px-4 py-2 text-xs">
                <span>已选择 {selectedUserIds.length} 名成员（可跨页累计）</span>
                <div className="flex gap-2">
                  {canManageGroups && (
                    <Button size="sm" onClick={() => setBulkGroupOpen(true)}>
                      批量分配数据组
                    </Button>
                  )}
                  <Button size="sm" onClick={clearSelection}>
                    清除选择
                  </Button>
                </div>
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1040px] border-separate border-spacing-0 text-sm">
                <thead>
                  <tr>
                    <th className={TH_CLASS}>
                      <input
                        type="checkbox"
                        aria-label="选择本页成员"
                        checked={allPageSelected}
                        onChange={(event) =>
                          (() => {
                            setSelectedUserIds((current) =>
                              event.target.checked
                                ? Array.from(new Set([...current, ...pageUserIds]))
                                : current.filter((id) => !pageUserIds.includes(id)),
                            );
                            setSelectedUsersById((current) => {
                              const next = { ...current };
                              if (event.target.checked) {
                                for (const user of filtered) next[user.id] = user;
                              } else {
                                for (const id of pageUserIds) delete next[id];
                              }
                              return next;
                            });
                          })()
                        }
                      />
                    </th>
                    {["成员", "角色", "数据组", "状态", "近期标注量", "准确率", "加入时间", ""].map(
                      (h, i) => (
                        <th key={i} className={`${TH_CLASS} ${i === 0 ? "pl-4" : ""}`}>
                          {h}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {usersPaused && filtered.length > 0 && (
                    <tr>
                      <td
                        colSpan={9}
                        className="px-3 py-2.5 text-center text-xs text-status-caution"
                      >
                        当前离线，正在等待网络恢复；以下为上次加载的数据。
                      </td>
                    </tr>
                  )}
                  {usersPaused && filtered.length === 0 && !isLoading && !usersError && (
                    <tr>
                      <td colSpan={9} className="p-10 text-center">
                        <div className="mx-auto flex max-w-md flex-col items-center gap-2 text-sm">
                          <Icon name="monitor" size={22} className="text-status-caution" />
                          <span className="font-medium">暂时离线，等待网络恢复</span>
                          <span className="text-xs text-muted-foreground">
                            网络恢复后会自动继续加载用户列表。
                          </span>
                        </div>
                      </td>
                    </tr>
                  )}
                  {isLoading && (
                    <tr>
                      <td colSpan={9} className="p-10 text-center text-muted-foreground">
                        加载中...
                      </td>
                    </tr>
                  )}
                  {usersError && !isLoading && !usersPaused && (
                    <tr>
                      <td colSpan={9} className="p-10 text-center">
                        <div className="mx-auto flex max-w-md flex-col items-center gap-2 text-sm">
                          <Icon
                            name={userQueryStatus === 403 ? "shieldAlert" : "warning"}
                            size={22}
                            className={
                              userQueryStatus === 403 ? "text-status-caution" : "text-status-danger"
                            }
                          />
                          <span className="font-medium">{userQueryTitle}</span>
                          <span className="text-xs text-muted-foreground">
                            {userQueryErrorCopy}
                          </span>
                          <Button
                            size="sm"
                            onClick={() => void refetchUsers()}
                            disabled={usersFetching}
                          >
                            <Icon name="refresh" size={12} /> {usersFetching ? "重试中…" : "重试"}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  )}
                  {!isLoading && !usersError && !usersPaused && filtered.length === 0 && (
                    <tr>
                      <td colSpan={9} className="p-10 text-center text-sm text-muted-foreground">
                        {queryDraft || selectedRole !== "全部" || groupFilter
                          ? "没有匹配的账号。"
                          : `暂无${USER_STATUS_FILTER_LABELS[userStatus]}。`}
                      </td>
                    </tr>
                  )}
                  {filtered.map((u: UserResponse) => {
                    const isActive = u.is_active !== false;
                    const statusLabel = isActive ? (STATUS_LABEL[u.status] ?? u.status) : "已停用";
                    const disabledKindLabel = u.disabled_kind
                      ? (DISABLED_KIND_LABELS[u.disabled_kind] ?? u.disabled_kind)
                      : null;
                    return (
                      <tr key={u.id}>
                        <td className={TD_CLASS}>
                          <input
                            type="checkbox"
                            aria-label={`选择 ${u.name}`}
                            checked={selectedUserIds.includes(u.id)}
                            onChange={(event) => {
                              setSelectedUserIds((current) =>
                                event.target.checked
                                  ? [...current, u.id]
                                  : current.filter((id) => id !== u.id),
                              );
                              setSelectedUsersById((current) => {
                                const next = { ...current };
                                if (event.target.checked) next[u.id] = u;
                                else delete next[u.id];
                                return next;
                              });
                            }}
                          />
                        </td>
                        <td className={`${TD_CLASS} pl-4`}>
                          <div className="flex items-center gap-2.5">
                            <UserAvatar user={u} size="md" />
                            <div className="min-w-0">
                              <div className="max-w-[240px] truncate text-sm font-medium">
                                {u.name}
                              </div>
                              <div className="mono max-w-[240px] truncate text-xs text-muted-foreground">
                                {u.email}
                              </div>
                            </div>
                          </div>
                        </td>
                        <td className={TD_CLASS}>
                          <Badge variant={ROLE_COLORS[u.role] || "outline"}>
                            {ROLE_LABELS[u.role as UserRole] ?? u.role}
                          </Badge>
                        </td>
                        <td className={`${TD_CLASS} max-w-[160px] truncate`}>
                          {u.group_name ?? "—"}
                        </td>
                        <td className={TD_CLASS}>
                          <Badge variant={STATUS_COLORS[statusLabel] || "outline"} dot>
                            {statusLabel}
                          </Badge>
                          {!isActive && disabledKindLabel && (
                            <div className="mt-1 max-w-[180px] text-2xs text-muted-foreground">
                              <div className="truncate">{disabledKindLabel}</div>
                              <div className="truncate">时间：{formatDateTime(u.disabled_at)}</div>
                              <div className="truncate" title={u.disabled_reason ?? undefined}>
                                原因：{u.disabled_reason || "未填写"}
                              </div>
                            </div>
                          )}
                        </td>
                        <td className={TD_CLASS}>
                          <span className="text-xs text-muted-foreground">—</span>
                        </td>
                        <td className={TD_CLASS}>
                          <span className="text-xs text-muted-foreground">—</span>
                        </td>
                        <td className={`${TD_CLASS} text-xs text-muted-foreground`}>
                          {formatDate(u.created_at)}
                        </td>
                        <td className={`${TD_CLASS} whitespace-nowrap text-right`}>
                          <div className="inline-flex gap-0.5 whitespace-nowrap">
                            {canViewAudit && (
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => navigate(`/audit?actor_id=${u.id}`)}
                                title={`查看 ${u.name} 的审计追溯`}
                              >
                                <Icon name="activity" size={11} />
                              </Button>
                            )}
                            {me?.id !== u.id &&
                            editableTargets.includes(u.role as UserRole) &&
                            u.is_managed !== false ? (
                              <>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => setEditing(u)}
                                  title="编辑成员"
                                >
                                  <Icon name="edit" size={11} />
                                </Button>
                                {isActive && (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setResettingPwd(u)}
                                    title="重置密码"
                                  >
                                    <Icon name="key" size={11} />
                                  </Button>
                                )}
                                {/* 生命周期写入（离职/删除/恢复）需要目标的派生项目
                                    全部在自己的管理范围内；跨项目行仅开放账号级操作，
                                    否则按钮点开必然 403。 */}
                                {isActive && u.is_lifecycle_managed !== false && (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setOffboardingUser(u)}
                                    title="离职处理"
                                  >
                                    <Icon
                                      name="shieldAlert"
                                      size={11}
                                      className="text-status-caution"
                                    />
                                  </Button>
                                )}
                                {isActive && u.is_lifecycle_managed !== false && (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setDeleting(u)}
                                    title="删除账号"
                                  >
                                    <Icon name="trash" size={11} className="text-status-danger" />
                                  </Button>
                                )}
                                {!isActive &&
                                  u.is_lifecycle_managed !== false &&
                                  REACTIVATABLE_KINDS.has(u.disabled_kind ?? "") && (
                                    <>
                                      <Button
                                        variant="ghost"
                                        size="sm"
                                        onClick={() => setOffboardingUser(u)}
                                        title="继续交接"
                                      >
                                        <Icon
                                          name="arrowRight"
                                          size={11}
                                          className="text-status-caution"
                                        />
                                      </Button>
                                      <Button
                                        variant="ghost"
                                        size="sm"
                                        onClick={() => setReactivatingUser(u)}
                                        title="恢复账号"
                                      >
                                        <Icon
                                          name="rotate-ccw"
                                          size={11}
                                          className="text-status-positive"
                                        />
                                      </Button>
                                    </>
                                  )}
                              </>
                            ) : (
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled
                                title={
                                  me?.id === u.id
                                    ? "不能修改自己"
                                    : u.is_managed === false
                                      ? u.role === "super_admin"
                                        ? "仅可查看：超级管理员账号"
                                        : "该用户不在你管理的项目内，仅可查看"
                                      : "无权修改该用户"
                                }
                              >
                                <Icon name="edit" size={11} className="opacity-40" />
                              </Button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {pageMeta && pageMeta.pages > 1 && (
              <div className="flex items-center justify-between border-t border-border px-4 py-3 text-xs text-muted-foreground">
                <span>
                  第 {pageMeta.page} / {pageMeta.pages} 页 · 共 {pageMeta.total} 名成员
                </span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={page <= 1}
                    onClick={() => patch({ page: Math.max(1, page - 1) }, { replace: false })}
                  >
                    上一页
                  </Button>
                  <Button
                    size="sm"
                    disabled={page >= pageMeta.pages}
                    onClick={() => patch({ page: page + 1 }, { replace: false })}
                  >
                    下一页
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {tab === "roles" && (
          <div className="grid grid-cols-2 gap-3 p-4">
            {roleKeys.map((rk) => {
              const perms = ROLE_PERMISSIONS[rk];
              const permsSet = new Set<Permission>(perms);
              return (
                <div key={rk} className="rounded-lg border border-border bg-card p-3.5">
                  <div className="mb-1.5 flex items-center gap-2">
                    <Badge variant={ROLE_COLORS[rk] || "outline"}>{ROLE_LABELS[rk] ?? rk}</Badge>
                  </div>
                  <div className="mb-2.5 text-sm text-muted-foreground">{ROLE_DESC[rk]}</div>
                  <div className="flex flex-col gap-2">
                    {PERMISSION_GROUPS.map((group) => {
                      const granted = group.perms.filter((p) => permsSet.has(p));
                      const denied = group.perms.filter((p) => !permsSet.has(p));
                      if (granted.length === 0 && denied.length === 0) return null;
                      return (
                        <div key={group.key}>
                          <div className="mb-1 text-2xs font-semibold uppercase tracking-[0.5px] text-muted-foreground">
                            {group.title}
                          </div>
                          <div className="flex flex-wrap gap-1">
                            {granted.map((p) => (
                              <Badge key={p} variant="success">
                                <Icon name="check" size={9} />
                                {PERMISSION_LABELS[p]}
                              </Badge>
                            ))}
                            {denied.map((p) => (
                              <Badge key={p} variant="outline">
                                {PERMISSION_LABELS[p]}
                              </Badge>
                            ))}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {tab === "groups" && (
          <div className="p-4">
            {groupsData.length === 0 && (
              <div className="p-7.5 text-center text-sm text-muted-foreground">
                暂无数据组。
                <Can permission="group.manage">
                  <a
                    onClick={() => setManageGroupsOpen(true)}
                    className="cursor-pointer text-brand"
                  >
                    新建一个
                  </a>
                </Can>
              </div>
            )}
            {groupsData.map((g) => {
              return (
                <div
                  key={g.id}
                  className="mb-2 flex items-center justify-between rounded-md border border-border bg-card px-3.5 py-3"
                >
                  <div className="flex items-center gap-3">
                    <Icon name="folder" size={18} className="text-muted-foreground" />
                    <div>
                      <div className="text-sm font-medium">{g.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {g.member_count ?? "—"} 名成员
                        {g.description ? ` · ${g.description}` : ""}
                      </div>
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      clearSelection();
                      patch({ groupId: g.id, page: 1, tab: "members" }, { replace: false });
                    }}
                  >
                    查看成员
                  </Button>
                </div>
              );
            })}
          </div>
        )}

        {tab === "invitations" && <InvitationListPanel />}
      </Card>

      <InviteUserModal open={inviteOpen} onClose={() => setInviteOpen(false)} />
      <BulkInviteModal open={bulkInviteOpen} onClose={() => setBulkInviteOpen(false)} />
      <BulkGroupAssignmentModal
        open={bulkGroupOpen}
        users={selectedUsers}
        groups={groupsData}
        onClose={() => setBulkGroupOpen(false)}
      />
      <EditUserModal open={!!editing} user={editing} onClose={() => setEditing(null)} />
      <GroupManageModal open={manageGroupsOpen} onClose={() => setManageGroupsOpen(false)} />
      <OffboardingDialog
        open={!!offboardingUser}
        user={offboardingUser}
        onClose={() => setOffboardingUser(null)}
      />
      <ReactivateDialog
        open={!!reactivatingUser}
        user={reactivatingUser}
        onClose={() => setReactivatingUser(null)}
      />

      <Modal
        open={!!resettingPwd}
        onClose={() => {
          if (pwdResetSubmitting) return;
          setResettingPwd(null);
        }}
        title="重置用户密码"
        width={460}
      >
        {resettingPwd && (
          <div className="flex flex-col gap-3.5 text-sm">
            <div className="text-muted-foreground">
              将为以下用户生成一次性临时密码。请通过安全渠道（IM / 当面）告知用户，
              并提醒首次登录后立即修改密码。
            </div>
            <div className={SUMMARY_CARD_CLASS}>
              <UserAvatar user={resettingPwd} size="md" />
              <div>
                <div className="text-sm font-medium">{resettingPwd.name}</div>
                <div className="mono text-xs text-muted-foreground">{resettingPwd.email}</div>
              </div>
              <span className="ml-auto">
                <Badge variant={ROLE_COLORS[resettingPwd.role] || "outline"}>
                  {ROLE_LABELS[resettingPwd.role as UserRole] ?? resettingPwd.role}
                </Badge>
              </span>
            </div>
            <div className="flex justify-end gap-2">
              <Button onClick={() => setResettingPwd(null)} disabled={pwdResetSubmitting}>
                取消
              </Button>
              <Button
                variant="primary"
                disabled={pwdResetSubmitting}
                onClick={async () => {
                  if (!resettingPwd) return;
                  setPwdResetSubmitting(true);
                  try {
                    const r = await usersApi.adminResetPassword(resettingPwd.id);
                    setTempPwdResult({ user: resettingPwd, password: r.temp_password });
                    setResettingPwd(null);
                  } catch (e) {
                    pushToast({ msg: "重置失败", sub: (e as Error).message, kind: "warning" });
                  } finally {
                    setPwdResetSubmitting(false);
                  }
                }}
              >
                {pwdResetSubmitting ? "生成中..." : "生成临时密码"}
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={!!tempPwdResult}
        onClose={() => setTempPwdResult(null)}
        title="临时密码已生成"
        width={460}
      >
        {tempPwdResult && (
          <div className="flex flex-col gap-3.5 text-sm">
            <div className="text-muted-foreground">
              请立即复制并通过安全渠道告知 <b>{tempPwdResult.user.email}</b>。
              关闭此窗口后无法再次查看；用户首次登录后系统会强制要求修改密码。
            </div>
            <div className="break-all rounded-md border border-dashed border-status-caution bg-muted p-3 font-mono text-sm font-medium select-all">
              {tempPwdResult.password}
            </div>
            <div className="flex justify-end gap-2">
              <Button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(tempPwdResult.password);
                    pushToast({ msg: "已复制到剪贴板", kind: "success" });
                  } catch {
                    pushToast({ msg: "复制失败，请手动选择文本", kind: "warning" });
                  }
                }}
              >
                复制
              </Button>
              <Button variant="primary" onClick={() => setTempPwdResult(null)}>
                我已记下，关闭
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={!!deleting}
        onClose={() => {
          if (deleteUser.isPending) return;
          setDeleting(null);
          setTransferStage(null);
          setTransferToId("");
          deleteUser.reset();
        }}
        title={transferStage ? "先转交未完成任务" : "删除账号确认"}
        width={520}
      >
        {deleting && (
          <div className="flex flex-col gap-3.5 text-sm">
            <div className="text-muted-foreground">
              {transferStage
                ? "该用户当前持有未完成任务或锁定任务；删除前请选择一名接收者，所有任务将被转交。"
                : "确认删除以下账号？该用户将无法登录，但历史标注与审计记录仍会保留。"}
            </div>
            <div className={SUMMARY_CARD_CLASS}>
              <UserAvatar user={deleting} size="md" />
              <div>
                <div className="text-sm font-medium">{deleting.name}</div>
                <div className="mono text-xs text-muted-foreground">{deleting.email}</div>
              </div>
              <span className="ml-auto">
                <Badge variant={ROLE_COLORS[deleting.role] || "outline"}>
                  {ROLE_LABELS[deleting.role as UserRole] ?? deleting.role}
                </Badge>
              </span>
            </div>

            {transferStage && (
              <>
                <div className="flex flex-col gap-1 rounded-md border border-status-caution bg-status-caution-soft px-3 py-2.5 text-sm">
                  <div>
                    <Icon name="warning" size={12} /> 未完成任务{" "}
                    <strong>{transferStage.pending}</strong> 个
                    {transferStage.locked > 0 && (
                      <>
                        {" "}
                        · 锁定任务 <strong>{transferStage.locked}</strong> 个
                      </>
                    )}
                  </div>
                  {transferStage.sample.length > 0 && (
                    <div className="mono text-xs text-muted-foreground">
                      示例：{transferStage.sample.slice(0, 3).join(", ")}
                      {transferStage.sample.length > 3 && " ..."}
                    </div>
                  )}
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                    转交给（同项目启用用户）
                  </label>
                  <select
                    value={transferToId}
                    onChange={(e) => setTransferToId(e.target.value)}
                    className={`${SELECT_BASE} w-full cursor-pointer px-2.5 py-2 text-sm`}
                  >
                    <option value="">— 选择接收用户 —</option>
                    {transferUsers
                      .filter(
                        (u: UserResponse) =>
                          u.id !== deleting.id &&
                          u.is_active &&
                          (u.role === "employee" || u.role === "project_admin"),
                      )
                      .map((u: UserResponse) => (
                        <option key={u.id} value={u.id}>
                          {u.name} ({ROLE_LABELS[u.role as UserRole] ?? u.role}) · {u.email}
                        </option>
                      ))}
                  </select>
                </div>
              </>
            )}

            {deleteUser.error && (
              <div className="flex items-center gap-2 rounded-md border border-status-danger bg-status-danger-soft px-3 py-2 text-sm text-status-danger">
                <Icon name="warning" size={12} />{" "}
                {(deleteUser.error as Error)?.message ?? "删除失败"}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <Button
                onClick={() => {
                  setDeleting(null);
                  setTransferStage(null);
                  setTransferToId("");
                  deleteUser.reset();
                }}
                disabled={deleteUser.isPending}
              >
                取消
              </Button>
              <Button
                variant="danger"
                onClick={async () => {
                  try {
                    await deleteUser.mutateAsync({
                      userId: deleting.id,
                      transferToUserId: transferStage ? transferToId || undefined : undefined,
                    });
                    pushToast({
                      msg: transferStage
                        ? `已删除 ${deleting.name}，任务已转交`
                        : `已删除账号 ${deleting.name}`,
                      kind: "success",
                    });
                    setDeleting(null);
                    setTransferStage(null);
                    setTransferToId("");
                  } catch (err) {
                    // 检测 409 + has_pending_tasks → 切到二阶段
                    if (err instanceof ApiError && err.status === 409) {
                      const raw = err.detailRaw as
                        | {
                            reason?: string;
                            pending_task_count?: number;
                            locked_task_count?: number;
                            sample_task_ids?: string[];
                          }
                        | undefined;
                      if (raw?.reason === "has_pending_tasks") {
                        setTransferStage({
                          pending: raw.pending_task_count ?? 0,
                          locked: raw.locked_task_count ?? 0,
                          sample: raw.sample_task_ids ?? [],
                        });
                        deleteUser.reset();
                        return;
                      }
                    }
                    void err;
                  }
                }}
                disabled={deleteUser.isPending || (transferStage !== null && !transferToId)}
              >
                <Icon name="trash" size={12} />
                {deleteUser.isPending
                  ? transferStage
                    ? "转交并删除中…"
                    : "删除中…"
                  : transferStage
                    ? "转交并删除"
                    : "确认删除"}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </PageContainer>
  );
}
