import { useQuery } from "@tanstack/react-query";
import { apiClient } from "./client";

export interface SuggestedBackend {
  name: string;
  repo_url: string;
  summary: string;
  research_link: string | null;
  infra: string | null;
  builtin: boolean;
}

export interface ProtocolTask {
  id: string;
  label: string;
  summary: string;
  default_geometry: string[];
  default_modalities: string[];
  typical_models: string[];
  protocol_notes: string;
  suggested_backends: SuggestedBackend[];
}

export interface ProtocolInfra {
  id: string;
  label: string;
  summary: string;
}

export interface ProtocolModality {
  id: string;
  label: string;
  summary: string;
}

export interface ProtocolGeometry {
  id: string;
  label: string;
  summary: string;
}

export interface ProtocolPrompt {
  id: string;
  label: string;
  summary: string;
  requires_input: boolean;
  interactive_route: boolean;
}

export interface ProtocolInput {
  id: string;
  label: string;
  summary: string;
}

export interface ProtocolCapabilities {
  version: string;
  tasks: ProtocolTask[];
  infras: ProtocolInfra[];
  modalities: ProtocolModality[];
  geometries: ProtocolGeometry[];
  prompts: ProtocolPrompt[];
  inputs: ProtocolInput[];
}

export interface InstanceVariantOption {
  value: string;
  label?: string;
  vram_gb?: number;
  tier?: string;
  recommended?: boolean;
  note?: string;
}

export interface InstanceVariantGroup {
  key: string;
  title?: string;
  description?: string;
  variants?: InstanceVariantOption[];
}

export interface CapabilityInstanceModel {
  id: string;
  display_name: string;
  task: string;
  model_family?: string | null;
  composition?: "atom" | "composite";
  infra: string | null;
  is_interactive: boolean;
  supported_prompts: string[];
  supported_inputs?: string[];
  default_input_type?: string | null;
  resource_profile?: Record<string, unknown>;
  supported_geometric_outputs: string[];
  supported_trackers: string[];
  modality: string | null;
  supported_variants?: InstanceVariantGroup[];
  variant_combinations?: string[][];
  variants_shared_across_tasks?: boolean;
  default_variants?: Record<string, string>;
  output_attribute_types?: string[];
  output_attribute_schema?: OutputAttributeSchemaItem[];
  classes?: { index: number; name: string }[];
}

export interface OutputAttributeSchemaItem {
  key: string;
  label: string;
  type: string;
  options?: { value: string; label: string }[];
}

export interface CapabilityInstance {
  backend_id: string;
  state: string;
  source: "env" | "manual" | string;
  name: string;
  infra: string;
  warmup_endpoint?: boolean;
  models: CapabilityInstanceModel[];
}

export interface CapabilityInstancesResponse {
  instances: CapabilityInstance[];
}

export const mlCapabilitiesApi = {
  getProtocol: () => apiClient.get<ProtocolCapabilities>("/ml-capabilities/protocol"),
  getInstances: () => apiClient.get<CapabilityInstancesResponse>("/ml-capabilities/instances"),
};

export function useProtocolCapabilities() {
  return useQuery({
    queryKey: ["ml-capabilities", "protocol"],
    queryFn: () => mlCapabilitiesApi.getProtocol(),
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
  });
}

export function useCapabilityInstances() {
  return useQuery({
    queryKey: ["ml-capabilities", "instances"],
    queryFn: () => mlCapabilitiesApi.getInstances(),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}
