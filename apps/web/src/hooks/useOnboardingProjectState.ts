import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { authApi, type OnboardingProjectState, type UserPreferences } from "@/api/auth";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { isGuideSeen } from "@/utils/annotationGuide";

type OnboardingProjectStatePatch = Partial<OnboardingProjectState>;

export function useOnboardingProjectState(projectId: string, guideVersion: string) {
  const user = useAuthStore((state) => state.user);
  const setUser = useAuthStore((state) => state.setUser);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [pendingPatch, setPendingPatch] = useState<OnboardingProjectStatePatch | null>(null);
  const requestSeq = useRef(0);
  const scope = useMemo(() => ({}), [user?.id, projectId, guideVersion]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  useEffect(() => {
    setIsSaving(false);
    setSaveError(null);
    setPendingPatch(null);
    return () => {
      requestSeq.current += 1;
    };
  }, [scope]);

  const stored = user?.preferences?.onboarding?.projects?.[projectId];
  const matchesVersion = stored?.guide_version === guideVersion;
  const guideReadFromStorage = isGuideSeen(user?.id, projectId, guideVersion);

  const state = useMemo(
    () => ({
      dismissed: matchesVersion ? Boolean(stored?.dismissed) : false,
      guideRead: (matchesVersion && Boolean(stored?.guide_read)) || guideReadFromStorage,
    }),
    [guideReadFromStorage, matchesVersion, stored?.dismissed, stored?.guide_read],
  );

  const save = useCallback(
    async (patch: OnboardingProjectStatePatch): Promise<boolean> => {
      const ownerId = user?.id;
      if (!ownerId || !isCurrentAuthOwner(ownerId)) return false;

      const seq = ++requestSeq.current;
      const current = () =>
        currentScope.current === scope && seq === requestSeq.current && isCurrentAuthOwner(ownerId);
      setSaveError(null);
      setPendingPatch(patch);
      setIsSaving(true);
      try {
        const preferences = await authApi.updatePreferences({
          onboarding: {
            projects: {
              [projectId]: {
                ...(useAuthStore.getState().user?.preferences?.onboarding?.projects?.[projectId]
                  ?.guide_version === guideVersion
                  ? {}
                  : { guide_read: false, dismissed: false }),
                guide_version: guideVersion,
                ...patch,
              },
            },
          },
        });
        if (!current()) return false;

        const currentUser = useAuthStore.getState().user;
        if (!currentUser || currentUser.id !== ownerId) return false;
        const currentPreferences = currentUser.preferences ?? {};
        const savedEntry = preferences.onboarding?.projects?.[projectId];
        if (!savedEntry) throw new Error("Missing saved checklist state");
        setUser({
          ...currentUser,
          preferences: {
            ...currentPreferences,
            onboarding: {
              ...currentPreferences.onboarding,
              projects: {
                ...currentPreferences.onboarding?.projects,
                [projectId]: savedEntry,
              },
            },
          },
        });
        setPendingPatch(null);
        return true;
      } catch {
        if (current()) {
          setSaveError("保存开工清单进度失败，当前状态未持久化。请重试。");
        }
        return false;
      } finally {
        if (current()) {
          setIsSaving(false);
        }
      }
    },
    [guideVersion, projectId, setUser, scope, user?.id],
  );

  const retry = useCallback(
    () => (pendingPatch ? save(pendingPatch) : Promise.resolve(false)),
    [pendingPatch, save],
  );
  const dismiss = useCallback(() => save({ dismissed: true }), [save]);
  const reopen = useCallback(() => save({ dismissed: false }), [save]);
  const markGuideRead = useCallback(() => save({ guide_read: true }), [save]);

  return { ...state, isSaving, saveError, dismiss, reopen, markGuideRead, retry };
}

export function onboardingProjects(prefs: UserPreferences | undefined) {
  return prefs?.onboarding?.projects ?? {};
}
