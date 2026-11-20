import { useMemo, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import type { MyBatchItem } from "@/api/dashboard";
import styles from "./AnnotateSidebar.module.css";

interface Props {
  batches: MyBatchItem[];
  selectedBatchId: string;
  onSelect: (b: MyBatchItem | null) => void;
}

interface Group {
  project_id: string;
  project_name: string;
  items: MyBatchItem[];
  remaining: number;
}

const STATUS_COLOR: Record<string, "accent" | "warning" | "danger" | "outline"> = {
  active: "outline",
  annotating: "accent",
  reviewing: "warning",
  rejected: "danger",
};

/** 标注工作台左侧栏：项目→批次的两级树（与 ReviewSidebar 对位）。
 *  仅展示我手里的批次（active / annotating / rejected / reviewing）。 */
export function AnnotateSidebar({ batches, selectedBatchId, onSelect }: Props) {
  const groups = useMemo<Group[]>(() => {
    const m = new Map<string, Group>();
    for (const b of batches) {
      const remaining = Math.max(0, b.total_tasks - b.completed_tasks);
      const g = m.get(b.project_id) ?? {
        project_id: b.project_id,
        project_name: b.project_name,
        items: [],
        remaining: 0,
      };
      g.items.push(b);
      g.remaining += remaining;
      m.set(b.project_id, g);
    }
    const arr = [...m.values()];
    arr.sort((a, b) => b.remaining - a.remaining || a.project_name.localeCompare(b.project_name));
    const STATUS_ORDER: Record<string, number> = {
      annotating: 0,
      rejected: 1,
      active: 2,
      reviewing: 3,
    };
    for (const g of arr) {
      g.items.sort((a, b) => {
        const ra = STATUS_ORDER[a.status] ?? 9;
        const rb = STATUS_ORDER[b.status] ?? 9;
        if (ra !== rb) return ra - rb;
        return a.batch_display_id.localeCompare(b.batch_display_id);
      });
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
      <div className={styles.emptyState}>
        <Icon name="inbox" size={32} className={styles.emptyIcon} />
        <div>暂无分派批次</div>
        <div className={styles.emptyHint}>请联系项目管理员将你加入批次</div>
      </div>
    );
  }

  return (
    <div className={styles.root}>
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={`${styles.allButton} ${selectedBatchId === "" ? styles.allButtonActive : ""}`}
      >
        <Icon name="layers" size={12} />
        <span>全部待标任务</span>
      </button>

      {groups.map((g) => {
        const isCollapsed = collapsed.has(g.project_id) && g.project_id !== selectedProjectId;
        return (
          <div key={g.project_id} className={styles.projectGroup}>
            <button
              type="button"
              onClick={() => toggle(g.project_id)}
              className={styles.projectButton}
            >
              <Icon name={isCollapsed ? "chevRight" : "chevDown"} size={11} />
              <span className={styles.ellipsis}>{g.project_name}</span>
              {g.remaining > 0 && (
                <span className={styles.countBadge}>
                  <Badge variant="accent">{g.remaining}</Badge>
                </span>
              )}
            </button>

            {!isCollapsed && (
              <div className={styles.batchList}>
                {g.items.map((b) => {
                  const active = b.batch_id === selectedBatchId;
                  const remaining = Math.max(0, b.total_tasks - b.completed_tasks);
                  const variant = STATUS_COLOR[b.status] ?? "outline";
                  return (
                    <button
                      key={b.batch_id}
                      type="button"
                      onClick={() => onSelect(b)}
                      className={`${styles.batchButton} ${active ? styles.batchButtonActive : ""}`}
                    >
                      <div className={styles.batchTitleRow}>
                        <span className={`mono ${styles.batchId}`}>{b.batch_display_id}</span>
                        <span className={styles.batchName}>{b.batch_name}</span>
                        <span className={styles.statusBadge}>
                          <Badge variant={variant}>
                            {b.status === "annotating"
                              ? "进行"
                              : b.status === "rejected"
                                ? "驳回"
                                : b.status === "reviewing"
                                  ? "送审"
                                  : "未启"}
                          </Badge>
                        </span>
                      </div>
                      <div className={styles.batchMeta}>
                        共 {b.total_tasks} · 完成 {b.completed_tasks}
                        {remaining > 0 && ` · 待标 ${remaining}`}
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
