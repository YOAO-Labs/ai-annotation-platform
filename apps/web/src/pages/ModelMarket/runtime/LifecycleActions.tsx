/**
 * v0.23.4 · lifecycle action cluster for a pool member.
 *
 * Draining and a fresh zero-work observation gate the unload request. The
 * server still decides whether the backend is safe to unload. This component
 * keeps a routable / busy / stale instance from offering that request.
 *
 * Mutations are wired through existing hooks (registry health/unload,
 * project-scoped warmup) plus the v0.23.3 pool-member drain/resume contract.
 * All success paths invalidate topology + runtime-snapshot + observe so the
 * caller's three queries refresh together.
 */
import type { ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { adminMlIntegrationsApi } from "@/api/adminMlIntegrations";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/shadcn/ui/tooltip";
import { useRegistryHealth, useRegistryUnload } from "../useGlobalRegistry";
import {
  evaluateUnloadGate,
  type MemberViewModel,
  type RuntimeTopologyViewModel,
} from "../runtimeTopology";

export interface LifecycleActionsProps {
  poolId: string;
  member: MemberViewModel;
  /**
   * View model supplies route execution-state freshness for the unload request
   * gate. Null when runtime snapshot is unavailable (Project Admin or load
   * failure) — in that case the request remains disabled.
   */
  topology: RuntimeTopologyViewModel | null;
  /** Project id for warmup; null/undefined when no project binding exists. */
  projectId?: string | null;
  /** Compact mode for table rows (icon-only buttons). */
  compact?: boolean;
}

/** Whether the backend contract ships a force-unload endpoint (v0.23.3: no). */
export const FORCE_UNLOAD_CONTRACT_AVAILABLE = false;

function ledgerFreshFromTopology(topology: RuntimeTopologyViewModel | null): boolean {
  if (!topology) return false;
  const ledger = topology.sources.find((s) => s.name === "router_ledger");
  // An absent source cannot establish that there is no open execution work.
  if (!ledger) return false;
  return !ledger.stale;
}

export function LifecycleActions({
  poolId,
  member,
  topology,
  projectId,
  compact = false,
}: LifecycleActionsProps): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const qc = useQueryClient();
  const health = useRegistryHealth();
  const unload = useRegistryUnload();

  const routerMode = topology?.router_mode ?? "off";
  const ledgerFresh = ledgerFreshFromTopology(topology);
  const gate = evaluateUnloadGate(member, routerMode, ledgerFresh);

  const drain = useMutation({
    mutationFn: () => adminMlIntegrationsApi.drainPoolMember(poolId, member.registry_id),
    onSuccess: () => {
      invalidateRuntimeQueries(qc);
      pushToast({
        msg: `${member.name} 已停止接流（draining）`,
        kind: "success",
      });
    },
    onError: (e) => pushToast({ msg: "停流失败", sub: (e as Error).message, kind: "error" }),
  });

  const resume = useMutation({
    mutationFn: () => adminMlIntegrationsApi.resumePoolMember(poolId, member.registry_id),
    onSuccess: () => {
      invalidateRuntimeQueries(qc);
      pushToast({ msg: `${member.name} 已恢复接流（active）`, kind: "success" });
    },
    onError: (e) =>
      pushToast({
        msg: "恢复接流失败",
        sub: (e as Error).message,
        kind: "error",
      }),
  });

  // This submits an unload request; the server performs the final safety check.
  const onUnload = () => {
    unload.mutate(member.registry_id, {
      onSuccess: (res) =>
        pushToast({
          msg: res.unloaded
            ? `${member.name} 已接受卸载请求，等待 residency 确认`
            : `${member.name} 未报告需要卸载`,
          kind: "success",
        }),
      onError: (e) => pushToast({ msg: "卸载失败", sub: (e as Error).message, kind: "error" }),
    });
  };

  const onHealth = () => {
    health.mutate(member.registry_id, {
      onSuccess: (res) =>
        pushToast({
          msg: `${member.name}: ${res.status}`,
          kind: res.status === "ok" ? "success" : "warning",
        }),
      onError: (e) =>
        pushToast({
          msg: "健康检查失败",
          sub: (e as Error).message,
          kind: "error",
        }),
    });
  };

  const drainTip = "停止接收新的路由执行（active → draining）";
  const unloadTip = gate.can_request_unload
    ? "当前观测未见活动执行；可请求卸载，服务端仍会再次核验运行状态"
    : `暂不可请求卸载：${gate.reasons.join("；")}`;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {member.traffic_state === "active" && (
        <TooltipProvider delayDuration={200}>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size={compact ? "xs" : "xs"}
                onClick={() => drain.mutate()}
                disabled={drain.isPending}
              >
                <Icon name="pause" size={10} />
                停流
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top">{drainTip}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )}
      {member.traffic_state === "draining" && (
        <Button
          size="xs"
          onClick={() => resume.mutate()}
          disabled={resume.isPending}
          title="恢复接流（draining → active）"
        >
          <Icon name="play" size={10} />
          恢复接流
        </Button>
      )}
      <TooltipProvider delayDuration={200}>
        <Tooltip>
          <TooltipTrigger asChild>
            {/* span wraps the disabled button: Radix TooltipTrigger won't
                forward its ref onto a disabled <button>, so anchor on span. */}
            <span tabIndex={0} aria-label="卸载状态">
              <Button
                size="xs"
                onClick={onUnload}
                disabled={!gate.can_request_unload || unload.isPending}
              >
                <Icon name="pause" size={10} />
                卸载
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs whitespace-normal text-left">
            {unloadTip}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <Button size="xs" onClick={onHealth} disabled={health.isPending} title="触发健康检查">
        <Icon name="refresh" size={10} />
        健康检查
      </Button>

      {/* Warmup lives in the detail Sheet (variant picker is heavy); kept off
          the compact row to avoid duplicating the picker. projectId is still
          surfaced via the detail Sheet for the warm path. */}
      {!compact && projectId && (
        <span className="text-2xs text-muted-foreground" title="预热入口在详情 Sheet">
          可预热
        </span>
      )}

      {FORCE_UNLOAD_CONTRACT_AVAILABLE && null /* future AlertDialog hook */}
    </div>
  );
}

/** Invalidate the three runtime queries + legacy registry lists. */
export function invalidateRuntimeQueries(qc: ReturnType<typeof useQueryClient>): void {
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "topology"] });
  qc.invalidateQueries({
    queryKey: ["admin", "ml-integrations", "runtime-snapshot"],
  });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "observe"] });
  // legacy registry list still consumed by other surfaces (wizard, etc.)
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "all"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "overview"] });
}
