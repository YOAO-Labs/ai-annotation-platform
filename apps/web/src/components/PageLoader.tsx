import { Skeleton } from "@/components/shadcn/ui/skeleton";
import styles from "./PageLoader.module.css";

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
