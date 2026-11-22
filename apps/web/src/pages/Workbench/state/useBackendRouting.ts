import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { apiErrorDetailMessage } from "@/api/client";
import { mlBackendsApi, mlBackendSetupQueryKey, type MLBackendCapability } from "@/api/ml-backends";
import { INTERACTIVE_ROUTE_PROMPT_IDS } from "@/api/generated/capabilityVocab.gen";

export const INTERACTIVE_PROMPTS = INTERACTIVE_ROUTE_PROMPT_IDS;
export type InteractivePrompt = (typeof INTERACTIVE_ROUTE_PROMPT_IDS)[number];

function isInteractivePrompt(p: string): p is InteractivePrompt {
  return (INTERACTIVE_PROMPTS as readonly string[]).includes(p);
}

export interface BackendCapEntry {
  prompts: Set<InteractivePrompt>;
  interactiveModels: Array<{
    id: string;
    prompts: Set<InteractivePrompt>;
    inputs: Set<string>;
    outputs: Set<string>;
  }>;
  videoModels: Array<{
    id: string;
    trackers: string[];
    prompts: Set<string>;
    inputs: Set<string>;
    outputs: Set<string>;
    textDrivenTrackers: Set<string>;
    maxWindowFrames: number | null;
  }>;
  textCapable: boolean;
  isInteractive: boolean;
  trackers: string[];
  textDrivenTrackers: string[];
  reachable: boolean;
}

export function buildCapEntry(cap: MLBackendCapability | undefined): BackendCapEntry {
  const prompts = new Set<InteractivePrompt>();
  const interactiveModels: BackendCapEntry["interactiveModels"] = [];
  let textCapable = false;
  let isInteractive = false;
  const trackers = new Set<string>();
  const textDrivenTrackers = new Set<string>();

  if (!cap) {
    return {
      prompts,
      interactiveModels: [],
      videoModels: [],
      textCapable,
      isInteractive,
      trackers: [],
      textDrivenTrackers: [],
      reachable: false,
    };
  }

  const models = Array.isArray(cap.models) ? cap.models : [];
  const videoModels: BackendCapEntry["videoModels"] = [];
  const addPrompts = (interactive: boolean, supported: string[] | undefined) => {
    for (const p of supported ?? []) {
      if (p === "text") textCapable = true;
      if (interactive && isInteractivePrompt(p)) prompts.add(p);
    }
  };

  if (models.length > 0) {
    for (const m of models) {
      const mInteractive = m.is_interactive === true;
      if (mInteractive) isInteractive = true;
      addPrompts(mInteractive, m.supported_prompts);
      if (mInteractive) {
        interactiveModels.push({
          id: m.id,
          prompts: new Set((m.supported_prompts ?? []).filter(isInteractivePrompt)),
          inputs: new Set(m.supported_inputs ?? []),
          outputs: new Set(m.supported_geometric_outputs ?? []),
        });
      }
      if ((m.supported_trackers?.length ?? 0) > 0) {
        videoModels.push({
          id: m.id,
          trackers: [...(m.supported_trackers ?? [])],
          prompts: new Set(m.supported_prompts ?? []),
          inputs: new Set(m.supported_inputs ?? []),
          outputs: new Set(m.supported_geometric_outputs ?? []),
          textDrivenTrackers: new Set(m.text_driven_trackers ?? []),
          maxWindowFrames:
            typeof m.max_window_frames === "number" &&
            Number.isInteger(m.max_window_frames) &&
            m.max_window_frames > 0
              ? m.max_window_frames
              : null,
        });
      }
      for (const t of m.supported_trackers ?? []) trackers.add(t);
      for (const t of m.text_driven_trackers ?? []) textDrivenTrackers.add(t);
    }
  } else {
    const topInteractive = cap.is_interactive !== false;
    if (topInteractive) isInteractive = true;
    addPrompts(topInteractive, cap.supported_prompts);
    if (topInteractive) {
      interactiveModels.push({
        id: "__top__",
        prompts: new Set((cap.supported_prompts ?? []).filter(isInteractivePrompt)),
        inputs: new Set(cap.supported_inputs ?? []),
        outputs: new Set(cap.supported_geometric_outputs ?? []),
      });
    }
    if ((cap.supported_trackers?.length ?? 0) > 0) {
      videoModels.push({
        id: "__top__",
        trackers: [...(cap.supported_trackers ?? [])],
        prompts: new Set(cap.supported_prompts ?? []),
        inputs: new Set(cap.supported_inputs ?? []),
        outputs: new Set(cap.supported_geometric_outputs ?? []),
        textDrivenTrackers: new Set(cap.text_driven_trackers ?? []),
        maxWindowFrames:
          typeof cap.max_window_frames === "number" &&
          Number.isInteger(cap.max_window_frames) &&
          cap.max_window_frames > 0
            ? cap.max_window_frames
            : null,
      });
    }
  }
  for (const t of cap.supported_trackers ?? []) trackers.add(t);
  for (const t of cap.text_driven_trackers ?? []) textDrivenTrackers.add(t);

  return {
    prompts,
    interactiveModels,
    videoModels,
    textCapable,
    isInteractive,
    trackers: [...trackers],
    textDrivenTrackers: [...textDrivenTrackers],
    reachable: true,
  };
}

export type CapIndex = Record<string, BackendCapEntry>;

export function capFingerprint(cap: MLBackendCapability | undefined): string {
  if (!cap) return "";
  const models = Array.isArray(cap.models) ? cap.models : [];
  if (models.length > 0) {
    return models
      .map(
        (m) =>
          `${m.is_interactive ? 1 : 0}/${(m.supported_prompts ?? []).join(",")}/${(m.supported_inputs ?? []).join(",")}/${(m.supported_geometric_outputs ?? []).join(",")}/${(m.supported_trackers ?? []).join(",")}/${(m.text_driven_trackers ?? []).join(",")}/${m.max_window_frames ?? ""}`,
      )
      .join(";");
  }
  return `${cap.is_interactive === false ? 0 : 1}/${(cap.supported_prompts ?? []).join(",")}/${(cap.supported_inputs ?? []).join(",")}/${(cap.supported_geometric_outputs ?? []).join(",")}/${(cap.supported_trackers ?? []).join(",")}/${(cap.text_driven_trackers ?? []).join(",")}/${cap.max_window_frames ?? ""}`;
}

export interface InteractiveRouteRequirement {
  prompt: InteractivePrompt;
  requiredInputs: string[];
  output: string;
}

function entrySupportsRequest(
  entry: BackendCapEntry | undefined,
  requirement: InteractiveRouteRequirement,
): boolean {
  return Boolean(
    entry?.reachable &&
    entry.interactiveModels.some(
      (model) =>
        model.prompts.has(requirement.prompt) &&
        model.outputs.has(requirement.output) &&
        requirement.requiredInputs.every((input) => model.inputs.has(input)),
    ),
  );
}

export function candidatesForRequest(
  capIndex: CapIndex,
  order: string[],
  requirement: InteractiveRouteRequirement,
): string[] {
  return order.filter((id) => entrySupportsRequest(capIndex[id], requirement));
}

export function resolveInteractiveRequest(
  capIndex: CapIndex,
  order: string[],
  defaultBackendId: string | null,
  preferredId: string | null,
  requirement: InteractiveRouteRequirement,
): string | null {
  const candidates = candidatesForRequest(capIndex, order, requirement);
  if (preferredId && candidates.includes(preferredId)) return preferredId;
  if (defaultBackendId && candidates.includes(defaultBackendId)) return defaultBackendId;
  return candidates[0] ?? null;
}

export function candidatesFor(capIndex: CapIndex, order: string[], p: InteractivePrompt): string[] {
  return order.filter((id) => {
    const e = capIndex[id];
    return !!e && e.reachable && e.prompts.has(p);
  });
}

export function resolveInteractive(
  capIndex: CapIndex,
  order: string[],
  defaultBackendId: string | null,
  preferredId: string | null,
  p: InteractivePrompt,
): string | null {
  const cands = candidatesFor(capIndex, order, p);
  if (cands.length === 0) return null;
  if (preferredId && cands.includes(preferredId)) return preferredId;
  if (defaultBackendId && cands.includes(defaultBackendId)) return defaultBackendId;
  return cands[0];
}

export function pickDefaultPreferred(
  capIndex: CapIndex,
  order: string[],
  defaultBackendId: string | null,
): string | null {
  if (
    defaultBackendId &&
    capIndex[defaultBackendId]?.reachable &&
    capIndex[defaultBackendId].prompts.size > 0
  ) {
    return defaultBackendId;
  }
  return order.find((id) => capIndex[id]?.reachable && capIndex[id].prompts.size > 0) ?? null;
}

export interface BackendRoutingArgs {
  projectId: string | null | undefined;
  backends: Array<{ id: string; name: string }>;
  defaultBackendId: string | null;
  savedInteractiveBackendId?: string | null;
  onSaveInteractiveBackend: (id: string | null) => void;
}

export interface BackendRoutingResult {
  capIndex: CapIndex;
  isLoading: boolean;
  isFetching: boolean;
  capabilityErrors: Array<{ backendId: string; backendName: string; message: string }>;
  retryCapabilities: () => Promise<unknown>;
  isPromptSupported: (type: string) => boolean;
  resolveInteractive: (p: InteractivePrompt) => string | null;
  resolveInteractiveRequest: (requirement: InteractiveRouteRequirement) => string | null;
  candidatesFor: (p: InteractivePrompt) => string[];
  preferredInteractiveId: string | null;
  setPreferredInteractiveId: (id: string | null) => void;
}

export function useBackendRouting({
  projectId,
  backends,
  defaultBackendId,
  savedInteractiveBackendId,
  onSaveInteractiveBackend,
}: BackendRoutingArgs): BackendRoutingResult {
  const order = useMemo(() => backends.map((b) => b.id), [backends]);

  const queries = useQueries({
    queries: backends.map((b) => ({
      queryKey: mlBackendSetupQueryKey(projectId, b.id),
      queryFn: () => mlBackendsApi.setup(projectId as string, b.id),
      enabled: !!projectId,
      staleTime: 5 * 60 * 1000,
      retry: 0,
    })),
  });

  const isLoading = !!projectId && queries.some((q) => q.isLoading);
  const isFetching = !!projectId && queries.some((q) => q.isFetching);
  const capabilityErrors = projectId
    ? queries.flatMap((query, index) =>
        query.isError
          ? [
              {
                backendId: backends[index].id,
                backendName: backends[index].name,
                message: apiErrorDetailMessage(query.error) || "模型能力加载失败，请重试",
              },
            ]
          : [],
      )
    : [];
  const retryCapabilities = useCallback(
    () =>
      Promise.all(
        projectId
          ? queries
              .filter((query) => query.isError)
              .map((query) => query.refetch({ cancelRefetch: false }))
          : [],
      ),
    [projectId, queries],
  );

  const capSignature = queries
    .map(
      (q, i) =>
        `${order[i]}:${q.isError ? "err" : q.data ? `ok(${capFingerprint(q.data)})` : "pend"}`,
    )
    .join("|");
  const capIndex = useMemo<CapIndex>(() => {
    const idx: CapIndex = {};
    backends.forEach((b, i) => {
      const q = queries[i];
      idx[b.id] = q?.isError ? buildCapEntry(undefined) : buildCapEntry(q?.data);
    });
    return idx;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capSignature]);

  const [preferredOverride, setPreferredOverride] = useState<string | null>(null);
  useEffect(() => {
    setPreferredOverride(savedInteractiveBackendId ?? null);
  }, [savedInteractiveBackendId]);

  const fallbackPreferred = useMemo(
    () => pickDefaultPreferred(capIndex, order, defaultBackendId),
    [capIndex, order, defaultBackendId],
  );
  const overrideValid =
    !!preferredOverride &&
    !!capIndex[preferredOverride]?.reachable &&
    capIndex[preferredOverride].prompts.size > 0;
  const preferredInteractiveId = overrideValid ? preferredOverride : fallbackPreferred;

  const setPreferredInteractiveId = useCallback(
    (id: string | null) => {
      setPreferredOverride(id);
      onSaveInteractiveBackend(id);
    },
    [onSaveInteractiveBackend],
  );

  return {
    capIndex,
    isLoading,
    isFetching,
    capabilityErrors,
    retryCapabilities,
    isPromptSupported: (type: string) => {
      if (isInteractivePrompt(type)) {
        return candidatesFor(capIndex, order, type).length > 0;
      }
      if (type === "text") {
        return order.some((id) => capIndex[id]?.reachable && capIndex[id].textCapable);
      }
      return false;
    },
    resolveInteractive: (p) =>
      resolveInteractive(capIndex, order, defaultBackendId, preferredInteractiveId, p),
    resolveInteractiveRequest: (requirement) =>
      resolveInteractiveRequest(
        capIndex,
        order,
        defaultBackendId,
        preferredInteractiveId,
        requirement,
      ),
    candidatesFor: (p) => candidatesFor(capIndex, order, p),
    preferredInteractiveId,
    setPreferredInteractiveId,
  };
}
