// B4 异步可见：大 dataset(>2000 items) 关联项目后异步建 task,
// 用 useAsyncJob 轮询 create_tasks job 并显示进度条,完成后 invalidate 相关查询。
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useToastStore } from "@/components/ui/Toast";
import { useAsyncJob } from "@/hooks/useAsyncJob";
import styles from "./LinkJobProgress.module.css";

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * 轮询一个「关联建 task」异步 job 的进度。终态时 toast + invalidate,并回调清空 jobId。
 * jobId 为空时不渲染任何内容。
 */
export function LinkJobProgress({
  jobId,
  projectId,
  onDone,
}: {
  jobId: string | null;
  projectId: string;
  onDone: () => void;
}) {
  const { data: job } = useAsyncJob(jobId, true);
  const pushToast = useToastStore((s) => s.push);
  const qc = useQueryClient();
  const settledRef = useRef<string | null>(null);

  const terminal =
    job?.status === "completed" || job?.status === "failed" || job?.status === "cancelled";

  useEffect(() => {
    if (!job || !terminal || settledRef.current === job.id) return;
    settledRef.current = job.id;

    if (job.status === "completed") {
      const created = readNumber(job.result?.created_tasks);
      pushToast({
        msg: created !== null ? `已建 ${created} 个任务` : "数据集任务已建完",
        kind: "success",
      });
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["unclassified-count", projectId] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
    } else {
      pushToast({
        msg: job.status === "cancelled" ? "建任务已取消" : "建任务失败",
        sub: job.error_message ?? undefined,
        kind: job.status === "cancelled" ? "warning" : "error",
      });
    }
    onDone();
  }, [job, terminal, projectId, pushToast, qc, onDone]);

  if (!jobId) return null;

  const pct = job?.progress_pct ?? 0;

  return (
    <div className={styles.root}>
      <div className={styles.label}>正在建任务… {Math.round(pct)}%</div>
      <ProgressBar value={pct} />
    </div>
  );
}
