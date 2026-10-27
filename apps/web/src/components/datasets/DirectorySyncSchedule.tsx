import { useEffect, useId, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";

const states: Record<string, string> = {
  idle: "等待下次周期",
  scanning: "扫描中",
  preflighting: "预检中",
  awaiting_review: "等待核对差异",
  registering: "登记中",
  blocked: "已阻断，需重新核对授权或配置",
};
const errors: Record<string, string> = {
  schedule_revision_changed: "周期计划已变化，请刷新状态后重试",
  schedule_review_changed: "待核对轮次已变化，请刷新后重新查看差异",
  schedule_cycle_authority_pinned:
    "当前扫描或预检仍在执行，请先完成或取消任务，再更换执行账号或凭据",
  worker_anchor_unverified: "请先对来源执行一次只读预扫描，确认工作节点可访问",
  source_busy: "来源有手动任务，等待结束后再扫描",
  dispatch_unavailable: "派发暂不可用，检查点已保留",
  access_revoked: "执行账号权限已变化，请核对后重新启用",
  api_key_authority_revoked: "执行凭据已撤销、过期或变更，请重新启用",
  configuration_changed: "来源或位置配置已变化，请核对后重新启用",
  root_identity_changed: "存储根身份已变化，请先核对存储位置",
  cancelled_by_user: "本轮任务已取消，按间隔安排下一轮",
  run_authority_changed: "本轮任务已由其他身份接续，请核对后重新启用",
};

function scheduleError(error: unknown): string {
  if (error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object") {
    const code = String((error.detailRaw as { code?: unknown }).code ?? "");
    if (errors[code]) return errors[code];
  }
  return verifiedContentError(error);
}

export function DirectorySyncSchedule({
  sourceId,
  owner,
  datasetId,
}: {
  sourceId: string;
  owner: string;
  datasetId: string;
}) {
  const client = useQueryClient();
  const policyHint = useId();
  const capture = useSessionGuard();
  const [policyDraft, setPolicyDraft] = useState<"manual" | "pure_additions" | null>(null);
  const [timezoneDraft, setTimezoneDraft] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const query = useQuery({
    queryKey: ["source-sync-schedule", owner, sourceId],
    queryFn: ({ signal }) => api.syncSchedule(sourceId, signal),
    refetchInterval: (q) => (q.state.data?.enabled ? 5000 : false),
  });
  const row = query.data;
  const policy = policyDraft ?? row?.new_file_policy ?? "manual";
  const timezone = timezoneDraft ?? row?.timezone ?? "UTC";
  const displayTime = (date: string) => {
    try {
      return new Date(date).toLocaleString(undefined, { timeZone: row?.timezone ?? "UTC" });
    } catch {
      return `${new Date(date).toISOString()}（UTC，本机不支持已选时区）`;
    }
  };
  const interval = draft ?? String(row?.interval_minutes ?? 60);
  useEffect(() => {
    setReviewed(false);
  }, [row?.scan_id, row?.revision]);
  useEffect(() => {
    if (!row?.scan_id) return;
    void client.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    void client.invalidateQueries({ queryKey: ["dataset-item", datasetId] });
    void client.invalidateQueries({ queryKey: ["dataset-workbench", datasetId] });
    void client.invalidateQueries({ queryKey: ["datasets"] });
    void client.invalidateQueries({ queryKey: ["dataset", datasetId] });
    void client.invalidateQueries({ queryKey: ["dataset-scans", owner, sourceId] });
    void client.invalidateQueries({ queryKey: ["directory-registration", owner, row.scan_id] });
  }, [client, owner, sourceId, datasetId, row?.scan_id, row?.state]);
  const act = async (work: () => Promise<unknown>) => {
    const current = capture();
    setBusy(true);
    setError("");
    try {
      await work();
      if (current()) {
        setDraft(null);
        setPolicyDraft(null);
        setTimezoneDraft(null);
        setReviewed(false);
      }
    } catch (e) {
      if (current()) setError(scheduleError(e));
    } finally {
      if (current()) {
        await client.invalidateQueries({ queryKey: ["source-sync-schedule", owner, sourceId] });
        if (current()) setBusy(false);
      }
    }
  };
  return (
    <section aria-label="周期扫描计划" className="mt-4 text-sm">
      <h5 className="text-sm font-semibold">周期扫描与预检</h5>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        按本次启用账号的当前权限自动扫描、预检。默认人工登记；可选择仅在整轮均为新增或未变化、且无错误时自动登记新增。其他差异整轮等待核对。暂停停止后续触发，已确认的任务可在任务面板单独取消。
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label>
          间隔（分钟）{" "}
          <input
            aria-label="周期扫描间隔（分钟）"
            type="number"
            min={15}
            max={10080}
            className="w-24 rounded-md border border-input bg-background px-2 py-1"
            value={interval}
            onChange={(e) => setDraft(e.target.value)}
          />
        </label>
        <label>
          新增接纳策略{" "}
          <select
            aria-label="新增接纳策略"
            aria-describedby={policyHint}
            className="rounded-md border border-input bg-background px-2 py-1"
            value={policy}
            onChange={(e) => setPolicyDraft(e.target.value as typeof policy)}
          >
            <option value="manual">人工确认</option>
            <option value="pure_additions">纯新增轮次自动登记</option>
          </select>
        </label>
        <label>
          显示时区{" "}
          <input
            aria-label="周期计划时区"
            className="w-40 rounded-md border border-input bg-background px-2 py-1"
            value={timezone}
            onChange={(e) => setTimezoneDraft(e.target.value)}
            placeholder="Asia/Shanghai"
          />
        </label>
        <Button
          size="sm"
          disabled={
            query.isPending ||
            !!query.error ||
            busy ||
            !Number.isInteger(Number(interval)) ||
            Number(interval) < 15 ||
            Number(interval) > 10080
          }
          onClick={() =>
            void act(() =>
              api.configureSync(sourceId, {
                expected_revision: row?.revision ?? 0,
                enabled: true,
                interval_minutes: Number(interval),
                new_file_policy: policy,
                timezone,
              }),
            )
          }
        >
          {row?.enabled ? "保存周期计划" : "启用周期扫描"}
        </Button>
        {row?.enabled && (
          <Button
            size="sm"
            disabled={busy}
            onClick={() =>
              void act(() =>
                api.configureSync(sourceId, {
                  expected_revision: row.revision,
                  enabled: false,
                  interval_minutes: row.interval_minutes,
                  new_file_policy: row.new_file_policy,
                  timezone: row.timezone,
                }),
              )
            }
          >
            暂停周期扫描
          </Button>
        )}
        <Button size="sm" disabled={busy} onClick={() => void query.refetch()}>
          刷新周期状态
        </Button>
      </div>
      <p id={policyHint} className="mt-2 text-xs leading-relaxed text-muted-foreground">
        默认由人工核对差异；自动登记仅适用于纯新增轮次。
      </p>
      {row && (
        <p role="status" className="mt-2">
          {row.enabled || row.state === "blocked" ? states[row.state] : "周期计划已暂停"}
          {row.next_run_at && row.enabled ? ` · 下次计划 ${displayTime(row.next_run_at)}` : ""}
          {` · 时区 ${row.timezone}`}
          {row.last_checked_at ? ` · 最近检查 ${displayTime(row.last_checked_at)}` : ""}
        </p>
      )}
      {row?.last_error && (
        <p className="mt-1 text-status-caution">
          {errors[row.last_error] ?? "周期检查暂未完成，请稍后刷新或查看后台任务"}
        </p>
      )}
      {row?.enabled && row.state === "awaiting_review" && row.scan_id && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={reviewed}
              onChange={(e) => setReviewed(e.target.checked)}
            />
            已核对本轮差异，跳过时不登记任何样本
          </label>
          <Button
            size="sm"
            disabled={busy || !reviewed}
            onClick={() =>
              void act(() =>
                api.continueSync(sourceId, {
                  expected_revision: row.revision,
                  scan_id: row.scan_id!,
                }),
              )
            }
          >
            跳过本轮差异，继续周期
          </Button>
        </div>
      )}
      {(error || query.error) && (
        <p role="alert" className="mt-2 text-status-danger">
          {error || scheduleError(query.error)}
        </p>
      )}
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        使用 IANA
        时区名称；间隔按实际分钟计算（1440分钟为24小时），不按本地日历调整夏令时。策略变更只影响新轮次，已开始的人工轮次仍需人工核对。间隔范围15–10080分钟；由后台调度器按分钟检查，节点繁忙或离线时可能延后，不补跑错过的周期。权限或配置变化后需重新启用。
      </p>
    </section>
  );
}
