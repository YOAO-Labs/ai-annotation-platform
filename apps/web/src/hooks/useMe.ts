import { useMutation, useQueryClient } from "@tanstack/react-query";
import { meApi, type PasswordChangePayload, type ProfileUpdatePayload } from "../api/me";
import type { MeResponse } from "../api/auth";
import { isCurrentAuthOwner, useAuthStore } from "../stores/authStore";

export function useUpdateProfile() {
  const qc = useQueryClient();
  const setAuth = useAuthStore((s) => s.setAuth);
  const token = useAuthStore((s) => s.token);
  return useMutation({
    mutationFn: (payload: ProfileUpdatePayload) => meApi.updateProfile(payload),
    onSuccess: (user) => {
      if (token) setAuth(token, user);
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (payload: PasswordChangePayload) => meApi.changePassword(payload),
  });
}

export function useRequestDeactivation() {
  const qc = useQueryClient();
  const setAuth = useAuthStore((s) => s.setAuth);
  const token = useAuthStore((s) => s.token);
  return useMutation({
    mutationFn: (reason: string) => meApi.requestDeactivation(reason),
    onSuccess: (user) => {
      if (token) setAuth(token, user);
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useCancelDeactivation() {
  const qc = useQueryClient();
  const setAuth = useAuthStore((s) => s.setAuth);
  const token = useAuthStore((s) => s.token);
  return useMutation({
    mutationFn: () => meApi.cancelDeactivation(),
    onSuccess: (user) => {
      if (token) setAuth(token, user);
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

function useAvatarMutation<TArgs>(mutationFn: (args: TArgs) => Promise<MeResponse>) {
  const qc = useQueryClient();
  const setAuth = useAuthStore((s) => s.setAuth);
  return useMutation<MeResponse, Error, TArgs, { ownerId: string | null }>({
    mutationFn,
    onMutate: () => ({ ownerId: useAuthStore.getState().user?.id ?? null }),
    onSuccess: (user, _args, context) => {
      const token = useAuthStore.getState().token;
      if (!context?.ownerId || !token || !isCurrentAuthOwner(context.ownerId)) return;
      setAuth(token, user);
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useSetAvatarRef() {
  return useAvatarMutation((avatarRef: string | null) => meApi.setAvatarRef(avatarRef));
}

export function useClearAvatar() {
  return useAvatarMutation(() => meApi.clearAvatar());
}

export function useUploadAvatar() {
  return useAvatarMutation(
    ({ file, onProgress }: { file: File; onProgress?: (percent: number) => void }) =>
      meApi.uploadAvatar(file, onProgress),
  );
}
