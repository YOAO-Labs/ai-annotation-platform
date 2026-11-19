import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

/**
 * 工作台交互工具「引擎(模型)选择」的用户级偏好。
 *
 * 与 {@link useAiToolParamPrefs} 同窝在 `User.preferences.ai` 下 (各占一个子键:
 * `params_by_backend` / `model_by_backend`)，跟用户走、跨设备、每位标注员各自一份。
 * 按 backend id 分桶: 不同 backend 的可选 model 集不同, 各记各的。
 *
 * 读取优先级链由调用方实现: 本会话显式选择 > 本偏好 (savedModelId) > /setup 默认 model
 * (见 useMLCapabilities 的 preferredModelId 入参)。
 *
 * 注: 后端 `ai` 子树为「深一层合并」(me.py update_preferences), 故本 hook 只提交
 * `{ai:{model_by_backend}}` 不会冲掉 `params_by_backend`。
 *
 * 拉取收敛到共享 {@link useUserPreferences} query; 本地态作乐观覆盖, 写回 600ms
 * 节流 + 成功后 invalidate。
 */
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

  // 切账号/登出: 清掉上一个用户的 byBackend + 取消 pending 写 (issue claude[bot] P1)。
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
      // 切完模型立刻离开 workbench → unmount flush pending, 否则节流窗口内最后一次选择会丢
      // (违背"跨设备持久化"承诺)。
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
