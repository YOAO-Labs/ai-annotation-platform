import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useBulkInviteUsers, usePreviewBulkInviteUsers } from "@/hooks/useUsers";
import { useGroups } from "@/hooks/useGroups";
import { useProjects } from "@/hooks/useProjects";
import { usePermissions } from "@/hooks/usePermissions";
import { ROLE_LABELS, PROJECT_ROLE_LABELS } from "@/constants/roles";
import type { BulkInviteItemPayload, BulkInviteResponse } from "@/api/users";
import type { PlatformRole, ProjectRole } from "@/types";

interface Props {
  open: boolean;
  onClose: () => void;
}
const INPUT_CLASS =
  "box-border w-full rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";

const ALL_PROJECT_ROLES: ProjectRole[] = ["annotator", "reviewer", "viewer"];

export function BulkInviteModal({ open, onClose }: Props) {
  const { role: actorRole } = usePermissions();
  const { data: groups = [] } = useGroups(open);
  const { data: projects = [] } = useProjects();
  const [rawEmails, setRawEmails] = useState("");
  const [role, setRole] = useState<PlatformRole>("employee");
  const [projectRole, setProjectRole] = useState<ProjectRole>("annotator");
  const [groupName, setGroupName] = useState("");
  const [projectId, setProjectId] = useState("");
  const [preview, setPreview] = useState<BulkInviteResponse | null>(null);
  const [result, setResult] = useState<BulkInviteResponse | null>(null);
  const bulk = useBulkInviteUsers();
  const previewMutation = usePreviewBulkInviteUsers();
  const pushToast = useToastStore((state) => state.push);
  const busy = bulk.isPending || previewMutation.isPending;
  const roles: PlatformRole[] =
    actorRole === "super_admin" && !projectId
      ? ["super_admin", "project_admin", "employee", "viewer"]
      : ["employee", "viewer"];
  const projectRoleOptions: ProjectRole[] = role === "viewer" ? ["viewer"] : ALL_PROJECT_ROLES;
  const items = useMemo<BulkInviteItemPayload[]>(
    () =>
      [
        ...new Set(
          rawEmails
            .split(/[\s,;]+/)
            .map((email) => email.trim().toLowerCase())
            .filter(Boolean),
        ),
      ].map((email) => ({
        email,
        role,
        ...(projectId ? { project_id: projectId, project_member_role: projectRole } : {}),
        ...(groupName ? { group_name: groupName } : {}),
      })),
    [rawEmails, role, projectRole, groupName, projectId],
  );
  const close = () => {
    if (busy) return;
    setRawEmails("");
    setRole("employee");
    setProjectRole("annotator");
    setGroupName("");
    setProjectId("");
    setPreview(null);
    setResult(null);
    bulk.reset();
    previewMutation.reset();
    onClose();
  };
  const fail = (error: unknown) =>
    pushToast({
      msg: "批量邀请失败",
      sub: error instanceof Error ? error.message : String(error),
      kind: "error",
    });
  const requestPreview = () => {
    if (busy || items.length === 0 || items.length > 500) return;
    previewMutation.mutate(items, { onSuccess: setPreview, onError: fail });
  };
  const submit = (indices: number[]) => {
    if (busy || !preview || indices.length === 0) return;
    bulk.mutate(
      indices.map((index) => items[index]),
      {
        onSuccess: (response) => {
          setResult((previous) => {
            const updated = new Map(
              response.items.map((item) => [
                indices[item.index],
                { ...item, index: indices[item.index] },
              ]),
            );
            const merged = (previous?.items ?? preview.items).map(
              (item) => updated.get(item.index) ?? item,
            );
            return {
              items: merged,
              succeeded: merged.filter((item) => item.ok).length,
              failed: merged.filter((item) => !item.ok).length,
            };
          });
          pushToast({
            msg: `本次处理：成功 ${response.succeeded} 条，失败 ${response.failed} 条`,
            kind: response.failed ? "warning" : "success",
          });
        },
        onError: fail,
      },
    );
  };
  const rows = result ?? preview;
  return (
    <Modal open={open} onClose={close} title="批量邀请成员" width={680}>
      <div className="flex flex-col gap-3.5 text-sm">
        {!preview && (
          <>
            <p className="m-0 text-muted-foreground">
              每行输入一个邮箱，也支持空格、逗号或分号分隔。预览会校验权限、邮箱、项目及邀请配额。
            </p>
            <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
              <label className="flex flex-col gap-1">
                邮箱清单
                <textarea
                  value={rawEmails}
                  onChange={(event) => setRawEmails(event.target.value)}
                  rows={6}
                  className={INPUT_CLASS}
                  placeholder="alice@example.com"
                />
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="flex flex-col gap-1">
                  目标项目
                  <select
                    value={projectId}
                    onChange={(event) => {
                      setProjectId(event.target.value);
                      if (event.target.value && ["super_admin", "project_admin"].includes(role))
                        setRole("employee");
                    }}
                    className={INPUT_CLASS}
                  >
                    <option value="">不指定项目</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  默认账号角色
                  <select
                    value={role}
                    onChange={(event) => {
                      const nextRole = event.target.value as PlatformRole;
                      setRole(nextRole);
                      // 后端规则：平台观察者仅可持有观察者职责。切换账号角色时
                      // 同步归一项目职责，避免提交 stale 的组合被预览/后端拒绝。
                      if (nextRole === "viewer") setProjectRole("viewer");
                      else if (projectRole === "viewer") setProjectRole("annotator");
                    }}
                    className={INPUT_CLASS}
                  >
                    {roles.map((value) => (
                      <option key={value} value={value}>
                        {ROLE_LABELS[value]}
                      </option>
                    ))}
                  </select>
                </label>
                {projectId && (
                  <label className="flex flex-col gap-1">
                    项目职责
                    <select
                      value={projectRole}
                      onChange={(event) => setProjectRole(event.target.value as ProjectRole)}
                      className={INPUT_CLASS}
                    >
                      {projectRoleOptions.map((value) => (
                        <option key={value} value={value}>
                          {PROJECT_ROLE_LABELS[value]}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label className="flex flex-col gap-1">
                  数据组（可选）
                  <select
                    value={groupName}
                    onChange={(event) => setGroupName(event.target.value)}
                    className={INPUT_CLASS}
                  >
                    <option value="">不指定数据组</option>
                    {groups.map((group) => (
                      <option key={group.id} value={group.name}>
                        {group.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </fieldset>
            <div className="text-xs text-muted-foreground">
              待预览 {items.length} 条，重复地址已合并。单次最多 500 条。
            </div>
            <div className="flex justify-end gap-2">
              <Button onClick={close} disabled={busy}>
                取消
              </Button>
              <Button
                variant="primary"
                disabled={busy || items.length === 0 || items.length > 500}
                onClick={requestPreview}
              >
                {busy ? "预览中…" : "预览邀请"}
              </Button>
            </div>
          </>
        )}
        {rows && (
          <>
            <div className="rounded-md border border-border bg-muted/40 px-3 py-2">
              {result ? "成功" : "可邀请"} {rows.succeeded} 条 · {result ? "失败" : "已阻止"}{" "}
              {rows.failed} 条
            </div>
            {!result && (
              <p className="m-0 text-xs text-muted-foreground">
                确认后只创建预览中可邀请的条目。邮件可在生成链接后单独发送。
              </p>
            )}
            <div className="max-h-72 overflow-y-auto rounded-md border border-border">
              {rows.items.map((item) => (
                <div
                  key={item.index}
                  className="flex items-start gap-2 border-b border-border px-3 py-2 last:border-0"
                >
                  <Badge variant={item.ok ? "success" : "danger"}>
                    {item.ok ? (result ? "成功" : "可邀请") : result ? "失败" : "阻止"}
                  </Badge>
                  <div className="min-w-0 flex-1">
                    <div className="truncate">{item.email}</div>
                    {item.error && <div className="text-xs text-status-danger">{item.error}</div>}
                    {result && item.ok && item.invite_url && (
                      <input
                        aria-label={`邀请链接 ${item.email}`}
                        readOnly
                        value={item.invite_url}
                        onFocus={(event) => event.target.select()}
                        className={`${INPUT_CLASS} mt-1 text-xs`}
                      />
                    )}
                  </div>
                </div>
              ))}
            </div>
            <div className="flex justify-end gap-2">
              {!result && (
                <>
                  <Button disabled={busy} onClick={() => setPreview(null)}>
                    返回修改
                  </Button>
                  <Button
                    variant="primary"
                    disabled={busy || preview?.succeeded === 0}
                    onClick={() =>
                      submit(preview!.items.filter((item) => item.ok).map((item) => item.index))
                    }
                  >
                    {busy ? "处理中…" : `确认邀请 ${preview?.succeeded} 人`}
                  </Button>
                </>
              )}
              {result && (
                <>
                  {result.items.some((item) => !item.ok && item.retryable) && (
                    <Button
                      disabled={busy}
                      onClick={() =>
                        submit(
                          result.items
                            .filter((item) => !item.ok && item.retryable)
                            .map((item) => item.index),
                        )
                      }
                    >
                      {busy ? "重试中…" : "仅重试失败项"}
                    </Button>
                  )}
                  <Button variant="primary" disabled={busy} onClick={close}>
                    完成
                  </Button>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
