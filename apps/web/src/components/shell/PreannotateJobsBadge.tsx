/**
 * v0.9.8 · Topbar 全局预标 job 徽章.
 *
 * 紫色徽章显示 in-progress job 数, 0 时不渲染. 点击展开 popover 列每个 job
 * (项目名 / 进度 / 跳转链接), 让 admin 跑完后切到别处也能看到。
 */

import { useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useNavigate } from "react-router-dom";

import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useGlobalPreannotationJobs } from "@/hooks/useGlobalPreannotationJobs";

function JobProgressFill({ pct }: { pct: number }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--preannotate-job-progress": `${pct}%`,
  } as CSSProperties);

  return (
    <div
      ref={ref}
      className="h-full w-[var(--preannotate-job-progress)] bg-violet-500 transition-[width] duration-200 ease-out"
    />
  );
}

export function PreannotateJobsBadge() {
  const { runningJobs } = useGlobalPreannotationJobs();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const sorted = useMemo(
    () => [...runningJobs].sort((a, b) => b.receivedAt - a.receivedAt),
    [runningJobs],
  );

  if (runningJobs.length === 0) return null;

  const jumpToProject = (projectId: string) => {
    setOpen(false);
    navigate(`/ai-pre?project_id=${projectId}`);
  };

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={`${runningJobs.length} 个预标作业进行中`}
        className="inline-flex cursor-pointer items-center gap-1 rounded-full border border-violet-500 bg-violet-500/[0.18] px-2 py-1 text-xs font-semibold leading-[1.2] text-status-info"
      >
        <Icon name="sparkles" size={12} />
        <span>{runningJobs.length}</span>
      </button>

      {open && (
        <>
          <div onClick={() => setOpen(false)} className="fixed inset-0 z-notification-backdrop" />
          <div
            role="dialog"
            aria-label="预标进行中"
            className="absolute right-0 top-[calc(100%+6px)] z-notification flex w-[min(400px,calc(100vw-24px))] min-w-0 flex-col gap-1 rounded-md border border-border bg-popover p-2 shadow-lg"
          >
            <div className="border-b border-border px-2.5 pb-2 pt-1.5 text-xs font-semibold text-muted-foreground">
              预标进行中 ({runningJobs.length})
            </div>
            {sorted.map((j) => {
              const pct = j.total > 0 ? Math.round((j.current / j.total) * 100) : 0;
              return (
                <button
                  key={j.job_id}
                  type="button"
                  onClick={() => jumpToProject(j.project_id)}
                  className="flex cursor-pointer appearance-none flex-col items-stretch gap-1 rounded-sm border-0 bg-transparent px-2.5 py-2 text-left text-foreground transition-colors duration-200 hover:bg-muted"
                >
                  <div className="flex justify-between gap-3 text-xs font-medium">
                    <span className="overflow-hidden text-ellipsis whitespace-nowrap">
                      {j.project_name ?? j.project_id.slice(0, 8)}
                    </span>
                    <span className="tabular-nums text-muted-foreground">
                      {j.current}/{j.total} · {pct}%
                    </span>
                  </div>
                  <div className="h-[3px] overflow-hidden rounded-[2px] bg-border">
                    <JobProgressFill pct={pct} />
                  </div>
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
