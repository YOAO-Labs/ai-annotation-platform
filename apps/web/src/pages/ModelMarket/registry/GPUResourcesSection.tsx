/**
 * v0.23.4 P3 · registry "GPU 资源" tab (Super Admin only).
 *
 * Plan §4.3（模型市场多 TAB UI 优化 · 阶段一）: columns collapse to 资源身份 /
 * 静态声明与预算 / 运行时 committed / 队列与租约 / 模式 / 最高诊断. The static
 * (claimed vs allocatable) and runtime (committed) readings stay TWO separate
 * bars — static oversell percentage is never presented as physical VRAM
 * utilization; an unknown budget renders text instead of a fake 0% bar.
 *
 * Identity rows prefer the physical device token / node and show the full
 * gpu_resource_id only as a short id + copy (完整值在 tooltip/详情)。No device
 * serial → no invented "GPU 0/1" naming. `gpu_id` deep-links expand + scroll
 * to the row; unknown ids get an explicit note. A `gpu_q` search matches
 * resource id / node / device token and lives in this view's toolbar.
 */
import { useEffect, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/shadcn/ui/table";
import { Progress } from "@/components/shadcn/ui/progress";
import { Input } from "@/components/shadcn/ui/input";

import type {
  GPUArbiterResourceItem,
  GPUArbiterResourcesResponse,
} from "@/api/adminMlIntegrations";
import { formatShortId } from "./registryShared";
import { urlIssuesForView } from "../marketUrlState";
import { EmptyState, LoadingState, NullCell, ShortCopyableId, UrlIssueChips } from "./registryUi";
import type { RegistryScope, RegistrySectionProps } from "./registryTypes";
import type { Diagnostic } from "../runtimeTopology";
import { DiagnosticBadge } from "../runtime/DiagnosticBadge";

type GpuStatus = GPUArbiterResourceItem["status"];

const GPU_STATUS_VARIANT: Record<GpuStatus, "success" | "accent" | "warning" | "danger"> = {
  ok: "success",
  info: "accent",
  warning: "warning",
  critical: "danger",
  blocker: "danger",
};

function gpuStatusLabel(s: GpuStatus): string {
  switch (s) {
    case "ok":
      return "正常";
    case "info":
      return "信息";
    case "warning":
      return "告警";
    case "critical":
      return "严重";
    case "blocker":
      return "阻断";
    default:
      return s;
  }
}

export function GPUResourcesSection({
  scope,
  url,
  patchUrl,
  urlIssues,
  summary,
}: RegistrySectionProps & { summary: GPUArbiterResourcesResponse | null }): ReactNode {
  const { gpuResources, loading } = scope;
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // gpu_id 定位键（plan §5）：资源存在 → 展开受影响实例并滚动到该行。
  const focusGpuId = url.gpuId;
  const resources = gpuResources ?? [];
  const focusGpuExists = resources.some((r) => r.gpu_resource_id === focusGpuId);
  useEffect(() => {
    if (!focusGpuId || !focusGpuExists) return;
    setExpanded((prev) => {
      if (prev.has(focusGpuId)) return prev;
      const next = new Set(prev);
      next.add(focusGpuId);
      return next;
    });
    const row = document.querySelector<HTMLElement>(`[data-gpu-row="${focusGpuId}"]`);
    row?.scrollIntoView?.({ block: "nearest" });
  }, [focusGpuId, focusGpuExists]);

  const rows = resources.filter((r) => {
    if (!url.gpuQ) return true;
    const q = url.gpuQ.trim().toLowerCase();
    return `${r.gpu_resource_id} ${r.node_id} ${r.physical_device_token ?? ""}`
      .toLowerCase()
      .includes(q);
  });

  if (loading.gpu) {
    return <LoadingState label="加载 GPU 资源…" />;
  }

  if (!gpuResources) {
    return (
      <EmptyState
        icon="activity"
        message="GPU 资源不可用"
        hint="仅超级管理员可见；若刚切换角色请刷新页面。"
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Icon
            name="search"
            size={12}
            className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            aria-label="搜索 GPU 资源"
            placeholder="搜索资源 ID / 节点 / 设备"
            value={url.gpuQ}
            onChange={(e) => patchUrl({ gpuQ: e.target.value })}
            className="h-8 w-56 pl-7 text-xs"
          />
        </div>
        <UrlIssueChips issues={urlIssuesForView(urlIssues, "gpu")} onDismiss={() => undefined} />
      </div>
      {summary && <GPUResourceSummary summary={summary} />}
      {focusGpuId && !focusGpuExists && (
        <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
          <span>对象不存在或当前不可访问（gpu_id={focusGpuId}）</span>
          <Button size="sm" variant="ghost" onClick={() => patchUrl({ gpuId: "" })}>
            移除定位
          </Button>
        </div>
      )}
      {resources.length === 0 ? (
        <EmptyState icon="activity" message="尚未配置 GPU_ARBITER_RESOURCES_JSON" />
      ) : (
        <Table containerClassName="overflow-hidden rounded-lg border border-border bg-card">
          <TableHeader>
            <TableRow>
              <TableHead className="w-8" />
              <TableHead>资源</TableHead>
              <TableHead>静态声明 / 预算</TableHead>
              <TableHead>运行时 committed</TableHead>
              <TableHead>队列与租约</TableHead>
              <TableHead>模式</TableHead>
              <TableHead>最高诊断</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((resource) => (
              <GpuResourceRow
                key={resource.gpu_resource_id}
                resource={resource}
                scope={scope}
                expanded={expanded.has(resource.gpu_resource_id)}
                focused={resource.gpu_resource_id === focusGpuId}
                onToggle={() => toggle(resource.gpu_resource_id)}
              />
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={7}>
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    没有匹配的 GPU 资源
                  </div>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

function GPUResourceSummary({ summary }: { summary: GPUArbiterResourcesResponse }): ReactNode {
  return (
    <div
      data-testid="gpu-resource-summary"
      className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/30 px-3 py-2"
    >
      <Badge variant={summary.runtime_ready ? "success" : "danger"}>
        <Icon name={summary.runtime_ready ? "checkCircle" : "alert-triangle"} size={11} />
        {summary.runtime_ready ? "运行时就绪" : "运行时未就绪"}
      </Badge>
      <span className="text-xs text-muted-foreground">全局期望模式</span>
      <Badge variant="outline">{summary.global_desired_mode}</Badge>
      <Badge variant={summary.observe_runtime_ready ? "success" : "warning"}>
        Observe {summary.observe_runtime_ready ? "就绪" : "未就绪"}
      </Badge>
      <Badge variant={summary.enforce_runtime_ready ? "success" : "warning"}>
        Enforce {summary.enforce_runtime_ready ? "就绪" : "未就绪"}
      </Badge>
      <Badge variant={summary.rollout_enabled ? "accent" : "outline"}>
        Rollout {summary.rollout_enabled ? "已启用" : "未启用"}
      </Badge>
    </div>
  );
}

function GpuResourceRow({
  resource,
  scope,
  expanded,
  focused,
  onToggle,
}: {
  resource: GPUArbiterResourceItem;
  scope: RegistryScope;
  expanded: boolean;
  focused: boolean;
  onToggle: () => void;
}): ReactNode {
  const allocatable = resource.allocatable_mb;
  const claimed = resource.claimed_budget_mb;
  const committed = resource.runtime?.committed_mb ?? null;
  const budgetKnown = allocatable > 0;
  // 静态超售百分比只描述「声明 / 可分配预算」，绝不冒充物理显存实时利用率；
  // 预算未知时不画空进度条冒充 0%（plan §4.3）。
  const staticPct = budgetKnown ? (claimed / allocatable) * 100 : null;
  const runtimePct =
    committed != null && budgetKnown ? Math.min(100, (committed / allocatable) * 100) : null;
  const maxDiagnostic = pickMaxSeverityGpuDiagnostic(resource.gpu_resource_id, scope.diagnostics);
  const affectedInstances = collectAffectedInstances(resource.gpu_resource_id, scope);
  // 不凭空命名：有设备序号用设备序号，否则显示短 ID。
  const deviceToken = resource.physical_device_token?.trim() || "";

  return (
    <>
      <TableRow
        data-gpu-row={resource.gpu_resource_id}
        aria-expanded={expanded}
        className={expanded || focused ? "bg-muted/40" : undefined}
      >
        <TableCell className="align-middle">
          <button
            type="button"
            onClick={onToggle}
            aria-label={expanded ? "折叠受影响实例" : "展开受影响实例"}
            aria-expanded={expanded}
            className="inline-flex size-6 items-center justify-center rounded text-muted-foreground hover:bg-accent"
          >
            <Icon name={expanded ? "chevDown" : "chevRight"} size={12} />
          </button>
        </TableCell>
        <TableCell>
          <div className="flex flex-col gap-0.5">
            <div className="flex items-center gap-2">
              <Badge variant={GPU_STATUS_VARIANT[resource.status]}>
                <Icon
                  name={resource.status === "ok" ? "checkCircle" : "alert-triangle"}
                  size={11}
                />
                <span>{gpuStatusLabel(resource.status)}</span>
              </Badge>
              {deviceToken ? (
                <span className="mono text-xs font-medium">{deviceToken}</span>
              ) : (
                <span className="text-xs text-muted-foreground">未提供设备序号</span>
              )}
            </div>
            <div className="flex items-center gap-2 text-2xs text-muted-foreground">
              <span>节点 · {resource.node_id}</span>
            </div>
            <ShortCopyableId value={resource.gpu_resource_id} label="GPU 资源 ID" />
            <div className="text-2xs text-muted-foreground">
              配置 · {resource.configured_mode ?? "未配置"}
            </div>
          </div>
        </TableCell>
        <TableCell>
          <div className="flex w-36 flex-col gap-1">
            <div className="flex items-center justify-between text-2xs text-muted-foreground">
              <span className="mono">{claimed.toLocaleString()} MiB</span>
              <span>{staticPct != null ? `${Math.round(staticPct)}%` : "预算未知"}</span>
            </div>
            {staticPct != null ? (
              <Progress
                value={Math.min(100, staticPct)}
                className={staticPct > 100 ? "bg-status-caution-soft" : undefined}
              />
            ) : null}
            <span className="text-2xs text-muted-foreground">
              {budgetKnown ? `预算 ${allocatable.toLocaleString()} MiB · ` : ""}
              {resource.claimed_backend_count} 个 backend
            </span>
            {staticPct != null && staticPct > 100 && (
              <span className="text-2xs text-status-caution">弹性超售</span>
            )}
          </div>
        </TableCell>
        <TableCell>
          {committed != null && runtimePct != null ? (
            <div className="flex w-36 flex-col gap-1">
              <div className="flex items-center justify-between text-2xs text-muted-foreground">
                <span className="mono">{committed.toLocaleString()} MiB</span>
                <span>{Math.round(runtimePct)}%</span>
              </div>
              {/* Separate bar from static — plan §6.1 mandates two. */}
              <Progress value={runtimePct} />
            </div>
          ) : committed != null ? (
            <div className="flex flex-col gap-0.5">
              <span className="mono text-xs">{committed.toLocaleString()} MiB</span>
              <span className="text-2xs text-muted-foreground">预算未知，无法计算占用</span>
            </div>
          ) : (
            <NullCell>未上报</NullCell>
          )}
        </TableCell>
        <TableCell>
          <div className="flex flex-col gap-0.5 text-2xs text-muted-foreground">
            <span>card 队列 · {fmtQueue(resource.runtime?.card_queue_count)}</span>
            <span>backend 队列 · {fmtQueue(resource.runtime?.backend_queue_count)}</span>
            <span>
              租约 ·{" "}
              {resource.runtime?.lease_count != null ? (
                <span className="text-foreground">{resource.runtime.lease_count}</span>
              ) : (
                "未上报"
              )}
            </span>
          </div>
        </TableCell>
        <TableCell>
          <div className="flex flex-col gap-0.5">
            <Badge variant="outline" className="w-fit text-2xs">
              {resource.desired_mode}
            </Badge>
            <span className="text-2xs text-muted-foreground">→ {resource.effective_mode}</span>
          </div>
        </TableCell>
        <TableCell>
          {maxDiagnostic ? (
            <DiagnosticBadge diagnostic={maxDiagnostic} showAffected />
          ) : (
            <NullCell>无</NullCell>
          )}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow className="bg-muted/20">
          <TableCell />
          <TableCell colSpan={6}>
            <AffectedInstancesSubRow
              gpuResourceId={resource.gpu_resource_id}
              instances={affectedInstances}
            />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function AffectedInstancesSubRow({
  gpuResourceId,
  instances,
}: {
  gpuResourceId: string;
  instances: Array<{ registry_id: string; name: string; pool_name: string }>;
}): ReactNode {
  if (instances.length === 0) {
    return (
      <div className="py-2 text-xs text-muted-foreground">
        GPU 资源 {formatShortId(gpuResourceId)} 当前没有拓扑纳管的实例
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 py-2">
      <div className="text-2xs text-muted-foreground">受影响实例（{instances.length}）</div>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs">
        {instances.map((it) => (
          <li key={it.registry_id} className="flex items-center gap-2">
            <Icon name="bot" size={11} className="text-muted-foreground" />
            <span className="font-medium">{it.name}</span>
            <span className="text-2xs text-muted-foreground">服务池 · {it.pool_name}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── helpers ──────────────────────────────────────────────────────────────────

function fmtQueue(n: number | null | undefined): string {
  if (n == null) return "—";
  return String(n);
}

function pickMaxSeverityGpuDiagnostic(
  gpuResourceId: string,
  diagnostics: Diagnostic[],
): Diagnostic | null {
  const sevRank: Record<Diagnostic["severity"], number> = {
    info: 0,
    warning: 1,
    critical: 2,
    blocker: 3,
  };
  const related = diagnostics.filter((d) => d.affected_gpu_resource_ids.includes(gpuResourceId));
  if (related.length === 0) return null;
  return related.reduce((max, d) => (sevRank[d.severity] > sevRank[max.severity] ? d : max));
}

function collectAffectedInstances(
  gpuResourceId: string,
  scope: RegistryScope,
): Array<{ registry_id: string; name: string; pool_name: string }> {
  const out: Array<{ registry_id: string; name: string; pool_name: string }> = [];
  for (const pool of scope.vm.pools) {
    for (const member of pool.members) {
      if (member.gpu_resource_id === gpuResourceId) {
        out.push({
          registry_id: member.registry_id,
          name: member.name,
          pool_name: pool.name,
        });
      }
    }
  }
  return out;
}

// Re-export the queue formatter for callers that want consistent labels.
export const _gpuQueueLabel = fmtQueue;
