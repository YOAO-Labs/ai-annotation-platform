import { listStyles } from "@/components/ui/listStyles";
import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { useMemo } from "react";
import type { CSSProperties } from "react";
import { Badge } from "@/components/ui/Badge";
import { Thumbnail } from "@/components/Thumbnail";
import { useElementStyle } from "@/components/ui/useElementStyle";
import type { MyBatchItem } from "@/api/dashboard";
import styles from "./BatchCardGrid.module.css";

interface Props {
  batches: MyBatchItem[];
  onSelect: (b: MyBatchItem) => void;
}

interface Group {
  project_id: string;
  project_name: string;
  items: MyBatchItem[];
  remaining: number;
}

const STATUS: Record<
  string,
  { label: string; variant: "accent" | "warning" | "danger" | "outline" }
> = {
  active: { label: "未开始", variant: "outline" },
  annotating: { label: "标注中", variant: "accent" },
  reviewing: { label: "审核中", variant: "warning" },
  rejected: { label: "已驳回", variant: "danger" },
};

const STATUS_ORDER: Record<string, number> = {
  annotating: 0,
  rejected: 1,
  active: 2,
  reviewing: 3,
};

/** 单根进度条；与工作台进度卡的三档配色对齐（标注中=brand / 送审=caution / 通过=positive）。 */
function ProgressRow({
  label,
  count,
  total,
  pct,
  color,
}: {
  label: string;
  count: number;
  total: number;
  pct: number;
  color: string;
}) {
  const ref = useElementStyle<HTMLDivElement>({
    "--progress-pct": `${Math.min(100, pct)}%`,
    "--progress-color": color,
  } as CSSProperties);
  return (
    <div className={styles.progressRow}>
      <span className={styles.progressLabel}>{label}</span>
      <div className={styles.track}>
        <div ref={ref} className={styles.fill} />
      </div>
      <span className={`mono ${styles.progressValue}`}>
        {count}/{total}
      </span>
    </div>
  );
}

/** 标注工作台未选批次时的右侧批次卡片网格：项目分组 + 批次卡片，点卡直接选中批次。
 *  分组/排序与 AnnotateSidebar 对齐。 */
export function BatchCardGrid({ batches, onSelect }: Props) {
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

  const pagination = useListPagination(
    groups.flatMap((group) => group.items),
    batches.map((batch) => batch.batch_id).join(","),
  );
  const visibleIds = new Set(pagination.items.map((batch) => batch.batch_id));
  return (
    <section aria-label="标注批次列表" className={listStyles.surface}>
      <div className={`${styles.root} p-4`}>
        {groups
          .filter((group) => group.items.some((batch) => visibleIds.has(batch.batch_id)))
          .map((g) => (
            <div key={g.project_id} className={styles.projectSection}>
              <div className={styles.projectHeader}>
                <span className={styles.projectName}>{g.project_name}</span>
                {g.remaining > 0 && (
                  <span className={styles.remainingBadge}>
                    <Badge variant="accent">待标 {g.remaining}</Badge>
                  </span>
                )}
              </div>
              <div className={styles.grid}>
                {g.items
                  .filter((batch) => visibleIds.has(batch.batch_id))
                  .map((b) => {
                    const total = b.total_tasks;
                    // 与工作台进度卡一致的三档累计口径。
                    const startedDone =
                      (b.in_progress_tasks ?? 0) + b.review_tasks + b.completed_tasks;
                    const reviewDone = b.review_tasks + b.completed_tasks;
                    const approvedDone = b.completed_tasks;
                    const pctOf = (n: number) => (total ? Math.round((n / total) * 1000) / 10 : 0);
                    const s = STATUS[b.status] ?? { label: b.status, variant: "outline" as const };
                    return (
                      <button
                        key={b.batch_id}
                        type="button"
                        className={styles.card}
                        onClick={() => onSelect(b)}
                      >
                        <div className={styles.cover}>
                          <Thumbnail
                            src={b.thumbnail_url ?? undefined}
                            blurhash={b.cover_blurhash ?? undefined}
                            width={88}
                            height={88}
                          />
                        </div>
                        <div className={styles.body}>
                          <div className={styles.cardTop}>
                            <span className={`mono ${styles.cardId}`}>{b.batch_display_id}</span>
                            <span className={styles.cardStatus}>
                              <Badge variant={s.variant} dot>
                                {s.label}
                              </Badge>
                            </span>
                          </div>
                          <div className={styles.cardName}>{b.batch_name}</div>
                          <div className={styles.progress}>
                            <ProgressRow
                              label="标注中"
                              count={startedDone}
                              total={total}
                              pct={pctOf(startedDone)}
                              color="var(--sc-brand)"
                            />
                            <ProgressRow
                              label="送审"
                              count={reviewDone}
                              total={total}
                              pct={pctOf(reviewDone)}
                              color="var(--sc-caution)"
                            />
                            <ProgressRow
                              label="通过"
                              count={approvedDone}
                              total={total}
                              pct={pctOf(approvedDone)}
                              color="var(--sc-positive)"
                            />
                          </div>
                        </div>
                      </button>
                    );
                  })}
              </div>
            </div>
          ))}
      </div>
      <ListPagination
        label="批次"
        page={pagination.page}
        pageSize={pagination.pageSize}
        total={pagination.total}
        onPageChange={pagination.setPage}
        onPageSizeChange={pagination.setPageSize}
      />
    </section>
  );
}
