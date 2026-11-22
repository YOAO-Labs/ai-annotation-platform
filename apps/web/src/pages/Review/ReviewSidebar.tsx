import { useMemo, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import type { ReviewingBatchItem } from "@/api/dashboard";

interface Props {
  batches: ReviewingBatchItem[];
  selectedBatchId: string;
  onSelect: (b: ReviewingBatchItem | null) => void;
}

interface Group {
  project_id: string;
  project_name: string;
  items: ReviewingBatchItem[];
  pending: number;
}

export function ReviewSidebar({ batches, selectedBatchId, onSelect }: Props) {
  const groups = useMemo<Group[]>(() => {
    const m = new Map<string, Group>();
    for (const b of batches) {
      const g = m.get(b.project_id) ?? {
        project_id: b.project_id,
        project_name: b.project_name,
        items: [],
        pending: 0,
      };
      g.items.push(b);
      g.pending += b.review_tasks;
      m.set(b.project_id, g);
    }
    const arr = [...m.values()];
    arr.sort((a, b) => b.pending - a.pending || a.project_name.localeCompare(b.project_name));
    for (const g of arr) {
      g.items.sort(
        (a, b) =>
          b.review_tasks - a.review_tasks || a.batch_display_id.localeCompare(b.batch_display_id),
      );
    }
    return arr;
  }, [batches]);

  const selectedProjectId = useMemo(
    () => batches.find((b) => b.batch_id === selectedBatchId)?.project_id ?? null,
    [batches, selectedBatchId],
  );

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggle = (pid: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });

  if (batches.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-xs text-muted-foreground">
        <Icon name="check" size={32} className="mb-2 opacity-25" />
        <div>暂无可审核批次</div>
      </div>
    );
  }

  return (
    <div className="px-1 py-2">
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={`mb-1.5 flex w-full cursor-pointer appearance-none items-center gap-1.5 rounded-md border border-border px-3 py-2 text-left text-sm text-foreground [font:inherit] ${
          selectedBatchId === "" ? "bg-brand/10" : "bg-transparent"
        }`}
      >
        <Icon name="layers" size={12} />
        <span>全部待审任务</span>
      </button>

      {groups.map((g) => {
        const isCollapsed = collapsed.has(g.project_id) && g.project_id !== selectedProjectId;
        return (
          <div key={g.project_id} className="mb-1">
            <button
              type="button"
              onClick={() => toggle(g.project_id)}
              className="flex w-full cursor-pointer appearance-none items-center gap-1.5 border-0 bg-transparent px-2.5 py-1.5 text-left text-xs font-semibold text-muted-foreground [font:inherit]"
            >
              <Icon name={isCollapsed ? "chevRight" : "chevDown"} size={11} />
              <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap">
                {g.project_name}
              </span>
              {g.pending > 0 && <Badge variant="warning">{g.pending}</Badge>}
            </button>

            {!isCollapsed && (
              <div className="ml-3 border-l border-border">
                {g.items.map((b) => {
                  const active = b.batch_id === selectedBatchId;
                  const remaining = Math.max(0, b.total_tasks - b.completed_tasks - b.review_tasks);
                  return (
                    <button
                      key={b.batch_id}
                      type="button"
                      onClick={() => onSelect(b)}
                      className={`my-0.5 w-full cursor-pointer appearance-none rounded-sm border-0 px-2.5 py-2 text-left text-sm text-foreground [font:inherit] ${
                        active ? "bg-brand/10" : "bg-transparent"
                      }`}
                    >
                      <div className="flex items-center gap-1.5">
                        <span className="mono text-xs font-semibold text-brand">
                          {b.batch_display_id}
                        </span>
                        <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">
                          {b.batch_name}
                        </span>
                        {b.review_tasks > 0 && <Badge variant="warning">{b.review_tasks}</Badge>}
                      </div>
                      <div className="mt-0.5 text-2xs text-muted-foreground">
                        共 {b.total_tasks} 任务 · 完成 {b.completed_tasks}
                        {remaining > 0 && ` · 未交 ${remaining}`}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
