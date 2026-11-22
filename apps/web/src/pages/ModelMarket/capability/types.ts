import type { MLBackendItem } from "@/api/adminMlIntegrations";
import type { CapabilityWarning, MLModelCapability } from "@/api/ml-backends";

export interface FlatModel {
  model: MLModelCapability;
  backendId: string;
  backendName: string;
  projectId: string;
  projectName: string;
  source: "env_only" | "registered";
  registeredProjects: string[];
  backendInfra?: string;
  backendModalities?: string[];
  healthMeta?: MLBackendItem["health_meta"];
  warmupEndpoint?: boolean;
  stale: boolean;
  warnings?: CapabilityWarning[];
}

export type CatalogViewMode = "cards" | "list";
export type CatalogGroupBy = "none" | "backend" | "task" | "infra";

export interface ListRow {
  parent: FlatModel;
  rowKey: string;
  primaryLabel: string;
  primaryId: string;
  tasks: string[];
  geometries: string[];
  secondaryLabel: string;
  secondaryTitle?: string;
  warmVariants: Record<string, string>;
  runtimeKey?: string;
}
