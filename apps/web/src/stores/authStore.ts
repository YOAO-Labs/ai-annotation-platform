import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { MeResponse } from "../api/auth";
import { markProactiveLogout } from "../utils/authRedirect";
import { randomId } from "../utils/id";

interface AuthStore {
  token: string | null;
  user: MeResponse | null;
  sessionId: string | null;
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
      sessionId: null,
      setToken: (token) => {
        localStorage.setItem("token", token);
        set((state) => ({
          token,
          sessionId: state.token && state.sessionId ? state.sessionId : randomId(),
        }));
      },
      setUser: (user) =>
        set((state) => ({
          user,
          sessionId: state.user?.id === user.id && state.sessionId ? state.sessionId : randomId(),
        })),
      setAuth: (token, user) => {
        localStorage.setItem("token", token);
        set((state) => ({
          token,
          user,
          sessionId:
            state.token === token && state.user?.id === user.id && state.sessionId
              ? state.sessionId
              : randomId(),
        }));
      },
      logout: () => {
        localStorage.removeItem("token");
        set({ token: null, user: null, sessionId: null });
      },
    }),
    {
      name: "auth-storage",
      partialize: (s) => ({ token: s.token, user: s.user, sessionId: s.sessionId }),
      merge: (persisted, current) => {
        const saved = persisted as Partial<AuthStore> | undefined;
        return {
          ...current,
          ...saved,
          sessionId:
            typeof saved?.sessionId === "string" &&
            saved.sessionId.length > 0 &&
            saved.sessionId.length <= 100
              ? saved.sessionId
              : null,
        };
      },
      onRehydrateStorage: () => (state) => {
        if (state?.token && state.user && !state.sessionId) state.setAuth(state.token, state.user);
      },
    },
  ),
);

export function isCurrentAuthOwner(userId: string): boolean {
  const current = useAuthStore.getState();
  return (
    current.user?.id === userId &&
    !!current.token &&
    current.token === localStorage.getItem("token")
  );
}

export interface AuthSessionOwner {
  userId: string;
  sessionId: string;
}

export function authSessionToken(owner: AuthSessionOwner): string | null {
  const { user, token, sessionId } = useAuthStore.getState();
  if (!token || user?.id !== owner.userId || sessionId !== owner.sessionId) return null;
  try {
    if (localStorage.getItem("token") !== token) return null;
    const saved = JSON.parse(localStorage.getItem("auth-storage") ?? "null")?.state;
    return saved?.token === token &&
      saved.user?.id === owner.userId &&
      saved.sessionId === sessionId
      ? token
      : null;
  } catch {
    return null;
  }
}

export function bindAuthStorage(): () => void {
  const sync = (event: StorageEvent) => {
    if (event.storageArea !== localStorage) return;
    if (event.key !== null && event.key !== "token" && event.key !== "auth-storage") return;
    try {
      const stored = JSON.parse(localStorage.getItem("auth-storage") ?? "null");
      const token = localStorage.getItem("token");
      if ((stored?.state?.token ?? null) !== token) return;
      if (!token && useAuthStore.getState().token) markProactiveLogout();
      void useAuthStore.persist.rehydrate();
    } catch {
      // Malformed storage cannot be adopted as an authenticated identity.
    }
  };
  window.addEventListener("storage", sync);
  return () => window.removeEventListener("storage", sync);
}
