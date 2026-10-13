/**
 * v0.23.4 P3 · 注册管理 tab — orchestrator shell.
 *
 * Plan §4.3 / §5（模型市场多 TAB UI 优化 · 阶段一）：this file is the only
 * place that owns the registry queries, the registry URL state and the
 * role-aware tab structure. It:
 *   - reads `usePermissions()` and gates Super Admin vs Project Admin tabs;
 *   - owns the topology / runtime-snapshot / gpu-resources / overview queries;
 *   - merges topology + snapshot via the pure view-model (no URL-join);
 *   - collects diagnostics for the Issue Center;
 *   - renders one compact summary strip (routable instances / pending issues /
 *     data availability) + one refresh action + sub-TAB counts;
 *   - keeps the registry sub-TAB and per-view filters in the URL
 *     (`registry_view=…`, plan §5) so refresh / back-forward / deep links
 *     restore the working context — object jumps navigate via URL instead of
 *     the removed `registry:focus-tab` custom event;
 *   - delegates to the five section components under `registry/`, each of
 *     which owns the toolbar that actually applies to its collection.
 *
 * Mutations invalidate topology + all + overview + gpu-resources (extended in
 * useGlobalRegistry.ts). Role projection is server-side (P1) — this view does
 * NOT add client-side hiding as the only gate; it simply does not render what
 * the server already nulled (plan §5 + Appendix A.6).
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Card } from "@/components/ui/Card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/shadcn/ui/tooltip";
import { adminMlIntegrationsApi } from "@/api/adminMlIntegrations";
import { usePermissions } from "@/hooks/usePermissions";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";

import {
  MARKET_URL_KEYS,
  PROJECT_ADMIN_REGISTRY_VIEWS,
  REGISTRY_URL_DEFAULTS,
  registryUrlCodec,
  type RegistryView,
} from "./marketUrlState";
import { collectDiagnostics, mergeTopologyAndSnapshot } from "./runtimeTopology";
import { GlobalBackendFormModal } from "./GlobalBackendFormModal";
import { ServicePoolsSection } from "./registry/ServicePoolsSection";
import { BackendInstancesSection } from "./registry/BackendInstancesSection";
import { GPUResourcesSection } from "./registry/GPUResourcesSection";
import { ProjectBindingsSection } from "./registry/ProjectBindingsSection";
import { IssueCenter } from "./registry/IssueCenter";
import type { RegistryScope } from "./registry/registryTypes";
import type { Severity } from "./runtimeTopology";
import { SEVERITY_TOKENS } from "./runtime/StateTokens";

export function RegisteredBackendsTab(): ReactNode {
  const { role } = usePermissions();
  const isSuperAdmin = role === "super_admin";

  // 注册子 TAB + 各子视图筛选全部写入 URL（plan §5）。子 TAB 显式切换 push
  // 历史；文本/筛选更新 replace（由各 section 决定）。
  const {
    state: url,
    patch: patchUrl,
    issues: urlIssues,
  } = useUrlFilterState({
    codec: registryUrlCodec,
    defaults: REGISTRY_URL_DEFAULTS,
  });
  const [registerOpen, setRegisterOpen] = useState(false);

  // 未知子视图枚举 → 回退默认页并规范化 URL；项目管理员深链到超管专属子视图
  // → 回退到其可见子页（查询本身已按角色 enabled，不会发出超管请求）。
  useEffect(() => {
    const invalidView = urlIssues.some((issue) => issue.key === MARKET_URL_KEYS.registryView);
    const needsRoleFallback =
      !isSuperAdmin && !PROJECT_ADMIN_REGISTRY_VIEWS.includes(url.registryView);
    if (invalidView || needsRoleFallback) {
      patchUrl({ registryView: "pools" });
    }
  }, [isSuperAdmin, urlIssues, url.registryView, patchUrl]);

  const setRegistryView = (view: RegistryView) => {
    if (view !== url.registryView) {
      patchUrl({ registryView: view }, { replace: false });
    }
  };

  // ── queries ──────────────────────────────────────────────────────────────
  // Plan §9.1 + §9.3: topology is the registry default read model and is
  // always fetched (both roles); runtime-snapshot / gpu-resources / overview
  // are SUPER_ADMIN-only.
  const topologyQ = useQuery({
    queryKey: ["admin", "ml-integrations", "topology"],
    queryFn: () => adminMlIntegrationsApi.topology(),
    staleTime: 60_000,
  });

  const snapshotQ = useQuery({
    queryKey: ["admin", "ml-integrations", "runtime-snapshot"],
    queryFn: () => adminMlIntegrationsApi.runtimeSnapshot(),
    staleTime: 30_000,
    enabled: isSuperAdmin,
  });

  const gpuQ = useQuery({
    queryKey: ["admin", "ml-integrations", "gpu-resources"],
    queryFn: () => adminMlIntegrationsApi.gpuResources(),
    staleTime: 60_000,
    enabled: isSuperAdmin,
  });

  const overviewQ = useQuery({
    queryKey: ["admin", "ml-integrations", "overview"],
    queryFn: () => adminMlIntegrationsApi.overview(),
    staleTime: 60_000,
    enabled: isSuperAdmin,
  });

  const allQ = useQuery({
    queryKey: ["admin", "ml-integrations", "all"],
    queryFn: () => adminMlIntegrationsApi.listAll(),
    staleTime: 60_000,
  });

  const refreshAll = () => {
    topologyQ.refetch();
    if (isSuperAdmin) {
      snapshotQ.refetch();
      gpuQ.refetch();
      overviewQ.refetch();
    }
    allQ.refetch();
  };
  const refreshing =
    topologyQ.isFetching ||
    (isSuperAdmin && (snapshotQ.isFetching || gpuQ.isFetching || overviewQ.isFetching)) ||
    allQ.isFetching;

  // ── view-model merge ──────────────────────────────────────────────────────
  // topologyQ.data may be undefined while loading; once loaded it is non-null.
  // We narrow here so the rest of the component (and the RegistryScope it
  // builds) can rely on a non-null TopologyResponse.
  const topology = topologyQ.data ?? null;
  const snapshot = isSuperAdmin ? (snapshotQ.data ?? null) : null;
  const vm = useMemo(() => {
    if (!topology) return null;
    return mergeTopologyAndSnapshot(topology, snapshot);
  }, [topology, snapshot]);

  const diagnostics = useMemo(() => {
    if (!topology) return [];
    return collectDiagnostics(
      topology,
      snapshot,
      isSuperAdmin ? (gpuQ.data?.resources ?? null) : null,
    );
  }, [topology, snapshot, gpuQ.data, isSuperAdmin]);

  // ── loading / error / partial-fail ────────────────────────────────────────
  // Topology is load-bearing for every tab; if it fails and we have nothing
  // cached, surface a full error block. If only runtime-snapshot fails, keep
  // the topology view and mark partial (plan §6.3).
  if (topologyQ.isLoading) {
    return <LoadingShell label="加载服务池拓扑…" />;
  }
  if (topologyQ.isError && !topology) {
    return (
      <ErrorShell
        message={`拓扑加载失败：${(topologyQ.error as Error)?.message ?? "未知错误"}`}
        onRetry={() => topologyQ.refetch()}
      />
    );
  }
  if (!vm || !topology) {
    return <LoadingShell label="组装视图模型…" />;
  }
  // After the guards above, both `vm` and `topology` are non-null. Bind a
  // narrowed local so TS carries the non-nullability into RegistryScope.
  const narrowedTopology = topology;
  const narrowedVm = vm;

  // ── summary counts ───────────────────────────────────────────────────────
  // 每个计数来源明确（plan §3）：可路由实例来自 topology 视图模型；待处理问题
  // 来自去重诊断；数据可用性来自快照 partial 标记。请求未完成的计数显示加载
  // 占位，不写成 0。
  const routableInstances = narrowedVm.pools.reduce((sum, p) => sum + p.availability.routable, 0);
  const totalInstances = narrowedVm.pools.reduce((sum, p) => sum + p.availability.total, 0);
  const instanceCountPending = allQ.isLoading;
  // 查询失败且无缓存时如实显示未知（?），不把失败的列表冒充成空注册表。
  const instanceCountError = allQ.isError;
  const instanceCount = allQ.data?.items.length ?? 0;
  const gpuCountPending = isSuperAdmin && gpuQ.isLoading;
  const gpuCount = gpuQ.data?.resources.length ?? 0;
  const gpuCountError = isSuperAdmin && gpuQ.isError;
  const projectCountPending = isSuperAdmin && overviewQ.isLoading;
  const projectCount = overviewQ.data?.projects.length ?? 0;
  const projectCountError = isSuperAdmin && overviewQ.isError;
  const issueCountPending = isSuperAdmin && snapshotQ.isLoading;
  const issueCount = diagnostics.length;
  const issueMaxSeverity = maxSeverityOf(diagnostics);
  const partialData = isSuperAdmin && narrowedVm.partial;

  const scope: RegistryScope = {
    isSuperAdmin,
    vm: narrowedVm,
    topology: narrowedTopology,
    servicePools: null,
    backends: allQ.data?.items ?? [],
    gpuResources: isSuperAdmin ? (gpuQ.data?.resources ?? null) : null,
    overview: isSuperAdmin ? (overviewQ.data ?? null) : null,
    diagnostics,
    routerMode: narrowedVm.router_mode,
    loading: {
      backends: allQ.isLoading,
      gpu: isSuperAdmin && gpuQ.isLoading,
      overview: isSuperAdmin && overviewQ.isLoading,
    },
  };

  const gpuTab = (
    <TabsTrigger value="gpu">
      <Icon name="activity" size={12} />
      GPU 资源
      <TabCount value={gpuCountPending ? "…" : gpuCountError ? "?" : gpuCount} />
    </TabsTrigger>
  );
  const projectsTab = (
    <TabsTrigger value="projects">
      <Icon name="folder" size={12} />
      项目绑定
      <TabCount value={projectCountPending ? "…" : projectCountError ? "?" : projectCount} />
    </TabsTrigger>
  );
  const issuesTab = (
    <TabsTrigger value="issues">
      <Icon name="alert-triangle" size={12} />
      问题中心
      {issueCount > 0 && issueMaxSeverity && !issueCountPending && (
        <Badge variant={SEVERITY_TOKENS[issueMaxSeverity].variant} className="ml-1 text-2xs">
          {issueCount}
        </Badge>
      )}
      {issueCountPending && <TabCount value="…" />}
    </TabsTrigger>
  );

  return (
    // TooltipProvider: several descendants (CopyableId, FreshnessIndicator,
    // SourceErrorBadge) use Radix Tooltip, which requires a provider ancestor.
    <TooltipProvider>
      <div className="flex flex-col gap-3">
        {/* 紧凑摘要带（plan §3）：可路由实例 · 待处理问题 · 数据可用性 + 唯一刷新 */}
        <div
          data-testid="registry-summary"
          className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs"
        >
          <SummaryItem label="可路由实例">
            <span className="text-status-positive">{routableInstances}</span>
            <span className="text-muted-foreground"> / {totalInstances}</span>
          </SummaryItem>
          {isSuperAdmin && (
            <SummaryItem label="待处理问题">{issueCountPending ? "…" : issueCount}</SummaryItem>
          )}
          {partialData && (
            <Badge variant="warning" className="text-2xs">
              <Icon name="alert-triangle" size={11} />
              数据部分可用
            </Badge>
          )}
          <div className="ml-auto flex items-center gap-2">
            {(snapshotQ.isError || gpuQ.isError || overviewQ.isError || allQ.isError) && (
              <div className="flex flex-wrap items-center gap-1.5">
                {isSuperAdmin && snapshotQ.isError && (
                  <SourceErrorBadge
                    label="运行时快照"
                    message={(snapshotQ.error as Error)?.message ?? "运行时快照加载失败"}
                  />
                )}
                {isSuperAdmin && gpuQ.isError && (
                  <SourceErrorBadge
                    label="GPU 资源"
                    message={(gpuQ.error as Error)?.message ?? "GPU 资源加载失败"}
                  />
                )}
                {isSuperAdmin && overviewQ.isError && (
                  <SourceErrorBadge
                    label="项目绑定"
                    message={(overviewQ.error as Error)?.message ?? "项目绑定概览加载失败"}
                  />
                )}
                {allQ.isError && (
                  <SourceErrorBadge
                    label="实例列表"
                    message={(allQ.error as Error)?.message ?? "实例列表加载失败"}
                  />
                )}
              </div>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={refreshAll}
              disabled={refreshing}
              title="刷新注册管理全部数据源"
            >
              <Icon
                name={refreshing ? "loader2" : "refresh"}
                size={11}
                className={refreshing ? "spin" : undefined}
              />
              刷新
            </Button>
          </div>
        </div>

        <Tabs
          value={url.registryView}
          activationMode="manual"
          onValueChange={(value) => setRegistryView(value as RegistryView)}
        >
          <TabsList className="mb-2 max-md:mb-2 max-md:w-full max-md:justify-start max-md:overflow-x-auto">
            <TabsTrigger value="pools">
              <Icon name="layers" size={12} />
              服务池
              <TabCount value={narrowedVm.pools.length} />
            </TabsTrigger>
            <TabsTrigger value="instances">
              <Icon name="bot" size={12} />
              实例
              <TabCount
                value={instanceCountPending ? "…" : instanceCountError ? "?" : instanceCount}
              />
            </TabsTrigger>
            {isSuperAdmin && gpuTab}
            {isSuperAdmin && projectsTab}
            {isSuperAdmin && issuesTab}
          </TabsList>
          <TabsContent value="pools">
            <ServicePoolsSection
              scope={scope}
              url={url}
              patchUrl={patchUrl}
              urlIssues={urlIssues}
              onOpenRegister={isSuperAdmin ? () => setRegisterOpen(true) : undefined}
            />
          </TabsContent>
          <TabsContent value="instances">
            <BackendInstancesSection
              scope={scope}
              url={url}
              patchUrl={patchUrl}
              urlIssues={urlIssues}
              onOpenRegister={isSuperAdmin ? () => setRegisterOpen(true) : undefined}
            />
          </TabsContent>
          {isSuperAdmin && (
            <TabsContent value="gpu">
              <GPUResourcesSection
                scope={scope}
                url={url}
                patchUrl={patchUrl}
                urlIssues={urlIssues}
                summary={gpuQ.data ?? null}
              />
            </TabsContent>
          )}
          {isSuperAdmin && (
            <TabsContent value="projects">
              <ProjectBindingsSection
                scope={scope}
                url={url}
                patchUrl={patchUrl}
                urlIssues={urlIssues}
              />
            </TabsContent>
          )}
          {isSuperAdmin && (
            <TabsContent value="issues">
              <IssueCenter scope={scope} url={url} patchUrl={patchUrl} urlIssues={urlIssues} />
            </TabsContent>
          )}
        </Tabs>

        <GlobalBackendFormModal
          open={registerOpen}
          backend={null}
          onClose={() => setRegisterOpen(false)}
        />
      </div>
    </TooltipProvider>
  );
}

// ── summary pieces ───────────────────────────────────────────────────────────

function SummaryItem({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium">{children}</span>
    </span>
  );
}

/** Sub-TAB count; pending loads render "…" instead of a fake 0 (plan §3). */
function TabCount({ value }: { value: string | number }): ReactNode {
  return <span className="text-2xs tabular-nums text-muted-foreground">{value}</span>;
}

function SourceErrorBadge({ label, message }: { label: string; message: string }): ReactNode {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="danger" className="cursor-help">
          <Icon name="alert-triangle" size={11} />
          <span>{label}加载失败</span>
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{message}</TooltipContent>
    </Tooltip>
  );
}

/** Highest diagnostic severity, or null when there are no diagnostics. */
function maxSeverityOf(diagnostics: Array<{ severity: Severity }>): Severity | null {
  const rank: Record<Severity, number> = { info: 0, warning: 1, critical: 2, blocker: 3 };
  let max: Severity | null = null;
  for (const d of diagnostics) {
    if (max === null || rank[d.severity] > rank[max]) max = d.severity;
  }
  return max;
}

function LoadingShell({ label }: { label: string }): ReactNode {
  return (
    <Card>
      <div className="flex items-center justify-center gap-2 p-12 text-sm text-muted-foreground">
        <Icon name="loader2" size={16} className="spin" />
        {label}
      </div>
    </Card>
  );
}

function ErrorShell({ message, onRetry }: { message: string; onRetry: () => void }): ReactNode {
  return (
    <Card>
      <div className="flex flex-col items-center gap-2 p-12 text-center text-sm text-status-danger">
        <Icon name="warning" size={20} />
        <div>{message}</div>
        <Button size="sm" variant="ghost" onClick={onRetry}>
          <Icon name="refresh" size={11} />
          重试
        </Button>
      </div>
    </Card>
  );
}
