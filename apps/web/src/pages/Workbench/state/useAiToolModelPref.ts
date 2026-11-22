import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

export function useAiToolModelPref(backendId: string | null | undefined) {
  const userId = useAuthStore((s) => s.user?.id);
  const queryClient = useQueryClient();
  const { prefs, loaded } = useUserPreferences();
  const server = prefs?.ai?.model_by_backend;
  const [byBackend, setByBackend] = useState<Record<string, string>>({});
  const pendingRef = useRef<Record<string, string> | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setByBackend(server ?? {});
  }, [server]);

  useEffect(() => {
    if (userId) return;
    setByBackend({});
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
          authApi.updatePreferences({ ai: { model_by_backend: payload } }).catch(() => {});
          pendingRef.current = null;
        }
      }
    };
  }, []);

  const savedModelId = backendId ? byBackend[backendId] : undefined;

  const save = useCallback(
    (modelId: string) => {
      if (!backendId) return;
      setByBackend((prev) => {
        const next = { ...prev, [backendId]: modelId };
        pendingRef.current = next;
        return next;
      });
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        const payload = pendingRef.current;
        if (!payload) return;
        authApi
          .updatePreferences({ ai: { model_by_backend: payload } })
          .then(() => queryClient.invalidateQueries({ queryKey: userPreferencesQueryKey(userId) }))
          .catch(() => {});
      }, 600);
    },
    [backendId, queryClient, userId],
  );

  return { savedModelId, loaded, save };
}
