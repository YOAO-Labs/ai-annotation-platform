import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiErrorDetailMessage } from "@/api/client";
import {
  mlBackendsApi,
  mlBackendSetupQueryKey,
  type MLBackendCapability,
  type MLModelCapability,
} from "@/api/ml-backends";

const FALLBACK_PROMPTS = ["point", "interactive_box", "text"] as const;

export interface MLCapabilitiesResult {
  prompts: string[];
  paramsSchema: MLBackendCapability["params"] | undefined;
  inputs: string[];
  capability: MLBackendCapability | undefined;
  isPromptSupported: (type: string) => boolean;
  isInputSupported: (type: string) => boolean;
  isLoading: boolean;
  isFetching: boolean;
  isError: boolean;
  error: string | null;
  refetch: () => Promise<unknown>;
  models: MLModelCapability[];
  activeModel: MLModelCapability | undefined;
  activeModelId: string | undefined;
  setActiveModelId: (id: string) => void;
  hasMultipleModels: boolean;
}

function pickDefaultModel(models: MLModelCapability[]): MLModelCapability | undefined {
  if (models.length === 0) return undefined;
  return models.find((m) => m.is_interactive) ?? models[0];
}

export function useMLCapabilities(
  projectId: string | undefined | null,
  backendId: string | undefined | null,
  preferredModelId?: string | null,
  requirement?: {
    prompt: string;
    requiredInputs: string[];
    output: string;
  } | null,
): MLCapabilitiesResult {
  const enabled = Boolean(projectId && backendId);
  const query = useQuery({
    queryKey: mlBackendSetupQueryKey(projectId, backendId),
    queryFn: () => mlBackendsApi.setup(projectId!, backendId!),
    enabled,
    staleTime: 5 * 60 * 1000,
    retry: 0,
  });

  const refetchSetup = query.refetch;
  const refetch = useCallback(
    () => (enabled ? refetchSetup({ cancelRefetch: false }) : Promise.resolve(undefined)),
    [enabled, refetchSetup],
  );
  const capability = query.data;
  const allModels = useMemo<MLModelCapability[]>(
    () => (Array.isArray(capability?.models) ? capability!.models : []),
    [capability],
  );
  const models = useMemo<MLModelCapability[]>(() => {
    if (!requirement) return allModels;
    return allModels.filter(
      (model) =>
        (model.supported_prompts ?? []).includes(requirement.prompt) &&
        (model.supported_geometric_outputs ?? []).includes(requirement.output) &&
        requirement.requiredInputs.every((input) => (model.supported_inputs ?? []).includes(input)),
    );
  }, [allModels, requirement]);

  const [selectedModelId, setSelectedModelId] = useState<string | undefined>(undefined);
  const activeModel = useMemo<MLModelCapability | undefined>(() => {
    if (models.length === 0) return undefined;
    const bySession = selectedModelId ? models.find((m) => m.id === selectedModelId) : undefined;
    const byPref = preferredModelId ? models.find((m) => m.id === preferredModelId) : undefined;
    return bySession ?? byPref ?? pickDefaultModel(models);
  }, [models, selectedModelId, preferredModelId]);

  let prompts: string[];
  if (query.isError) {
    prompts = [];
  } else if (activeModel && Array.isArray(activeModel.supported_prompts)) {
    prompts = activeModel.supported_prompts;
  } else if (requirement && allModels.length > 0) {
    prompts = [];
  } else if (capability && Array.isArray(capability.supported_prompts)) {
    prompts = capability.supported_prompts;
  } else if (capability) {
    if (typeof console !== "undefined") {
      console.warn(
        "[useMLCapabilities] backend /setup missing supported_prompts; falling back to point/interactive_box/text. Use a backend that declares supported_prompts to report its actual capabilities.",
      );
    }
    prompts = [...FALLBACK_PROMPTS];
  } else {
    prompts = [];
  }

  const paramsSchema =
    activeModel?.params && Object.keys(activeModel.params.properties ?? {}).length > 0
      ? activeModel.params
      : capability?.params;
  const inputs = activeModel?.supported_inputs ?? capability?.supported_inputs ?? [];

  return {
    prompts,
    paramsSchema,
    inputs,
    capability,
    isPromptSupported: (type: string) => prompts.includes(type),
    isInputSupported: (type: string) => inputs.includes(type),
    isLoading: enabled && query.isLoading,
    isFetching: enabled && query.isFetching,
    isError: enabled && query.isError,
    error:
      enabled && query.isError
        ? apiErrorDetailMessage(query.error) || "模型能力加载失败，请重试"
        : null,
    refetch,
    models,
    activeModel,
    activeModelId: activeModel?.id,
    setActiveModelId: setSelectedModelId,
    hasMultipleModels: models.length > 1,
  };
}
