import { useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { AssigneeAvatarStack } from "@/components/ui/AssigneeAvatarStack";
import { useToastStore } from "@/components/ui/Toast";
import type { BatchResponse } from "@/api/batches";
import { cn } from "@/lib/utils";

const COLUMNS: {
  id: string;
  label: string;
  variant: "default" | "accent" | "warning" | "success" | "danger" | "ai";
}[] = [
  { id: "draft", label: "草稿", variant: "default" },
  { id: "active", label: "激活", variant: "accent" },
  { id: "pre_annotated", label: "AI 预标已就绪", variant: "ai" },
  { id: "annotating", label: "标注中", variant: "accent" },
  { id: "reviewing", label: "审核中", variant: "warning" },
  { id: "approved", label: "已通过", variant: "success" },
  { id: "rejected", label: "已退回", variant: "danger" },
  { id: "archived", label: "已归档", variant: "default" },
];

const VALID_TRANSITIONS: Record<string, Set<string>> = {
  draft: new Set(["active"]),
  active: new Set(["annotating", "pre_annotated", "archived"]),
  pre_annotated: new Set(["annotating", "active", "archived"]),
  annotating: new Set(["reviewing", "archived"]),
  reviewing: new Set(["approved", "rejected"]),
  approved: new Set(["archived", "reviewing"]),
  rejected: new Set(["active", "archived", "reviewing"]),
  archived: new Set(["active"]),
};

interface Props {
  batches: BatchResponse[];
  isOwner: boolean;
  onTransition: (batch: BatchResponse, target: string) => void;
}

export function BatchesKanbanView({ batches, isOwner, onTransition }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [hoverColumn, setHoverColumn] = useState<string | null>(null);

  const grouped = COLUMNS.reduce<Record<string, BatchResponse[]>>((acc, col) => {
    acc[col.id] = batches.filter((b) => b.status === col.id);
    return acc;
  }, {});

  const draggingBatch = draggingId ? (batches.find((b) => b.id === draggingId) ?? null) : null;

  const handleDrop = (targetStatus: string) => {
    if (!draggingBatch) return;
    if (draggingBatch.status === targetStatus) {
      setDraggingId(null);
      setHoverColumn(null);
      return;
    }
    const allowed = VALID_TRANSITIONS[draggingBatch.status];
    if (!allowed?.has(targetStatus)) {
      pushToast({
        msg: "不合法的状态迁移",
        sub: `不能从「${COLUMNS.find((col) => col.id === draggingBatch.status)?.label ?? draggingBatch.status}」转到「${COLUMNS.find((col) => col.id === targetStatus)?.label ?? targetStatus}」`,
        kind: "warning",
      });
      setDraggingId(null);
      setHoverColumn(null);
      return;
    }
    onTransition(draggingBatch, targetStatus);
    setDraggingId(null);
    setHoverColumn(null);
  };

  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] items-start gap-2 p-2">
      {COLUMNS.map((col) => {
        const items = grouped[col.id] ?? [];
        const canDrop =
          isOwner &&
          draggingBatch !== null &&
          draggingBatch.status !== col.id &&
          VALID_TRANSITIONS[draggingBatch.status]?.has(col.id);
        return (
          <div
            key={col.id}
            onDragOver={(e) => {
              if (!isOwner || !draggingBatch) return;
              e.preventDefault();
              setHoverColumn(col.id);
            }}
            onDragLeave={() => {
              if (hoverColumn === col.id) setHoverColumn(null);
            }}
            onDrop={(e) => {
              e.preventDefault();
              handleDrop(col.id);
            }}
            className={cn(
              "flex max-h-[calc(100vh-280px)] min-w-0 flex-col gap-1.5 overflow-y-auto rounded-md border border-border bg-muted p-2",
              canDrop && "border-dashed border-brand",
              hoverColumn === col.id && canDrop && "bg-brand/10",
            )}
          >
            <div className="flex min-w-0 items-center justify-between gap-1.5 overflow-hidden border-b border-border px-1 pt-0.5 pb-1.5">
              <Badge variant={col.variant} dot>
                {col.label}
              </Badge>
              <span className="text-xs text-muted-foreground">{items.length}</span>
            </div>
            {items.length === 0 && (
              <div className="py-4 text-center text-xs text-muted-foreground">—</div>
            )}
            {items.map((b) => (
              <KanbanCard
                key={b.id}
                batch={b}
                isOwner={isOwner}
                isDragging={draggingId === b.id}
                onDragStart={() => setDraggingId(b.id)}
                onDragEnd={() => {
                  setDraggingId(null);
                  setHoverColumn(null);
                }}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function KanbanCard({
  batch,
  isOwner,
  isDragging,
  onDragStart,
  onDragEnd,
}: {
  batch: BatchResponse;
  isOwner: boolean;
  isDragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
}) {
  const stackUsers = [batch.annotator, batch.reviewer].filter(
    (u): u is NonNullable<typeof u> => u !== null,
  );
  return (
    <div
      draggable={isOwner}
      onDragStart={() => onDragStart()}
      onDragEnd={() => onDragEnd()}
      className={cn(
        "flex cursor-default flex-col gap-1.5 rounded-sm border border-border bg-card px-2.5 py-2",
        isOwner && "cursor-grab",
        isDragging && "opacity-50",
      )}
    >
      <div className="flex items-center justify-between gap-1.5">
        <span className="mono text-xs text-muted-foreground">{batch.display_id}</span>
        <AssigneeAvatarStack users={stackUsers} size="sm" max={2} />
      </div>
      <div
        className="overflow-hidden text-sm font-medium text-ellipsis whitespace-nowrap"
        title={batch.name}
      >
        {batch.name}
      </div>
      <ProgressBar value={batch.progress_pct ?? 0} />
      <div className="text-2xs text-muted-foreground">
        {batch.completed_tasks}/{batch.total_tasks} 个任务
      </div>
    </div>
  );
}
