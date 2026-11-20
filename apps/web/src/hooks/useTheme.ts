import { useCallback, useEffect, useState } from "react";

import { authApi, type ThemePref } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

export type { ThemePref };
export type Resolved = "light" | "dark";

const STORAGE_KEY = "anno.theme";

function readLocalPref(): ThemePref {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* SSR / private mode */
  }
  return "system";
}

function writeLocalPref(pref: ThemePref) {
  try {
    localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    /* ignore */
  }
}

function systemResolved(): Resolved {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolvePref(pref: ThemePref): Resolved {
  return pref === "system" ? systemResolved() : pref;
}

function applyTheme(resolved: Resolved) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = resolved;
}

/**
 * 主题切换由服务端偏好持久化。
 * - **真值源分层**：登录后以服务端偏好 `user.preferences.ui.theme` 为准(跟随账号跨设备);
 *   登出 / 首屏 hydration 前回落 localStorage(本机缓存),都没有则 'system'。
 * - **首屏不闪**：localStorage 仍作 bootstrap 缓存(`initThemeFromStorage` 在 main.tsx 应用);
 *   每次变更同步写回,登录后服务端值到达再对齐(一般相同,故无闪烁)。
 * - `setTheme`：本地即时生效 + 写本机缓存;登录态再乐观更新 authStore + PATCH 服务端持久化。
 * - 'system' 模式跟随 prefers-color-scheme，并监听变更。
 * - 写 `<html data-theme="...">` 触发 shadcn.css 暗色块覆盖。
 */
export function useTheme() {
  // 服务端偏好(登录后)= 真值源;响应式订阅,登录 / 用户刷新即同步。未登录 / 未设置为 undefined。
  const serverTheme = useAuthStore((s) => s.user?.preferences?.ui?.theme);

  const [pref, setPrefState] = useState<ThemePref>(() => serverTheme ?? readLocalPref());
  const [resolved, setResolved] = useState<Resolved>(() =>
    resolvePref(serverTheme ?? readLocalPref()),
  );

  // 服务端偏好到达 / 变化(登录、用户刷新)→ 采纳为真值源。仅在服务端值存在且与当前不同才覆盖,
  // 避免登出时把本地选择重置。
  useEffect(() => {
    if (serverTheme && serverTheme !== pref) setPrefState(serverTheme);
  }, [serverTheme, pref]);

  // pref 变化 → 写本机缓存(首屏 bootstrap)+ 重算 resolved + 应用 DOM。
  useEffect(() => {
    writeLocalPref(pref);
    const r = resolvePref(pref);
    setResolved(r);
    applyTheme(r);
  }, [pref]);

  // 'system' 模式下监听 OS 主题变化。
  useEffect(() => {
    if (pref !== "system") return;
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      const r: Resolved = mql.matches ? "dark" : "light";
      setResolved(r);
      applyTheme(r);
    };
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    }
    return undefined;
  }, [pref]);

  const setTheme = useCallback((next: ThemePref) => {
    setPrefState(next);
    writeLocalPref(next);
    // 登录态:持久化到服务端偏好,跟随账号跨设备。乐观更新 authStore 让其它 useTheme 实例与
    // 持久缓存(auth-storage)同步;PATCH 失败为低风险,保留本地选择,下次成功加载再对齐。
    const { user, setUser } = useAuthStore.getState();
    if (user) {
      const prevPrefs = user.preferences ?? {};
      setUser({
        ...user,
        preferences: { ...prevPrefs, ui: { ...prevPrefs.ui, theme: next } },
      });
      void authApi.updatePreferences({ ui: { theme: next } }).catch(() => {});
    }
  }, []);

  return { theme: pref, resolved, setTheme };
}

/** 启动时从 localStorage 应用初始主题，避免 paint flash（服务端值在登录 hydration 后对齐）。 */
export function initThemeFromStorage(): Resolved {
  const pref = readLocalPref();
  const resolved: Resolved = resolvePref(pref);
  applyTheme(resolved);
  return resolved;
}
