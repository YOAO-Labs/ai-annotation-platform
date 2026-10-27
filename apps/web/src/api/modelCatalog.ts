import { useQuery } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { apiClient } from "./client";
import type { CatalogEntry, ModelCatalogResponse } from "./generated/types.gen";

export type {
  CatalogEntry,
  CatalogOperation,
  CatalogClassMetadata,
  ModelCatalogResponse,
} from "./generated/types.gen";

export type CatalogFilters = {
  project_id?: string;
  task?: string;
  source?: string;
  status?: string;
  q?: string;
  page?: number;
  page_size?: number;
};

export const modelCatalogApi = {
  list: (filters: CatalogFilters = {}, projectId?: string, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined && value !== "") query.set(key, String(value));
    });
    return apiClient.get<ModelCatalogResponse>(
      `${projectId ? `/projects/${projectId}` : ""}/model-catalog?${query}`,
      { signal },
    );
  },
};

export function useModelCatalog(filters: CatalogFilters = {}, projectId?: string, enabled = true) {
  const owner = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["model-catalog", owner, projectId ?? "global", filters],
    queryFn: ({ signal }) => modelCatalogApi.list(filters, projectId, signal),
    enabled,
    staleTime: 15_000,
  });
}

/** Read all matching variants before paginating display groups, so families never split across pages. */
export function useModelDirectory(filters: Omit<CatalogFilters, "page" | "page_size">) {
  const owner = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["model-catalog", owner, "directory", filters],
    queryFn: async ({ signal }) => {
      const first = await modelCatalogApi.list({ ...filters, page_size: 200 }, undefined, signal);
      const items = [...first.items];
      for (let page = 2; items.length < first.total; page++) {
        const next = await modelCatalogApi.list(
          { ...filters, page_size: 200, page },
          undefined,
          signal,
        );
        if (!next.items.length) break;
        items.push(...next.items);
      }
      return { ...first, items };
    },
    enabled: !!owner,
    staleTime: 15_000,
  });
}

export function isImageDetectionEntry(entry: CatalogEntry): boolean {
  return (
    entry.task === "detect" &&
    !entry.capabilities.is_interactive &&
    !(entry.capabilities.supported_prompts ?? []).includes("text") &&
    (entry.capabilities.supported_geometric_outputs ?? []).includes("bbox")
  );
}

export function classSourceLabel(entry: Pick<CatalogEntry, "class_metadata">): string {
  const metadata = entry.class_metadata;
  if (metadata.verification_status === "mismatch") return "权重类别与声明不一致";
  if (metadata.class_source === "checkpoint") return "来自权重 · 已核验";
  if (metadata.class_source === "training_config") return "来自训练配置 · 待权重核验";
  if (metadata.class_source === "request_prompt") return "按本次提示确定";
  return "预置声明 · 未核验";
}

/** Private project selection cache; reading never installs or loads a model. */
export function useProjectDetectionCatalog(projectId: string) {
  const owner = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["project-model-selection", owner, projectId],
    queryFn: async ({ signal }) => {
      const first = await modelCatalogApi.list({ page_size: 200 }, projectId, signal);
      const items = [...first.items];
      for (let page = 2; items.length < first.total; page++) {
        const next = await modelCatalogApi.list({ page_size: 200, page }, projectId, signal);
        if (!next.items.length) break;
        items.push(...next.items);
      }
      return { ...first, items };
    },
    enabled: !!projectId && !!owner,
    staleTime: 15_000,
  });
}
