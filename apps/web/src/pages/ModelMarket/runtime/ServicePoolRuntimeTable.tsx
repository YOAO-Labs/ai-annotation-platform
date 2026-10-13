/**
 * v0.23.4 · service-pool runtime overview.
 *
 * 模型市场多 TAB UI 优化 · 阶段三 (plan §4.2)：池卡是两行轻量字段带——
 * 第一行身份（展开按钮 + 名称 + 短 ID/策略 + 健康、路由、新鲜度分开显示），
 * 第二行带标签的字段（可路由/总实例、并发、驻留、CPU 回退、流量）。去掉旧版
 * 四个内层小卡与悬浮位移动画；无流量指标的池只显示一条「暂无路由指标」，
 * 缺失字段不在首层冒充数值，仍保留下钻处的未知语义；驻留已知性同样取自
 * 「可信且可解析」的状态——`{state:"unknown"}`、畸形载荷或未核实来源都显示
 * 「未知」，不把显式未知的数据折算成确定的 0。正常池保持 topology 稳定
 * 顺序，不因轮询数值重排；展开成员时跨两列，成员与维护动作沿用既有组件。
 */
import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, Server } from "lucide-react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/shadcn/ui/empty";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/ui/tooltip";
import type { GlobalBackendItem, ObserveTarget } from "@/api/adminMlIntegrations";
import {
  NO_METRICS_LABEL,
  derivePoolEffectiveRouting,
  type FreshnessViewModel,
  type PoolViewModel,
  type RuntimeTopologyViewModel,
} from "../runtimeTopology";
import { RuntimeStatusBadge } from "./RuntimeStatusBadge";
import { TrafficDistributionBar, type TrafficSegment } from "./TrafficDistributionBar";
import { BackendInstanceRow } from "./BackendInstanceRow";
import type { VariantWarmTarget } from "../VariantPanel";
import {
  isActiveResidency,
  isFreshCachedHealth,
  parseResidency,
  residencyStateToAxis,
} from "./parseResidency";
import { formatShortId } from "../registry/registryShared";

/** Per-member lookups the orchestrator pre-computes from /all + /observe. */
export interface MemberLookups {
  backend: GlobalBackendItem | undefined;
  observe: ObserveTarget | undefined;
  /** Project id enabling warmup; undefined when no project binding. */
  projectId: string | undefined;
}

export interface ServicePoolRuntimeTableProps {
  topology: RuntimeTopologyViewModel;
  /** registry_id → member lookups (backend / observe / projectId). */
  lookup: (registryId: string) => MemberLookups;
  /** Warmup callback delegated to the orchestrator (owns the hooks). */
  onWarm?: (registryId: string, target?: VariantWarmTarget) => void;
  /** Set of registry_ids currently warming (disables warmup buttons). */
  warming?: Set<string>;
}

export function ServicePoolRuntimeTable({
  topology,
  lookup,
  onWarm,
  warming,
}: ServicePoolRuntimeTableProps): ReactNode {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  const toggle = (poolId: string) => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(poolId)) next.delete(poolId);
      else next.add(poolId);
      return next;
    });
  };

  if (topology.pools.length === 0) {
    return (
      <Empty className="border border-border bg-muted/20">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Server strokeWidth={1.6} aria-hidden="true" />
          </EmptyMedia>
          <EmptyTitle>暂无服务池</EmptyTitle>
          <EmptyDescription>
            在“注册管理”新建服务池并加入实例后，运行时状态会显示在这里。
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <section className="grid gap-3 xl:grid-cols-2" aria-label="服务池运行时列表">
      {topology.pools.map((pool) => {
        const isOpen = expanded.has(pool.id);
        const residentCount = pool.members.filter((member) => {
          const { residency, trusted } = resolveMemberResidency(lookup(member.registry_id));
          return isActiveResidency(residency, trusted);
        }).length;
        // 已知 = 至少一个成员给出可信且可解析的驻留状态；`{state:"unknown"}`、
        // 畸形载荷或未核实来源都不能把「未知」折算成确定的 0（plan §4.2 / ADR-0051）。
        const residencyKnown = pool.members.some((member) => {
          const { residency, trusted } = resolveMemberResidency(lookup(member.registry_id));
          if (!trusted) return false;
          const parsed = parseResidency(residency);
          return parsed != null && residencyStateToAxis(parsed.state) !== "unknown";
        });
        const cpuFallbackCount = pool.members.filter((member) => {
          const { backend, observe } = lookup(member.registry_id);
          const compute = observe?.compute ?? backend?.health_meta?.compute ?? null;
          return isCpuFallbackInline(compute);
        }).length;
        const computeKnown = pool.members.some((member) => {
          const { backend, observe } = lookup(member.registry_id);
          return (observe?.compute ?? backend?.health_meta?.compute ?? null) != null;
        });
        const trafficSegments: TrafficSegment[] = pool.members.map((member) => ({
          instance_id: member.registry_id,
          instance_name: member.name,
          count: member.runtime?.selection_count_window ?? null,
        }));
        const trafficTotal =
          pool.members.reduce(
            (sum, member) => sum + (member.runtime?.selection_count_window ?? 0),
            0,
          ) || null;

        return (
          <PoolRuntimeCard
            key={pool.id}
            pool={pool}
            isOpen={isOpen}
            onToggle={() => toggle(pool.id)}
            topology={topology}
            lookup={lookup}
            onWarm={onWarm}
            warming={warming}
            trafficSegments={trafficSegments}
            trafficTotal={trafficTotal}
            residentCount={residentCount}
            residencyKnown={residencyKnown}
            cpuFallbackCount={cpuFallbackCount}
            cpuFallbackKnown={computeKnown}
          />
        );
      })}
    </section>
  );
}

/**
 * Resolve one member's residency payload and whether its source is trusted
 * (fresh direct probe / fresh cached health). This is the same resolution the
 * instance rows render, so pool-level counts and row-level states can never
 * disagree about what "known" means.
 */
function resolveMemberResidency({ backend, observe }: MemberLookups): {
  residency: unknown;
  trusted: boolean;
} {
  const hasDirectResidency = observe?.residency != null;
  const residency: unknown = hasDirectResidency
    ? observe.residency
    : backend?.health_meta?.residency;
  const trusted = hasDirectResidency
    ? observe?.ok === true
    : backend != null && isFreshCachedHealth(backend.state, backend.last_checked_at);
  return { residency, trusted };
}

function PoolRuntimeCard({
  pool,
  isOpen,
  onToggle,
  topology,
  lookup,
  onWarm,
  warming,
  trafficSegments,
  trafficTotal,
  residentCount,
  residencyKnown,
  cpuFallbackCount,
  cpuFallbackKnown,
}: {
  pool: PoolViewModel;
  isOpen: boolean;
  onToggle: () => void;
  topology: RuntimeTopologyViewModel;
  lookup: (registryId: string) => MemberLookups;
  onWarm?: (registryId: string, target?: VariantWarmTarget) => void;
  warming?: Set<string>;
  trafficSegments: TrafficSegment[];
  trafficTotal: number | null;
  residentCount: number;
  residencyKnown: boolean;
  cpuFallbackCount: number;
  cpuFallbackKnown: boolean;
}): ReactNode {
  // 健康与路由分开显示（plan §4.2 / ADR-0051 四状态轴）。
  const effectiveRouting = derivePoolEffectiveRouting(pool);

  return (
    <article
      className={cn(
        "overflow-hidden rounded-xl border border-border bg-card",
        isOpen && "border-primary/30 shadow-sm xl:col-span-2",
      )}
    >
      <div className="flex flex-col gap-2 px-4 py-3">
        {/* 第 1 行：身份 + 健康 / 路由 / 新鲜度（独立轴）。 */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <Button
            variant="ghost"
            size="xs"
            className="size-6 p-0"
            onClick={onToggle}
            aria-expanded={isOpen}
            aria-label={isOpen ? "收起服务池成员" : "展开服务池成员"}
            title={isOpen ? "收起" : "展开"}
          >
            {isOpen ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
          </Button>
          <Server
            className="size-4 shrink-0 text-muted-foreground"
            strokeWidth={1.6}
            aria-hidden="true"
          />
          <div className="flex min-w-0 items-center gap-1.5">
            <h4 className="truncate text-sm font-semibold tracking-tight">{pool.name}</h4>
            {!pool.enabled && <Badge variant="outline">已停用</Badge>}
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="mono cursor-pointer text-2xs text-muted-foreground hover:text-foreground">
                {formatShortId(pool.id)}
              </span>
            </TooltipTrigger>
            <TooltipContent>{pool.id}</TooltipContent>
          </Tooltip>
          <span className="text-2xs text-muted-foreground">{pool.routing_policy}</span>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
            <RuntimeStatusBadge axis="health" value={pool.status} />
            <RuntimeStatusBadge axis="routing" value={effectiveRouting} />
            <FreshnessSummary sources={topology.sources} />
          </div>
        </div>

        {/* 第 2 行：带标签的字段带（可路由 / 并发 / 驻留 / CPU 回退 / 流量）。 */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md bg-muted/35 px-3 py-2 text-xs">
          <PoolField label="可路由">
            <span className="text-status-positive">{pool.availability.routable}</span>
            <span className="text-muted-foreground"> / {pool.availability.total}</span>
            {(pool.availability.draining > 0 || pool.availability.offline > 0) && (
              <span className="ml-1 text-2xs text-muted-foreground">
                {[
                  pool.availability.draining > 0 ? `${pool.availability.draining} 停流` : null,
                  pool.availability.offline > 0 ? `${pool.availability.offline} 离线` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            )}
          </PoolField>
          <PoolField label="并发">
            {pool.capacity.inflight != null ? (
              <>
                <span className="tabular-nums">{pool.capacity.inflight}</span>
                <span className="text-muted-foreground"> / {pool.capacity.limit ?? "未声明"}</span>
              </>
            ) : (
              <span className="text-muted-foreground">{NO_METRICS_LABEL}</span>
            )}
          </PoolField>
          {pool.capacity.saturated && (
            <Badge variant="danger">
              <span>熔断</span>
            </Badge>
          )}
          <PoolField label="驻留">
            {residencyKnown ? (
              `${residentCount} 个`
            ) : (
              <span className="text-muted-foreground">未知</span>
            )}
          </PoolField>
          <PoolField label="CPU 回退">
            {cpuFallbackKnown ? (
              cpuFallbackCount > 0 ? (
                <span className="text-status-caution">{cpuFallbackCount} 个</span>
              ) : (
                <span className="text-muted-foreground">0</span>
              )
            ) : (
              <span className="text-muted-foreground">—</span>
            )}
          </PoolField>
          <PoolField label="流量">
            {/* 同一池无流量指标时只显示一条「暂无路由指标」（plan §4.2），
                不画空分布条冒充数据。 */}
            {trafficTotal != null ? (
              <TrafficDistributionBar segments={trafficSegments} total={trafficTotal} />
            ) : (
              <span className="text-muted-foreground">{NO_METRICS_LABEL}</span>
            )}
          </PoolField>
        </div>

        {pool.status_reason_codes.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-2xs text-muted-foreground">
            <Badge variant={pool.status === "healthy" ? "outline" : "warning"}>状态依据</Badge>
            <span className="font-mono">{pool.status_reason_codes.join(" · ")}</span>
          </div>
        )}
      </div>

      {isOpen && (
        <div className="border-t border-border bg-muted/15 px-3 pb-3 pt-3">
          <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="text-xs font-semibold">实例明细</div>
            <div className="text-2xs text-muted-foreground">
              {pool.members.length} 个实例 · 权重与路由操作保持实时
            </div>
          </div>
          {pool.members.length > 0 ? (
            <div className="flex flex-col gap-2">
              {pool.members.map((member) => {
                const { backend, observe, projectId } = lookup(member.registry_id);
                return (
                  <BackendInstanceRow
                    key={member.registry_id}
                    pool={pool}
                    member={member}
                    topology={topology}
                    backend={backend}
                    observe={observe}
                    projectId={projectId}
                    onWarm={onWarm ? (target) => onWarm(member.registry_id, target) : undefined}
                    isWarming={warming?.has(member.registry_id) ?? false}
                  />
                );
              })}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-border bg-background/70 px-4 py-6 text-center text-xs text-muted-foreground">
              该服务池暂无成员实例
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function PoolField({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span className="shrink-0 text-2xs text-muted-foreground">{label}</span>
      <span className="inline-flex min-w-0 items-center gap-1 font-medium">{children}</span>
    </span>
  );
}

function FreshnessSummary({ sources }: { sources: FreshnessViewModel[] }): ReactNode {
  if (sources.length === 0) {
    return <Badge variant="outline">新鲜度未知</Badge>;
  }
  const staleCount = sources.filter((source) => source.stale).length;
  const title = sources
    .map((source) => `${source.label}: ${source.stale ? "陈旧" : "新鲜"}`)
    .join("；");
  if (staleCount > 0) {
    return (
      <span title={title}>
        <Badge variant="warning">
          {sources.length - staleCount}/{sources.length} 新鲜
        </Badge>
      </span>
    );
  }
  return (
    <span title={title}>
      <Badge variant="success">数据新鲜</Badge>
    </span>
  );
}

function isCpuFallbackInline(
  compute: import("@/utils/mlBackendCompute").MLBackendCompute | null | undefined,
): boolean {
  if (!compute || compute.cpu_fallback_supported === false) return false;
  const normalize = (value: string | undefined | null) => value?.trim().toLowerCase() ?? "";
  const configured = normalize(compute.configured_device);
  const configuredForGpu =
    configured === "gpu" || configured === "cuda" || configured.startsWith("cuda:");
  if (!configuredForGpu) return false;
  return (
    normalize(compute.effective_device) === "cpu" ||
    normalize(compute.effective_provider) === "cpuexecutionprovider"
  );
}
