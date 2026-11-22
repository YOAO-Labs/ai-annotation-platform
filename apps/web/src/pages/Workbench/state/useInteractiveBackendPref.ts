import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

export function useInteractiveBackendPref(projectId: string | null | undefined) {
  const userId = useAuthStore((s) => s.user?.id);
  const queryClient = useQueryClient();
  const { prefs, loaded } = useUserPreferences();
  const server = prefs?.ai?.interactive_backend_by_project;
  const [byProject, setByProject] = useState<Record<string, string>>({});
  const pendingRef = useRef<Record<string, string> | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setByProject(server ?? {});
  }, [server]);

  useEffect(() => {
    if (userId) return;
    setByProject({});
    pendingRef.current = null;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, [userId]);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
        const payload = pendingRef.current;
        if (payload) {
          authApi
            .updatePreferences({ ai: { interactive_backend_by_project: payload } })
            .catch(() => {});
          pendingRef.current = null;
        }
      }
    };
  }, []);

  const savedBackendId = projectId ? byProject[projectId] : undefined;

  const save = useCallback(
    (backendId: string | null) => {
      if (!projectId) return;
      setByProject((prev) => {
        const next = { ...prev };
        if (backendId) next[projectId] = backendId;
        else delete next[projectId];
        pendingRef.current = next;
        return next;
      });
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        const payload = pendingRef.current;
        if (!payload) return;
        authApi
          .updatePreferences({ ai: { interactive_backend_by_project: payload } })
          .then(() => queryClient.invalidateQueries({ queryKey: userPreferencesQueryKey(userId) }))
          .catch(() => {});
      }, 600);
    },
    [projectId, queryClient, userId],
  );

  return { savedBackendId, loaded, save };
}
