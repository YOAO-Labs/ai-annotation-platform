/** Compact member panel shown inside an expanded service pool. */
import { useState, type ReactNode } from "react";
import { Clock3, Gauge, Server, Waypoints, type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/shadcn/ui/tooltip";
import { isCpuFallback } from "@/utils/mlBackendCompute";
import type { GlobalBackendItem, ObserveTarget } from "@/api/adminMlIntegrations";
import {
  NO_METRICS_LABEL,
  type MemberViewModel,
  type PoolViewModel,
  type RuntimeTopologyViewModel,
} from "../runtimeTopology";
import { RuntimeStatusBadge } from "./RuntimeStatusBadge";
import { InstanceDetailSheet } from "./InstanceDetailSheet";
import { LifecycleActions } from "./LifecycleActions";
import { isFreshCachedHealth, parseResidency } from "./parseResidency";

export interface BackendInstanceRowProps {
  pool: PoolViewModel;
  member: MemberViewModel;
  topology: RuntimeTopologyViewModel | null;
  backend: GlobalBackendItem | undefined;
  observe: ObserveTarget | undefined;
  projectId?: string | null;
  onWarm?: (target?: import("../VariantPanel").VariantWarmTarget) => void;
  isWarming?: boolean;
}

export function BackendInstanceRow({
  pool,
  member,
  topology,
  backend,
  observe,
  projectId,
  onWarm,
  isWarming,
}: BackendInstanceRowProps): ReactNode {
  const [detailOpen, setDetailOpen] = useState(false);
  const runtime = member.runtime;
  const health = deriveHealthPresentation(backend, observe);
  const compute = observe?.compute ?? backend?.health_meta?.compute ?? null;
  const url = backend?.url ?? observe?.url ?? "—";
  const hasWindowMetrics = [
    runtime?.selection_count_window,
    runtime?.rejection_count_window,
    runtime?.p95_ms,
    runtime?.error_rate,
    runtime?.last_selected_at,
  ].some((value) => value != null);

  return (
    <article className="rounded-lg border border-border bg-background px-3.5 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
            <Server className="size-4" strokeWidth={1.6} aria-hidden="true" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant={health.variant} dot>
                {health.label}
              </Badge>
              <h5 className="truncate text-xs font-semibold" title={member.name}>
                {member.name}
              </h5>
              <RuntimeStatusBadge axis="routing" value={member.routing} />
              {member.weight != null && <Badge variant="outline">权重 {member.weight}</Badge>}
              {/* 容量 / 驻留在首层以带标签字段呈现（plan §4.2 成员四条独立信息）。 */}
              <span className="inline-flex items-center gap-1 text-2xs text-muted-foreground">
                并发
                <span className="font-medium text-foreground tabular-nums">
                  {runtime?.route_inflight == null ? "—" : runtime.route_inflight}
                </span>
                <span aria-hidden="true">/</span>
                <span>未声明</span>
              </span>
              <span className="inline-flex items-center gap-1 text-2xs text-muted-foreground">
                驻留
                <ResidencyInline backend={backend} observe={observe} />
              </span>
              {!hasWindowMetrics && <Badge variant="outline">{NO_METRICS_LABEL}</Badge>}
            </div>
            <div className="mt-1 truncate font-mono text-2xs text-muted-foreground" title={url}>
              {url}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-1">
          <Button size="xs" onClick={() => setDetailOpen(true)} title="查看实例详情">
            详情
          </Button>
          <LifecycleActions
            poolId={pool.id}
            member={member}
            topology={topology}
            projectId={projectId}
            compact
          />
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <InstanceMetric
          icon={Gauge}
          label="当前 inflight"
          value={runtime?.route_inflight == null ? "—" : String(runtime.route_inflight)}
          detail="并发上限未声明"
        />
        <InstanceMetric
          icon={Waypoints}
          label="窗口选择"
          value={
            runtime?.selection_count_window == null ? "—" : String(runtime.selection_count_window)
          }
          detail={`拒绝 ${runtime?.rejection_count_window == null ? "—" : runtime.rejection_count_window}`}
        />
        <InstanceMetric
          icon={Gauge}
          label="P95 延迟"
          value={runtime?.p95_ms == null ? "—" : `${runtime.p95_ms} ms`}
          detail={`错误率 ${runtime?.error_rate == null ? "—" : `${(runtime.error_rate * 100).toFixed(2)}%`}`}
        />
        <InstanceMetric
          icon={Clock3}
          label="最近选择"
          value={formatShortTime(runtime?.last_selected_at ?? null)}
          detail={runtime?.last_selected_at ? "路由账本记录" : "暂无选择记录"}
        />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-border pt-3">
        <span title={backend?.last_checked_at ?? undefined}>
          <Badge variant={health.variant}>{health.detail}</Badge>
        </span>
        {isCpuFallback(compute) && (
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="warning">CPU 回退</Badge>
              </TooltipTrigger>
              <TooltipContent side="top">配置了 GPU 但已静默退回 CPU 推理</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
        {backend?.gpu_resource_id && (
          <Badge variant="outline" className="max-w-full font-mono">
            <span className="truncate" title={backend.gpu_resource_id}>
              {backend.gpu_resource_id}
            </span>
          </Badge>
        )}
        {runtime?.circuit_open && <Badge variant="danger">熔断</Badge>}
        <span className="ml-auto text-2xs text-muted-foreground">
          最近探活 {formatShortTime(backend?.last_checked_at ?? null)}
        </span>
      </div>

      <InstanceDetailSheet
        open={detailOpen}
        onOpenChange={setDetailOpen}
        member={member}
        pool={pool}
        topology={topology}
        backend={backend}
        observe={observe}
        projectId={projectId}
        onWarm={onWarm}
        isWarming={isWarming}
      />
    </article>
  );
}

type HealthBadgeVariant = "success" | "warning" | "danger" | "outline";

/**
 * 驻留首层摘要：可信驻留显示状态，未知如实显示「未知」，
 * 不把缺失画成空闲（plan §4.2 / ADR-0051）。
 */
function ResidencyInline({
  backend,
  observe,
}: {
  backend: GlobalBackendItem | undefined;
  observe: ObserveTarget | undefined;
}): ReactNode {
  const hasDirectResidency = observe?.residency != null;
  const rawResidency: unknown = hasDirectResidency
    ? observe.residency
    : backend?.health_meta?.residency;
  const residency = parseResidency(rawResidency);
  if (!residency || residency.state === "unknown") {
    return <span className="font-medium text-muted-foreground">未知</span>;
  }
  const trusted = hasDirectResidency
    ? observe?.ok === true
    : backend != null && isFreshCachedHealth(backend.state, backend.last_checked_at);
  if (!trusted) {
    return <span className="font-medium text-muted-foreground">{residency.state}（未核实）</span>;
  }
  return <span className="font-medium text-foreground">{residency.state}</span>;
}

interface HealthPresentation {
  variant: HealthBadgeVariant;
  label: string;
  detail: string;
}

function deriveHealthPresentation(
  backend: GlobalBackendItem | undefined,
  observe: ObserveTarget | undefined,
): HealthPresentation {
  if (observe) {
    return observe.ok
      ? { variant: "success", label: "在线", detail: "实时探活正常" }
      : { variant: "danger", label: "离线", detail: "实时探活失败" };
  }

  if (!backend) {
    return { variant: "outline", label: "状态未知", detail: "暂无探活数据" };
  }

  if (backend.state === "connected") {
    return isFreshCachedHealth(backend.state, backend.last_checked_at)
      ? { variant: "warning", label: "缓存在线", detail: "缓存探活，非实时" }
      : { variant: "warning", label: "状态过期", detail: "探活状态已过期" };
  }

  return {
    variant: "warning",
    label: "缓存异常",
    detail: `注册状态 ${backend.state}，无实时探活`,
  };
}

function InstanceMetric({
  icon: MetricIcon,
  label,
  value,
  detail,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  detail: string;
}): ReactNode {
  return (
    <div className="flex min-w-0 items-start gap-2 rounded-md bg-muted/35 px-2.5 py-2">
      <MetricIcon
        className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
        strokeWidth={1.6}
        aria-hidden="true"
      />
      <div className="min-w-0">
        <div className="text-2xs text-muted-foreground">{label}</div>
        <div className="mt-0.5 truncate text-xs font-semibold tabular-nums">{value}</div>
        <div className="truncate text-2xs text-muted-foreground" title={detail}>
          {detail}
        </div>
      </div>
    </div>
  );
}

function formatShortTime(iso: string | null): string {
  if (!iso) return "—";
  const timestamp = new Date(iso);
  if (Number.isNaN(timestamp.getTime())) return iso;
  return timestamp.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
