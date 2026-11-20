import { Skeleton } from "@/components/shadcn/ui/skeleton";
import styles from "./PageLoader.module.css";

/**
 * 路由 lazy-load 时的最简 fallback。
 * 使用与仪表盘表面接近的骨架，避免短暂切页时出现裸文字闪烁。
 */
export function PageLoader() {
  return (
    <div className={styles.root} role="status" aria-label="页面加载中">
      <div className={styles.panel}>
        <Skeleton className={styles.title} />
        <div className={styles.metrics}>
          <Skeleton className={styles.metric} />
          <Skeleton className={styles.metric} />
          <Skeleton className={styles.metric} />
        </div>
        <Skeleton className={styles.table} />
      </div>
    </div>
  );
}
