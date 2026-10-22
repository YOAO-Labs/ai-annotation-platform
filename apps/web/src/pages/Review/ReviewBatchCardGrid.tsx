import { listStyles } from "@/components/ui/listStyles";
import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { useMemo } from "react";
import type { CSSProperties } from "react";
import { Badge } from "@/components/ui/Badge";
import { Thumbnail } from "@/components/Thumbnail";
import { useElementStyle } from "@/components/ui/useElementStyle";
import type { ReviewingBatchItem } from "@/api/dashboard";
import styles from "./ReviewBatchCardGrid.module.css";

interface Props {
  batches: ReviewingBatchItem[];
  onSelect: (b: ReviewingBatchItem) => void;
}

interface Group {
  project_id: string;
  project_name: string;
  items: ReviewingBatchItem[];
  pending: number;
}

/** 单根进度条；与质检进度卡的两档配色对齐（待审=caution / 通过=positive）。 */
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

/** 质检审核未选批次时的右侧批次卡片网格：项目分组 + 批次卡片，点卡直接选中批次。
 *  封面左下角叠标注员头像，让审核员一眼看到「这批是谁标的」。 */
export function ReviewBatchCardGrid({ batches, onSelect }: Props) {
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

  const pagination = useListPagination(
    groups.flatMap((group) => group.items),
    batches.map((batch) => batch.batch_id).join(","),
  );
  const visibleIds = new Set(pagination.items.map((batch) => batch.batch_id));
  return (
    <section aria-label="审核批次列表" className={listStyles.surface}>
      <div className={`${styles.root} p-4`}>
        {groups
          .filter((group) => group.items.some((batch) => visibleIds.has(batch.batch_id)))
          .map((g) => (
            <div key={g.project_id} className={styles.projectSection}>
              <div className={styles.projectHeader}>
                <span className={styles.projectName}>{g.project_name}</span>
                {g.pending > 0 && (
                  <span className={styles.remainingBadge}>
                    <Badge variant="warning">待审 {g.pending}</Badge>
                  </span>
                )}
              </div>
              <div className={styles.grid}>
                {g.items
                  .filter((batch) => visibleIds.has(batch.batch_id))
                  .map((b) => {
                    const total = b.total_tasks;
                    const pctOf = (n: number) => (total ? Math.round((n / total) * 1000) / 10 : 0);
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
                              <Badge variant="warning" dot>
                                审核中
                              </Badge>
                            </span>
                          </div>
                          <div className={styles.cardName}>{b.batch_name}</div>
                          {b.annotator && (
                            <div className={styles.annotatorRow}>
                              <span>标注员</span>
                              <span className={styles.annotatorName}>{b.annotator.name}</span>
                            </div>
                          )}
                          <div className={styles.progress}>
                            <ProgressRow
                              label="待审"
                              count={b.review_tasks}
                              total={total}
                              pct={pctOf(b.review_tasks)}
                              color="var(--sc-caution)"
                            />
                            <ProgressRow
                              label="通过"
                              count={b.completed_tasks}
                              total={total}
                              pct={pctOf(b.completed_tasks)}
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
