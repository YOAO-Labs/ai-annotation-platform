import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { storageApi, type StorageFaultFilter } from "@/api/storage";
import type {
  StorageFaultOut,
  StorageObservationRateCounts,
  StorageObservationRates,
} from "@/api/generated/types.gen";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { storageAccessDenied } from "./storageQueryState";
import { evidenceDate, sourceHref } from "./StorageSourceDiagnostics";

const CATEGORIES: Record<StorageFaultOut["category"], string> = {
  root_unavailable: "根目录不可用",
  access_denied: "存储访问被拒绝",
  source_changed: "原件内容变化",
  preview_failed: "预览生成失败",
};
const OPERATIONS: Record<StorageFaultOut["operation"], string> = {
  scan: "目录扫描",
  scan_content: "内容核验",
  reference_read: "原件读取",
  preview_generate: "预览生成",
};
const FILTERS: [StorageFaultFilter, string][] = [
  ["active", "未恢复"],
  ["recovered", "已恢复"],
  ["all", "全部"],
];
const SCOPE_LABELS = {
  current: "保存的观测范围匹配",
  historical: "历史配置或内容范围",
  unknown: "当前范围尚未确认",
};

function utcTime(value: string): number | null {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)
  ) {
    return null;
  }
  const time = Date.parse(value);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 19) === value.slice(0, 19)
    ? time
    : null;
}

function validWindow(
  value: StorageObservationRates | null | undefined,
): value is StorageObservationRates {
  if (
    !value ||
    value.timezone !== "UTC" ||
    value.unit !== "attempt" ||
    value.coverage !== "recorded_attempts_only"
  ) {
    return false;
  }
  const start = utcTime(value.window_start);
  const end = utcTime(value.window_end);
  const measured = utcTime(value.measured_at);
  return (
    start !== null &&
    end !== null &&
    measured === end &&
    start <= end &&
    start % 86_400_000 === 0 &&
    new Date(start).toISOString().slice(0, 10) === new Date(end).toISOString().slice(0, 10)
  );
}

function validCounts(
  value: StorageObservationRateCounts | null | undefined,
): value is StorageObservationRateCounts {
  if (!value) return false;
  const {
    success_count: success,
    failure_count: failure,
    total_count: total,
    failure_rate: rate,
  } = value;
  if (
    ![success, failure, total].every((count) => Number.isSafeInteger(count) && count >= 0) ||
    success + failure !== total
  ) {
    return false;
  }
  return total === 0
    ? rate === null
    : typeof rate === "number" &&
        Number.isFinite(rate) &&
        rate >= 0 &&
        rate <= 1 &&
        (failure !== 0 || rate === 0) &&
        (success !== 0 || rate === 1) &&
        Math.abs(rate - failure / total) <= 1e-9;
}

const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });
const failurePercent = (value: number) =>
  value > 0 && value < 0.0001
    ? "<0.01%"
    : value < 1 && value > 0.9999
      ? ">99.99%"
      : percent.format(value);
const utcDate = (value: string) => new Date(value).toISOString().slice(0, 19).replace("T", " ");

function ObservationRates({ value }: { value: StorageObservationRates | null | undefined }) {
  const window = validWindow(value) ? value : null;
  return (
    <div className="space-y-2">
      <dl className="space-y-2" aria-label="当日已记录完成尝试">
        {(
          [
            ["original_read", "原图不可读率"],
            ["preview_generate", "预览失败率"],
          ] as const
        ).map(([metric, label]) => {
          const counts = window?.[metric];
          const valid = validCounts(counts);
          return (
            <div
              key={metric}
              role="group"
              aria-label={label}
              className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1"
            >
              <dt className="font-medium">{label}</dt>
              <dd className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 tabular-nums">
                <span>
                  {!valid
                    ? "未知"
                    : counts.total_count === 0
                      ? "暂无可计算观测"
                      : failurePercent(counts.failure_rate!)}
                </span>
                {valid && (
                  <span className="text-xs leading-relaxed text-muted-foreground">
                    成功 {counts.success_count} · 失败 {counts.failure_count} · 分母{" "}
                    {counts.total_count}
                  </span>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
      <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
        {window ? (
          <>
            <p>
              当日 UTC 窗口：{utcDate(window.window_start)} 至 {utcDate(window.window_end)}。
            </p>
            <p>服务器观测于 {utcDate(window.measured_at)} UTC。</p>
          </>
        ) : (
          <p>当日 UTC 窗口与比率观测未知；尚未提供或数据无效。</p>
        )}
        <p>
          失败率为失败 ÷（成功 + 失败），仅统计已记录的完成尝试，未记录调用的覆盖未知。
          窗口可能包含旧配置，不代表当前健康；故障筛选和分页不改变此汇总。
        </p>
      </div>
    </div>
  );
}

interface Props {
  locationId: string;
  returnTo?: string;
  onJob: (id: string, trigger: HTMLElement) => void;
}

export function StorageFaultsPanel(props: Props) {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  if (role !== "super_admin" && role !== "project_admin") return null;
  return <FaultsOwner key={`${revision}/${role}/${props.locationId}`} {...props} />;
}

function FaultsOwner({ locationId, returnTo, onJob }: Props) {
  const { role } = usePermissions();
  const actor = useAuthStore((s) => s.user?.id);
  const session = useAuthStore((s) => s.sessionId);
  const [state, setState] = useState<StorageFaultFilter>("active");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["storage-faults", actor, session, role, locationId, state, offset],
    queryFn: ({ signal }) => storageApi.faults(locationId, state, offset, signal),
    retry: false,
  });
  const denied = storageAccessDenied(query.error);
  const data = denied ? undefined : query.data;
  return (
    <section aria-label="来源故障观测" className="min-w-0 space-y-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-md font-semibold">来源故障观测</h3>
        <Button size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
          刷新故障观测
        </Button>
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">
        只读已保存观测，不探测原件。没有记录不代表实时健康；恢复仅证明同一范围曾实际成功。
      </p>
      {data && <ObservationRates value={data.rates} />}
      <div role="group" aria-label="故障状态" className="flex flex-wrap items-center gap-2">
        {FILTERS.map(([value, label]) => (
          <Button
            key={value}
            size="sm"
            variant={state === value ? "primary" : "default"}
            aria-pressed={state === value}
            onClick={() => {
              setState(value);
              setOffset(0);
            }}
          >
            {label}
          </Button>
        ))}
        {data && (
          <span className="text-xs text-muted-foreground">
            未恢复 {data.counts.active} · 已恢复 {data.counts.recovered}
          </span>
        )}
      </div>
      {query.isPending && <p role="status">正在读取故障观测…</p>}
      {query.isError && (
        <p role="alert" className="text-status-danger">
          {denied
            ? "无权查看此位置的故障观测。"
            : data
              ? "故障观测刷新失败，以下保留上次结果。"
              : "故障观测读取失败，请重试。"}
        </p>
      )}
      {data && (
        <>
          {!data.items.length && (
            <p className="text-muted-foreground">
              {state === "active" ? "暂无未恢复的故障观测。" : "此页暂无匹配的故障观测。"}
            </p>
          )}
          <ul className="m-0 list-none space-y-3 p-0">
            {data.items.map((fault) => (
              <li key={fault.id} className="min-w-0 space-y-2 rounded-md border border-border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h4 className="text-sm font-semibold">{CATEGORIES[fault.category]}</h4>
                  <Badge variant={fault.state === "recovered" ? "success" : "warning"}>
                    {fault.state === "recovered" ? "已恢复" : "未恢复"}
                  </Badge>
                </div>
                <p className="min-w-0 break-words">
                  {fault.source_name ?? "来源记录已删除"} · {OPERATIONS[fault.operation]}
                </p>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {SCOPE_LABELS[fault.scope_status]} · 已接收失败观测 {fault.failure_count} 次
                </p>
                <dl className="grid min-w-0 gap-1 text-xs leading-relaxed text-muted-foreground">
                  <div className="flex flex-wrap gap-x-2">
                    <dt>首次失败</dt>
                    <dd>{evidenceDate(fault.first_failed_at)}</dd>
                  </div>
                  <div className="flex flex-wrap gap-x-2">
                    <dt>最近失败</dt>
                    <dd>{evidenceDate(fault.last_failed_at)}</dd>
                  </div>
                  {fault.recovered_at && (
                    <div className="flex flex-wrap gap-x-2">
                      <dt>{fault.state === "active" ? "上次恢复观测" : "恢复观测"}</dt>
                      <dd>{evidenceDate(fault.recovered_at)}</dd>
                    </div>
                  )}
                </dl>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  处理方：
                  {fault.responsible_role === "platform_admin" ? "平台管理员" : "数据集管理员"}
                  {fault.responsible_user_id === null && " · 未指定个人"}
                </p>
                <div className="flex flex-wrap items-center gap-3">
                  {fault.dataset_id && fault.source_id && (
                    <Link
                      className="text-xs text-brand underline"
                      to={sourceHref(fault.dataset_id, fault.source_id, returnTo)}
                    >
                      查看来源
                    </Link>
                  )}
                  {fault.job_id && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={(event) => onJob(fault.job_id!, event.currentTarget)}
                    >
                      故障任务详情
                    </Button>
                  )}
                  {role === "super_admin" && (
                    <Link
                      className="text-xs text-brand underline"
                      to={`/audit?${new URLSearchParams({ target_type: "storage_location", detail_key: "fault_id", detail_value: fault.id })}`}
                    >
                      查看故障转换记录
                    </Link>
                  )}
                </div>
              </li>
            ))}
          </ul>
          <nav
            aria-label="故障观测分页"
            className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
          >
            <Button
              size="sm"
              disabled={!offset || query.isFetching}
              onClick={() => setOffset(Math.max(0, offset - 20))}
            >
              上一页故障
            </Button>
            <span>
              第 {offset / 20 + 1} 页 · 共 {data.total} 条
            </span>
            <Button
              size="sm"
              disabled={query.isFetching || offset + 20 >= data.total}
              onClick={() => setOffset(offset + 20)}
            >
              下一页故障
            </Button>
          </nav>
        </>
      )}
    </section>
  );
}
