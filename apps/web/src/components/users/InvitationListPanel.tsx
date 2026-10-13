import { FilterGroup, FilterSelect, FilterToggle } from "@/components/filters/FilterControls";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { FilterTrigger } from "@/components/filters/FilterTrigger";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import {
  useInvitationStats,
  useInvitationPage,
  useResendInvitation,
  useRevokeInvitation,
  useSendInvitationEmail,
} from "@/hooks/useInvitations";
import { useProjects } from "@/hooks/useProjects";
import { ROLE_LABELS } from "@/constants/roles";
import { usePermissions } from "@/hooks/usePermissions";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { invitationsApi, type InvitationResponse, type InvitationStatus } from "@/api/invitations";
import type { UserRole } from "@/types";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import {
  INVITATION_URL_DEFAULTS,
  INVITATION_URL_KEYS,
  invitationUrlCodec,
} from "@/pages/Users/usersUrlState";
import styles from "./InvitationListPanel.module.css";

const STATUS_LABEL: Record<InvitationStatus, string> = {
  pending: "待接受",
  accepted: "已接受",
  expired: "已过期",
  revoked: "已撤销",
};
const STATUS_COLORS = {
  pending: "warning",
  accepted: "success",
  expired: "outline",
  revoked: "danger",
} as const;

export function InvitationListPanel() {
  const { role } = usePermissions();
  const ownerId = useAuthStore((state) => state.user?.id);
  const urlState = useUrlFilterState({
    codec: invitationUrlCodec,
    defaults: INVITATION_URL_DEFAULTS,
    ownedKeys: INVITATION_URL_KEYS,
  });
  const { state: filters, issues, patch } = urlState;
  const [searchDraft, setSearchDraft] = useState(filters.q);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const syncingSearchDraft = useRef(false);
  const lastUrlQuery = useRef(filters.q);
  const debouncedSearch = useDebouncedValue(searchDraft, 250);
  const [exporting, setExporting] = useState(false);
  const [copiedLink, setCopiedLink] = useState<string | null>(null);
  const scope = role === "super_admin" ? filters.scope : "me";
  const page = filters.page;

  useEffect(() => {
    if (lastUrlQuery.current === filters.q) return;
    lastUrlQuery.current = filters.q;
    if (searchDraft !== filters.q) {
      syncingSearchDraft.current = true;
      setSearchDraft(filters.q);
    }
  }, [filters.q, searchDraft]);

  useEffect(() => {
    if (syncingSearchDraft.current) {
      if (debouncedSearch === filters.q) syncingSearchDraft.current = false;
      return;
    }
    const nextSearch = debouncedSearch.trim();
    if (nextSearch !== searchDraft.trim()) return;
    if (nextSearch === filters.q) return;
    patch({ q: nextSearch, page: 1 }, { replace: true });
  }, [debouncedSearch, filters.q, patch, searchDraft]);

  useEffect(() => {
    if (role === "super_admin" || filters.scope === "me") return;
    patch({ scope: "me" }, { replace: true });
  }, [filters.scope, patch, role]);

  const appliedFilters = useMemo(
    () => ({
      status: filters.status,
      scope,
      search: filters.q || undefined,
      project_id: filters.projectId || undefined,
      role: filters.role || undefined,
    }),
    [filters.projectId, filters.q, filters.role, filters.status, scope],
  );
  const query = useInvitationPage({ ...appliedFilters, page, page_size: 25 });
  const statsQuery = useInvitationStats(appliedFilters);
  const { data: projects = [] } = useProjects();
  const invites = query.data?.items ?? [];
  const stats = statsQuery.data;
  const revoke = useRevokeInvitation();
  const resend = useResendInvitation();
  const sendEmail = useSendInvitationEmail();
  const pushToast = useToastStore((state) => state.push);
  const busy = revoke.isPending || resend.isPending || sendEmail.isPending;

  const actOnInvite = async (
    invitation: InvitationResponse,
    action: "revoke" | "resend" | "email",
  ) => {
    if (!ownerId || !isCurrentAuthOwner(ownerId) || busy) return;
    const token = useAuthStore.getState().token;
    const current = () => isCurrentAuthOwner(ownerId) && useAuthStore.getState().token === token;
    try {
      if (action === "revoke") await revoke.mutateAsync(invitation.id);
      if (action === "email") await sendEmail.mutateAsync(invitation.id);
      if (action === "resend") {
        const result = await resend.mutateAsync(invitation.id);
        if (!current()) return;
        setCopiedLink(result.invite_url);
        try {
          await navigator.clipboard.writeText(result.invite_url);
        } catch {
          /* The visible link remains usable. */
        }
      }
      if (current())
        pushToast({
          msg:
            action === "email"
              ? "邀请邮件已发送"
              : action === "revoke"
                ? "邀请已撤销"
                : "已生成新邀请链接",
          kind: "success",
        });
    } catch (error) {
      if (current())
        pushToast({
          msg: "邀请操作失败",
          sub: error instanceof Error ? error.message : String(error),
          kind: "error",
        });
    }
  };
  const exportRows = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      await invitationsApi.exportInvitations(appliedFilters);
    } catch (error) {
      pushToast({
        msg: "导出失败",
        sub: error instanceof Error ? error.message : String(error),
        kind: "error",
      });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className={styles.root}>
      <div className={`${styles.toolbar} flex-wrap gap-2`}>
        <FilterGroup label="邀请状态" hideLabel compact>
          {(["all", "pending", "accepted", "expired", "revoked"] as const).map((value) => (
            <FilterToggle
              compact
              active={filters.status === value}
              type="button"
              key={value}
              onClick={() => {
                patch({ status: value, page: 1 }, { replace: false });
              }}
            >
              {value === "all" ? "全部" : STATUS_LABEL[value]}
            </FilterToggle>
          ))}
        </FilterGroup>
        <FilterPanel
          open={filtersOpen}
          onOpenChange={setFiltersOpen}
          trigger={
            <FilterTrigger
              count={
                Number(scope !== "me") +
                Number(Boolean(filters.projectId)) +
                Number(Boolean(filters.role))
              }
            />
          }
          title="邀请筛选"
          description="即时生效 · 保留邀请状态与搜索。"
          align="start"
          footer={
            <div className="flex justify-between gap-2">
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  patch({ scope: "me", projectId: "", role: "", page: 1 }, { replace: false })
                }
              >
                恢复默认范围
              </Button>
              <Button size="sm" onClick={() => setFiltersOpen(false)}>
                完成
              </Button>
            </div>
          }
        >
          <div className="grid gap-3">
            {role === "super_admin" && (
              <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
                范围
                <FilterSelect
                  aria-label="邀请范围"
                  value={scope}
                  onChange={(event) => {
                    patch(
                      { scope: event.target.value as "me" | "all", page: 1 },
                      { replace: false },
                    );
                  }}
                  className="w-full"
                >
                  <option value="me">我邀请的</option>
                  <option value="all">全部邀请</option>
                </FilterSelect>
              </label>
            )}
            <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
              项目
              <FilterSelect
                aria-label="邀请项目筛选"
                value={filters.projectId}
                onChange={(event) => {
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
                aria-label="邀请角色筛选"
                value={filters.role}
                onChange={(event) => {
                  patch({ role: event.target.value, page: 1 }, { replace: false });
                }}
                className="w-full"
              >
                <option value="">全部角色</option>
                {Object.entries(ROLE_LABELS).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </FilterSelect>
            </label>
          </div>
        </FilterPanel>
        <input
          value={searchDraft}
          onChange={(event) => {
            syncingSearchDraft.current = false;
            setSearchDraft(event.target.value);
          }}
          placeholder="搜索邮箱或邀请人"
          className={styles.select}
          aria-label="搜索邀请"
        />
        <Button size="sm" onClick={() => void exportRows()} disabled={exporting}>
          {exporting ? "导出中…" : "导出筛选结果"}
        </Button>
      </div>
      {(scope !== "me" || filters.projectId || filters.role) && (
        <div
          className="flex flex-wrap gap-1.5 px-4 pb-2"
          role="group"
          aria-label="已应用的邀请筛选"
        >
          {scope !== "me" && (
            <ActiveFilterChip
              label="范围"
              value="全部邀请"
              onClick={() => setFiltersOpen(true)}
              onRemove={() => patch({ scope: "me", page: 1 }, { replace: false })}
            />
          )}
          {filters.projectId && (
            <ActiveFilterChip
              label="项目"
              value={
                projects.find((project) => project.id === filters.projectId)?.name ?? "指定项目"
              }
              onClick={() => setFiltersOpen(true)}
              onRemove={() => patch({ projectId: "", page: 1 }, { replace: false })}
            />
          )}
          {filters.role && (
            <ActiveFilterChip
              label="角色"
              value={ROLE_LABELS[filters.role as UserRole] ?? filters.role}
              onClick={() => setFiltersOpen(true)}
              onRemove={() => patch({ role: "", page: 1 }, { replace: false })}
            />
          )}
        </div>
      )}
      {!!issues.length && (
        <div role="alert" className="px-4 pb-2 text-xs text-status-caution">
          URL 邀请筛选无法完整恢复，已使用安全默认值。
        </div>
      )}
      {stats && (
        <div className="grid grid-cols-2 gap-2 px-4 pb-3 text-xs text-muted-foreground sm:grid-cols-5">
          <span>筛选结果 {stats.total}</span>
          <span>待接受 {stats.pending}</span>
          <span>已接受 {stats.accepted}</span>
          <span>已过期 {stats.expired}</span>
          <span>已撤销 {stats.revoked}</span>
        </div>
      )}
      {(query.isError || statsQuery.isError) && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 px-4 py-3 text-sm text-status-danger"
        >
          <span>邀请记录或统计加载失败，请重试。</span>
          <Button
            size="sm"
            onClick={() => {
              void query.refetch();
              void statsQuery.refetch();
            }}
          >
            重试
          </Button>
        </div>
      )}
      {copiedLink && (
        <label className="flex flex-col gap-1 px-4 pb-3 text-xs text-muted-foreground">
          新邀请链接（旧链接已失效；可选中复制）
          <input
            readOnly
            value={copiedLink}
            className={styles.select}
            onFocus={(event) => event.target.select()}
          />
        </label>
      )}
      <div className={styles.tableScroller}>
        <table className={styles.table}>
          <thead>
            <tr>
              {["邮箱", "角色", "数据组", "项目", "状态", "邀请人", "过期时间", "操作"].map(
                (title) => (
                  <th key={title} className={styles.th}>
                    {title}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {query.isLoading && (
              <tr>
                <td colSpan={8} className={styles.cell}>
                  加载邀请中…
                </td>
              </tr>
            )}
            {!query.isLoading && !query.isError && invites.length === 0 && (
              <tr>
                <td colSpan={8} className={styles.cell}>
                  没有符合筛选条件的邀请。
                </td>
              </tr>
            )}
            {invites.map((invitation) => (
              <tr key={invitation.id}>
                <td className={styles.cell}>
                  <span className={`mono ${styles.email}`} title={invitation.email}>
                    {invitation.email}
                  </span>
                </td>
                <td className={styles.cell}>
                  {ROLE_LABELS[invitation.role as UserRole] ?? invitation.role}
                </td>
                <td className={styles.cell}>
                  <span className={styles.truncateText} title={invitation.group_name ?? undefined}>
                    {invitation.group_name ?? "—"}
                  </span>
                </td>
                <td className={styles.cell}>
                  <span
                    className={styles.truncateText}
                    title={invitation.project_name ?? undefined}
                  >
                    {invitation.project_name ?? "未指定"}
                  </span>
                </td>
                <td className={styles.cell}>
                  <Badge variant={STATUS_COLORS[invitation.status]}>
                    {STATUS_LABEL[invitation.status]}
                  </Badge>
                </td>
                <td className={styles.cell}>
                  <span
                    className={styles.truncateText}
                    title={invitation.invited_by_name ?? undefined}
                  >
                    {invitation.invited_by_name ?? "—"}
                  </span>
                </td>
                <td className={`${styles.cell} ${styles.dateCell}`}>
                  {new Date(invitation.expires_at).toLocaleString("zh-CN")}
                </td>
                <td className={`${styles.cell} ${styles.actionsCell}`}>
                  {invitation.status !== "accepted" && (
                    <>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => void actOnInvite(invitation, "resend")}
                        title="生成新链接（旧链接将失效）"
                      >
                        <Icon name="refresh" size={11} />
                        新链接
                      </Button>
                      {invitation.status === "pending" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => void actOnInvite(invitation, "email")}
                          title="发送邀请邮件（保留当前链接）"
                        >
                          <Icon name="mail" size={11} />
                          邮件
                        </Button>
                      )}
                      {invitation.status !== "revoked" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => void actOnInvite(invitation, "revoke")}
                          title="撤销邀请"
                        >
                          <Icon name="x" size={11} />
                        </Button>
                      )}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {query.data && query.data.pages > 1 && (
        <div className="flex items-center justify-between border-t border-border px-4 py-3 text-xs text-muted-foreground">
          <span>
            第 {query.data.page} / {query.data.pages} 页 · 共 {query.data.total} 条
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
              disabled={page >= query.data.pages}
              onClick={() => patch({ page: page + 1 }, { replace: false })}
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
