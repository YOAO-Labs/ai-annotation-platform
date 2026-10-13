import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { MeResponse } from "../api/auth";
import { markProactiveLogout } from "../utils/authRedirect";

interface AuthStore {
  token: string | null;
  user: MeResponse | null;
  setToken: (token: string) => void;
  setUser: (user: MeResponse) => void;
  setAuth: (token: string, user: MeResponse) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthStore>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      setToken: (token) => {
        localStorage.setItem("token", token);
        set({ token });
      },
      setUser: (user) => set({ user }),
      setAuth: (token, user) => {
        localStorage.setItem("token", token);
        set({ token, user });
      },
      logout: () => {
        localStorage.removeItem("token");
        set({ token: null, user: null });
      },
    }),
    { name: "auth-storage", partialize: (s) => ({ token: s.token, user: s.user }) },
  ),
);

/** A second tab may replace shared credentials before this tab updates its UI. */
export function isCurrentAuthOwner(userId: string): boolean {
  const current = useAuthStore.getState();
  return (
    current.user?.id === userId &&
    !!current.token &&
    current.token === localStorage.getItem("token")
  );
}

/** Adopt the complete token/user pair written by another tab, without writing it back. */
export function bindAuthStorage(): () => void {
  const sync = (event: StorageEvent) => {
    if (event.storageArea !== localStorage) return;
    if (event.key !== null && event.key !== "token" && event.key !== "auth-storage") return;
    try {
      const stored = JSON.parse(localStorage.getItem("auth-storage") ?? "null");
      const token = localStorage.getItem("token");
      // Login writes its token before fetching the user. Wait for the complete
      // persisted identity; authenticated requests reject this intermediate pair.
      if ((stored?.state?.token ?? null) !== token) return;
      // Issue #123 · 另一个标签页主动退出(令牌被清除)时,把"主动退出"意图同步到
      // 本标签页,避免本页 RequireAuth 把当前受限页写回 state.from。同标签页的
      // logout 已在 useLogout 中置位;storage 事件不会回到发起页。
      if (!token && useAuthStore.getState().token) markProactiveLogout();
      void useAuthStore.persist.rehydrate();
    } catch {
      // Malformed storage cannot be adopted as an authenticated identity.
    }
  };
  window.addEventListener("storage", sync);
  return () => window.removeEventListener("storage", sync);
}
