import { randomId } from "@/utils/id";
import { useCallback, useMemo, useReducer, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import {
  authApi,
  MAX_NAMED_WORKSPACE_PRESETS,
  MAX_WORKSPACE_PRESET_NAME_LENGTH,
  type NamedWorkspacePreset,
  type StoredNamedWorkspacePresets,
  type UserPreferences,
} from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import {
  readWorkspaceEnvelope,
  sanitizeWorkspaceSnapshot,
  WORKSPACE_SCHEMA_VERSION,
  WORKSPACE_CONTEXTS,
  type WorkspaceContext,
  type WorkspaceSnapshot,
} from "../layout/workbenchLayoutSnapshot";
import { userPreferencesQueryKey } from "./useUserPreferences";

export interface WorkbenchNamedPreset {
  id: string;
  name: string;
  context: WorkspaceContext;
  snapshot: WorkspaceSnapshot | null;
}

export type NamedPresetFailure =
  | "invalid-name"
  | "duplicate-name"
  | "limit"
  | "conflict"
  | "request";

function isNamedPresetsConflict(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  const detail = error.detailRaw;
  return (
    !!detail &&
    typeof detail === "object" &&
    (detail as { code?: unknown }).code === "named_presets_conflict"
  );
}

function readStoredPresets(value: unknown): StoredNamedWorkspacePresets {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as StoredNamedWorkspacePresets;
}

function readVisiblePreset(id: string, value: unknown): WorkbenchNamedPreset | null {
  if (
    !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id) ||
    !value ||
    typeof value !== "object" ||
    Array.isArray(value)
  )
    return null;
  const preset = value as Partial<NamedWorkspacePreset>;
  if (
    typeof preset.name !== "string" ||
    !WORKSPACE_CONTEXTS.includes(preset.context as WorkspaceContext)
  )
    return null;
  return {
    id,
    name: preset.name,
    context: preset.context as WorkspaceContext,
    snapshot: readWorkspaceEnvelope(preset).snapshot,
  };
}

export function useWorkbenchNamedPresets() {
  const userId = useAuthStore((state) => state.user?.id);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: userPreferencesQueryKey(userId),
    queryFn: () => authApi.getPreferences(),
    enabled: !!userId,
    staleTime: 5 * 60 * 1000,
  });
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const saving = useRef(false);

  const stored = useMemo(
    () => readStoredPresets(query.data?.workbench?.layout?.workspace?.namedPresets),
    [query.data],
  );
  const revision = query.data?.namedPresetsRevision ?? "0";

  const presets = useMemo<WorkbenchNamedPreset[]>(
    () =>
      Object.entries(stored)
        .map(([id, preset]) => readVisiblePreset(id, preset))
        .filter((preset): preset is WorkbenchNamedPreset => preset !== null)
        .sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN")),
    [stored],
  );

  const write = useCallback(
    async (next: StoredNamedWorkspacePresets): Promise<NamedPresetFailure | null> => {
      if (!userId || saving.current) return "request";
      saving.current = true;
      refresh();
      const queryKey = userPreferencesQueryKey(userId);
      try {
        await queryClient.cancelQueries({ queryKey, exact: true });
        const response = await authApi.updateNamedPresets({
          namedPresetsRevision: revision,
          workbench: { layout: { workspace: { namedPresets: next } } },
        });
        await queryClient.cancelQueries({ queryKey, exact: true });
        queryClient.setQueryData<UserPreferences>(queryKey, (previous) => {
          if (!previous) return response;
          const workspace =
            previous.workbench.layout.workspace ?? response.workbench.layout.workspace;
          return {
            ...previous,
            namedPresetsRevision: response.namedPresetsRevision ?? previous.namedPresetsRevision,
            workbench: {
              ...previous.workbench,
              layout: {
                ...previous.workbench.layout,
                workspace: workspace && {
                  ...workspace,
                  engine: response.workbench.layout.workspace?.engine ?? workspace.engine,
                  namedPresets: response.workbench.layout.workspace?.namedPresets ?? next,
                },
              },
            },
          };
        });
        return null;
      } catch (error) {
        const failure = isNamedPresetsConflict(error) ? "conflict" : "request";
        await queryClient.invalidateQueries({ queryKey, exact: true });
        return failure;
      } finally {
        saving.current = false;
        refresh();
      }
    },
    [queryClient, revision, userId],
  );

  const save = useCallback(
    async (
      name: string,
      context: WorkspaceContext,
      snapshot: WorkspaceSnapshot,
    ): Promise<NamedPresetFailure | null> => {
      const trimmed = name.trim();
      if (!trimmed || trimmed.length > MAX_WORKSPACE_PRESET_NAME_LENGTH) return "invalid-name";
      const matchingId = Object.entries(stored).find(([, value]) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) return false;
        const rawName = (value as { name?: unknown }).name;
        return typeof rawName === "string" && rawName.trim() === trimmed;
      })?.[0];
      const existing = presets.find(
        (preset) => preset.id === matchingId && preset.snapshot !== null,
      );
      if (matchingId && !existing) return "duplicate-name";
      if (!existing && Object.keys(stored).length >= MAX_NAMED_WORKSPACE_PRESETS) return "limit";
      let clean: WorkspaceSnapshot;
      try {
        clean = sanitizeWorkspaceSnapshot(snapshot);
      } catch {
        return "request";
      }
      const id = existing?.id ?? randomId();
      const next = {
        ...stored,
        [id]: {
          name: trimmed,
          context,
          schemaVersion: WORKSPACE_SCHEMA_VERSION,
          snapshot: clean,
        },
      };
      return write(next);
    },
    [presets, stored, write],
  );

  const rename = useCallback(
    async (id: string, name: string): Promise<NamedPresetFailure | null> => {
      const trimmed = name.trim();
      const preset = presets.find((entry) => entry.id === id);
      const raw = stored[id];
      if (!preset?.snapshot || !raw || typeof raw !== "object" || Array.isArray(raw))
        return "request";
      if (!trimmed || trimmed.length > MAX_WORKSPACE_PRESET_NAME_LENGTH) return "invalid-name";
      if (presets.some((value) => value.id !== id && value.name.trim() === trimmed))
        return "duplicate-name";
      const next = { ...stored, [id]: { ...raw, name: trimmed } };
      return write(next);
    },
    [presets, stored, write],
  );

  const remove = useCallback(
    async (id: string): Promise<NamedPresetFailure | null> => {
      if (!(id in stored)) return null;
      const next = Object.fromEntries(Object.entries(stored).filter(([other]) => other !== id));
      return write(next);
    },
    [stored, write],
  );

  return {
    presets,
    loaded: !!userId && !query.isPending && !query.isError,
    saving: saving.current,
    count: Object.keys(stored).length,
    full: Object.keys(stored).length >= MAX_NAMED_WORKSPACE_PRESETS,
    save,
    rename,
    remove,
  };
}
