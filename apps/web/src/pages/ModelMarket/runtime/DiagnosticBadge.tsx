import { CopyableError } from "@/components/ui/CopyableError";
/**
 * v0.23.4 · diagnostic severity badge with affected-objects count.
 *
 * Plan §7.2: severity badges use icon + text (never color-only). The badge
 * shows the affected-object count so an operator can scan scope at a glance;
 * clicking jumps to the diagnostic detail (caller wires the onClick).
 */
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { SEVERITY_TOKENS } from "./StateTokens";
import type { Diagnostic } from "../runtimeTopology";

export interface DiagnosticBadgeProps {
  diagnostic: Diagnostic;
  onClick?: (d: Diagnostic) => void;
  /** Show affected-objects count chip (default true). */
  showAffected?: boolean;
}

export function DiagnosticBadge({
  diagnostic,
  onClick,
  showAffected = true,
}: DiagnosticBadgeProps): ReactNode {
  const token = SEVERITY_TOKENS[diagnostic.severity];
  const affectedCount =
    diagnostic.affected_service_pool_ids.length +
    diagnostic.affected_instance_ids.length +
    diagnostic.affected_gpu_resource_ids.length;
  const clickable = onClick !== undefined;
  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        disabled={!clickable}
        onClick={clickable ? () => onClick?.(diagnostic) : undefined}
        className={
          clickable
            ? "cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-full"
            : "cursor-default"
        }
      >
        <Badge variant={token.variant}>
          <Icon name={token.icon} size={11} />
          <span>{diagnostic.message}</span>
          {showAffected && affectedCount > 1 && (
            <span className="text-muted-foreground">影响 {affectedCount}</span>
          )}
        </Badge>
      </button>
      <CopyableError message={diagnostic.message} iconOnly />
    </span>
  );
}
