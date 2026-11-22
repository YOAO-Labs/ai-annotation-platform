import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

export type SecondaryPrefEntry = {
  params?: Record<string, unknown>;
  variants?: Record<string, unknown>;
};

export function useSecondaryParamPrefs() {
  const userId = useAuthStore((s) => s.user?.id);
  const queryClient = useQueryClient();
  const { prefs, loaded } = useUserPreferences();
  const server = prefs?.ai?.secondary_by_model as Record<string, SecondaryPrefEntry> | undefined;
  const [byModel, setByModel] = useState<Record<string, SecondaryPrefEntry>>({});
  const pendingRef = useRef<Record<string, SecondaryPrefEntry> | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setByModel(server ?? {});
  }, [server]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const save = useCallback(
    (key: string, entry: SecondaryPrefEntry) => {
      setByModel((prev) => {
        const next = { ...prev, [key]: { ...prev[key], ...entry } };
        pendingRef.current = next;
        return next;
      });
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        const payload = pendingRef.current;
        if (!payload) return;
        authApi
          .updatePreferences({ ai: { secondary_by_model: payload } })
          .then(() => queryClient.invalidateQueries({ queryKey: userPreferencesQueryKey(userId) }))
          .catch(() => {});
      }, 600);
    },
    [queryClient, userId],
  );

  return { byModel, loaded, save };
}
