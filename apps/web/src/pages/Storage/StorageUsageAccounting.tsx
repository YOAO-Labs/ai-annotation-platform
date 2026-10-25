import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import type {
  StorageUsageAccountingSummary,
  StorageUsageReconciliationSummary,
  UsageScheduleSummary,
} from "@/api/storage";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useUpdateStorageUsageSchedule } from "@/hooks/useStorage";

interface Props {
  accounting?: StorageUsageAccountingSummary | null;
  reconciliation?: StorageUsageReconciliationSummary | null;
  schedule?: UsageScheduleSummary | null;
  onAccessDenied: () => void;
}

const PAUSE_REASONS: Record<string, string> = {
  access_revoked: "执行权限已失效，请确认权限后重新授权。",
  configuration_changed: "存储配置已变化，请核对当前配置后重新授权。",
};
const field =
  "w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground";

function bytes(value: number) {
  const magnitude = Math.abs(value);
  if (magnitude === 0) return "0 B";
  const unit = Math.min(4, Math.floor(Math.log(magnitude) / Math.log(1024)));
  return `${(magnitude / 1024 ** unit).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}

function change(value: number) {
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${bytes(value)}`;
}

function date(value: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN") : "尚无记录";
}

export function StorageUsageAccounting(props: Props) {
  const revision = useSessionRevision();
  return <AccountingOwner key={revision} {...props} />;
}

function AccountingOwner({ accounting, reconciliation, schedule, onAccessDenied }: Props) {
  const id = useId();
  const capture = useSessionGuard();
  const mutation = useUpdateStorageUsageSchedule();
  const dirty = useRef(false);
  const [saved, setSaved] = useState(schedule);
  const [enabled, setEnabled] = useState(schedule?.enabled ?? false);
  const [interval, setInterval] = useState(String(schedule?.interval_hours ?? 24));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const busy = saving || mutation.isPending;

  useEffect(() => {
    setSaved(schedule);
    if (!dirty.current) {
      setEnabled(schedule?.enabled ?? false);
      setInterval(String(schedule?.interval_hours ?? 24));
    }
  }, [schedule]);

  function edit() {
    dirty.current = true;
    setError(null);
    setNotice(null);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const hours = Number(interval);
    if (!interval.trim() || !Number.isInteger(hours) || hours < 1 || hours > 168) {
      setError("盘点间隔须为 1 至 168 小时的整数。");
      return;
    }
    const current = capture();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await mutation.mutateAsync({ enabled, interval_hours: hours });
      if (!current()) return;
      dirty.current = false;
      setSaved(result);
      setEnabled(result.enabled);
      setInterval(String(result.interval_hours));
      setNotice("周期计划已保存。");
    } catch (cause) {
      if (!current()) return;
      if (storageAccessDenied(cause)) {
        onAccessDenied();
      } else {
        setError("无法保存周期计划，请检查权限与存储配置后重试。");
      }
    } finally {
      if (current()) setSaving(false);
    }
  }

  return (
    <details className="border-t border-border">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">增量账目与周期对账</summary>
      <div className="space-y-4 px-4 pb-4">
        <section aria-label="已确认增量">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">已确认变化</h3>
            <Badge variant="outline">{accounting ? "部分覆盖" : "尚无摘要"}</Badge>
          </div>
          {accounting ? (
            <>
              <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                {[
                  ["累计写入", change(accounting.confirmed_added_bytes)],
                  ["累计移除", change(-accounting.confirmed_removed_bytes)],
                  ["确认净变化", change(accounting.net_change_bytes)],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-xs text-muted-foreground">{label}</dt>
                    <dd className="mt-1 text-sm font-medium tabular-nums">{value}</dd>
                  </div>
                ))}
              </dl>
              <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                待确认 {accounting.pending_operation_count.toLocaleString("zh-CN")} 次 · 结果未知{" "}
                {accounting.unknown_operation_count.toLocaleString("zh-CN")}{" "}
                次。未知结果不按零计量。
              </p>
            </>
          ) : (
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              尚无增量账目摘要，已有盘点快照继续保留。
            </p>
          )}
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            {accounting?.producers.includes("prediction_inputs") && "已接入临时输入写入／清理。"}
            {accounting?.producers.includes("managed_original_creation") &&
              "已接入托管原件受控新增；原件退役不代表物理移除。"}
            其他用途和外部变动尚未覆盖，不能据此推算平台总占用。
            只计当前可见对象内容字节，不含历史版本、删除标记与磁盘开销。
          </p>
        </section>

        <section aria-label="最近对账" className="border-t border-border pt-3">
          <h3 className="text-sm font-semibold">最近对账</h3>
          {reconciliation ? (
            <>
              {reconciliation.status === "no_baseline" ? (
                <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                  已建立首次基准，尚无前次完整盘点可比较。
                </p>
              ) : (
                <>
                  <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div>
                      <dt className="text-xs text-muted-foreground">观测间已确认净变化</dt>
                      <dd className="mt-1 text-sm tabular-nums">
                        {change(reconciliation.confirmed_change_bytes)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted-foreground">未解释变化</dt>
                      <dd className="mt-1 text-sm tabular-nums">
                        {reconciliation.unexplained_change_bytes === null
                          ? "未知"
                          : change(reconciliation.unexplained_change_bytes)}
                      </dd>
                    </div>
                  </dl>
                  {reconciliation.unexplained_change_bytes === null && (
                    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                      可核对部分的未解释净变化：
                      {change(reconciliation.known_unexplained_change_bytes)}，整体变化仍未知。
                    </p>
                  )}
                </>
              )}
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                已比较 {reconciliation.compared_object_count.toLocaleString("zh-CN")} 个对象 ·{" "}
                {reconciliation.bucket_count.toLocaleString("zh-CN")} 个桶 · 不确定{" "}
                {reconciliation.uncertain_object_count.toLocaleString("zh-CN")} 个对象
                {reconciliation.unexplained_object_count > 0 &&
                  ` · ${reconciliation.unexplained_object_count.toLocaleString("zh-CN")} 个对象存在未解释变化`}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                对账时间：{date(reconciliation.measured_at)}
              </p>
            </>
          ) : (
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              暂无完整对账。首轮盘点建立基准，后续完整盘点才能比较。
            </p>
          )}
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            跨观测窗口、未决操作与版本歧义保留为不确定。未解释变化也可能来自未接入的用途或外部写入，
            不表示账目损坏，也不提供删除许可。
          </p>
        </section>

        <form noValidate onSubmit={save} className="border-t border-border pt-3">
          <h3 className="text-sm font-semibold">周期计划</h3>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            {saved?.status === "paused"
              ? `计划已暂停。${PAUSE_REASONS[saved.pause_reason ?? ""] ?? "执行授权或配置需要重新确认。"}`
              : saved?.enabled
                ? `计划已启用，下次到期：${date(saved.next_due_at)}。`
                : "计划已关闭。普通查看不会自动启用。"}
          </p>
          {saved?.last_started_at && (
            <p className="mt-1 text-xs text-muted-foreground">
              最近启动：{date(saved.last_started_at)}
            </p>
          )}
          <div className="mt-3 grid items-start gap-4 sm:grid-cols-2">
            <div className="min-w-0">
              <label htmlFor={`${id}-enabled`} className="block text-sm">
                周期盘点
              </label>
              <div className="mt-2 flex min-h-8 items-center gap-2 text-sm">
                <input
                  id={`${id}-enabled`}
                  type="checkbox"
                  className="size-4 accent-primary"
                  checked={enabled}
                  disabled={busy}
                  aria-describedby={`${id}-enabled-hint`}
                  onChange={(event) => {
                    edit();
                    setEnabled(event.target.checked);
                  }}
                />
                <span>启用</span>
              </div>
              <p
                id={`${id}-enabled-hint`}
                className="mt-1 text-xs leading-relaxed text-muted-foreground"
              >
                保存后生效；关闭计划不会取消正在运行的盘点。
              </p>
            </div>
            <div className="grid min-w-0 gap-1 text-sm">
              <label htmlFor={`${id}-interval`}>盘点间隔（小时）</label>
              <input
                id={`${id}-interval`}
                className={field}
                type="number"
                min="1"
                max="168"
                step="1"
                value={interval}
                disabled={busy}
                aria-describedby={`${id}-interval-hint`}
                onChange={(event) => {
                  edit();
                  setInterval(event.target.value);
                }}
              />
              <span
                id={`${id}-interval-hint`}
                className="text-xs leading-relaxed text-muted-foreground"
              >
                1 至 168 小时；已有盘点运行时不会重叠启动。
              </span>
            </div>
          </div>
          {error && (
            <p role="alert" className="mt-3 text-sm text-status-danger">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="mt-3 text-xs text-muted-foreground">
              {notice}
            </p>
          )}
          <div className="mt-3">
            <Button type="submit" size="sm" disabled={busy}>
              {busy
                ? "正在保存…"
                : saved?.status === "paused" && enabled
                  ? "重新授权并保存"
                  : "保存周期计划"}
            </Button>
          </div>
        </form>
      </div>
    </details>
  );
}
