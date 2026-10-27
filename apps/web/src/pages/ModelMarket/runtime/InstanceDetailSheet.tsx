import { CopyableError } from "@/components/ui/CopyableError";
/**
 * v0.23.4 · instance detail Sheet (plan §6.2 / §10).
 *
 * Opened from a member row's "详情" button. Pushes the full model residency
 * pool, cache stats, variant catalog, raw builder/borrower/generation/gate/
 * identity fields and full diagnostics text off the main table. Also hosts
 * the warmup path via `VariantPanel` (kept live per plan).
 *
 * Plan Appendix A.1: residency axis + identity fields are display-only here;
 * the table row keeps the compact summary.
 */
import { useMemo, type ReactNode } from "react";

import type { GlobalBackendItem, ObserveTarget } from "@/api/adminMlIntegrations";
import { Badge } from "@/components/ui/Badge";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/shadcn/ui/sheet";
import { ScrollArea } from "@/components/shadcn/ui/scroll-area";
import { isCpuFallback } from "@/utils/mlBackendCompute";
import { VariantPanel, type VariantWarmTarget } from "../VariantPanel";
import {
  NO_METRICS_LABEL,
  type MemberViewModel,
  type PoolViewModel,
  type RuntimeTopologyViewModel,
} from "../runtimeTopology";
import { effectiveGpuLoaded, isFreshCachedHealth, parseResidency } from "./parseResidency";
import { LifecycleActions } from "./LifecycleActions";

export interface InstanceDetailSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  member: MemberViewModel;
  pool: PoolViewModel;
  topology: RuntimeTopologyViewModel | null;
  /** Registry entry matched by registry_id (from /all). Supplies GPU claim. */
  backend: GlobalBackendItem | undefined;
  /** Direct-probe target matched by registry_id (from /observe). */
  observe: ObserveTarget | undefined;
  /** Project id for warmup; undefined when no project binding exists. */
  projectId?: string | null;
  /** Warmup callback (delegated to the orchestrator which owns the hooks). */
  onWarm?: (target?: VariantWarmTarget) => void;
  isWarming?: boolean;
}

export function InstanceDetailSheet({
  open,
  onOpenChange,
  member,
  pool,
  topology,
  backend,
  observe,
  projectId,
  onWarm,
  isWarming = false,
}: InstanceDetailSheetProps): ReactNode {
  const residency = useMemo(() => {
    const raw = observe?.residency ?? backend?.health_meta?.residency ?? null;
    return parseResidency(raw);
  }, [observe?.residency, backend?.health_meta?.residency]);

  const trusted = useMemo(() => {
    const hasDirect = observe?.ok === true && observe?.residency != null;
    if (hasDirect) return true;
    return backend ? isFreshCachedHealth(backend.state, backend.last_checked_at) : false;
  }, [observe, backend]);

  const gpuLoaded = residency ? effectiveGpuLoaded(residency, trusted) : null;
  const modelVersion = observe?.model_version ?? backend?.health_meta?.model_version;
  const compute = observe?.compute ?? backend?.health_meta?.compute ?? null;
  const cacheHitRate = observe?.cache?.hit_rate ?? backend?.health_meta?.cache?.hit_rate;

  const rt = member.runtime;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full flex-col gap-0 p-0 sm:max-w-xl md:max-w-2xl">
        <SheetHeader className="border-b border-border">
          <div className="flex items-center gap-2">
            <SheetTitle className="text-base">{member.name}</SheetTitle>
            <Badge variant="outline" className="mono">
              {member.traffic_state}
            </Badge>
            <CopyValueButton value={member.registry_id} kind="id" label="复制实例 ID" />
          </div>
          <SheetDescription className="mono break-all text-2xs">
            {backend?.url ?? observe?.url ?? "—"}
          </SheetDescription>
        </SheetHeader>

        <ScrollArea className="flex-1">
          <div className="flex flex-col gap-4 p-4">
            {/* Lifecycle actions (drain/resume/unload gated, health, warmup) */}
            <section className="flex flex-col gap-2">
              <SectionTitle>生命周期操作</SectionTitle>
              <LifecycleActions
                poolId={pool.id}
                member={member}
                topology={topology}
                projectId={projectId}
                compact={false}
              />
              {!projectId && (
                <div className="text-2xs text-muted-foreground">
                  尚未启用到项目；全局健康检查和卸载可用，预热需先建立项目启用关系。
                </div>
              )}
            </section>

            {/* Routing snapshot */}
            <section className="flex flex-col gap-2">
              <SectionTitle>路由与容量</SectionTitle>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                <DetailRow label="接流状态" value={member.traffic_state} />
                <DetailRow
                  label="权重"
                  value={member.weight == null ? "—" : String(member.weight)}
                />
                <DetailRow
                  label="当前并发"
                  value={rt?.route_inflight == null ? NO_METRICS_LABEL : String(rt.route_inflight)}
                />
                <DetailRow label="最大并发" value="未声明" muted />
                <DetailRow
                  label="窗口选择"
                  value={
                    rt?.selection_count_window == null
                      ? NO_METRICS_LABEL
                      : String(rt.selection_count_window)
                  }
                />
                <DetailRow
                  label="窗口拒绝"
                  value={
                    rt?.rejection_count_window == null
                      ? NO_METRICS_LABEL
                      : String(rt.rejection_count_window)
                  }
                />
                <DetailRow
                  label="P95"
                  value={rt?.p95_ms == null ? NO_METRICS_LABEL : `${rt.p95_ms}ms`}
                />
                <DetailRow
                  label="错误率"
                  value={
                    rt?.error_rate == null
                      ? NO_METRICS_LABEL
                      : `${(rt.error_rate * 100).toFixed(2)}%`
                  }
                />
                <DetailRow label="最近选中" value={rt?.last_selected_at ?? NO_METRICS_LABEL} />
                <DetailRow
                  label="熔断"
                  value={
                    rt?.circuit_open == null ? NO_METRICS_LABEL : rt.circuit_open ? "是" : "否"
                  }
                />
              </div>
            </section>

            {/* GPU claim summary */}
            {backend && <GpuClaimBlock backend={backend} />}

            {/* Runtime metrics (model version, gpu info, pool counts, cache) */}
            <section className="flex flex-col gap-2">
              <SectionTitle>运行时观测</SectionTitle>
              {isCpuFallback(compute) && (
                <span title="配置了 GPU 但已静默退回 CPU 推理">
                  <Badge variant="warning">⚠ CPU 回退</Badge>
                </span>
              )}
              <RuntimeMetricsBlock
                modelVersion={modelVersion}
                gpuInfo={observe?.gpu_info ?? backend?.health_meta?.gpu_info}
                pool={observe?.pool ?? backend?.health_meta?.pool}
                videoPool={observe?.video_pool ?? backend?.health_meta?.video_pool}
                cacheHitRate={cacheHitRate}
              />
            </section>

            {/* Residency detail (full builder/borrower/gate/identity) */}
            <section className="flex flex-col gap-2">
              <SectionTitle>模型驻留（residency）</SectionTitle>
              <ResidencyDetail residency={residency} gpuLoaded={gpuLoaded} trusted={trusted} />
            </section>

            {/* Variant warmup panel */}
            {projectId && backend && (
              <VariantPanel
                projectId={projectId}
                backend={{
                  id: member.registry_id,
                  health_meta: backend.health_meta,
                }}
                onWarm={onWarm ?? (() => undefined)}
                isWarming={isWarming}
              />
            )}

            {/* Raw identity + diagnostics text */}
            <section className="flex flex-col gap-2">
              <SectionTitle>原始字段</SectionTitle>
              <RawFieldsBlock
                residency={residency}
                identityResourceId={residency?.identityResourceId ?? null}
                backend={backend}
              />
            </section>
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}

function SectionTitle({ children }: { children: ReactNode }): ReactNode {
  return <div className="text-xs font-semibold text-muted-foreground">{children}</div>;
}

function DetailRow({
  label,
  value,
  muted,
}: {
  label: string;
  value: ReactNode;
  muted?: boolean;
}): ReactNode {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className={muted ? "text-muted-foreground" : "text-foreground"}>{value}</span>
    </div>
  );
}

function GpuClaimBlock({ backend }: { backend: GlobalBackendItem }): ReactNode {
  const config = backend.gpu_config;
  if (!config) {
    return (
      <section className="flex flex-col gap-2">
        <SectionTitle>GPU claim</SectionTitle>
        <div className="text-xs text-muted-foreground">GPU 配置仅超级管理员可见</div>
      </section>
    );
  }
  const status = config.status ?? "ok";
  const variant =
    status === "blocker" || status === "critical"
      ? "danger"
      : status === "warning"
        ? "warning"
        : status === "info"
          ? "accent"
          : "outline";
  return (
    <section className="flex flex-col gap-2">
      <SectionTitle>GPU claim</SectionTitle>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge variant={variant}>GPU claim {status}</Badge>
        <span className="mono text-muted-foreground">{backend.gpu_resource_id ?? "无声明"}</span>
        {backend.gpu_resource_id && (
          <>
            <span className="text-muted-foreground">
              预算 {backend.vram_budget_mb ?? "—"}/{config.allocatable_mb ?? "—"} MiB
            </span>
            <span className="text-muted-foreground">优先级 {backend.eviction_priority ?? "—"}</span>
            <span className="text-muted-foreground">
              {config.desired_mode ?? "off"}→{config.effective_mode ?? "off"}
            </span>
          </>
        )}
      </div>
      {(config.diagnostics?.length ?? 0) > 0 && (
        <div className="flex flex-col gap-1 text-2xs text-muted-foreground">
          {config.diagnostics!.map((d, i) => (
            <span key={`${d.code}-${i}`}>
              <CopyableError message={d.message} />
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

function RuntimeMetricsBlock({
  modelVersion,
  gpuInfo,
  pool,
  videoPool,
  cacheHitRate,
}: {
  modelVersion?: string | null;
  gpuInfo?: import("@/api/adminMlIntegrations").GpuInfo | null;
  pool?: {
    cap?: number;
    current_size?: number;
    loaded_keys?: unknown[];
    loaded_variants?: unknown[];
  } | null;
  videoPool?: {
    cap?: number;
    current_size?: number;
    loaded_keys?: unknown[];
    loaded_variants?: string[];
    active_sessions?: number;
  } | null;
  cacheHitRate?: number;
}): ReactNode {
  const loadedCount =
    pool?.current_size ?? pool?.loaded_keys?.length ?? pool?.loaded_variants?.length ?? 0;
  const videoLoadedCount =
    videoPool?.current_size ??
    videoPool?.loaded_keys?.length ??
    videoPool?.loaded_variants?.length ??
    0;
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
      {modelVersion && <span className="mono">{modelVersion}</span>}
      {gpuInfo?.memory_used_mb != null && gpuInfo?.memory_total_mb != null && (
        <span title="卡号 · 整卡已用/总显存 · 本容器自用（torch 保留）">
          GPU{gpuInfo.device_index ?? 0} {gpuInfo.memory_used_mb}/{gpuInfo.memory_total_mb} MB
          {gpuInfo.process_memory_mb != null && ` · 自用 ${gpuInfo.process_memory_mb} MB`}
        </span>
      )}
      {(gpuInfo?.physical_device_token || gpuInfo?.mig_uuid || gpuInfo?.device_uuid) && (
        <span className="inline-flex items-center gap-1">
          设备
          <CopyValueButton
            value={gpuInfo.physical_device_token || gpuInfo.mig_uuid || gpuInfo.device_uuid}
            kind="id"
            label="复制物理设备 ID"
          />
        </span>
      )}
      {cacheHitRate != null && <span>cache {(cacheHitRate * 100).toFixed(1)}%</span>}
      <span>
        图像池 {loadedCount}
        {pool?.cap != null && `/${pool.cap}`}
      </span>
      {videoPool && (
        <span>
          视频池 {videoLoadedCount}
          {videoPool.cap != null && `/${videoPool.cap}`}
          {videoPool.active_sessions != null && ` · ${videoPool.active_sessions} 会话`}
        </span>
      )}
      {!modelVersion && !gpuInfo && !pool && !videoPool && cacheHitRate == null && (
        <span>{NO_METRICS_LABEL}</span>
      )}
    </div>
  );
}

function ResidencyDetail({
  residency,
  gpuLoaded,
  trusted,
}: {
  residency: ReturnType<typeof parseResidency>;
  gpuLoaded: boolean | null;
  trusted: boolean;
}): ReactNode {
  if (!residency) {
    return (
      <div className="text-xs text-muted-foreground">
        未上报 residency · {trusted ? "缓存 health（新鲜）" : "缓存 health（过期或未知）"}
      </div>
    );
  }
  const unmanaged = trusted && gpuLoaded !== false && !residency.generation;
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-border bg-muted/30 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge
          variant={
            residency.state === "resident"
              ? "success"
              : residency.state === "unknown"
                ? "warning"
                : "outline"
          }
        >
          {residency.state}
        </Badge>
        <Badge
          variant={gpuLoaded === true ? "warning" : gpuLoaded === false ? "success" : "outline"}
        >
          {gpuLoaded === true ? "GPU 仍驻留" : gpuLoaded === false ? "GPU 空" : "GPU 驻留未知"}
        </Badge>
        {unmanaged && <Badge variant="warning">unmanaged</Badge>}
        <span className="text-2xs text-muted-foreground">
          {trusted ? "缓存 health（新鲜）" : "缓存 health（过期或未知）"}
        </span>
      </div>
      {!trusted && (
        <div className="text-2xs text-status-caution">
          residency 证据已过期或来源未知，仅展示原始状态，不据此判断 GPU 空闲。
        </div>
      )}
      {residency.malformed && (
        <div className="text-2xs text-status-caution">
          residency 含畸形字段，已安全归一化且不参与 GPU 空闲判断。
        </div>
      )}
      {trusted && residency.gpuLoaded === false && gpuLoaded !== false && !residency.malformed && (
        <div className="text-2xs text-status-caution">
          gpu_loaded=false 缺少全 pool、builder 或 borrower 空闲证据，按未知处理。
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3 text-2xs text-muted-foreground">
        <span>active {residency.activeRequests ?? "—"}</span>
        <span>builders {residency.builders ?? "—"}</span>
        <span>borrowers {residency.borrowers ?? "—"}</span>
        <span>draining {residency.draining == null ? "—" : String(residency.draining)}</span>
        <span>evictable {residency.evictable == null ? "—" : String(residency.evictable)}</span>
        <span>gate {residency.lifecycleGate ?? "—"}</span>
        <span>generation {residency.generation ?? "—"}</span>
        {residency.identityResourceId && (
          <span className="mono">identity {residency.identityResourceId}</span>
        )}
      </div>
      {residency.pools.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-2xs text-muted-foreground">
          {residency.pools.map((p) => (
            <span key={p.id} className="mono">
              {p.id}:{" "}
              {p.resident === true && trusted
                ? "GPU"
                : p.resident === false && trusted && !residency.malformed
                  ? "empty"
                  : "unknown"}
              {p.device ? ` · ${p.device}` : ""}
              {p.provider ? ` · ${p.provider}` : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function RawFieldsBlock({
  residency,
  identityResourceId,
  backend,
}: {
  residency: ReturnType<typeof parseResidency>;
  identityResourceId: string | null;
  backend: GlobalBackendItem | undefined;
}): ReactNode {
  const raw = useMemo(() => {
    const r: Record<string, unknown> = {};
    if (residency) {
      r.state = residency.state;
      r.gpu_loaded = residency.gpuLoaded;
      r.active_requests = residency.activeRequests;
      r.builders = residency.builders;
      r.borrowers = residency.borrowers;
      r.draining = residency.draining;
      r.evictable = residency.evictable;
      r.lifecycle_gate = residency.lifecycleGate;
      r.generation = residency.generation;
    }
    if (identityResourceId) r.identity_gpu_resource_id = identityResourceId;
    if (backend?.gpu_resource_id) r.gpu_resource_id = backend.gpu_resource_id;
    if (backend?.vram_budget_mb != null) r.vram_budget_mb = backend.vram_budget_mb;
    if (backend?.eviction_priority != null) r.eviction_priority = backend.eviction_priority;
    return r;
  }, [residency, identityResourceId, backend]);

  return (
    <pre className="mono max-h-48 overflow-auto rounded-md border border-border bg-muted/40 p-2 text-2xs text-muted-foreground">
      {JSON.stringify(raw, null, 2)}
    </pre>
  );
}
