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

export function useTheme() {
  const serverTheme = useAuthStore((s) => s.user?.preferences?.ui?.theme);

  const [pref, setPrefState] = useState<ThemePref>(() => serverTheme ?? readLocalPref());
  const [resolved, setResolved] = useState<Resolved>(() =>
    resolvePref(serverTheme ?? readLocalPref()),
  );

  useEffect(() => {
    if (serverTheme && serverTheme !== pref) setPrefState(serverTheme);
  }, [serverTheme, pref]);

  useEffect(() => {
    writeLocalPref(pref);
    const r = resolvePref(pref);
    setResolved(r);
    applyTheme(r);
  }, [pref]);

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

export function initThemeFromStorage(): Resolved {
  const pref = readLocalPref();
  const resolved: Resolved = resolvePref(pref);
  applyTheme(resolved);
  return resolved;
}
