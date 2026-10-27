import { apiClient } from "./client";
import type {
  LineageOut,
  LineageNode,
  ModelDecisionCreate,
  ModelDecisionOut,
  ModelDecisionPage,
} from "./generated/types.gen";
export type NodeKind = LineageNode["kind"];
export type { LineageOut, LineageNode, ModelDecisionCreate, ModelDecisionOut };
export const modelLineageApi = {
  get: (
    projectId: string,
    kind: NodeKind,
    nodeId: string,
    relation?: string,
    cursor?: string,
    options?: RequestInit,
    limit = 20,
  ) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (relation) query.set("relation", relation);
    if (cursor) query.set("cursor", cursor);
    return apiClient.get<LineageOut>(
      `/projects/${projectId}/model-lineage/${kind}/${nodeId}?${query}`,
      options,
    );
  },
  decisions: (
    projectId: string,
    modelId: string,
    before?: number,
    options?: RequestInit,
    limit = 20,
  ) =>
    apiClient.get<ModelDecisionPage>(
      `/projects/${projectId}/trained-models/${modelId}/decisions?limit=${limit}${before ? `&before=${before}` : ""}`,
      options,
    ),
  append: (projectId: string, modelId: string, body: ModelDecisionCreate, options?: RequestInit) =>
    apiClient.post<ModelDecisionOut>(
      `/projects/${projectId}/trained-models/${modelId}/decisions`,
      body,
      options,
    ),
};

export function lineageReturn(projectId: string, value: string | null) {
  const base = `/projects/${projectId}/learning`;
  if (!value || value.length > 3000 || value.includes("\\")) return `${base}?tab=models`;
  try {
    const url = new URL(value, "https://lineage.invalid");
    if (
      url.origin !== "https://lineage.invalid" ||
      !(url.pathname === base || url.pathname.startsWith(`${base}/`))
    )
      return `${base}?tab=models`;
    return url.pathname + url.search;
  } catch {
    return `${base}?tab=models`;
  }
}
