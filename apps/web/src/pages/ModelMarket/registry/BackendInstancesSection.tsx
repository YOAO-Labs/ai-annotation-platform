/**
 * v0.23.4 P3 · registry "实例" tab.
 *
 * Plan §4.3（模型市场多 TAB UI 优化 · 阶段一）: the table is for configuring
 * and locating physical endpoints. Main columns collapse to six —
 * 实例身份 (name + short id + URL subrow + source tag) / 所属服务池 /
 * 健康及路由 / 并发与 GPU 摘要 / 最近检查 / 操作 — so identity and actions fit
 * on screen together at 1440px. URL drops to a name subrow; weight, the full
 * GPU claim and raw snapshots live in the detail Sheet.
 *
 * Toolbar + filters belong to this view (plan §5): `instance_q` (name/ID/URL),
 * `instance_health`, and the removable pool focus condition
 * (`instance_pool=<id>`, set via the pool row's 查看实例 jump). `instance_id`
 * deep-links straight into the detail Sheet; unknown objects render an
 * explicit "对象不存在或当前不可访问" note instead of silently widening the
 * filter.
 *
 * Project Admin: weight, GPU claim and internal reason are hidden (server-side
 * projection already nulled them). Super Admin sees the risk-ordered action
 * menu; 详情 stays visible for both roles.
 */
import { useMemo, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { DropdownMenu, type DropdownItem } from "@/components/ui/DropdownMenu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/shadcn/ui/table";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/shadcn/ui/sheet";
import { ScrollArea } from "@/components/shadcn/ui/scroll-area";
import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { Input } from "@/components/shadcn/ui/input";

import type { GlobalBackendItem } from "@/api/adminMlIntegrations";
import { formatDateTime, gpuClaimOf, NO_LIMIT, registryStateToHealthAxis } from "./registryShared";
import { EmptyState, LoadingState, NullCell, ShortCopyableId, UrlIssueChips } from "./registryUi";
import { CapabilityDriftReviewDialog } from "./CapabilityDriftReviewDialog";
import type { RegistryScope, RegistrySectionProps } from "./registryTypes";
import type { MemberViewModel } from "../runtimeTopology";
import { evaluateUnloadGate } from "../runtimeTopology";
import { urlIssuesForView } from "../marketUrlState";
import { RuntimeStatusBadge } from "../runtime/RuntimeStatusBadge";
import { GlobalBackendFormModal, type GlobalRegistryEditTarget } from "../GlobalBackendFormModal";
import {
  useDeleteRegistry,
  useDrainPoolMember,
  useRegistryHealth,
  useRegistryUnload,
  useResumePoolMember,
} from "../useGlobalRegistry";

interface DetailState {
  backend: GlobalBackendItem;
}

interface ConfirmState {
  kind: "unload" | "delete";
  backend: GlobalBackendItem;
}

export function BackendInstancesSection({
  scope,
  url,
  patchUrl,
  urlIssues,
  onOpenRegister,
}: RegistrySectionProps & { onOpenRegister?: () => void }): ReactNode {
  const { isSuperAdmin, backends, loading } = scope;
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [editTarget, setEditTarget] = useState<GlobalRegistryEditTarget | null>(null);
  const [editOpen, setEditOpen] = useState(false);

  const registryToPool = useMemo(() => buildRegistryPoolLookup(scope), [scope]);
  const focusPool = url.instancePool
    ? (scope.vm.pools.find((p) => p.id === url.instancePool) ?? null)
    : null;
  const focusPoolUnknown = Boolean(url.instancePool) && !loading.backends && !focusPool;

  // The URL owns detail selection so history and refreshed data cannot leave a stale sheet.
  const focusInstanceId = url.instanceId;
  const detailBackend = backends.find((b) => b.id === focusInstanceId);
  const detail: DetailState | null = detailBackend ? { backend: detailBackend } : null;

  const closeDetail = () => {
    if (url.instanceId) patchUrl({ instanceId: "" });
  };

  const rows = useMemo(() => {
    return backends.filter((b) => {
      if (url.instancePool && registryToPool.get(b.id)?.poolId !== url.instancePool) {
        return false;
      }
      if (url.instanceQ) {
        const q = url.instanceQ.trim().toLowerCase();
        const hay = `${b.name} ${b.url} ${b.id} ${b.source_project_name ?? ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (url.instanceHealth !== "all") {
        const axis = registryStateToHealthAxis(b.state);
        if (axis !== url.instanceHealth) return false;
      }
      return true;
    });
  }, [backends, url.instanceQ, url.instanceHealth, url.instancePool, registryToPool]);

  if (loading.backends) {
    return <LoadingState label="加载实例列表…" />;
  }

  if (backends.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        <InstanceToolbar
          scope={scope}
          url={url}
          patchUrl={patchUrl}
          urlIssues={urlIssues}
          onOpenRegister={onOpenRegister}
        />
        <EmptyState
          icon="bot"
          message="尚无注册实例"
          hint={isSuperAdmin && onOpenRegister ? "点击「注册实例」添加。" : undefined}
        />
      </div>
    );
  }

  // Other registry sections wrap their toolbar/conditions/table in `gap-3`;
  // the instances view used a bare fragment, so its toolbar sat flush against
  // the table instead of matching the sibling tabs (plan §4.3).
  return (
    <div className="flex flex-col gap-3">
      <InstanceToolbar
        scope={scope}
        url={url}
        patchUrl={patchUrl}
        urlIssues={urlIssues}
        onOpenRegister={onOpenRegister}
      />
      {focusPool && (
        <div className="flex flex-wrap items-center gap-2">
          <ActiveFilterChip
            label={`仅显示服务池「${focusPool.name}」的实例`}
            onRemove={() => patchUrl({ instancePool: "" })}
          />
        </div>
      )}
      {focusPoolUnknown && (
        <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
          <span>对象不存在或当前不可访问（instance_pool={url.instancePool}）</span>
          <Button size="sm" variant="ghost" onClick={() => patchUrl({ instancePool: "" })}>
            移除定位
          </Button>
        </div>
      )}
      {focusInstanceId && !backends.some((b) => b.id === focusInstanceId) && (
        <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
          <span>对象不存在或当前不可访问（instance_id={focusInstanceId}）</span>
          <Button size="sm" variant="ghost" onClick={() => patchUrl({ instanceId: "" })}>
            移除定位
          </Button>
        </div>
      )}
      <Table containerClassName="overflow-hidden rounded-lg border border-border bg-card">
        <TableHeader>
          <TableRow>
            <TableHead>实例</TableHead>
            <TableHead>所属服务池</TableHead>
            <TableHead>健康及路由</TableHead>
            <TableHead>并发 / GPU</TableHead>
            <TableHead>最近检查</TableHead>
            <TableHead className="text-right">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((b) => (
            <InstanceRow
              key={b.id}
              backend={b}
              scope={scope}
              poolInfo={registryToPool.get(b.id) ?? null}
              onOpenDetail={(backend) => {
                // 对象跳转写入 URL 并 push 历史（plan §5），深链/返回可恢复。
                patchUrl({ instanceId: backend.id }, { replace: false });
              }}
              onConfirm={setConfirm}
              onEdit={(target) => {
                setEditTarget(target);
                setEditOpen(true);
              }}
            />
          ))}
          {rows.length === 0 && (
            <TableRow>
              <TableCell colSpan={6}>
                <div className="p-6 text-center text-sm text-muted-foreground">没有匹配的实例</div>
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>

      <InstanceDetailSheet detail={detail} scope={scope} onClose={closeDetail} />

      <InstanceConfirmDialog confirm={confirm} scope={scope} onClose={() => setConfirm(null)} />

      <GlobalBackendFormModal
        open={editOpen}
        backend={editTarget}
        onClose={() => setEditOpen(false)}
      />
    </div>
  );
}

/** 本子视图的工具栏：搜索/健康筛选/池定位清除 + 明确主操作「注册实例」。 */
function InstanceToolbar({
  scope,
  url,
  patchUrl,
  urlIssues,
  onOpenRegister,
}: RegistrySectionProps & { onOpenRegister?: () => void }): ReactNode {
  const hasConditions = url.instanceHealth !== "all" || Boolean(url.instancePool);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Icon
            name="search"
            size={12}
            className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            aria-label="搜索实例"
            placeholder="搜索名称 / ID / URL"
            value={url.instanceQ}
            onChange={(e) => patchUrl({ instanceQ: e.target.value })}
            className="h-8 w-56 pl-7 text-xs"
          />
        </div>
        <FilterGroup label="健康" hideLabel compact>
          <FilterSelect
            aria-label="按健康状态筛选实例"
            value={url.instanceHealth}
            onChange={(e) =>
              patchUrl({ instanceHealth: e.target.value as typeof url.instanceHealth })
            }
            className="h-8 text-xs"
          >
            <option value="all">全部</option>
            <option value="healthy">健康</option>
            <option value="degraded">降级</option>
            <option value="offline">离线</option>
            <option value="unknown">未知</option>
          </FilterSelect>
        </FilterGroup>
        {hasConditions && (
          <button
            type="button"
            onClick={() => patchUrl({ instanceHealth: "all", instancePool: "" })}
            className="text-2xs text-brand no-underline hover:underline"
          >
            清除条件
          </button>
        )}
        <div className="ml-auto flex items-center gap-2">
          {scope.isSuperAdmin && onOpenRegister && (
            <Button size="sm" variant="primary" onClick={onOpenRegister}>
              <Icon name="plus" size={11} />
              注册实例
            </Button>
          )}
        </div>
      </div>
      <UrlIssueChips
        issues={urlIssuesForView(urlIssues, "instances")}
        onDismiss={(key) => {
          if (key === "instance_health") patchUrl({ instanceHealth: "all" });
        }}
      />
    </div>
  );
}

function InstanceRow({
  backend,
  scope,
  poolInfo,
  onOpenDetail,
  onConfirm,
  onEdit,
}: {
  backend: GlobalBackendItem;
  scope: RegistryScope;
  poolInfo: {
    poolId: string;
    poolName: string;
    poolEnabled: boolean;
    member: MemberViewModel | null;
  } | null;
  onOpenDetail: (b: GlobalBackendItem) => void;
  onConfirm: (s: ConfirmState) => void;
  onEdit: (target: GlobalRegistryEditTarget) => void;
}): ReactNode {
  const { isSuperAdmin } = scope;
  const member = poolInfo?.member ?? null;
  const trafficState = member?.traffic_state ?? null;
  const claim = gpuClaimOf(backend);
  const maxConcurrency = backend.extra_params?.max_concurrency;

  return (
    <TableRow>
      {/* 身份：名称 + 短 ID + URL 副行 + 来源轻标签（plan §4.3）。 */}
      <TableCell>
        <div className="flex max-w-[260px] flex-col gap-0.5">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium" title={backend.name}>
              {backend.name}
            </span>
            <Badge variant="outline" className="shrink-0 text-2xs text-muted-foreground">
              {backend.source_project_name || "env"}
            </Badge>
          </div>
          <span
            className="mono max-w-full truncate text-2xs text-muted-foreground"
            title={backend.url}
          >
            {backend.url}
          </span>
          <ShortCopyableId value={backend.id} label="实例 ID" />
        </div>
      </TableCell>
      <TableCell>
        {poolInfo?.poolId ? (
          <div className="flex flex-col gap-0.5">
            <span className="text-sm">{poolInfo.poolName}</span>
            <ShortCopyableId value={poolInfo.poolId} label="服务池 ID" />
          </div>
        ) : (
          <NullCell>未纳管</NullCell>
        )}
      </TableCell>
      <TableCell>
        <TrafficStateCell
          axis={registryStateToHealthAxis(backend.state)}
          trafficState={trafficState}
          routing={member?.routing ?? "unknown"}
        />
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span className="text-sm">
            {typeof maxConcurrency === "number" ? (
              maxConcurrency
            ) : (
              <NullCell>并发 {NO_LIMIT}</NullCell>
            )}
          </span>
          {isSuperAdmin &&
            (claim ? (
              <span
                className="mono text-2xs text-muted-foreground"
                title={`${claim.gpu_resource_id} · ${claim.vram_budget_mb} MiB`}
              >
                GPU {claim.gpu_resource_id}
              </span>
            ) : (
              <span className="text-2xs text-muted-foreground">无 GPU 声明</span>
            ))}
        </div>
      </TableCell>
      <TableCell>
        <span className="text-xs text-muted-foreground">
          {formatDateTime(backend.last_checked_at)}
        </span>
      </TableCell>
      <TableCell className="text-right">
        <div className="flex items-center justify-end gap-1.5">
          <Button size="sm" variant="ghost" onClick={() => onOpenDetail(backend)} title="详情">
            <Icon name="info" size={11} />
            详情
          </Button>
          {isSuperAdmin && (
            <InstanceActionsMenu
              backend={backend}
              poolInfo={poolInfo}
              routerMode={scope.vm.router_mode}
              ledgerFresh={
                scope.vm.sources.find((source) => source.name === "router_ledger")?.stale === false
              }
              onConfirm={onConfirm}
              onEdit={onEdit}
            />
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

function TrafficStateCell({
  axis,
  trafficState,
  routing,
}: {
  axis: "healthy" | "degraded" | "offline" | "unknown";
  trafficState: MemberViewModel["traffic_state"] | null;
  routing: MemberViewModel["routing"];
}): ReactNode {
  return (
    <div className="flex flex-col gap-1">
      <RuntimeStatusBadge axis="health" value={axis} />
      {trafficState && (
        <Badge variant="outline" className="w-fit text-2xs">
          接流 · {trafficStateLabel(trafficState)}
        </Badge>
      )}
      <RuntimeStatusBadge axis="routing" value={routing} prefix="路由" />
    </div>
  );
}

function InstanceActionsMenu({
  backend,
  poolInfo,
  routerMode,
  ledgerFresh,
  onConfirm,
  onEdit,
}: {
  backend: GlobalBackendItem;
  poolInfo: {
    poolId: string;
    poolName: string;
    poolEnabled: boolean;
    member: MemberViewModel | null;
  } | null;
  routerMode: RegistryScope["vm"]["router_mode"];
  ledgerFresh: boolean;
  onConfirm: (s: ConfirmState) => void;
  onEdit: (target: GlobalRegistryEditTarget) => void;
}): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const health = useRegistryHealth();
  const drain = useDrainPoolMember();
  const resume = useResumePoolMember();
  const [reviewOpen, setReviewOpen] = useState(false);

  const poolId = poolInfo?.poolId ?? null;
  const member = poolInfo?.member ?? null;
  const unloadGate = member ? evaluateUnloadGate(member, routerMode, ledgerFresh) : null;

  const onHealth = () => {
    health.mutate(backend.id, {
      onSuccess: (res) =>
        pushToast({
          msg:
            res.status === "ok"
              ? `「${res.backend_name}」健康检查通过`
              : `「${res.backend_name}」检查失败`,
          kind: res.status === "ok" ? "success" : "warning",
        }),
      onError: (e) =>
        pushToast({ msg: "健康检查失败", sub: (e as Error).message, kind: "warning" }),
    });
  };

  const onEditClick = () => {
    onEdit({
      id: backend.id,
      name: backend.name,
      url: backend.url,
      auth_method: backend.auth_method,
      gpu_resource_id: backend.gpu_resource_id,
      vram_budget_mb: backend.vram_budget_mb,
      eviction_priority: backend.eviction_priority ?? 0,
    });
  };

  const onDrain = () => {
    if (!poolId) {
      pushToast({ msg: "实例未纳管到服务池，无法停流", kind: "warning" });
      return;
    }
    drain.mutate(
      { poolId, registryId: backend.id },
      {
        onSuccess: () => pushToast({ msg: `已对「${backend.name}」发起停流`, kind: "success" }),
        onError: (e) => pushToast({ msg: "停流失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  const onResume = () => {
    if (!poolId) {
      pushToast({ msg: "实例未纳管到服务池，无法恢复", kind: "warning" });
      return;
    }
    resume.mutate(
      { poolId, registryId: backend.id },
      {
        onSuccess: () => pushToast({ msg: `已恢复「${backend.name}」接流`, kind: "success" }),
        onError: (e) =>
          pushToast({ msg: "恢复接流失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  const memberTraffic = member?.traffic_state ?? null;
  const items: DropdownItem[] = [
    {
      id: "health",
      label: "健康检查",
      icon: "activity",
      disabled: health.isPending,
      onSelect: onHealth,
    },
    {
      id: "edit",
      label: "编辑",
      icon: "edit",
      onSelect: onEditClick,
    },
    { id: "div-1", divider: true, label: "" },
    {
      id: "drain",
      label: "暂停接流",
      icon: "pause",
      disabled: !poolId || memberTraffic !== "active" || drain.isPending,
      onSelect: onDrain,
    },
    {
      id: "resume",
      label: "恢复接流",
      icon: "play",
      disabled: !poolId || memberTraffic !== "draining" || resume.isPending,
      onSelect: onResume,
    },
    ...(memberTraffic === "disabled"
      ? [
          {
            id: "review-capability-drift",
            label: "审核能力变更",
            icon: "shield",
            onSelect: () => setReviewOpen(true),
          } as DropdownItem,
        ]
      : []),
    { id: "div-2", divider: true, label: "" },
    {
      id: "unload",
      label: "卸载",
      icon: "box",
      disabled: unloadGate !== null && !unloadGate.can_unload,
      onSelect: () => onConfirm({ kind: "unload", backend }),
    },
    {
      id: "delete",
      label: "删除",
      icon: "trash",
      onSelect: () => onConfirm({ kind: "delete", backend }),
    },
  ];

  return (
    <>
      <DropdownMenu
        minWidth={180}
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
            title="实例操作"
          >
            <Icon name="more" size={11} />
          </Button>
        )}
      />
      {poolInfo && memberTraffic === "disabled" && (
        <CapabilityDriftReviewDialog
          open={reviewOpen}
          onOpenChange={setReviewOpen}
          poolId={poolInfo.poolId}
          poolName={poolInfo.poolName}
          poolEnabled={poolInfo.poolEnabled}
          registryId={backend.id}
          registryName={backend.name}
        />
      )}
    </>
  );
}

function InstanceConfirmDialog({
  confirm,
  scope,
  onClose,
}: {
  confirm: ConfirmState | null;
  scope: RegistryScope;
  onClose: () => void;
}): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const del = useDeleteRegistry();
  const unload = useRegistryUnload();

  if (!confirm) return null;
  const { kind, backend } = confirm;
  const member = findMemberByRegistry(scope, backend.id);
  const ledgerFresh =
    scope.vm.sources.find((source) => source.name === "router_ledger")?.stale === false;
  const unloadGate =
    kind === "unload" && member
      ? evaluateUnloadGate(member, scope.vm.router_mode, ledgerFresh)
      : null;
  const unloadBlocked = unloadGate !== null && !unloadGate.can_unload;

  const onConfirm = () => {
    if (kind === "delete") {
      del.mutate(backend.id, {
        onSuccess: () => {
          pushToast({ msg: `已删除「${backend.name}」`, kind: "success" });
          onClose();
        },
        onError: (e) => {
          const err = e as { status?: number; message?: string };
          pushToast({
            msg: err.status === 409 ? "存在运行中的预标任务，无法删除" : "删除失败",
            sub: err.status === 409 ? err.message : (e as Error).message,
            kind: "warning",
          });
          onClose();
        },
      });
    } else {
      unload.mutate(backend.id, {
        onSuccess: () => {
          pushToast({ msg: `已对「${backend.name}」发起卸载`, kind: "success" });
          onClose();
        },
        onError: (e) => pushToast({ msg: "卸载失败", sub: (e as Error).message, kind: "warning" }),
      });
    }
  };

  const submitting = del.isPending || unload.isPending;

  return (
    <AlertDialog
      open={!!confirm}
      onOpenChange={(open) => {
        if (!open && !submitting) onClose();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {kind === "delete" ? "删除实例" : "卸载实例"}「{backend.name}」
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="flex flex-col gap-2 text-sm text-foreground">
              {kind === "delete" ? (
                <p>
                  确认删除实例？此操作不可撤销，且仅在没有运行中预标任务时可成功。删除前请先解除服务池成员关系。
                </p>
              ) : (
                <>
                  <p>卸载将触发 GPU 模型驻留回收（residence draining），不影响路由接流。</p>
                  {unloadBlocked && (
                    <div className="flex items-start gap-1.5 rounded-md border border-status-caution bg-status-caution-soft px-2.5 py-2 text-xs text-status-caution">
                      <Icon name="warning" size={12} className="mt-0.5 flex-shrink-0" />
                      <span>{unloadGate.reasons.join("；")}</span>
                    </div>
                  )}
                </>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>取消</AlertDialogCancel>
          <AlertDialogAction
            variant={kind === "delete" ? "destructive" : "default"}
            disabled={submitting || unloadBlocked}
            onClick={(e) => {
              e.preventDefault();
              onConfirm();
            }}
          >
            {submitting
              ? kind === "delete"
                ? "删除中..."
                : "卸载中..."
              : kind === "delete"
                ? "确认删除"
                : "确认卸载"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/**
 * 详情 Sheet（plan §4.3）：主行收敛后的重字段——权重、完整 GPU claim、原始
 * 快照——都落在这里；同时补充只读身份字段（所属服务池 / 接流状态 / 路由 /
 * 最大并发），详情关闭后焦点返回触发按钮由 Sheet 的RADIX 焦点管理兜底。
 */
function InstanceDetailSheet({
  detail,
  scope,
  onClose,
}: {
  detail: DetailState | null;
  scope: RegistryScope;
  onClose: () => void;
}): ReactNode {
  return (
    <Sheet open={!!detail} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-[min(560px,100vw)] sm:max-w-[560px]">
        <SheetHeader>
          <SheetTitle>{detail?.backend.name ?? ""}</SheetTitle>
          <SheetDescription>实例身份、服务池关系、GPU 配置与健康诊断</SheetDescription>
        </SheetHeader>
        {detail && (
          <DetailBody
            backend={detail.backend}
            poolInfo={buildRegistryPoolLookup(scope).get(detail.backend.id) ?? null}
            isSuperAdmin={scope.isSuperAdmin}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function DetailBody({
  backend,
  poolInfo,
  isSuperAdmin,
}: {
  backend: GlobalBackendItem;
  poolInfo: {
    poolId: string;
    poolName: string;
    poolEnabled: boolean;
    member: MemberViewModel | null;
  } | null;
  isSuperAdmin: boolean;
}): ReactNode {
  const gpuConfig = backend.gpu_config;
  const member = poolInfo?.member ?? null;
  return (
    <ScrollArea className="h-[calc(100vh-8rem)]">
      <div className="flex flex-col gap-4 px-4 pb-8 text-sm">
        <DetailSection title="基本">
          <DetailRow label="实例 ID">
            <span className="mono text-xs">{backend.id}</span>
          </DetailRow>
          <DetailRow label="URL">
            <span className="mono text-xs">{backend.url}</span>
          </DetailRow>
          <DetailRow label="状态">
            <Badge variant="outline">{backend.state}</Badge>
          </DetailRow>
          <DetailRow label="来源">
            <span>{backend.source_project_name || "—"}</span>
          </DetailRow>
          <DetailRow label="所属服务池">
            {poolInfo ? (
              <span className="text-xs">
                {poolInfo.poolName}
                <span className="mono ml-1.5 text-2xs text-muted-foreground">
                  {poolInfo.poolId}
                </span>
              </span>
            ) : (
              <span className="text-xs text-muted-foreground">未纳管</span>
            )}
          </DetailRow>
          {isSuperAdmin && member && (
            <DetailRow label="接流状态">
              <span className="text-xs">
                {trafficStateLabel(member.traffic_state)}
                {member.weight != null && ` · 权重 ${member.weight}`}
              </span>
            </DetailRow>
          )}
          {isSuperAdmin && member && (
            <DetailRow label="路由">
              <span className="text-xs">{member.routing}</span>
            </DetailRow>
          )}
          <DetailRow label="最大并发">
            <span className="text-xs">
              {typeof backend.extra_params?.max_concurrency === "number"
                ? backend.extra_params.max_concurrency
                : "未声明"}
            </span>
          </DetailRow>
          <DetailRow label="最近检查">
            <span className="text-xs text-muted-foreground">
              {formatDateTime(backend.last_checked_at)}
            </span>
          </DetailRow>
        </DetailSection>

        {gpuConfig && (
          <DetailSection title="GPU 仲裁">
            <DetailRow label="资源">
              <span className="mono text-xs">{backend.gpu_resource_id || "无声明"}</span>
            </DetailRow>
            <DetailRow label="静态预算">
              <span className="text-xs">
                {backend.vram_budget_mb ?? "—"} / {gpuConfig.allocatable_mb ?? "—"} MiB
              </span>
            </DetailRow>
            <DetailRow label="desired → effective">
              <span className="text-xs">
                {gpuConfig.desired_mode ?? "off"} → {gpuConfig.effective_mode ?? "off"}
              </span>
            </DetailRow>
            {gpuConfig.rollout_state && (
              <DetailRow label="rollout">
                <Badge variant="outline" className="text-2xs">
                  {gpuConfig.rollout_state}
                </Badge>
              </DetailRow>
            )}
          </DetailSection>
        )}

        {gpuConfig?.diagnostics && gpuConfig.diagnostics.length > 0 && (
          <DetailSection title="GPU 诊断">
            <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs">
              {gpuConfig.diagnostics.map((d, i) => (
                <li key={`${d.code}-${i}`} className="flex items-start gap-1.5">
                  <Badge
                    variant={
                      d.level === "critical" || d.level === "blocker"
                        ? "danger"
                        : d.level === "warning"
                          ? "warning"
                          : "outline"
                    }
                    className="text-2xs"
                  >
                    {d.level}
                  </Badge>
                  <span>{d.message}</span>
                </li>
              ))}
            </ul>
          </DetailSection>
        )}

        {backend.health_meta && (
          <DetailSection title="健康自报">
            {backend.health_meta.model_version && (
              <DetailRow label="model_version">
                <span className="mono text-xs">{backend.health_meta.model_version}</span>
              </DetailRow>
            )}
            {backend.health_meta.capabilities && (
              <DetailRow label="能力">
                <div className="flex flex-wrap gap-1">
                  {backend.health_meta.capabilities.modalities.map((m) => (
                    <Badge key={m} variant="ai" className="text-2xs">
                      {m}
                    </Badge>
                  ))}
                </div>
              </DetailRow>
            )}
            {backend.health_meta.compute && (
              <DetailRow label="compute">
                <span className="text-xs text-muted-foreground">
                  {backend.health_meta.compute.effective_device ??
                    backend.health_meta.compute.configured_device ??
                    "—"}
                  {backend.health_meta.compute.effective_provider
                    ? ` · ${backend.health_meta.compute.effective_provider}`
                    : ""}
                </span>
              </DetailRow>
            )}
          </DetailSection>
        )}
      </div>
    </ScrollArea>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <section className="flex flex-col gap-2">
      <h4 className="m-0 text-xs font-semibold text-muted-foreground">{title}</h4>
      <div className="flex flex-col gap-1.5">{children}</div>
    </section>
  );
}

function DetailRow({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="w-24 flex-shrink-0 text-2xs text-muted-foreground">{label}</span>
      <div className="min-w-0 flex-1 text-right">{children}</div>
    </div>
  );
}

// ── helpers ──────────────────────────────────────────────────────────────────

function buildRegistryPoolLookup(
  scope: RegistryScope,
): Map<
  string,
  { poolId: string; poolName: string; poolEnabled: boolean; member: MemberViewModel | null }
> {
  const out = new Map<
    string,
    { poolId: string; poolName: string; poolEnabled: boolean; member: MemberViewModel | null }
  >();
  for (const pool of scope.vm.pools) {
    for (const member of pool.members) {
      out.set(member.registry_id, {
        poolId: pool.id,
        poolName: pool.name,
        poolEnabled: pool.enabled,
        member,
      });
    }
  }
  return out;
}

function findMemberByRegistry(scope: RegistryScope, registryId: string): MemberViewModel | null {
  for (const pool of scope.vm.pools) {
    for (const member of pool.members) {
      if (member.registry_id === registryId) return member;
    }
  }
  return null;
}

function trafficStateLabel(state: MemberViewModel["traffic_state"]): string {
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
