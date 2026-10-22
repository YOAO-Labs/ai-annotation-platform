import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useState } from "react";

import {
  type OffboardingMode,
  type OffboardingPreview,
  type OffboardingProjectPreview,
  type OffboardingResult,
  type OffboardingRole,
  type UserResponse,
} from "@/api/users";
import { ApiError } from "@/api/client";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useOffboardUser, useOffboardingPreview, useReactivateUser } from "@/hooks/useUsers";

const ROLES: OffboardingRole[] = ["owner", "annotator", "reviewer"];
const ROLE_LABELS: Record<OffboardingRole, string> = {
  owner: "项目负责人",
  annotator: "标注员",
  reviewer: "审核员",
};
const TASK_LABELS: Record<string, string> = {
  pending: "待处理",
  in_progress: "进行中",
  review: "待审核",
  rejected: "已驳回",
  locked: "锁定",
};
const UNRESOLVED_REASON_LABELS: Record<string, string> = {
  emergency_suspension_requires_later_handoff: "紧急停用，需后续交接",
};
const DISABLED_KIND_LABELS: Record<string, string> = {
  suspended: "停用（可恢复）",
  emergency_suspended: "紧急停用",
  deleted: "已删除",
  historical_unknown: "历史未知状态",
};
const REACTIVATABLE_KINDS = new Set(["suspended", "emergency_suspended"]);
const HISTORICAL_KINDS = new Set(["deleted", "historical_unknown"]);

type ReceiverSelections = Record<string, Partial<Record<OffboardingRole, string>>>;

function formatDateTime(iso: string | null | undefined) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

function getErrorStatus(error: unknown) {
  if (error instanceof ApiError) return error.status;
  if (error && typeof error === "object" && "status" in error) {
    const status = Number((error as { status?: unknown }).status);
    return Number.isFinite(status) ? status : undefined;
  }
  return undefined;
}

function getErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function getErrorCode(error: unknown) {
  if (!error || typeof error !== "object" || !("detailRaw" in error)) return undefined;
  const raw = (error as { detailRaw?: unknown }).detailRaw;
  if (!raw || typeof raw !== "object") return undefined;
  return "code" in raw ? String((raw as { code?: unknown }).code || "") : undefined;
}

function previewErrorCopy(error: unknown) {
  const status = getErrorStatus(error);
  if (status === 403) {
    return {
      title: "没有权限查看离职预览",
      description: "请联系超级管理员，或确认你仍负责该用户所在的项目。",
      kind: "warning" as const,
    };
  }
  if (status !== undefined && status >= 500) {
    return {
      title: "暂时无法加载离职预览",
      description: "服务器返回错误，请稍后重试。",
      kind: "error" as const,
    };
  }
  return {
    title: "加载离职预览失败",
    description: getErrorMessage(error),
    kind: "error" as const,
  };
}

function ErrorPanel({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const copy = previewErrorCopy(error);
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-border bg-muted px-4 py-10 text-center">
      <Icon
        name={copy.kind === "warning" ? "shieldAlert" : "warning"}
        size={22}
        className={copy.kind === "warning" ? "text-status-caution" : "text-status-danger"}
      />
      <div className="text-sm font-medium">{copy.title}</div>
      <div className="max-w-md text-xs text-muted-foreground">{copy.description}</div>
      <Button size="sm" onClick={onRetry}>
        <Icon name="refresh" size={12} /> 重试
      </Button>
    </div>
  );
}

function UserSummary({ user }: { user: UserResponse }) {
  const disabledKind = user.disabled_kind ?? "";
  const isActive = user.is_active !== false;
  return (
    <div className="rounded-lg border border-border bg-muted px-3.5 py-3">
      <div className="flex items-start gap-3">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-full bg-status-info-alt-soft text-sm font-semibold text-status-info-alt">
          {user.name[0] || "?"}
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
            <span>{user.name}</span>
            <Badge variant={isActive ? "success" : "warning"}>{isActive ? "启用" : "已停用"}</Badge>
          </div>
          <div className="mono mt-0.5 truncate text-xs text-muted-foreground">{user.email}</div>
        </div>
        {!isActive && disabledKind && (
          <Badge
            className="ml-auto"
            variant={HISTORICAL_KINDS.has(disabledKind) ? "danger" : "warning"}
          >
            {DISABLED_KIND_LABELS[disabledKind] ?? disabledKind}
          </Badge>
        )}
      </div>
      {!isActive && (
        <div className="mt-3 grid gap-1 border-t border-border pt-2.5 text-xs text-muted-foreground sm:grid-cols-2">
          <span>停用时间：{formatDateTime(user.disabled_at)}</span>
          <span>停用原因：{user.disabled_reason || "未填写"}</span>
        </div>
      )}
    </div>
  );
}

function TaskSummary({ project }: { project: OffboardingProjectPreview }) {
  const rows = Object.entries(project.tasks ?? {});
  if (rows.length === 0) {
    return <div className="text-xs text-muted-foreground">暂无任务统计</div>;
  }
  const statuses = ["pending", "in_progress", "review", "rejected", "locked"];
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full min-w-[520px] text-xs">
        <thead className="bg-muted text-left text-muted-foreground">
          <tr>
            <th className="px-2.5 py-2 font-medium">任务归属</th>
            {statuses.map((status) => (
              <th key={status} className="px-2.5 py-2 text-right font-medium">
                {TASK_LABELS[status]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([role, counts]) => (
            <tr key={role} className="border-t border-border">
              <td className="px-2.5 py-2 font-medium">
                {ROLE_LABELS[role as OffboardingRole] ?? role}
              </td>
              {statuses.map((status) => (
                <td key={status} className="px-2.5 py-2 text-right tabular-nums">
                  {counts?.[status] ?? 0}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProjectCard({
  project,
  selections,
  onSelect,
  disabled,
}: {
  project: OffboardingProjectPreview;
  selections: Partial<Record<OffboardingRole, string>>;
  onSelect: (role: OffboardingRole, value: string) => void;
  disabled: boolean;
}) {
  const presentRoles = ROLES.filter((role) => project.roles?.[role]?.present);
  return (
    <div className="rounded-lg border border-border bg-card p-3.5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">{project.project_name}</div>
          <div className="mono mt-0.5 text-2xs text-muted-foreground">{project.project_id}</div>
        </div>
        {project.blockers.length > 0 && (
          <Badge variant="warning">{project.blockers.length} 项阻塞</Badge>
        )}
      </div>

      {(project.locked_task_count ?? 0) > 0 && (
        <div className="mb-3 text-xs text-muted-foreground">
          此账号持有 {project.locked_task_count} 个编辑锁，停用时将释放。
        </div>
      )}

      {project.blockers.length > 0 && (
        <div className="mb-3 flex flex-col gap-1 rounded-md border border-status-caution/30 bg-status-caution-soft px-3 py-2 text-xs text-status-caution">
          {project.blockers.map((blocker) => (
            <div key={`${blocker.code}-${blocker.message}`} className="flex gap-1.5">
              <Icon name="warning" size={12} className="mt-0.5 shrink-0" />
              <span>{blocker.message}</span>
            </div>
          ))}
        </div>
      )}

      {presentRoles.length === 0 ? (
        <div className="text-xs text-muted-foreground">该用户在此项目没有需要交接的责任。</div>
      ) : (
        <div className="flex flex-col gap-2.5">
          {presentRoles.map((role) => {
            const rolePreview = project.roles[role];
            return (
              <div key={role} className="rounded-md border border-border bg-muted/40 px-3 py-2.5">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="text-xs font-semibold">{ROLE_LABELS[role]}</div>
                    {rolePreview.batches.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1 text-2xs text-muted-foreground">
                        <span>批次：</span>
                        {rolePreview.batches.map((batch) => (
                          <Badge key={batch.batch_id} variant="outline" title={batch.batch_id}>
                            {batch.batch_name}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>
                  {role === "owner" && rolePreview.batches.length === 0 && (
                    <span className="text-2xs text-muted-foreground">项目所有权</span>
                  )}
                </div>
                <label className="mt-2 block text-2xs font-medium text-muted-foreground">
                  交接接收人
                  <select
                    aria-label={`${project.project_name} ${ROLE_LABELS[role]}接收人`}
                    value={selections[role] ?? ""}
                    onChange={(event) => onSelect(role, event.target.value)}
                    disabled={disabled}
                    className="mt-1 w-full appearance-none rounded-md border border-border bg-card px-2.5 py-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <option value="" disabled hidden>
                      选择同项目合格接收人
                    </option>
                    {rolePreview.receiver_options.map((receiver) => (
                      <option key={receiver.id} value={receiver.id}>
                        {receiver.name} · {receiver.email}
                      </option>
                    ))}
                  </select>
                  {rolePreview.receiver_options.length === 0 && (
                    <span className="mt-1 block text-2xs text-status-caution">
                      当前没有符合该角色要求的启用接收人。
                    </span>
                  )}
                </label>
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-3 border-t border-border pt-2.5">
        <div className="mb-1.5 text-2xs font-semibold text-muted-foreground">任务状态与锁</div>
        <TaskSummary project={project} />
      </div>
    </div>
  );
}

function ApiKeysSummary({ preview }: { preview: OffboardingPreview }) {
  return (
    <div className="rounded-lg border border-border bg-card p-3.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-semibold">API Key</div>
        <Badge variant={preview.api_keys.some((key) => !key.revoked_at) ? "warning" : "outline"}>
          {preview.api_keys.length} 个
        </Badge>
      </div>
      {preview.api_keys.length === 0 ? (
        <div className="text-xs text-muted-foreground">该用户没有 API Key。</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {preview.api_keys.map((key) => (
            <div
              key={key.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-muted px-2.5 py-2 text-xs"
            >
              <Icon name="key" size={12} className="text-muted-foreground" />
              <span className="font-medium">{key.name}</span>
              <span className="mono text-muted-foreground">{key.key_prefix}</span>
              <span className="ml-auto text-2xs text-muted-foreground">
                {key.revoked_at
                  ? `已吊销 ${formatDateTime(key.revoked_at)}`
                  : `最近使用 ${formatDateTime(key.last_used_at)}`}
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="mt-2 text-2xs text-muted-foreground">
        停用会吊销当前密钥；恢复账号不会恢复旧密钥。
      </div>
    </div>
  );
}

function CommitResult({ result, onClose }: { result: OffboardingResult; onClose: () => void }) {
  const isEmergency = result.mode === "emergency_suspend";
  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex items-start gap-2.5 rounded-lg border border-status-positive/30 bg-status-positive-soft px-3.5 py-3 text-sm">
        <Icon name="checkCircle" size={18} className="mt-0.5 shrink-0 text-status-positive" />
        <div>
          <div className="font-semibold">
            {isEmergency ? "账号已紧急停用" : "离职交接已完成，账号已停用"}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            API Key 已吊销 {result.revoked_api_key_ids.length} 个
            {result.audit_id ? ` · 审计记录 #${result.audit_id}` : ""}
          </div>
        </div>
      </div>

      {result.transfers.length > 0 && (
        <div className="rounded-lg border border-border bg-card p-3.5">
          <div className="mb-2 text-sm font-semibold">已交接责任</div>
          <div className="flex flex-col gap-1.5 text-xs">
            {result.transfers.map((transfer, index) => (
              <div
                key={`${transfer.project_id}-${transfer.role}-${index}`}
                className="flex items-center justify-between gap-2 rounded-md bg-muted px-2.5 py-2"
              >
                <span>{ROLE_LABELS[transfer.role as OffboardingRole] ?? transfer.role}</span>
                <span className="text-muted-foreground">
                  {transfer.task_count} 个任务 · {transfer.lock_count} 个锁
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {result.unresolved.length > 0 ? (
        <div className="rounded-lg border border-status-caution/30 bg-status-caution-soft p-3.5">
          <div className="mb-1 text-sm font-semibold text-status-caution">待后续交接的责任</div>
          <div className="mb-2 text-xs text-muted-foreground">
            紧急停用保留了以下未交接项目、批次和任务，后续可从已停用用户中继续处理。
          </div>
          <div className="flex flex-col gap-1.5 text-xs">
            {result.unresolved.map((item, index) => (
              <div
                key={`${item.project_id}-${item.role}-${index}`}
                className="rounded-md border border-status-caution/20 bg-card/60 px-2.5 py-2"
              >
                <div className="font-medium">
                  {ROLE_LABELS[item.role as OffboardingRole] ?? item.role ?? "未分类责任"}
                </div>
                <div className="mt-0.5 text-muted-foreground">
                  {item.task_count} 个任务 · {item.lock_count} 个锁 ·{" "}
                  {UNRESOLVED_REASON_LABELS[item.reason] ?? item.reason}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : isEmergency ? (
        <div className="rounded-lg border border-border bg-muted px-3 py-2.5 text-xs text-muted-foreground">
          本次没有检测到需要后续交接的责任。
        </div>
      ) : null}

      <div className="flex justify-end">
        <Button variant="primary" onClick={onClose}>
          关闭
        </Button>
      </div>
    </div>
  );
}

export interface OffboardingDialogProps {
  open: boolean;
  user: UserResponse | null;
  onClose: () => void;
}

export function OffboardingDialog({ open, user, onClose }: OffboardingDialogProps) {
  const userId = user?.id ?? null;
  const previewQuery = useOffboardingPreview(userId, open);
  const offboard = useOffboardUser();
  const pushToast = useToastStore((state) => state.push);
  const previewPaused = previewQuery.fetchStatus === "paused";
  const [mode, setMode] = useState<OffboardingMode>("handoff");
  const [reason, setReason] = useState("");
  const [selections, setSelections] = useState<ReceiverSelections>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [result, setResult] = useState<OffboardingResult | null>(null);

  useEffect(() => {
    if (!open) return;
    setMode("handoff");
    setReason("");
    setSelections({});
    setFormError(null);
    setStale(false);
    setResult(null);
    offboard.reset();
    // Reset only when the target changes or a new dialog is opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, userId]);

  useEffect(() => {
    if (previewQuery.data?.preview_version) {
      setSelections({});
    }
  }, [previewQuery.data?.preview_version]);

  const preview = previewQuery.data;
  const lifecycleUser = previewQuery.data?.user ?? user;
  const historical = HISTORICAL_KINDS.has(lifecycleUser?.disabled_kind ?? "");
  const pendingHandoff = lifecycleUser?.disabled_kind === "emergency_suspended";
  const canStartLifecycle =
    !!lifecycleUser &&
    !historical &&
    (lifecycleUser.is_active !== false ||
      REACTIVATABLE_KINDS.has(lifecycleUser.disabled_kind ?? ""));

  const requiredRoles = useMemo(() => {
    if (!preview) return [];
    return preview.projects.flatMap((project) =>
      ROLES.filter((role) => project.roles?.[role]?.present).map((role) => ({ project, role })),
    );
  }, [preview]);

  const missingReceivers = requiredRoles.filter(({ project, role }) => {
    const selected = selections[project.project_id]?.[role];
    return (
      !selected || !project.roles[role].receiver_options.some((option) => option.id === selected)
    );
  });
  const blockers = preview
    ? [...preview.blockers, ...preview.projects.flatMap((project) => project.blockers)]
    : [];
  const handoffDisabled =
    !preview ||
    !preview.can_commit ||
    blockers.length > 0 ||
    missingReceivers.length > 0 ||
    stale ||
    offboard.isPending ||
    previewQuery.isFetching ||
    !canStartLifecycle;
  const emergencyDisabled =
    !preview || stale || offboard.isPending || previewQuery.isFetching || !canStartLifecycle;

  const refreshPreview = async () => {
    setSelections({});
    setStale(true);
    try {
      const result = await previewQuery.refetch();
      if (result.error) {
        setStale(true);
        setFormError("刷新离职预览失败，请检查网络后重试。旧接收人选择已清空。");
        return;
      }
      setStale(false);
      setFormError(null);
    } catch (error) {
      setStale(true);
      setFormError(`刷新离职预览失败：${getErrorMessage(error)}`);
    }
  };

  const selectReceiver = (projectId: string, role: OffboardingRole, receiverId: string) => {
    setSelections((current) => ({
      ...current,
      [projectId]: {
        ...current[projectId],
        ...(receiverId ? { [role]: receiverId } : { [role]: undefined }),
      },
    }));
    setFormError(null);
  };

  const submit = async () => {
    if (!user || !preview || offboard.isPending) return;
    if (
      mode === "handoff" &&
      (blockers.length > 0 || missingReceivers.length > 0 || !preview.can_commit)
    ) {
      setFormError(
        blockers.length > 0
          ? "当前预览存在阻塞项，请先处理阻塞项或改用紧急停用。"
          : "请为每个项目责任选择预览中提供的合格接收人。",
      );
      return;
    }
    if (stale) {
      setFormError("预览已过期，请刷新预览并重新确认接收人。");
      return;
    }
    setFormError(null);
    try {
      const response = await offboard.mutateAsync({
        userId: user.id,
        payload: {
          preview_version: preview.preview_version,
          reason: reason.trim(),
          mode,
          projects: preview.projects.map((project) => {
            const selected = mode === "handoff" ? (selections[project.project_id] ?? {}) : {};
            return {
              project_id: project.project_id,
              ...(selected.owner ? { owner_receiver_id: selected.owner } : {}),
              ...(selected.annotator ? { annotator_receiver_id: selected.annotator } : {}),
              ...(selected.reviewer ? { reviewer_receiver_id: selected.reviewer } : {}),
            };
          }),
        },
      });
      setResult(response);
      pushToast({
        msg:
          mode === "emergency_suspend"
            ? `已紧急停用 ${user.name}`
            : `已完成 ${user.name} 的离职交接`,
        kind: "success",
      });
    } catch (error) {
      const status = getErrorStatus(error);
      if (status === 409) {
        setSelections({});
        setStale(true);
        setFormError(
          getErrorCode(error) === "offboarding_preview_stale"
            ? "项目或接收人状态已变化，旧预览不能继续提交。请刷新后重新选择。"
            : "提交时发现项目或接收人状态已变化，请刷新预览并重新确认。",
        );
        void refreshPreview();
        return;
      }
      if (status === 403) {
        setFormError("没有权限执行此操作，或该用户已超出你的项目管理范围。请刷新后重试。");
      } else if (status !== undefined && status >= 500) {
        setFormError("服务器暂时不可用，离职操作未确认。请稍后重试。");
      } else {
        setFormError(getErrorMessage(error));
      }
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!offboard.isPending) onClose();
      }}
      title={user ? `${pendingHandoff ? "继续交接" : "离职处理"} · ${user.name}` : "离职处理"}
      width={780}
    >
      {!user ? null : result ? (
        <CommitResult result={result} onClose={onClose} />
      ) : previewPaused && !preview ? (
        <div className="flex flex-col items-center gap-2 rounded-lg border border-status-caution/30 bg-status-caution-soft px-4 py-10 text-center text-sm">
          <Icon name="monitor" size={22} className="text-status-caution" />
          <div className="font-medium">暂时离线，等待网络恢复</div>
          <div className="max-w-md text-xs text-muted-foreground">
            离职预览会在网络恢复后自动继续加载，请保持此窗口打开。
          </div>
        </div>
      ) : previewQuery.isLoading ? (
        <div className="flex flex-col items-center gap-2 py-12 text-center text-sm text-muted-foreground">
          <Icon name="loader2" size={22} className="animate-spin text-brand" />
          <span>正在读取项目责任、任务锁和 API Key…</span>
        </div>
      ) : previewQuery.isError ? (
        <ErrorPanel error={previewQuery.error} onRetry={refreshPreview} />
      ) : !preview ? (
        <ErrorPanel error={new Error("没有收到离职预览")} onRetry={refreshPreview} />
      ) : (
        <div className="flex flex-col gap-3.5">
          <UserSummary user={preview.user ?? user} />

          {previewPaused && (
            <div className="flex items-start gap-2 rounded-md border border-status-caution/30 bg-status-caution-soft px-3 py-2.5 text-xs text-muted-foreground">
              <Icon name="monitor" size={14} className="mt-0.5 shrink-0 text-status-caution" />
              <span>当前离线，以下为上次加载的离职预览；网络恢复后会自动继续更新。</span>
            </div>
          )}

          {pendingHandoff && (
            <div className="flex items-start gap-2 rounded-md border border-status-caution/30 bg-status-caution-soft px-3 py-2.5 text-xs text-muted-foreground">
              <Icon name="arrowRight" size={14} className="mt-0.5 shrink-0 text-status-caution" />
              <span>
                该账号曾执行紧急停用。选择正常离职交接可继续处理当前预览中的责任，已转交工作和已吊销密钥不会回滚。
              </span>
            </div>
          )}

          {historical && (
            <div className="flex items-start gap-2 rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-2.5 text-xs text-status-danger">
              <Icon name="shieldAlert" size={14} className="mt-0.5 shrink-0" />
              <span>该账号属于已删除或历史未知状态，不能再次交接或恢复。</span>
            </div>
          )}

          {preview.blockers.length > 0 && (
            <div className="rounded-lg border border-status-caution/30 bg-status-caution-soft px-3.5 py-3 text-xs">
              <div className="mb-1 font-semibold text-status-caution">预览阻塞项</div>
              <div className="flex flex-col gap-1 text-muted-foreground">
                {preview.blockers.map((blocker) => (
                  <div key={`${blocker.code}-${blocker.message}`} className="flex gap-1.5">
                    <Icon
                      name="warning"
                      size={12}
                      className="mt-0.5 shrink-0 text-status-caution"
                    />
                    <span>{blocker.message}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="grid gap-2 sm:grid-cols-2">
            <button
              type="button"
              className={`rounded-lg border px-3.5 py-3 text-left transition-colors ${mode === "handoff" ? "border-brand bg-status-info-alt-soft" : "border-border bg-card hover:bg-muted"}`}
              onClick={() => {
                setMode("handoff");
                setFormError(null);
              }}
              disabled={historical || offboard.isPending || previewQuery.isFetching}
            >
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Icon name="arrowRight" size={14} className="text-brand" /> 正常离职交接
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                按项目分别选择接收人，完成责任转移后停用账号。
              </div>
            </button>
            <button
              type="button"
              className={`rounded-lg border px-3.5 py-3 text-left transition-colors ${mode === "emergency_suspend" ? "border-status-caution bg-status-caution-soft" : "border-border bg-card hover:bg-muted"}`}
              onClick={() => {
                setMode("emergency_suspend");
                setFormError(null);
              }}
              disabled={historical || offboard.isPending || previewQuery.isFetching}
            >
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Icon name="shieldAlert" size={14} className="text-status-caution" /> 紧急停用
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                立即阻断登录并吊销 API Key，未交接责任保留到后续处理。
              </div>
            </button>
          </div>

          {preview.projects.length === 0 ? (
            <div className="rounded-lg border border-border bg-muted px-3.5 py-4 text-sm text-muted-foreground">
              该用户当前没有项目责任，但停用仍会吊销其 API Key。
            </div>
          ) : (
            <div className="flex flex-col gap-2.5">
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-semibold">项目责任与接收人</div>
                <span className="text-xs text-muted-foreground">
                  预览于 {formatDateTime(preview.generated_at)}
                </span>
              </div>
              {preview.projects.map((project) => (
                <ProjectCard
                  key={project.project_id}
                  project={project}
                  selections={selections[project.project_id] ?? {}}
                  onSelect={(role, receiverId) =>
                    selectReceiver(project.project_id, role, receiverId)
                  }
                  disabled={
                    mode === "emergency_suspend" ||
                    stale ||
                    historical ||
                    offboard.isPending ||
                    previewQuery.isFetching
                  }
                />
              ))}
            </div>
          )}

          <ApiKeysSummary preview={preview} />

          <label className="block text-sm">
            <span className="mb-1.5 block text-xs font-medium text-muted-foreground">
              处理原因（可选）
            </span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value.slice(0, 500))}
              maxLength={500}
              rows={3}
              placeholder={
                mode === "emergency_suspend"
                  ? "例如：账号疑似泄露，先行阻断登录"
                  : "例如：成员离职，已完成项目责任交接"
              }
              disabled={historical || offboard.isPending}
              className="w-full resize-y rounded-md border border-border bg-card px-2.5 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
            />
            <span className="mt-1 block text-right text-2xs text-muted-foreground">
              {reason.length}/500
            </span>
          </label>

          {stale && (
            <div className="flex items-center justify-between gap-2 rounded-md border border-status-caution/30 bg-status-caution-soft px-3 py-2 text-xs text-status-caution">
              <span className="flex items-center gap-1.5">
                <Icon name="refresh" size={12} /> 预览已刷新要求，旧接收人选择已清空。
              </span>
              <Button
                size="xs"
                onClick={() => void refreshPreview()}
                disabled={previewQuery.isFetching}
              >
                刷新预览
              </Button>
            </div>
          )}

          {formError && (
            <div className="flex items-start gap-2 rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-2 text-xs text-status-danger">
              <Icon name="warning" size={13} className="mt-0.5 shrink-0" />
              <span>
                <CopyableError message={formError} />
              </span>
            </div>
          )}

          <div className="flex items-center justify-between gap-3 border-t border-border pt-3">
            <div className="text-2xs text-muted-foreground">
              {mode === "emergency_suspend"
                ? "紧急停用不要求接收人；未交接任务会保留在结果中。"
                : missingReceivers.length > 0
                  ? `还有 ${missingReceivers.length} 项责任未选择接收人。`
                  : "提交后账号立即停用，历史记录仍会保留。"}
            </div>
            <div className="flex shrink-0 gap-2">
              <Button onClick={onClose} disabled={offboard.isPending}>
                取消
              </Button>
              <Button
                variant={mode === "emergency_suspend" ? "danger" : "primary"}
                onClick={() => void submit()}
                disabled={mode === "emergency_suspend" ? emergencyDisabled : handoffDisabled}
              >
                {offboard.isPending
                  ? "提交中…"
                  : mode === "emergency_suspend"
                    ? "确认紧急停用"
                    : "确认交接并停用"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}

export interface ReactivateDialogProps {
  open: boolean;
  user: UserResponse | null;
  onClose: () => void;
}

export function ReactivateDialog({ open, user, onClose }: ReactivateDialogProps) {
  const reactivate = useReactivateUser();
  const pushToast = useToastStore((state) => state.push);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const canReactivate = !!user && REACTIVATABLE_KINDS.has(user.disabled_kind ?? "");

  useEffect(() => {
    if (!open) return;
    setReason("");
    setError(null);
    reactivate.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user?.id]);

  const submit = async () => {
    if (!user || !canReactivate || reactivate.isPending) return;
    setError(null);
    try {
      await reactivate.mutateAsync({ userId: user.id, reason: reason.trim() || undefined });
      pushToast({
        msg: `已恢复账号 ${user.name}`,
        sub: "转交的工作和已吊销的 API Key 不会自动恢复。",
        kind: "success",
      });
      onClose();
    } catch (cause) {
      const status = getErrorStatus(cause);
      if (status === 403) setError("没有权限恢复该账号。");
      else if (status !== undefined && status >= 500)
        setError("服务器暂时不可用，恢复未确认，请稍后重试。");
      else setError(getErrorMessage(cause));
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!reactivate.isPending) onClose();
      }}
      title="恢复账号"
      width={500}
    >
      {!user ? null : (
        <div className="flex flex-col gap-3.5 text-sm">
          <UserSummary user={user} />
          {!canReactivate ? (
            <div className="rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-2.5 text-xs text-status-danger">
              只有“停用（可恢复）”或“紧急停用”的账号可以恢复；已删除或历史未知状态不能恢复。
            </div>
          ) : (
            <>
              <div className="rounded-md border border-status-caution/30 bg-status-caution-soft px-3 py-2.5 text-xs text-muted-foreground">
                恢复只允许该账号重新登录。已转交的项目、批次和任务不会转回，停用时吊销的 API Key
                也不会恢复，请按需重新创建。
              </div>
              <label className="block text-sm">
                <span className="mb-1.5 block text-xs font-medium text-muted-foreground">
                  恢复原因（可选）
                </span>
                <textarea
                  value={reason}
                  onChange={(event) => setReason(event.target.value.slice(0, 500))}
                  maxLength={500}
                  rows={3}
                  placeholder="例如：确认成员重新加入团队"
                  disabled={reactivate.isPending}
                  className="w-full resize-y rounded-md border border-border bg-card px-2.5 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
                />
              </label>
            </>
          )}
          {error && (
            <div className="flex items-start gap-2 rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-2 text-xs text-status-danger">
              <Icon name="warning" size={13} className="mt-0.5 shrink-0" />
              <span>
                <CopyableError message={error} />
              </span>
            </div>
          )}
          <div className="flex justify-end gap-2 border-t border-border pt-3">
            <Button onClick={onClose} disabled={reactivate.isPending}>
              取消
            </Button>
            <Button
              variant="primary"
              onClick={() => void submit()}
              disabled={!canReactivate || reactivate.isPending}
            >
              {reactivate.isPending ? "恢复中…" : "确认恢复账号"}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
