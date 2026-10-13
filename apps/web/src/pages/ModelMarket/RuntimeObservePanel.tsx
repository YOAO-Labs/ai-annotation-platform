/**
 * v0.23.4 · runtime observation orchestrator (plan §6.2 / §9.3).
 *
 * Pure shell: owns three queries (topology, runtime-snapshot, observe) and
 * delegates rendering to the runtime/ components. No URL-join — pool/member
 * join is done by {@link mergeTopologyAndSnapshot}. One user-visible refresh
 * button refetches all three in parallel; auto-refresh ticks runtime-snapshot
 * only. Partial source failure is surfaced as a banner, never an error block.
 *
 * Super-admin-only: the parent tab is hidden for non-super-admins (kept
 * assumption); this panel does not re-gate.
 */
import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronRight, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Switch } from "@/components/ui/Switch";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@/components/shadcn/ui/alert";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/shadcn/ui/card";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { TooltipProvider } from "@/components/shadcn/ui/tooltip";
import {
  adminMlIntegrationsApi,
  type GlobalBackendItem,
  type ObserveTarget,
} from "@/api/adminMlIntegrations";
import { mlBackendsApi, type MLBackendVariant } from "@/api/ml-backends";
import { mergeTopologyAndSnapshot, type RuntimeTopologyViewModel } from "./runtimeTopology";
import { ServicePoolRuntimeTable } from "./runtime/ServicePoolRuntimeTable";
import { EnvOnlyContainerCard } from "./runtime/EnvOnlyContainerCard";
import { FreshnessIndicator } from "./runtime/FreshnessIndicator";
import { GlobalBackendFormModal } from "./GlobalBackendFormModal";
import type { VariantWarmTarget } from "./VariantPanel";
import { invalidateRuntimeQueries } from "./runtime/LifecycleActions";

const TOPOLOGY_KEY = ["admin", "ml-integrations", "topology"] as const;
const SNAPSHOT_KEY = ["admin", "ml-integrations", "runtime-snapshot"] as const;
const OBSERVE_KEY = ["admin", "ml-integrations", "observe"] as const;

const AUTO_REFRESH_MS = 15_000;

function normalizeUrl(url: string): string {
  return url.replace(/\/+$/, "");
}

export function RuntimeObservePanel(): React.ReactElement {
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [registerOpen, setRegisterOpen] = useState(false);

  const topologyQ = useQuery({
    queryKey: [...TOPOLOGY_KEY],
    queryFn: () => adminMlIntegrationsApi.topology(),
    refetchInterval: 60_000,
  });
  const snapshotQ = useQuery({
    queryKey: [...SNAPSHOT_KEY],
    queryFn: () => adminMlIntegrationsApi.runtimeSnapshot(),
    refetchInterval: autoRefresh ? AUTO_REFRESH_MS : false,
  });
  const observeQ = useQuery({
    queryKey: [...OBSERVE_KEY],
    queryFn: () => adminMlIntegrationsApi.observe(),
    refetchInterval: 60_000,
  });
  // legacy /all + /overview — still needed for per-member backend lookup,
  // GPU claim display and the warmup projectId path (v0.23.3 pool members
  // reference registry_id; /all is the per-registry record source).
  const allQ = useQuery({
    queryKey: ["admin", "ml-integrations", "all"],
    queryFn: () => adminMlIntegrationsApi.listAll(),
    refetchInterval: 60_000,
  });
  const overviewQ = useQuery({
    queryKey: ["admin", "ml-integrations", "overview"],
    queryFn: () => adminMlIntegrationsApi.overview(),
    refetchInterval: 60_000,
  });

  const refetchAll = useCallback(() => {
    topologyQ.refetch();
    snapshotQ.refetch();
    observeQ.refetch();
    allQ.refetch();
    overviewQ.refetch();
  }, [topologyQ, snapshotQ, observeQ, allQ, overviewQ]);

  const topology = useMemo<RuntimeTopologyViewModel | null>(() => {
    if (!topologyQ.data) return null;
    return mergeTopologyAndSnapshot(topologyQ.data, snapshotQ.data ?? null);
  }, [topologyQ.data, snapshotQ.data]);

  // registry_id → GlobalBackendItem (from /all).
  const backendsById = useMemo(() => {
    const map = new Map<string, GlobalBackendItem>();
    for (const b of allQ.data?.items ?? []) map.set(b.id, b);
    return map;
  }, [allQ.data]);

  // url → ObserveTarget (normalized), for residency + env-only.
  const observeByUrl = useMemo(() => {
    const map = new Map<string, ObserveTarget>();
    for (const t of observeQ.data?.targets ?? []) map.set(normalizeUrl(t.url), t);
    return map;
  }, [observeQ.data]);

  // registry_id → ObserveTarget via its backend.url (display-only join — NOT a
  // routing-key join; observe has no pool/registry identity).
  const observeByRegistry = useMemo(() => {
    const map = new Map<string, ObserveTarget>();
    for (const b of backendsById.values()) {
      const t = observeByUrl.get(normalizeUrl(b.url));
      if (t) map.set(b.id, t);
    }
    return map;
  }, [backendsById, observeByUrl]);

  // registry_id → first enabling projectId (warmup path needs one).
  const projectByBackend = useMemo(() => {
    const map = new Map<string, string>();
    for (const project of overviewQ.data?.projects ?? []) {
      for (const backend of project.backends) {
        if (!map.has(backend.id)) map.set(backend.id, project.project_id);
      }
    }
    return map;
  }, [overviewQ.data]);

  const registeredUrls = useMemo(() => {
    const set = new Set<string>();
    for (const b of backendsById.values()) set.add(normalizeUrl(b.url));
    return set;
  }, [backendsById]);

  const envOnlyTargets = useMemo(
    () =>
      (observeQ.data?.targets ?? []).filter(
        (t) => t.registered === false && !registeredUrls.has(normalizeUrl(t.url)),
      ),
    [observeQ.data, registeredUrls],
  );

  const loading =
    topologyQ.isLoading || snapshotQ.isLoading || observeQ.isLoading || allQ.isLoading;
  const fetching =
    topologyQ.isFetching || snapshotQ.isFetching || observeQ.isFetching || allQ.isFetching;
  const hardError = topologyQ.error ?? (topology ? null : snapshotQ.error);

  const onWarm = useWarmupDispatcher(projectByBackend, backendsById);

  const lookup = useCallback(
    (registryId: string) => {
      const backend = backendsById.get(registryId);
      const observe = observeByRegistry.get(registryId);
      const projectId = projectByBackend.get(registryId);
      return { backend, observe, projectId };
    },
    [backendsById, observeByRegistry, projectByBackend],
  );

  const poolCount = topology?.pools.length ?? 0;
  const memberCount = topology?.pools.reduce((sum, p) => sum + p.members.length, 0) ?? 0;

  return (
    <TooltipProvider delayDuration={200}>
      <div className="mb-4">
        <Card className="overflow-hidden">
          <CardHeader className="py-3">
            <div className="min-w-0">
              <CardTitle className="text-sm tracking-tight">运行时观测</CardTitle>
              <CardDescription className="mt-0.5 max-w-2xl text-pretty text-xs">
                按服务池检查可用性、容量与数据可信度，展开后处理单个实例。
              </CardDescription>
            </div>
            <CardAction className="flex flex-wrap items-center justify-end gap-3 max-md:col-span-2 max-md:row-start-3 max-md:justify-start">
              <Switch
                checked={autoRefresh}
                onChange={setAutoRefresh}
                label="自动刷新"
                title="每 15 秒自动刷新运行时快照"
              />
              <Button size="sm" onClick={refetchAll} disabled={fetching}>
                <RefreshCw
                  data-icon="inline-start"
                  className={fetching ? "animate-spin" : undefined}
                  aria-hidden="true"
                />
                刷新
              </Button>
            </CardAction>
          </CardHeader>

          <CardContent className="flex flex-col gap-4">
            {topology ? (
              <RuntimeSummaryBand
                topology={topology}
                poolCount={poolCount}
                memberCount={memberCount}
                envOnlyCount={envOnlyTargets.length}
              />
            ) : loading ? (
              <RuntimeSummarySkeleton />
            ) : null}

            {topology && (
              <DataSourceRegion
                topology={topology}
                snapshotError={snapshotQ.error ? String((snapshotQ.error as Error).message) : null}
                observeError={observeQ.error ? String((observeQ.error as Error).message) : null}
              />
            )}

            {loading && topology === null ? (
              <PoolListSkeleton />
            ) : hardError ? (
              <Alert variant="destructive">
                <AlertTriangle aria-hidden="true" />
                <AlertTitle>运行时状态加载失败</AlertTitle>
                <AlertDescription>{(hardError as Error).message ?? "未知错误"}</AlertDescription>
              </Alert>
            ) : topology ? (
              <ServicePoolRuntimeTable topology={topology} lookup={lookup} onWarm={onWarm} />
            ) : null}

            {envOnlyTargets.length > 0 && (
              <EnvOnlySection targets={envOnlyTargets} onRegister={() => setRegisterOpen(true)} />
            )}
          </CardContent>
        </Card>

        {registerOpen && (
          <GlobalBackendFormModal open={registerOpen} onClose={() => setRegisterOpen(false)} />
        )}
      </div>
    </TooltipProvider>
  );
}

function RuntimeSummaryBand({
  topology,
  poolCount,
  memberCount,
  envOnlyCount,
}: {
  topology: RuntimeTopologyViewModel;
  poolCount: number;
  memberCount: number;
  envOnlyCount: number;
}): React.ReactElement {
  const routable = topology.pools.reduce((sum, pool) => sum + pool.availability.routable, 0);
  const attentionPools = topology.pools.filter((pool) => pool.status !== "healthy").length;
  const staleSources = topology.sources.filter((source) => source.stale).length;
  const freshSources = topology.sources.length - staleSources;
  const routerLabels: Record<string, string> = {
    enforce: "强制路由",
    observe: "影子观测",
    off: "直连观测",
  };

  // 紧凑摘要带（plan §4.2 阶段三）：路由模式 · 可路由实例 · 需关注服务池 ·
  // 数据来源可用性，一条可换行的字段带替代旧版四个大摘要块。
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-border bg-muted/30 px-3.5 py-2.5 text-xs"
      aria-label="运行时摘要"
    >
      <span className="inline-flex items-center gap-1.5">
        <span className="text-muted-foreground">路由模式</span>
        <span className="font-medium">{routerLabels[topology.router_mode] ?? "未知"}</span>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="text-muted-foreground">可路由实例</span>
        <span className="font-medium tabular-nums">
          <span className="text-status-positive">{routable}</span>
          <span className="text-muted-foreground"> / {memberCount}</span>
        </span>
        <span className="text-2xs text-muted-foreground">{poolCount} 个服务池</span>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="text-muted-foreground">需关注服务池</span>
        <span
          className={cn("font-medium tabular-nums", attentionPools > 0 && "text-status-caution")}
        >
          {attentionPools}
        </span>
        <span className="text-2xs text-muted-foreground">
          {attentionPools > 0 ? "健康状态非正常" : "当前无异常"}
        </span>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="text-muted-foreground">数据来源</span>
        {topology.sources.length > 0 ? (
          <Badge variant={staleSources > 0 ? "warning" : "success"}>
            {freshSources} / {topology.sources.length} 新鲜
          </Badge>
        ) : (
          <span className="font-medium">未上报</span>
        )}
        {envOnlyCount > 0 && (
          <span className="text-2xs text-status-caution">{envOnlyCount} 个容器未纳管</span>
        )}
      </span>
    </div>
  );
}

function RuntimeSummarySkeleton(): React.ReactElement {
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-border bg-muted/30 px-3.5 py-2.5"
      aria-label="正在加载运行时摘要"
    >
      {Array.from({ length: 4 }, (_, index) => (
        <div key={index} className="inline-flex items-center gap-1.5">
          <Skeleton className="h-2.5 w-12" />
          <Skeleton className="h-3.5 w-14" />
        </div>
      ))}
    </div>
  );
}

function PoolListSkeleton(): React.ReactElement {
  return (
    <div className="flex flex-col gap-3" aria-label="正在加载服务池">
      {Array.from({ length: 3 }, (_, index) => (
        <div key={index} className="rounded-xl border border-border p-4">
          <div className="flex items-center gap-3">
            <Skeleton className="size-8" />
            <div className="flex flex-1 flex-col gap-2">
              <Skeleton className="h-4 w-36" />
              <Skeleton className="h-3 w-52" />
            </div>
            <Skeleton className="h-5 w-16 rounded-full" />
          </div>
          <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }, (_, metricIndex) => (
              <Skeleton key={metricIndex} className="h-16 w-full" />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function DataSourceRegion({
  topology,
  snapshotError,
  observeError,
}: {
  topology: RuntimeTopologyViewModel;
  snapshotError: string | null;
  observeError: string | null;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  const observedAt = formatObservedAt(topology.observed_at);
  return (
    <div className="rounded-lg border border-border bg-muted/20 px-3.5 py-3">
      <button
        type="button"
        className="flex w-full cursor-pointer appearance-none items-center gap-2 border-0 bg-transparent p-0 text-left text-xs text-muted-foreground transition-colors hover:text-foreground"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5" strokeWidth={1.6} aria-hidden="true" />
        ) : (
          <ChevronRight className="size-3.5" strokeWidth={1.6} aria-hidden="true" />
        )}
        <span className="font-medium text-foreground">数据来源</span>
        <span className="text-2xs">更新于 {observedAt}</span>
        <Badge variant={topology.partial ? "warning" : "success"}>
          {topology.partial ? "部分可用" : "数据完整"}
        </Badge>
      </button>
      {open && (
        <div className="mt-3 flex flex-col gap-3">
          {topology.partial && (
            <Alert variant="warning">
              <AlertTriangle aria-hidden="true" />
              <AlertTitle>部分数据来源失败</AlertTitle>
              <AlertDescription>
                <p>
                  {topology.partial_reason ?? "原因未知"}。页面已保留其它可信来源，
                  不会把缺失指标显示为 0。
                </p>
                {snapshotError && <p>runtime-snapshot：{snapshotError}</p>}
                {observeError && <p>observe：{observeError}</p>}
              </AlertDescription>
            </Alert>
          )}
          <div className="flex flex-wrap gap-1.5">
            {topology.sources.length === 0 && (
              <span className="text-2xs text-muted-foreground">无来源新鲜度信息</span>
            )}
            {topology.sources.map((s) => (
              <FreshnessIndicator key={s.name} source={s} />
            ))}
          </div>
          {!topology.partial && snapshotError && (
            <div className="text-2xs text-status-danger">runtime-snapshot：{snapshotError}</div>
          )}
          {!topology.partial && observeError && (
            <div className="text-2xs text-status-danger">observe：{observeError}</div>
          )}
        </div>
      )}
    </div>
  );
}

function formatObservedAt(iso: string | null): string {
  if (!iso) return "—";
  const timestamp = new Date(iso);
  if (Number.isNaN(timestamp.getTime())) return iso;
  return timestamp.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function EnvOnlySection({
  targets,
  onRegister,
}: {
  targets: ObserveTarget[];
  onRegister: (url: string) => void;
}): React.ReactElement {
  const [collapsed, setCollapsed] = useState(true);
  return (
    <section className="flex flex-col gap-2.5">
      <button
        type="button"
        className="flex w-full cursor-pointer appearance-none items-center gap-2 border-0 bg-transparent p-0 text-left text-xs font-semibold text-muted-foreground hover:text-foreground"
        onClick={() => setCollapsed((v) => !v)}
        aria-expanded={!collapsed}
      >
        {collapsed ? (
          <ChevronRight className="size-3.5" strokeWidth={1.6} aria-hidden="true" />
        ) : (
          <ChevronDown className="size-3.5" strokeWidth={1.6} aria-hidden="true" />
        )}
        <span>未纳管容器（{targets.length}）</span>
        <span className="text-2xs font-normal">来自 ML_BACKEND_OBSERVE_URLS，未注册到全局表</span>
      </button>
      {!collapsed && (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {targets.map((t) => (
            <EnvOnlyContainerCard key={t.url} target={t} onRegister={onRegister} />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * Warmup dispatcher: resolves the projectId for a registry_id and invokes the
 * declared generic /warmup endpoint when available, falling back to legacy
 * /reload only for older backends.
 */
function useWarmupDispatcher(
  projectByBackend: Map<string, string>,
  backendsById: Map<string, GlobalBackendItem>,
): (registryId: string, target?: VariantWarmTarget) => void {
  const qc = useQueryClient();
  const reload = useMutation({
    mutationFn: ({
      projectId,
      backendId,
      variant,
      taskType,
    }: {
      projectId: string;
      backendId: string;
      variant?: MLBackendVariant;
      taskType?: "image" | "video";
    }) => mlBackendsApi.reload(projectId, backendId, variant, taskType),
    onSuccess: () => {
      invalidateRuntimeQueries(qc);
    },
  });
  const warmup = useMutation({
    mutationFn: ({
      projectId,
      backendId,
      target,
    }: {
      projectId: string;
      backendId: string;
      target?: VariantWarmTarget;
    }) =>
      mlBackendsApi.warmup(projectId, backendId, {
        ...(target?.task ? { task: target.task } : {}),
        ...(target?.variants ? { variants: target.variants } : {}),
        ...(target?.taskType ? { task_type: target.taskType } : {}),
      }),
    onSuccess: () => invalidateRuntimeQueries(qc),
  });

  return (registryId: string, target?: VariantWarmTarget) => {
    const projectId = projectByBackend.get(registryId);
    if (!projectId) return;
    const supportsWarmup =
      backendsById.get(registryId)?.health_meta?.capabilities?.warmup_endpoint === true;
    if (supportsWarmup) {
      warmup.mutate({ projectId, backendId: registryId, target });
      return;
    }
    const variant = target?.variants as MLBackendVariant | undefined;
    reload.mutate({
      projectId,
      backendId: registryId,
      variant,
      taskType: target?.taskType,
    });
  };
}
