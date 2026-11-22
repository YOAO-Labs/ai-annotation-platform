import { normalizeTask } from "@/api/generated/capabilityVocab.gen";
import { useMemo } from "react";
import { useQueries, useMutation, useQueryClient } from "@tanstack/react-query";
import { mlBackendsApi, type MLModelCapability } from "@/api/ml-backends";
import { hasInput, INPUT_CROP_ID } from "@/api/capabilityInputs";
import type { AttributeField } from "@/api/projects";
import {
  tasksApi,
  type SecondaryInferenceRequest,
  type SecondaryInferenceResponse,
} from "@/api/tasks";
import type { AnnotationResponse } from "@/types";
import { useMLBackends } from "@/hooks/useMLBackends";
import { isVariantField } from "../components/SchemaForm";

const ALLOWED_ATTR_TYPES = ["text", "number", "boolean", "select", "multiselect", "range"];

export interface SecondaryCapability {
  backendId: string;
  backendName: string;
  model: MLModelCapability;
  writeTarget: "attributes" | "geometry";
  label: string;
}

const GEOMETRY_TASKS = new Set(["detect", "segment", "obb", "pose"]);
const ATTRIBUTE_TASKS = new Set(["classify", "ocr", "doc_layout"]);

function deriveWriteTarget(m: MLModelCapability): "attributes" | "geometry" | null {
  const task = normalizeTask(m.task ?? "");
  if (GEOMETRY_TASKS.has(task)) return "geometry";
  if (ATTRIBUTE_TASKS.has(task)) return "attributes";
  if ((m.output_attribute_schema?.length ?? 0) > 0) return "attributes";
  if ((m.supported_geometric_outputs?.length ?? 0) > 0) return "geometry";
  return null;
}

function isCropRunnable(m: MLModelCapability): boolean {
  if (m.is_interactive) return false;
  return hasInput(m.supported_inputs, INPUT_CROP_ID);
}

export function hasConfigurableParams(m: MLModelCapability): boolean {
  const props = m.params?.properties;
  if (!props) return false;
  return Object.entries(props).some(
    ([key, raw]) => !isVariantField(key, (raw ?? {}) as Parameters<typeof isVariantField>[1]),
  );
}

export function useSecondaryCapabilities(projectId: string | undefined): {
  capabilities: SecondaryCapability[];
  isLoading: boolean;
} {
  const backendsQ = useMLBackends(projectId);
  const backends = backendsQ.data ?? [];

  const capabilityQueries = useQueries({
    queries: backends.map((b) => ({
      queryKey: ["ml-backends", projectId, b.id, "capabilities"],
      queryFn: () => mlBackendsApi.capabilities(projectId!, b.id),
      enabled: !!projectId,
      staleTime: 60_000,
    })),
  });

  const capabilities = useMemo<SecondaryCapability[]>(() => {
    const out: SecondaryCapability[] = [];
    capabilityQueries.forEach((q, i) => {
      const backend = backends[i];
      if (!backend || !q.data) return;
      for (const m of q.data.models ?? []) {
        if (!isCropRunnable(m)) continue;
        const writeTarget = deriveWriteTarget(m);
        if (!writeTarget) continue;
        out.push({
          backendId: backend.id,
          backendName: backend.name,
          model: m,
          writeTarget,
          label: m.display_name || m.id,
        });
      }
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backends, capabilityQueries.map((q) => q.data).join(",")]);

  return {
    capabilities,
    isLoading: backendsQ.isLoading || capabilityQueries.some((q) => q.isLoading),
  };
}

export function missingAttributeFields(
  cap: SecondaryCapability,
  existingKeys: Set<string>,
): AttributeField[] {
  if (cap.writeTarget !== "attributes") return [];
  const out: AttributeField[] = [];
  for (const item of cap.model.output_attribute_schema ?? []) {
    if (!item.key || existingKeys.has(item.key)) continue;
    const type = (
      ALLOWED_ATTR_TYPES.includes(item.type) ? item.type : "text"
    ) as AttributeField["type"];
    out.push({
      key: item.key,
      label: item.label || item.key,
      type,
      required: false,
      ...(item.options?.length
        ? { options: item.options.map((o) => ({ value: o.value, label: o.label })) }
        : {}),
    });
  }
  return out;
}

function mergeVariants(
  defaults: Record<string, string> | null | undefined,
  picked: Record<string, unknown> | undefined,
): Record<string, string> {
  const out: Record<string, string> = { ...(defaults ?? {}) };
  for (const [k, v] of Object.entries(picked ?? {})) {
    if (typeof v === "string") out[k] = v;
  }
  return out;
}

export function buildSecondaryInferencePayload(
  cap: SecondaryCapability,
  params?: Record<string, unknown>,
  variants?: Record<string, unknown>,
  prompt?: string,
): SecondaryInferenceRequest {
  const m = cap.model;
  const attrKeys = (m.output_attribute_schema ?? []).map((f) => f.key).filter(Boolean);
  const trimmedPrompt = prompt?.trim();
  return {
    ml_backend_id: cap.backendId,
    write_target: cap.writeTarget,
    model_id: m.id,
    model_variants:
      cap.writeTarget === "geometry" || (m.supported_variants?.length ?? 0) > 0
        ? mergeVariants(m.default_variants, variants)
        : null,
    task: m.task ? normalizeTask(m.task) : null,
    write_keys: cap.writeTarget === "attributes" && attrKeys.length > 0 ? attrKeys : null,
    params: params && Object.keys(params).length > 0 ? params : null,
    prompt: trimmedPrompt ? trimmedPrompt : null,
  };
}

export function needsTextPrompt(cap: SecondaryCapability): boolean {
  return (cap.model.supported_prompts ?? []).includes("text");
}

export function mergeSecondaryResult(
  prev: AnnotationResponse[] | undefined,
  data: SecondaryInferenceResponse,
): AnnotationResponse[] | undefined {
  if (!prev) return prev;
  const known = new Set(prev.map((a) => a.id));
  const merged = prev.map((a) =>
    a.id === data.annotation.id
      ? {
          ...a,
          attributes: data.annotation.attributes,
          attributes_meta: data.annotation.attributes_meta,
        }
      : a,
  );
  const fresh = data.created_children.filter((c) => !known.has(c.id));
  return fresh.length ? [...merged, ...fresh] : merged;
}

export function useRunSecondaryInference() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      annotationId,
      body,
    }: {
      taskId: string;
      annotationId: string;
      body: SecondaryInferenceRequest;
    }) => {
      if (!taskId) throw new Error("No task selected for secondary inference");
      return tasksApi.secondaryInference(taskId, annotationId, body);
    },
    onSuccess: (data, { taskId }) => {
      qc.setQueryData<AnnotationResponse[]>(["annotations", taskId], (prev) =>
        mergeSecondaryResult(prev, data),
      );
    },
  });
}
