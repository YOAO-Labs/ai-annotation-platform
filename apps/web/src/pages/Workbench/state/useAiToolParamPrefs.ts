import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

export function useAiToolParamPrefs(backendId: string | null | undefined) {
  const userId = useAuthStore((s) => s.user?.id);
  const queryClient = useQueryClient();
  const { prefs, loaded } = useUserPreferences();
  const server = prefs?.ai?.params_by_backend as
    | Record<string, Record<string, unknown>>
    | undefined;
  const [byBackend, setByBackend] = useState<Record<string, Record<string, unknown>>>({});
  const pendingRef = useRef<Record<string, Record<string, unknown>> | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setByBackend(server ?? {});
  }, [server]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const savedParams = backendId ? byBackend[backendId] : undefined;

  const save = useCallback(
    (params: Record<string, unknown>) => {
      if (!backendId) return;
      setByBackend((prev) => {
        const next = { ...prev, [backendId]: params };
        pendingRef.current = next;
        return next;
      });
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        const payload = pendingRef.current;
        if (!payload) return;
        authApi
          .updatePreferences({ ai: { params_by_backend: payload } })
          .then(() => queryClient.invalidateQueries({ queryKey: userPreferencesQueryKey(userId) }))
          .catch(() => {});
      }, 600);
    },
    [backendId, queryClient, userId],
  );

  return { savedParams, loaded, save };
}
