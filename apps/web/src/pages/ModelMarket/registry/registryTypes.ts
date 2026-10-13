/**
 * v0.23.4 P3 · shared prop shapes passed from the registry orchestrator
 * (`RegisteredBackendsTab`) to the five section components under `registry/`.
 *
 * The orchestrator owns the queries, the view-model merge and the registry URL
 * state (plan §5: URL / 角色 → codec → 编排壳 → 子视图); each section is a
 * presentational receiver that reads its own slice of `RegistryUrlState` and
 * patches it back through `RegistryUrlPatch`. Keeping these shapes in one
 * place lets the section components stay focused on rendering without
 * re-deriving the pool/member/GPU cross-references.
 */
import type { ComponentType } from "react";

import type {
  GPUArbiterResourceItem,
  GlobalBackendItem,
  MLIntegrationsOverview,
} from "@/api/adminMlIntegrations";
import type { ServicePoolAdminItem, TopologyResponse } from "@/api/generated/types.gen";
import type { UrlStateIssue } from "@/hooks/useUrlFilterState";
import type { Diagnostic, RuntimeTopologyViewModel } from "../runtimeTopology";
import type { RegistryUrlState } from "../marketUrlState";

/**
 * URL patcher handed down from the orchestrator's `useUrlFilterState`.
 * `replace: false` pushes a history entry — reserved for explicit tab and
 * object jumps (plan §5); text edits and filter tweaks replace.
 */
export type RegistryUrlPatch = (
  update: Partial<RegistryUrlState>,
  options?: { replace?: boolean },
) => void;

/** Static once-per-render bundle the orchestrator hands to sections. */
export interface RegistryScope {
  /** True for SUPER_ADMIN. Project Admin sees a trimmed, read-only view. */
  isSuperAdmin: boolean;
  /** Topology view model (pools with merged runtime snapshot). */
  vm: RuntimeTopologyViewModel;
  /** Raw topology (used by Issue Center for affected-member lookups). */
  topology: TopologyResponse;
  /** Service-pool admin list (Super Admin only; undefined for Project Admin). */
  servicePools: ServicePoolAdminItem[] | null;
  /** Global backend registry (super-admin: with GPU config; project admin: GPU-nulled). */
  backends: GlobalBackendItem[];
  /** GPU arbiter resources (Super Admin only). */
  gpuResources: GPUArbiterResourceItem[] | null;
  /** Overview (project bindings; Super Admin only). */
  overview: MLIntegrationsOverview | null;
  /** Deduped diagnostics for the Issue Center and severity badges. */
  diagnostics: Diagnostic[];
  /** Router mode (topology-side; always present). */
  routerMode: RuntimeTopologyViewModel["router_mode"];
  /**
   * Read-state flags so sections can tell "still loading" apart from a real
   * empty collection (plan §6: 只在请求完成后显示最终空态，未知不写成 0)。
   */
  loading: {
    backends: boolean;
    gpu: boolean;
    overview: boolean;
  };
}

/** Base props every registry section receives (scope + its URL slice). */
export interface RegistrySectionProps {
  scope: RegistryScope;
  /** Full registry URL state; sections read the keys they own. */
  url: RegistryUrlState;
  patchUrl: RegistryUrlPatch;
  /** URL-state issues (invalid enums etc.) the section should surface. */
  urlIssues: UrlStateIssue[];
}

/** A registry section component takes the shared scope + the URL slice. */
export type RegistrySectionComponent = ComponentType<RegistrySectionProps>;
