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

export type RegistryUrlPatch = (
  update: Partial<RegistryUrlState>,
  options?: { replace?: boolean },
) => void;

export interface RegistryScope {
  isSuperAdmin: boolean;
  vm: RuntimeTopologyViewModel;
  topology: TopologyResponse;
  servicePools: ServicePoolAdminItem[] | null;
  backends: GlobalBackendItem[];
  gpuResources: GPUArbiterResourceItem[] | null;
  overview: MLIntegrationsOverview | null;
  diagnostics: Diagnostic[];
  routerMode: RuntimeTopologyViewModel["router_mode"];
  loading: {
    backends: boolean;
    gpu: boolean;
    overview: boolean;
  };
}

export interface RegistrySectionProps {
  scope: RegistryScope;
  url: RegistryUrlState;
  patchUrl: RegistryUrlPatch;
  urlIssues: UrlStateIssue[];
}

export type RegistrySectionComponent = ComponentType<RegistrySectionProps>;
