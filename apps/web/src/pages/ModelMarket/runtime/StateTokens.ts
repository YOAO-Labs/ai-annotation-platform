/**
 * central label + variant maps for the 4-axis status model.
 *
 * Plan Appendix A.1: every status axis has a stable label + semantic color.
 * Components consume these maps so labels never drift across the registry and
 * runtime pages. Color mappings follow the design-system semantic palette
 * (docs-site/dev/reference/design-system.md §Semantic Color): failure→danger,
 * warning→caution, success→positive, info→info/info-alt.
 */
import type {
  CapacityAxis,
  HealthAxis,
  ResidencyAxis,
  RoutingAxis,
  Severity,
} from "../runtimeTopology";

/** Badge variants from @/components/ui/Badge (kept local to avoid private import). */
type BadgeVariant = "default" | "success" | "warning" | "danger" | "accent" | "ai" | "outline";

/** Icon names that exist in ICON_MAP and fit a status chip. */
type StatusIcon =
  | "checkCircle"
  | "alert-triangle"
  | "circleDot"
  | "clock"
  | "box"
  | "info"
  | "activity";

export interface AxisToken {
  label: string;
  variant: BadgeVariant;
  /** Icon name (never color-only — design system rule). */
  icon: StatusIcon;
}

export const HEALTH_TOKENS: Record<HealthAxis, AxisToken> = {
  healthy: { label: "健康", variant: "success", icon: "checkCircle" },
  degraded: { label: "降级", variant: "warning", icon: "alert-triangle" },
  offline: { label: "离线", variant: "danger", icon: "alert-triangle" },
  unknown: { label: "未知", variant: "outline", icon: "circleDot" },
};

export const ROUTING_TOKENS: Record<RoutingAxis, AxisToken> = {
  routable: { label: "可路由", variant: "success", icon: "checkCircle" },
  draining: { label: "停流中", variant: "warning", icon: "clock" },
  // bypassed: router_mode != enforce → configured state is shadow.
  bypassed: { label: "未生效", variant: "outline", icon: "circleDot" },
  blocked: { label: "阻塞", variant: "danger", icon: "alert-triangle" },
  unknown: { label: "未知", variant: "outline", icon: "circleDot" },
};

export const CAPACITY_TOKENS: Record<CapacityAxis, AxisToken> = {
  idle: { label: "空闲", variant: "success", icon: "checkCircle" },
  serving: { label: "服务中", variant: "accent", icon: "activity" },
  saturated: { label: "饱和", variant: "danger", icon: "alert-triangle" },
  unknown: { label: "未知", variant: "outline", icon: "circleDot" },
};

export const RESIDENCY_TOKENS: Record<ResidencyAxis, AxisToken> = {
  empty: { label: "空", variant: "outline", icon: "box" },
  loading: { label: "加载中", variant: "accent", icon: "clock" },
  resident: { label: "已驻留", variant: "success", icon: "checkCircle" },
  draining: { label: "卸载中", variant: "warning", icon: "clock" },
  unloading: { label: "卸载中", variant: "warning", icon: "clock" },
  unknown: { label: "未知", variant: "outline", icon: "circleDot" },
};

export const SEVERITY_TOKENS: Record<Severity, AxisToken> = {
  info: { label: "提示", variant: "accent", icon: "info" },
  warning: { label: "告警", variant: "warning", icon: "alert-triangle" },
  critical: { label: "严重", variant: "danger", icon: "alert-triangle" },
  blocker: { label: "阻断", variant: "danger", icon: "alert-triangle" },
};

/** Map a topology pool status → health-axis token (for the pool-level badge). */
export function poolStatusToken(status: HealthAxis): AxisToken {
  return HEALTH_TOKENS[status] ?? HEALTH_TOKENS.unknown;
}
