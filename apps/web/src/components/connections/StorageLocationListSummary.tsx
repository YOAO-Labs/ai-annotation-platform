import type { StorageLocationHealth, StorageSourceUsage } from "@/api/generated/types.gen";
import { Badge } from "@/components/ui/Badge";
import { evidenceDate } from "./StorageSourceDiagnostics";
import { formatSourceBytes } from "./StorageSourceUsage";

export function LocationHealthSummary({
  value,
  now,
  configRevision,
}: {
  value?: StorageLocationHealth;
  now: number;
  configRevision?: number;
}) {
  const expired = !!value?.fresh_until && new Date(value.fresh_until).getTime() <= now;
  const success = value?.current_success_count ?? 0;
  const failed = value?.current_failure_count ?? 0;
  const observed = value?.observed_node_count ?? 0;
  const changed =
    configRevision != null &&
    value?.latest != null &&
    value.latest.config_revision !== configRevision;
  const label = !value
    ? "健康未知"
    : expired
      ? "摘要已过期"
      : failed
        ? success
          ? "部分探测失败"
          : "探测失败"
        : success
          ? "探测通过"
          : observed
            ? changed
              ? "配置已变化"
              : "探测已过期"
            : "未探测";
  const variant =
    expired || (success && failed) || (observed && !success && !failed)
      ? "warning"
      : failed
        ? "danger"
        : success
          ? "success"
          : "outline";
  return (
    <div className="space-y-1">
      <Badge variant={variant}>{label}</Badge>
      {observed > 0 && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          {observed} 个节点
          {!expired && (success > 0 || failed > 0) && ` · 有效通过 ${success}／失败 ${failed}`}
        </p>
      )}
      <p className="text-xs leading-relaxed text-muted-foreground">
        {value?.latest
          ? `最近记录 ${evidenceDate(value.latest.observed_at)}`
          : value
            ? "暂无节点观测"
            : "尚无可用摘要"}
      </p>
    </div>
  );
}

export function LocationSourceScale({ value }: { value?: StorageSourceUsage }) {
  const total = value?.coverage === "complete" ? value.total_size_bytes : null;
  return (
    <div className="space-y-1">
      <p className="font-medium tabular-nums">
        {!value ? "待统计" : total != null ? formatSourceBytes(total) : "总量未知"}
      </p>
      {value && (
        <p className="text-xs leading-relaxed text-muted-foreground tabular-nums">
          {total == null && `已确认 ${formatSourceBytes(value.known_size_bytes)} · `}
          {value.file_count.toLocaleString("zh-CN")} 个文件
          {value.reference_count === 0 && " · 尚无采纳记录"}
        </p>
      )}
      <p className="text-xs leading-relaxed text-muted-foreground">
        {value?.observed_through
          ? `索引观测 ${evidenceDate(value.observed_through)}`
          : "暂无文件观测"}
      </p>
    </div>
  );
}
