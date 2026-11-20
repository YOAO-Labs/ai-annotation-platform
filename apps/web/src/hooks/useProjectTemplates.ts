// React Query hooks for project templates.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  projectTemplatesApi,
  type ProjectTemplateCreatePayload,
  type ProjectTemplateListParams,
  type ProjectTemplateUpdatePayload,
} from "../api/projectTemplates";
import { useAuthStore } from "@/stores/authStore";

const KEY = "project-templates";

export function useProjectTemplates(params?: ProjectTemplateListParams) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: [KEY, params, userId, tokenEpoch],
    queryFn: ({ signal }) => projectTemplatesApi.list(params, { signal }),
    enabled: Boolean(userId),
  });
}

export function useProjectTemplate(id: string | undefined) {
  return useQuery({
    queryKey: [KEY, "detail", id],
    queryFn: () => projectTemplatesApi.get(id!),
    enabled: !!id,
  });
}

export function useCreateProjectTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProjectTemplateCreatePayload) => projectTemplatesApi.create(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: [KEY] });
    },
  });
}

export function useUpdateProjectTemplate(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProjectTemplateUpdatePayload) => projectTemplatesApi.update(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: [KEY] });
    },
  });
}

export function useDeleteProjectTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => projectTemplatesApi.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: [KEY] });
    },
  });
}

export function useDuplicateProjectTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => projectTemplatesApi.duplicate(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: [KEY] });
    },
  });
}
