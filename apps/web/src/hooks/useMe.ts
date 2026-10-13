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

/**
 * 头像：选择内置像素头像 / 恢复默认 / 上传。
 *
 * 三者共用同一套成功处理——服务端返回更新后的 `UserOut`，写回 auth store 后顶栏与
 * 个人资料预览立即更新（auth store 已持久化到 localStorage，刷新后保持）。
 */
function useAvatarMutation<TArgs>(mutationFn: (args: TArgs) => Promise<MeResponse>) {
  const qc = useQueryClient();
  const setAuth = useAuthStore((s) => s.setAuth);
  return useMutation<MeResponse, Error, TArgs, { ownerId: string | null }>({
    mutationFn,
    // 在**请求发起时**快照发起账号。不能在渲染期捕获：账号变化会让 hooks 重新渲染，
    // `onSuccess` 会被换成最新的闭包，从而把「发起者」错认成当前账号。
    onMutate: () => ({ ownerId: useAuthStore.getState().user?.id ?? null }),
    onSuccess: (user, _args, context) => {
      // 完成的请求可能是「迟到者」：发起后用户已登出，或另一个标签页换成了别的账号。
      // 此时写回会把旧凭据/旧用户覆盖回 store 与 localStorage，等于撤销登出或污染新账号。
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
