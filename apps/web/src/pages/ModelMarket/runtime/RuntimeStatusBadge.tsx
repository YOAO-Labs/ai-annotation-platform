import type { ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import type { IconName } from "@/components/ui/Icon";
import {
  CAPACITY_TOKENS,
  HEALTH_TOKENS,
  RESIDENCY_TOKENS,
  ROUTING_TOKENS,
  SEVERITY_TOKENS,
} from "./StateTokens";
import type { AxisToken } from "./StateTokens";
import type {
  CapacityAxis,
  HealthAxis,
  ResidencyAxis,
  RoutingAxis,
  Severity,
} from "../runtimeTopology";

type AxisKind = "health" | "routing" | "capacity" | "residency" | "severity";

function tokenFor(axis: AxisKind, value: string): AxisToken {
  switch (axis) {
    case "health":
      return HEALTH_TOKENS[value as HealthAxis];
    case "routing":
      return ROUTING_TOKENS[value as RoutingAxis];
    case "capacity":
      return CAPACITY_TOKENS[value as CapacityAxis];
    case "residency":
      return RESIDENCY_TOKENS[value as ResidencyAxis];
    case "severity":
      return SEVERITY_TOKENS[value as Severity];
  }
}

export interface RuntimeStatusBadgeProps {
  axis: AxisKind;
  value: HealthAxis | RoutingAxis | CapacityAxis | ResidencyAxis | Severity;
  prefix?: string;
  className?: string;
}

export function RuntimeStatusBadge({
  axis,
  value,
  prefix,
  className,
}: RuntimeStatusBadgeProps): ReactNode {
  const token =
    tokenFor(axis, value) ??
    ({
      label: "未知",
      variant: "outline",
      icon: "circleDot",
    } as AxisToken);
  const label = prefix ? `${prefix}: ${token.label}` : token.label;
  return (
    <Badge variant={token.variant} className={className}>
      <Icon name={token.icon as IconName} size={11} />
      <span>{label}</span>
    </Badge>
  );
}
