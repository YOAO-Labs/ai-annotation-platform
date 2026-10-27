import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPageState } from "@/hooks/useListPageState";
import { useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Badge } from "@/components/ui/Badge";
import { useBatchAuditLogs } from "@/hooks/useBatches";
import type { BatchResponse, BatchAuditLogEntry } from "@/api/batches";

const ACTION_LABEL: Record<string, string> = {
  "batch.created": "创建",
  "batch.status_changed": "状态变更",
  "batch.rejected": "驳回",
  "batch.deleted": "删除",
  "batch.distribute_even": "项目级分派",
  "batch.bulk_archive": "批量归档",
  "batch.bulk_delete": "批量删除",
  "batch.bulk_reassign": "批量改派",
  "batch.bulk_activate": "批量激活",
};

const ROLE_VARIANT: Record<string, "default" | "accent" | "warning" | "success" | "danger"> = {
  super_admin: "danger",
  project_admin: "accent",
  reviewer: "warning",
  annotator: "default",
  viewer: "default",
};

const ROLE_LABEL: Record<string, string> = {
  super_admin: "超管",
  project_admin: "项目管理",
  reviewer: "质检员",
  annotator: "标注员",
  viewer: "只读",
};

export function BatchAuditLogDrawer({
  projectId,
  batch,
  onClose,
}: {
  projectId: string;
  batch: BatchResponse;
  onClose: () => void;
}) {
  const pagination = useListPageState(`${projectId}:${batch.id}`);
  const {
    data: logs = [],
    isLoading,
    isFetching,
    isError,
  } = useBatchAuditLogs(projectId, batch.id, true, {
    limit: pagination.pageSize + 1,
    offset: pagination.page * pagination.pageSize,
  });
  const items = logs.slice(0, pagination.pageSize);

  return (
    <Modal
      open
      onClose={onClose}
      title={`操作历史 · ${batch.display_id} ${batch.name}`}
      width={680}
    >
      <div className={listStyles.surface}>
        {isError && (
          <div role="alert" className={listStyles.empty}>
            操作记录加载失败，请重新打开后重试。
          </div>
        )}
        {isLoading && <div className="p-6 text-center text-sm text-muted-foreground">加载中…</div>}
        {!isLoading && logs.length === 0 && (
          <div className="p-6 text-center text-sm text-muted-foreground">暂无操作记录</div>
        )}
        {!isLoading && logs.length > 0 && (
          <div className="max-h-[60vh] overflow-y-auto pr-1">
            {items.map((log) => (
              <Entry key={log.id} log={log} />
            ))}
          </div>
        )}
        <ListPagination
          label="操作记录"
          page={pagination.page}
          pageSize={pagination.pageSize}
          count={items.length}
          hasNext={logs.length > pagination.pageSize}
          loading={isFetching}
          onPageChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </div>
    </Modal>
  );
}

function Entry({ log }: { log: BatchAuditLogEntry }) {
  const [open, setOpen] = useState(false);
  const time = log.created_at ? new Date(log.created_at).toLocaleString() : "—";
  const role = log.actor_role ?? "";
  const actionLabel = ACTION_LABEL[log.action] ?? log.action;

  // 状态变更：detail.before / detail.after / detail.reverse / detail.reason
  const detail = log.detail ?? {};
  const before = (detail as { before?: string }).before;
  const after = (detail as { after?: string }).after;
  const reverse = (detail as { reverse?: boolean }).reverse;
  const reason = (detail as { reason?: string }).reason;

  return (
    <div className={`${listStyles.row} text-sm`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono text-xs text-muted-foreground">{time}</span>
        {role && (
          <Badge variant={ROLE_VARIANT[role] ?? "default"} dot>
            {ROLE_LABEL[role] ?? role}
          </Badge>
        )}
        <span className="text-muted-foreground">{log.actor_email ?? "—"}</span>
        <span className="font-medium">{actionLabel}</span>
        {log.action === "batch.status_changed" && before && after && (
          <span className="text-muted-foreground">
            {before} → <strong className="text-foreground">{after}</strong>
            {reverse && (
              <span className="ml-1.5 rounded-full bg-status-caution-soft px-1.5 py-px text-2xs text-status-caution">
                逆向
              </span>
            )}
          </span>
        )}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="ml-auto cursor-pointer appearance-none border-0 bg-transparent text-xs text-brand [font:inherit]"
        >
          {open ? "收起" : "详情"}
        </button>
      </div>
      {reason && (
        <div className="mt-1 border-l-2 border-amber-500 bg-amber-500/[0.08] px-2 py-1 text-xs text-muted-foreground">
          原因：{reason}
        </div>
      )}
      {open && (
        <pre className="mono mt-1.5 overflow-x-auto rounded-sm bg-muted p-2 text-xs text-muted-foreground">
          {JSON.stringify(detail, null, 2)}
        </pre>
      )}
    </div>
  );
}
