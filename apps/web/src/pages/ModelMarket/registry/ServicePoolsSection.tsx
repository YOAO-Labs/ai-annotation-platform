/**
 * v0.23.4 P3 · registry "服务池" tab.
 *
 * Plan §4.3（模型市场多 TAB UI 优化 · 阶段一）: the pool table keeps its
 * 身份/成员/容量/关联项目/状态/操作 columns, but now owns the toolbar that
 * actually applies to it — pool-or-member search (`pool_q`) + health filter
 * (`pool_health`) both URL-backed, 新建服务池 as the primary action and
 * 注册实例 kept as the secondary entry. Row ids render as 名称 + 短 ID with
 * copy/tooltip (full value in detail views). 「查看实例」navigates via the URL
 * (`registry_view=instances&instance_pool=<id>`) instead of the removed
 * registry:focus-tab event. `pool_id` deep-links expand + scroll to the pool.
 *
 * Project Admin: routing_policy is hidden (server returns "unknown"), no GPU
 * column, no actions, no expansion of internal reason — read-only.
 *
 * Pure consumer of the orchestrator-provided `RegistryScope`. It does not own
 * queries; mutations come from `useGlobalRegistry` and invalidate the shared
 * keys.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { DropdownMenu, type DropdownItem } from "@/components/ui/DropdownMenu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/shadcn/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/ui/tooltip";
import { Input } from "@/components/shadcn/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/shadcn/ui/select";
import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { useToastStore } from "@/components/ui/Toast";

import { NO_LIMIT, NO_METRICS } from "./registryShared";
import { CapabilityDriftReviewDialog } from "./CapabilityDriftReviewDialog";
import {
  AffectedCountChip,
  EmptyState,
  NullCell,
  ShortCopyableId,
  UrlIssueChips,
} from "./registryUi";
import type { RegistryScope, RegistrySectionProps } from "./registryTypes";
import type { PoolViewModel } from "../runtimeTopology";
import { derivePoolEffectiveRouting, sortPoolsBySeverity } from "../runtimeTopology";
import { instancesForPoolPatch, urlIssuesForView } from "../marketUrlState";
import { FreshnessIndicator } from "../runtime/FreshnessIndicator";
import { RuntimeStatusBadge } from "../runtime/RuntimeStatusBadge";
import { poolStatusToken } from "../runtime/StateTokens";
import {
  useDrainPoolMember,
  useCreateServicePool,
  useDeleteServicePool,
  usePatchServicePool,
  usePutPoolMember,
  useRemovePoolMember,
  useResumePoolMember,
} from "../useGlobalRegistry";

/** Map a routing policy enum to a friendly Chinese label, "unknown"-aware. */
function policyLabel(policy: string): string {
  switch (policy) {
    case "smooth_weighted_round_robin":
      return "平滑加权轮询";
    case "weighted_round_robin":
      return "加权轮询";
    case "round_robin":
      return "轮询";
    case "unknown":
      return "未知";
    case "":
      return "未配置";
    default:
      return policy;
  }
}

type GpuSeverity = "ok" | "info" | "warning" | "critical" | "blocker";

/** Find the highest GPU diagnostic severity touching this pool's members. */
function poolMaxGpuSeverity(pool: PoolViewModel, scope: RegistryScope): GpuSeverity | null {
  if (!scope.gpuResources) return null;
  const gpuIds = new Set<string>();
  for (const m of pool.members) {
    if (m.gpu_resource_id) gpuIds.add(m.gpu_resource_id);
  }
  let max: GpuSeverity | null = null;
  const rank: Record<GpuSeverity, number> = {
    ok: 0,
    info: 1,
    warning: 2,
    critical: 3,
    blocker: 4,
  };
  for (const r of scope.gpuResources) {
    if (!gpuIds.has(r.gpu_resource_id)) continue;
    if (max === null || rank[r.status] > rank[max]) max = r.status;
  }
  return max;
}

function gpuSeverityBadge(sev: GpuSeverity): ReactNode {
  const variant =
    sev === "blocker" || sev === "critical" ? "danger" : sev === "warning" ? "warning" : "success";
  const label =
    sev === "blocker"
      ? "阻断"
      : sev === "critical"
        ? "严重"
        : sev === "warning"
          ? "告警"
          : sev === "info"
            ? "信息"
            : "正常";
  return (
    <Badge variant={variant}>
      <Icon name={sev === "ok" ? "checkCircle" : "alert-triangle"} size={11} />
      <span>{label}</span>
    </Badge>
  );
}

export function ServicePoolsSection({
  scope,
  url,
  patchUrl,
  urlIssues,
  onOpenRegister,
}: RegistrySectionProps & { onOpenRegister?: () => void }): ReactNode {
  const { isSuperAdmin, vm } = scope;
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [createOpen, setCreateOpen] = useState(false);
  const [poolName, setPoolName] = useState("");
  const createPool = useCreateServicePool();
  const pushToast = useToastStore((s) => s.push);

  // pool_id 定位键（plan §5）：池存在 → 展开成员并滚动到该行；对象不存在或
  // 当前不可访问 → 明确提示，不静默清掉条件。
  const focusPoolId = url.poolId;
  const focusPoolExists = vm.pools.some((p) => p.id === focusPoolId);
  useEffect(() => {
    if (!focusPoolId || !focusPoolExists) return;
    setExpanded((prev) => {
      if (prev.has(focusPoolId)) return prev;
      const next = new Set(prev);
      next.add(focusPoolId);
      return next;
    });
    const row = document.querySelector<HTMLElement>(`[data-pool-row="${focusPoolId}"]`);
    row?.scrollIntoView?.({ block: "nearest" });
  }, [focusPoolId, focusPoolExists]);

  const submitCreate = () => {
    const name = poolName.trim();
    if (!name) return;
    createPool.mutate(
      { name },
      {
        onSuccess: () => {
          pushToast({ msg: `已创建服务池「${name}」`, kind: "success" });
          setPoolName("");
          setCreateOpen(false);
        },
        onError: (error) =>
          pushToast({ msg: "创建服务池失败", sub: (error as Error).message, kind: "warning" }),
      },
    );
  };

  const togglePool = (poolId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(poolId)) next.delete(poolId);
      else next.add(poolId);
      return next;
    });
  };

  // 搜索池名/池 ID/策略以及成员名/成员 ID（plan §4.3：搜索池或成员）。
  const rows = useMemo(() => {
    const filtered = vm.pools.filter((p) => {
      if (url.poolQ) {
        const q = url.poolQ.trim().toLowerCase();
        const memberHay = p.members.map((m) => `${m.name} ${m.registry_id}`).join(" ");
        const hay = `${p.name} ${p.id} ${p.routing_policy} ${memberHay}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (url.poolHealth !== "all" && p.status !== url.poolHealth) return false;
      return true;
    });
    return sortPoolsBySeverity(filtered);
  }, [vm.pools, url.poolQ, url.poolHealth]);

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative">
        <Icon
          name="search"
          size={12}
          className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          aria-label="搜索服务池或成员"
          placeholder="搜索池名 / 池 ID / 成员"
          value={url.poolQ}
          onChange={(e) => patchUrl({ poolQ: e.target.value })}
          className="h-8 w-56 pl-7 text-xs"
        />
      </div>
      <FilterGroup label="健康" hideLabel compact>
        <FilterSelect
          aria-label="按健康状态筛选服务池"
          value={url.poolHealth}
          onChange={(e) => patchUrl({ poolHealth: e.target.value as typeof url.poolHealth })}
          className="h-8 text-xs"
        >
          <option value="all">全部</option>
          <option value="healthy">健康</option>
          <option value="degraded">降级</option>
          <option value="offline">离线</option>
          <option value="unknown">未知</option>
        </FilterSelect>
      </FilterGroup>
      {url.poolHealth !== "all" && (
        <button
          type="button"
          onClick={() => patchUrl({ poolHealth: "all" })}
          className="text-2xs text-brand no-underline hover:underline"
        >
          清除条件
        </button>
      )}
      <div className="ml-auto flex items-center gap-2">
        {isSuperAdmin && onOpenRegister && (
          <Button size="sm" variant="ghost" onClick={onOpenRegister} title="注册新的推理实例">
            <Icon name="plus" size={11} />
            注册实例
          </Button>
        )}
        {isSuperAdmin && (
          <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>
            <Icon name="plus" size={11} />
            新建服务池
          </Button>
        )}
      </div>
      <UrlIssueChips
        issues={urlIssuesForView(urlIssues, "pools")}
        onDismiss={(key) => {
          if (key === "pool_health") patchUrl({ poolHealth: "all" });
        }}
      />
    </div>
  );

  if (vm.pools.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        {toolbar}
        <EmptyState icon="layers" message="尚无服务池" />
        <CreatePoolDialog
          open={createOpen}
          name={poolName}
          pending={createPool.isPending}
          onNameChange={setPoolName}
          onOpenChange={setCreateOpen}
          onSubmit={submitCreate}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {toolbar}
      {focusPoolId && !focusPoolExists && (
        <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
          <span>对象不存在或当前不可访问（pool_id={focusPoolId}）</span>
          <Button size="sm" variant="ghost" onClick={() => patchUrl({ poolId: "" })}>
            移除定位
          </Button>
        </div>
      )}
      <Table containerClassName="overflow-hidden rounded-lg border border-border bg-card">
        <TableHeader>
          <TableRow>
            <TableHead className="w-8" />
            <TableHead>服务池</TableHead>
            <TableHead>成员</TableHead>
            <TableHead>容量</TableHead>
            <TableHead>项目</TableHead>
            {isSuperAdmin && <TableHead>GPU</TableHead>}
            <TableHead>状态</TableHead>
            {isSuperAdmin && <TableHead className="text-right">操作</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((pool) => (
            <PoolRow
              key={pool.id}
              pool={pool}
              scope={scope}
              patchUrl={patchUrl}
              expanded={expanded.has(pool.id)}
              focused={pool.id === focusPoolId}
              onToggle={() => togglePool(pool.id)}
            />
          ))}
          {rows.length === 0 && (
            <TableRow>
              <TableCell colSpan={isSuperAdmin ? 8 : 6}>
                <div className="p-6 text-center text-sm text-muted-foreground">
                  没有匹配的服务池
                </div>
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
      <CreatePoolDialog
        open={createOpen}
        name={poolName}
        pending={createPool.isPending}
        onNameChange={setPoolName}
        onOpenChange={setCreateOpen}
        onSubmit={submitCreate}
      />
    </div>
  );
}

function CreatePoolDialog({
  open,
  name,
  pending,
  onNameChange,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  name: string;
  pending: boolean;
  onNameChange: (value: string) => void;
  onOpenChange: (open: boolean) => void;
  onSubmit: () => void;
}): ReactNode {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建服务池</DialogTitle>
          <DialogDescription>创建后默认停用，加入能力等价的成员后再启用。</DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          <label htmlFor="service-pool-name" className="text-sm font-medium">
            名称
          </label>
          <Input
            id="service-pool-name"
            value={name}
            onChange={(event) => onNameChange(event.target.value)}
            autoFocus
          />
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={pending}>
            取消
          </Button>
          <Button onClick={onSubmit} disabled={pending || !name.trim()}>
            {pending ? "创建中…" : "创建"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PoolRow({
  pool,
  scope,
  patchUrl,
  expanded,
  focused,
  onToggle,
}: {
  pool: PoolViewModel;
  scope: RegistryScope;
  patchUrl: RegistrySectionProps["patchUrl"];
  expanded: boolean;
  focused: boolean;
  onToggle: () => void;
}): ReactNode {
  const { isSuperAdmin, vm } = scope;
  const projectCount = countProjectsForPool(pool.id, scope);
  const affectedCount = scope.diagnostics.filter(
    (d) => d.subject_type === "service_pool" && d.subject_id === pool.id,
  ).length;
  const gpuSev = poolMaxGpuSeverity(pool, scope);

  // Capacity column — plan Appendix A.2: limit null → "未声明"; no metrics → "暂无路由指标".
  const inflight = pool.capacity.inflight;
  const limit = pool.capacity.limit;
  const capacityText = !pool.metrics_available
    ? NO_METRICS
    : `${inflight ?? 0} / ${limit == null ? NO_LIMIT : limit}`;

  return (
    <>
      <TableRow
        data-pool-row={pool.id}
        aria-expanded={expanded}
        className={expanded || focused ? "bg-muted/40" : undefined}
      >
        <TableCell className="align-middle">
          <button
            type="button"
            onClick={onToggle}
            aria-label={expanded ? "折叠成员" : "展开成员"}
            aria-expanded={expanded}
            className="inline-flex size-6 items-center justify-center rounded text-muted-foreground hover:bg-accent"
          >
            <Icon name={expanded ? "chevDown" : "chevRight"} size={12} />
          </button>
        </TableCell>
        <TableCell>
          <div className="flex flex-col gap-0.5">
            <div className="flex items-center gap-2">
              <span className="font-medium">{pool.name}</span>
              {pool.enabled ? null : (
                <Badge variant="outline" className="text-2xs">
                  已停用
                </Badge>
              )}
              {affectedCount > 0 && <AffectedCountChip count={affectedCount} />}
            </div>
            <div className="flex items-center gap-2">
              <ShortCopyableId value={pool.id} label="服务池 ID" />
              {/* routing_policy: Super Admin only; Project Admin server-projection is "unknown". */}
              {isSuperAdmin && (
                <span className="text-2xs text-muted-foreground">
                  策略 · {policyLabel(pool.routing_policy)}
                </span>
              )}
            </div>
          </div>
        </TableCell>
        <TableCell>
          <div className="flex flex-col gap-1">
            <span className="text-sm">
              <span className="text-status-positive">{pool.availability.routable}</span>
              <span className="text-muted-foreground"> / {pool.availability.total}</span>
            </span>
            <div className="flex flex-wrap gap-1">
              {pool.availability.draining > 0 && (
                <Badge variant="warning" className="text-2xs">
                  {pool.availability.draining} 停流
                </Badge>
              )}
              {pool.availability.offline > 0 && (
                <Badge variant="danger" className="text-2xs">
                  {pool.availability.offline} 离线
                </Badge>
              )}
            </div>
          </div>
        </TableCell>
        <TableCell>
          {pool.capacity.saturated ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="danger">
                  <Icon name="alert-triangle" size={11} />
                  <span>饱和</span>
                </Badge>
              </TooltipTrigger>
              <TooltipContent>存在成员被动熔断（circuit_open）</TooltipContent>
            </Tooltip>
          ) : (
            <span className="text-xs">{capacityText}</span>
          )}
        </TableCell>
        <TableCell>
          {projectCount > 0 ? (
            <span className="text-sm">{projectCount}</span>
          ) : (
            <NullCell>0</NullCell>
          )}
        </TableCell>
        {isSuperAdmin && (
          <TableCell>{gpuSev ? gpuSeverityBadge(gpuSev) : <NullCell>无声明</NullCell>}</TableCell>
        )}
        <TableCell>
          <PoolStatusCell pool={pool} routerMode={vm.router_mode} sources={vm.sources} />
        </TableCell>
        {isSuperAdmin && (
          <TableCell className="text-right">
            <PoolActionsMenu pool={pool} scope={scope} patchUrl={patchUrl} />
          </TableCell>
        )}
      </TableRow>
      {expanded && <PoolMembersSubRows pool={pool} scope={scope} />}
    </>
  );
}

function PoolStatusCell({
  pool,
  routerMode,
  sources,
}: {
  pool: PoolViewModel;
  routerMode: RegistryScope["routerMode"];
  sources: RegistryScope["vm"]["sources"];
}): ReactNode {
  const statusToken = poolStatusToken(pool.status);
  const routerFreshness = sources.find((s) => s.name === "router_ledger");
  const topologyFreshness = sources.find((s) => s.name === "topology");
  // Routing effective axis — derived by the view-model (already accounts for shadow mode).
  const effectiveRouting = derivePoolEffectiveRouting(pool);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-1">
        <RuntimeStatusBadge axis="health" value={pool.status} prefix="健康" />
        <RuntimeStatusBadge axis="routing" value={effectiveRouting} prefix="路由" />
      </div>
      <div className="flex flex-wrap items-center gap-1 text-2xs text-muted-foreground">
        <Badge variant="outline" className="text-2xs">
          mode · {routerMode}
        </Badge>
        {routerFreshness && <FreshnessIndicator source={routerFreshness} />}
        {topologyFreshness && <FreshnessIndicator source={topologyFreshness} />}
      </div>
      {pool.status_reason_codes.length > 0 && (
        <div className="text-2xs text-muted-foreground">
          {statusToken.label} · {pool.status_reason_codes.join(", ")}
        </div>
      )}
    </div>
  );
}

function PoolMembersSubRows({
  pool,
  scope,
}: {
  pool: PoolViewModel;
  scope: RegistryScope;
}): ReactNode {
  if (pool.members.length === 0) {
    return (
      <TableRow className="bg-muted/20">
        <TableCell />
        <TableCell colSpan={scope.isSuperAdmin ? 7 : 5}>
          <div className="py-2 text-xs text-muted-foreground">该池暂无成员实例</div>
        </TableCell>
      </TableRow>
    );
  }
  return (
    <>
      {pool.members.map((m) => (
        <TableRow key={m.registry_id} className="bg-muted/20">
          <TableCell />
          <TableCell colSpan={scope.isSuperAdmin ? 7 : 5}>
            <div className="flex flex-wrap items-center gap-3 py-1 text-xs">
              <span className="font-medium">{m.name}</span>
              <Badge variant="outline" className="text-2xs">
                {trafficStateLabel(m.traffic_state)}
              </Badge>
              <span className="text-muted-foreground">
                权重 {scope.isSuperAdmin && m.weight != null ? m.weight : "—"}
              </span>
              <ShortCopyableId value={m.registry_id} label="实例 ID" />
              {m.gpu_resource_id && scope.isSuperAdmin && (
                <span className="mono text-2xs text-muted-foreground" title={m.gpu_resource_id}>
                  GPU · {m.gpu_resource_id}
                </span>
              )}
              {scope.isSuperAdmin && <PoolMemberActions pool={pool} member={m} />}
            </div>
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}

function trafficStateLabel(state: string): string {
  switch (state) {
    case "active":
      return "接流";
    case "draining":
      return "停流中";
    case "disabled":
      return "已禁用";
    default:
      return state;
  }
}

function PoolActionsMenu({
  pool,
  scope,
  patchUrl,
}: {
  pool: PoolViewModel;
  scope: RegistryScope;
  patchUrl: RegistrySectionProps["patchUrl"];
}): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const drain = useDrainPoolMember();
  const resume = useResumePoolMember();
  const patch = usePatchServicePool();
  const putMember = usePutPoolMember();
  const deletePool = useDeleteServicePool();
  const [addOpen, setAddOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [registryId, setRegistryId] = useState("");
  const [weight, setWeight] = useState("1");
  const [nextName, setNextName] = useState(pool.name);
  const owned = new Set(scope.vm.pools.flatMap((item) => item.members.map((m) => m.registry_id)));
  const candidates = scope.backends.filter((backend) => !owned.has(backend.id));

  const anyActive = pool.members.some((m) => m.traffic_state === "active");
  const anyDraining = pool.members.some((m) => m.traffic_state === "draining");

  const onDrainAll = () => {
    const targets = pool.members.filter((m) => m.traffic_state === "active");
    Promise.all(
      targets.map((m) => drain.mutateAsync({ poolId: pool.id, registryId: m.registry_id })),
    )
      .then(() =>
        pushToast({ msg: `已对「${pool.name}」${targets.length} 个成员发起停流`, kind: "success" }),
      )
      .catch((e) => pushToast({ msg: "停流失败", sub: (e as Error).message, kind: "warning" }));
  };

  const onResumeAll = () => {
    const targets = pool.members.filter((m) => m.traffic_state === "draining");
    Promise.all(
      targets.map((m) => resume.mutateAsync({ poolId: pool.id, registryId: m.registry_id })),
    )
      .then(() =>
        pushToast({ msg: `已恢复「${pool.name}」${targets.length} 个成员接流`, kind: "success" }),
      )
      .catch((e) => pushToast({ msg: "恢复接流失败", sub: (e as Error).message, kind: "warning" }));
  };

  const onToggleEnabled = () => {
    patch.mutate(
      { poolId: pool.id, payload: { enabled: !pool.enabled } },
      {
        onSuccess: () =>
          pushToast({
            msg: pool.enabled ? `已停用「${pool.name}」` : `已启用「${pool.name}」`,
            kind: "success",
          }),
        onError: (e) => pushToast({ msg: "操作失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  const items: DropdownItem[] = [
    {
      id: "view-instances",
      label: "查看实例",
      icon: "layers",
      onSelect: () => {
        // 子 TAB 与对象定位由 URL 承载（plan §5）：切到实例子视图并按池过滤，
        // 显式跳转 push 浏览器历史，刷新/前进后退可恢复。
        patchUrl(instancesForPoolPatch(pool.id), { replace: false });
      },
    },
    {
      id: "add-member",
      label: "添加成员",
      icon: "plus",
      disabled: candidates.length === 0,
      onSelect: () => setAddOpen(true),
    },
    {
      id: "rename-pool",
      label: "重命名",
      icon: "edit",
      onSelect: () => {
        setNextName(pool.name);
        setRenameOpen(true);
      },
    },
    { id: "div-1", divider: true, label: "" },
    {
      id: "drain",
      label: "暂停接流（全部 active 成员）",
      icon: "pause",
      disabled: !anyActive || drain.isPending,
      onSelect: onDrainAll,
    },
    {
      id: "resume",
      label: "恢复接流（全部 draining 成员）",
      icon: "play",
      disabled: !anyDraining || resume.isPending,
      onSelect: onResumeAll,
    },
    { id: "div-2", divider: true, label: "" },
    {
      id: "toggle-enabled",
      label: pool.enabled ? "停用服务池" : "启用服务池",
      icon: pool.enabled ? "circleDot" : "checkCircle",
      disabled: patch.isPending,
      onSelect: onToggleEnabled,
    },
    {
      id: "delete-pool",
      label: "删除服务池",
      icon: "trash",
      disabled: pool.members.length > 0,
      onSelect: () => setDeleteOpen(true),
    },
  ];

  const submitMember = () => {
    if (!registryId) return;
    putMember.mutate(
      { poolId: pool.id, registryId, payload: { weight: Number(weight) } },
      {
        onSuccess: () => {
          pushToast({ msg: "已添加服务池成员", kind: "success" });
          setAddOpen(false);
          setRegistryId("");
          setWeight("1");
        },
        onError: (error) =>
          pushToast({ msg: "添加成员失败", sub: (error as Error).message, kind: "warning" }),
      },
    );
  };

  return (
    <>
      <DropdownMenu
        minWidth={200}
        items={items}
        trigger={({ open, toggle, ref }) => (
          <Button
            ref={ref as never}
            size="sm"
            variant="ghost"
            onClick={(e) => {
              e.stopPropagation();
              toggle();
            }}
            aria-haspopup="menu"
            aria-expanded={open}
            title="服务池操作"
          >
            <Icon name="more" size={11} />
            操作
          </Button>
        )}
      />
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>向「{pool.name}」添加成员</DialogTitle>
            <DialogDescription>只能加入尚未归属其他服务池且能力指纹一致的实例。</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4">
            <div className="grid gap-2">
              <label className="text-sm font-medium">实例</label>
              <Select value={registryId} onValueChange={setRegistryId}>
                <SelectTrigger>
                  <SelectValue placeholder="选择实例" />
                </SelectTrigger>
                <SelectContent>
                  {candidates.map((backend) => (
                    <SelectItem key={backend.id} value={backend.id}>
                      {backend.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <label htmlFor={`member-weight-${pool.id}`} className="text-sm font-medium">
                权重
              </label>
              <Input
                id={`member-weight-${pool.id}`}
                type="number"
                min={1}
                max={100}
                value={weight}
                onChange={(event) => setWeight(event.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setAddOpen(false)}>
              取消
            </Button>
            <Button onClick={submitMember} disabled={!registryId || putMember.isPending}>
              添加
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重命名服务池</DialogTitle>
            <DialogDescription>名称仅用于展示，不改变服务池 ID 或路由成员。</DialogDescription>
          </DialogHeader>
          <Input value={nextName} onChange={(event) => setNextName(event.target.value)} autoFocus />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRenameOpen(false)}>
              取消
            </Button>
            <Button
              disabled={!nextName.trim() || patch.isPending}
              onClick={() =>
                patch.mutate(
                  { poolId: pool.id, payload: { name: nextName.trim() } },
                  {
                    onSuccess: () => {
                      setRenameOpen(false);
                      pushToast({ msg: "服务池已重命名", kind: "success" });
                    },
                    onError: (error) =>
                      pushToast({
                        msg: "服务池重命名失败",
                        sub: (error as Error).message,
                        kind: "warning",
                      }),
                  },
                )
              }
            >
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除服务池「{pool.name}」</AlertDialogTitle>
            <AlertDialogDescription>仅空服务池可删除，此操作不可撤销。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() =>
                deletePool.mutate(pool.id, {
                  onSuccess: () => setDeleteOpen(false),
                  onError: (error) =>
                    pushToast({
                      msg: "删除服务池失败",
                      sub: (error as Error).message,
                      kind: "warning",
                    }),
                })
              }
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function PoolMemberActions({
  pool,
  member,
}: {
  pool: PoolViewModel;
  member: PoolViewModel["members"][number];
}): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const put = usePutPoolMember();
  const remove = useRemovePoolMember();
  const [editOpen, setEditOpen] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [weight, setWeight] = useState(String(member.weight ?? 1));
  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => setEditOpen(true)}>
        编辑权重
      </Button>
      {member.traffic_state === "disabled" && (
        <Button size="sm" variant="ghost" onClick={() => setReviewOpen(true)}>
          审核能力变更
        </Button>
      )}
      <Button size="sm" variant="ghost" onClick={() => setRemoveOpen(true)}>
        移除
      </Button>
      <CapabilityDriftReviewDialog
        open={reviewOpen}
        onOpenChange={setReviewOpen}
        poolId={pool.id}
        poolName={pool.name}
        poolEnabled={pool.enabled}
        registryId={member.registry_id}
        registryName={member.name}
      />
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑「{member.name}」权重</DialogTitle>
          </DialogHeader>
          <Input
            type="number"
            min={1}
            max={100}
            value={weight}
            onChange={(event) => setWeight(event.target.value)}
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setEditOpen(false)}>
              取消
            </Button>
            <Button
              onClick={() =>
                put.mutate(
                  {
                    poolId: pool.id,
                    registryId: member.registry_id,
                    payload: { weight: Number(weight) },
                  },
                  {
                    onSuccess: () => setEditOpen(false),
                    onError: (error) =>
                      pushToast({
                        msg: "权重更新失败",
                        sub: (error as Error).message,
                        kind: "warning",
                      }),
                  },
                )
              }
            >
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog open={removeOpen} onOpenChange={setRemoveOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>移除成员「{member.name}」</AlertDialogTitle>
            <AlertDialogDescription>
              成员必须已 drain，且路由账本能确认 inflight 为 0。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() =>
                remove.mutate(
                  { poolId: pool.id, registryId: member.registry_id },
                  {
                    onSuccess: () => setRemoveOpen(false),
                    onError: (error) =>
                      pushToast({
                        msg: "移除成员失败",
                        sub: (error as Error).message,
                        kind: "warning",
                      }),
                  },
                )
              }
            >
              移除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

/**
 * Count AI-enabled projects whose primary pool equals this pool.
 *
 * Plan Appendix A.4: project ↔ pool relationship is authoritative (Project.ml_backend_pool_id).
 * Falls back to 0 when overview is unavailable (Project Admin / load failure).
 */
function countProjectsForPool(poolId: string, scope: RegistryScope): number {
  if (!scope.overview) return 0;
  // overview() returns per-project enabled backend rows; without a pool_id field
  // exposed on MLBackendItem, we approximate via the service-pool admin list:
  // if a pool contains a member whose registry_id appears in any project's
  // enabled backends, count it. This is a *display* approximation only — the
  // authoritative project↔pool binding lives server-side.
  const memberRegistryIds = new Set(
    (scope.vm.pools.find((p) => p.id === poolId)?.members ?? []).map((m) => m.registry_id),
  );
  if (memberRegistryIds.size === 0) return 0;
  let count = 0;
  for (const proj of scope.overview.projects) {
    if (proj.backends.some((b) => memberRegistryIds.has(b.id))) count += 1;
  }
  return count;
}

// (no re-exports — section is a leaf presentational component)
