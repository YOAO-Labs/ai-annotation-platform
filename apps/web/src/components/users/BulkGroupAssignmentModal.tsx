import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useBulkUserGroup, usePreviewBulkUserGroup } from "@/hooks/useUsers";
import type {
  BulkGroupAssignmentPreview,
  BulkGroupAssignmentResponse,
  BulkGroupAssignmentResultItem,
  UserResponse,
} from "@/api/users";
import type { GroupResponse } from "@/api/groups";

interface Props {
  open: boolean;
  users: UserResponse[];
  groups: GroupResponse[];
  onClose: () => void;
}

const INPUT_CLASS =
  "box-border w-full rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function BulkGroupAssignmentModal({ open, users, groups, onClose }: Props) {
  const [groupId, setGroupId] = useState<string>("");
  const [preview, setPreview] = useState<BulkGroupAssignmentPreview | null>(null);
  const [result, setResult] = useState<BulkGroupAssignmentResponse | null>(null);
  const previewMut = usePreviewBulkUserGroup();
  const assignMut = useBulkUserGroup();
  const pushToast = useToastStore((state) => state.push);
  const targetGroup = groups.find((group) => group.id === groupId);
  const userIds = useMemo(() => users.map((user) => user.id), [users]);

  const reset = () => {
    setGroupId("");
    setPreview(null);
    setResult(null);
    previewMut.reset();
    assignMut.reset();
  };

  const close = () => {
    if (previewMut.isPending || assignMut.isPending) return;
    reset();
    onClose();
  };

  const requestPreview = () => {
    if (userIds.length === 0 || userIds.length > 500 || previewMut.isPending) return;
    previewMut.mutate(
      { user_ids: userIds, group_id: groupId || null },
      {
        onSuccess: setPreview,
        onError: (error) =>
          pushToast({
            msg: "无法生成分组预览",
            sub: error instanceof Error ? error.message : String(error),
            kind: "error",
          }),
      },
    );
  };

  const apply = () => {
    if (!preview || assignMut.isPending) return;
    assignMut.mutate(
      {
        user_ids: preview.items.filter((item) => item.ok).map((item) => item.user_id),
        group_id: preview.group_id,
      },
      {
        onSuccess: (response) => {
          const blocked = preview.items
            .filter((item) => !item.ok)
            .map((item) => ({
              user_id: item.user_id,
              ok: false,
              retryable: false,
              error: item.error,
            }));
          setResult({
            ...response,
            items: [...response.items, ...blocked],
            failed: response.failed + blocked.length,
          });
          pushToast({
            msg: `分组完成：成功 ${response.succeeded} 条，失败 ${response.failed} 条`,
            kind: response.failed ? "warning" : "success",
          });
        },
        onError: (error) =>
          pushToast({
            msg: "批量分组失败",
            sub: error instanceof Error ? error.message : String(error),
            kind: "error",
          }),
      },
    );
  };

  const retryFailed = () => {
    if (!result) return;
    const failedIds = result.items
      .filter((item) => !item.ok && item.retryable)
      .map((item) => item.user_id);
    if (failedIds.length === 0) return;
    assignMut.mutate(
      { user_ids: failedIds, group_id: preview?.group_id ?? null },
      {
        onSuccess: (response) =>
          setResult((previous) => {
            if (!previous) return response;
            const updates = new Map(response.items.map((item) => [item.user_id, item]));
            const items = previous.items.map((item) => updates.get(item.user_id) ?? item);
            return {
              items,
              succeeded: items.filter((item) => item.ok).length,
              failed: items.filter((item) => !item.ok).length,
            };
          }),
        onError: (error) =>
          pushToast({
            msg: "重试分组失败",
            sub: error instanceof Error ? error.message : String(error),
            kind: "error",
          }),
      },
    );
  };

  return (
    <Modal open={open} onClose={close} title="批量分配数据组" width={680}>
      <div className="flex flex-col gap-3.5 text-sm">
        <p className="m-0 text-muted-foreground">
          先预览每位成员的当前与目标数据组，再执行变更。预览中被阻止的成员不会写入；失败项可以单独重试。
        </p>
        {!preview && !result && (
          <>
            <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              已选 {users.length} 名成员，单次最多 500 名
            </div>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-muted-foreground">目标数据组</span>
              <select
                disabled={previewMut.isPending}
                value={groupId}
                onChange={(event) => setGroupId(event.target.value)}
                className={INPUT_CLASS}
              >
                <option value="">— 清除数据组 —</option>
                {groups.map((group) => (
                  <option key={group.id} value={group.id}>
                    {group.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex justify-end gap-2">
              <Button onClick={close}>取消</Button>
              <Button
                variant="primary"
                onClick={requestPreview}
                disabled={userIds.length === 0 || userIds.length > 500 || previewMut.isPending}
              >
                {previewMut.isPending ? "预览中…" : "预览变更"}
              </Button>
            </div>
          </>
        )}
        {preview && !result && (
          <PreviewBody preview={preview} targetGroupName={targetGroup?.name ?? "未分配"} />
        )}
        {preview && !result && (
          <div className="flex justify-end gap-2">
            <Button disabled={assignMut.isPending} onClick={() => setPreview(null)}>
              返回修改
            </Button>
            <Button
              variant="primary"
              onClick={apply}
              disabled={assignMut.isPending || preview.applicable === 0}
            >
              {assignMut.isPending ? "保存中…" : `确认变更 ${preview.applicable} 人`}
            </Button>
          </div>
        )}
        {result && (
          <ResultBody
            result={result}
            onRetry={retryFailed}
            pending={assignMut.isPending}
            onClose={close}
          />
        )}
      </div>
    </Modal>
  );
}

function PreviewBody({
  preview,
  targetGroupName,
}: {
  preview: BulkGroupAssignmentPreview;
  targetGroupName: string;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge variant="success">可变更 {preview.applicable}</Badge>
        <Badge variant={preview.blocked ? "warning" : "outline"}>已阻止 {preview.blocked}</Badge>
        <span className="text-muted-foreground">目标：{targetGroupName}</span>
      </div>
      <div className="max-h-72 overflow-y-auto rounded-md border border-border">
        {preview.items.map((item) => (
          <div
            key={item.user_id}
            className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs last:border-0"
          >
            <Badge variant={item.ok ? "success" : "danger"}>{item.ok ? "可变更" : "阻止"}</Badge>
            <div className="min-w-0 flex-1">
              <div className="truncate">{item.name || item.email || item.user_id}</div>
              <div className="text-muted-foreground">
                {item.current_group_name || "未分配"} → {item.next_group_name || targetGroupName}
              </div>
            </div>
            {item.error && (
              <span className="text-status-danger">
                <CopyableError message={item.error} />
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function ResultBody({
  result,
  onRetry,
  pending,
  onClose,
}: {
  result: BulkGroupAssignmentResponse;
  onRetry: () => void;
  pending: boolean;
  onClose: () => void;
}) {
  const canRetry = result.items.some(
    (item: BulkGroupAssignmentResultItem) => !item.ok && item.retryable,
  );
  return (
    <div className="flex flex-col gap-3">
      <div className="text-xs text-muted-foreground">
        成功 {result.succeeded} 条 · 失败 {result.failed} 条
      </div>
      <div className="max-h-64 overflow-y-auto rounded-md border border-border">
        {result.items.map((item) => (
          <div
            key={item.user_id}
            className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs last:border-0"
          >
            <Badge variant={item.ok ? "success" : "danger"}>{item.ok ? "成功" : "失败"}</Badge>
            <span className="mono flex-1 truncate">{item.user_id}</span>
            {item.error && (
              <span className="text-status-danger">
                <CopyableError message={item.error} />
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="flex justify-end gap-2">
        {canRetry && (
          <Button onClick={onRetry} disabled={pending}>
            {pending ? "重试中…" : "仅重试失败项"}
          </Button>
        )}
        <Button variant="primary" onClick={onClose}>
          完成
        </Button>
      </div>
    </div>
  );
}
