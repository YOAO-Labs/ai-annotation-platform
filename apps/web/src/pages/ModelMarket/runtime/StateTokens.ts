import type {
  CapacityAxis,
  HealthAxis,
  ResidencyAxis,
  RoutingAxis,
  Severity,
} from "../runtimeTopology";

type BadgeVariant = "default" | "success" | "warning" | "danger" | "accent" | "ai" | "outline";

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

export function poolStatusToken(status: HealthAxis): AxisToken {
  return HEALTH_TOKENS[status] ?? HEALTH_TOKENS.unknown;
}
