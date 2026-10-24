import { useEffect, useRef, useState, type MouseEvent } from "react";
import { useAuthStore } from "@/stores/authStore";
import { useStorageBuckets, useStartStorageInventory } from "@/hooks/useStorage";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { listStyles } from "@/components/ui/listStyles";
import { ManagedOriginalReclamationPanel } from "./ManagedOriginalReclamationPanel";
import { StorageUsageBreakdown } from "./StorageUsageBreakdown";

const ROLES: Record<string, string> = {
  annotations: "标注资产",
  datasets: "数据集原件",
  "bug-reports": "问题附件",
  "media-cache": "派生媒体",
  "audit-archive": "审计归档",
  import: "导入中转",
  export: "导出产物",
  avatars: "头像",
  training: "训练资产",
};
const ERRORS: Record<string, string> = {
  object_listing_failed: "对象列举失败",
  object_classification_failed: "对象分类失败",
  inventory_budget_exceeded: "达到盘点预算",
  configuration_changed: "存储配置已变化",
  access_revoked: "执行权限已失效",
  worker_failed: "执行中断",
  inventory_failed: "部分桶盘点失败",
  cancelled: "已取消",
};
function bytes(value: number | null) {
  if (value === null) return "未统计";
  if (value === 0) return "0 B";
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** unit).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}
function date(value: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN") : "尚无成功记录";
}

export function StorageUsagePanel() {
  const revision = useSessionRevision();
  const role = useAuthStore((s) => s.user?.role);
  return role === "super_admin" ? <StorageUsageOwner key={revision} /> : null;
}

function StorageUsageOwner() {
  const query = useStorageBuckets();
  const start = useStartStorageInventory();
  const capture = useSessionGuard();
  const dialogTrigger = useRef<HTMLElement | null>(null);
  const summaryRef = useRef<HTMLElement | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionDenied, setActionDenied] = useState(false);
  const deniedAt = useRef(0);
  const denied = actionDenied || storageAccessDenied(query.error);
  const data = denied ? undefined : query.data;
  const active = ["pending", "running"].includes(data?.inventory_status ?? "");
  const stale = data?.items.some((item) => item.status === "stale");
  const failed = data?.inventory_status === "failed";
  useEffect(() => {
    if (denied) setJobId(null);
  }, [denied]);
  useEffect(() => {
    if (actionDenied && !query.error && query.dataUpdatedAt > deniedAt.current) {
      setActionDenied(false);
      setError(null);
    }
  }, [actionDenied, query.error, query.dataUpdatedAt]);
  const onStart = async (event: MouseEvent<HTMLButtonElement>) => {
    dialogTrigger.current = event.currentTarget;
    const current = capture();
    setError(null);
    try {
      const result = await start.mutateAsync();
      if (current()) setJobId(result.job_id);
    } catch (cause) {
      if (current()) {
        deniedAt.current = Date.now();
        setActionDenied(storageAccessDenied(cause));
        setError("无法启动盘点，请检查权限与存储配置后重试。");
      }
    }
  };
  return (
    <section className="mt-4" aria-label="平台对象占用">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3.5">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">平台对象占用</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              按部署配置的九种用途桶去重，桶标签不代表当前写入绑定。未列入范围的位置与桶、历史版本和磁盘开销不在此统计范围。
            </p>
          </div>
          <Button
            size="sm"
            disabled={!data || active || start.isPending || denied}
            onClick={onStart}
          >
            {start.isPending ? "正在排队…" : active ? "盘点中…" : "重新盘点"}
          </Button>
        </div>
        {query.isPending && (
          <p role="status" className="px-4 pb-4 text-sm text-muted-foreground">
            正在读取占用快照…
          </p>
        )}
        {query.error && (
          <p role="alert" className="px-4 pb-3 text-sm text-status-danger">
            {denied
              ? "当前账号无权读取平台占用。"
              : data
                ? "刷新失败，保留上次快照；数据可能过期。"
                : "无法读取占用快照。"}
          </p>
        )}
        {error && (
          <p role="alert" className="px-4 pb-3 text-sm text-status-danger">
            {error}
          </p>
        )}
        {data && (
          <>
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-border px-4 py-3 text-sm">
              <span>
                已统计{" "}
                <span className="tabular-nums font-medium">
                  {data.measured_bucket_count} / {data.bucket_count}
                </span>{" "}
                个桶
              </span>
              <span>
                部署范围对象字节小计{" "}
                <span className="tabular-nums font-medium">{bytes(data.total_size_bytes)}</span>
              </span>
              <span className="text-xs text-muted-foreground">
                物理容量、可用空间与配额：未提供
              </span>
            </div>
            {!!data.uncovered_binding_purposes?.length && (
              <p role="status" className="px-4 pb-3 text-xs leading-relaxed text-muted-foreground">
                当前写入目标未覆盖：
                {data.uncovered_binding_purposes
                  .map((purpose) => ROLES[purpose] ?? purpose)
                  .join("、")}
                。这些目标的对象未计入小计；旧部署桶中保留的对象继续统计。
              </p>
            )}
            {(active || failed || stale || data.inventory_status === "cancelled") && (
              <div role="status" className="flex flex-wrap items-center gap-3 px-4 pb-3 text-xs">
                <Badge variant={failed ? "danger" : "outline"}>
                  {active
                    ? "后台分页盘点中"
                    : failed
                      ? "最近盘点失败"
                      : data.inventory_status === "cancelled"
                        ? "最近盘点已取消"
                        : "快照已过期"}
                </Badge>
                <span className="text-muted-foreground">
                  {active
                    ? "已有成功值继续展示，完成后自动更新。"
                    : "保留各桶最近成功值与原统计时间，可重新盘点。"}
                </span>
                {data.inventory_job_id && (
                  <Button
                    size="sm"
                    onClick={(event) => {
                      dialogTrigger.current = event.currentTarget;
                      setJobId(data.inventory_job_id);
                    }}
                  >
                    查看盘点任务
                  </Button>
                )}
              </div>
            )}
            <StorageUsageBreakdown value={data.breakdown} />
            <details className="border-t border-border">
              <summary ref={summaryRef} className="cursor-pointer px-4 py-3 text-sm font-medium">
                查看各桶统计与时间
              </summary>
              <p className="px-4 pb-3 text-xs text-muted-foreground">
                数据来自当前对象的分页列举，为采样窗口内估算，非同一时点的原子快照；完整采样超过 24
                小时标为过期。失败和取消不会清零。
              </p>
              {data.inventory_job_id &&
                !active &&
                !failed &&
                !stale &&
                data.inventory_status !== "cancelled" && (
                  <div className="px-4 pb-3">
                    <Button
                      size="sm"
                      onClick={(event) => {
                        dialogTrigger.current = event.currentTarget;
                        setJobId(data.inventory_job_id);
                      }}
                    >
                      查看最近盘点任务
                    </Button>
                  </div>
                )}
              <div
                className="overflow-x-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                role="region"
                aria-label="各桶占用明细，可横向滚动"
                tabIndex={0}
              >
                <table className={`${listStyles.table} min-w-[760px]`}>
                  <thead>
                    <tr>
                      {["用途 / 桶", "对象数", "对象字节", "最近成功采样窗口", "最近尝试"].map(
                        (title) => (
                          <th
                            key={title}
                            className="px-4 py-2 text-left text-xs font-medium text-muted-foreground"
                          >
                            {title}
                          </th>
                        ),
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((item) => (
                      <tr key={item.name} className="border-t border-border text-sm">
                        <td className="min-w-[220px] max-w-[320px] px-4 py-3">
                          <div>{item.roles.map((role) => ROLES[role] ?? role).join("、")}</div>
                          <div className="mt-1 break-all text-xs text-muted-foreground">
                            {item.name}
                          </div>
                        </td>
                        <td className="px-4 py-3 tabular-nums">
                          {item.object_count === null
                            ? "未统计"
                            : item.object_count.toLocaleString("zh-CN")}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 tabular-nums">
                          {bytes(item.total_size_bytes)}
                        </td>
                        <td className="px-4 py-3 text-xs text-muted-foreground">
                          <div>{date(item.measurement_started_at)}</div>
                          {item.measured_at && (
                            <div>
                              至 {date(item.measured_at)}
                              {item.status === "stale" ? " · 已过期" : ""}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-xs">
                          <span
                            className={
                              item.attempt_status === "failed"
                                ? "text-status-danger"
                                : "text-muted-foreground"
                            }
                          >
                            {item.error
                              ? (ERRORS[item.error] ?? "盘点失败")
                              : ({
                                  never: "尚未盘点",
                                  pending: "等待中",
                                  running: "进行中",
                                  completed: "已完成",
                                  cancelled: "已取消",
                                }[item.attempt_status] ?? item.attempt_status)}
                          </span>
                          {item.attempted_at && (
                            <div className="mt-1 text-muted-foreground">
                              {date(item.attempted_at)}
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </>
        )}
        <ManagedOriginalReclamationPanel />
      </Card>
      {jobId && !denied && (
        <AsyncJobDetailModal
          jobId={jobId}
          returnFocusRef={dialogTrigger}
          onClose={() => {
            if (
              !dialogTrigger.current?.isConnected ||
              (dialogTrigger.current instanceof HTMLButtonElement && dialogTrigger.current.disabled)
            )
              dialogTrigger.current = summaryRef.current;
            setJobId(null);
            void query.refetch();
          }}
        />
      )}
    </section>
  );
}
