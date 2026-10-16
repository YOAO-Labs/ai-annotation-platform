import { afterEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "./authStore";
import { getAuthSessionRevision } from "./authSession";

const user = { id: "user-a" } as MeResponse;

afterEach(() => {
  useAuthStore.getState().logout();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("persistent authenticated session namespace", () => {
  it("creates and restores a login session when randomUUID is unavailable over HTTP", async () => {
    const getRandomValues = crypto.getRandomValues.bind(crypto);
    vi.stubGlobal("crypto", { getRandomValues });
    useAuthStore.getState().setAuth("http-token", user);
    const sessionId = useAuthStore.getState().sessionId;
    expect(sessionId).toBeTruthy();
    await useAuthStore.persist.rehydrate();
    expect(useAuthStore.getState().sessionId).toBe(sessionId);
  });

  it("survives rehydration and profile refresh while rotating after logout, token or account change", async () => {
    useAuthStore.getState().setAuth("token-a", user);
    const original = useAuthStore.getState().sessionId;
    expect(original).toBeTruthy();
    const revision = getAuthSessionRevision();
    useAuthStore.getState().setUser({ ...user, name: "updated" });
    await useAuthStore.persist.rehydrate();
    expect(useAuthStore.getState().sessionId).toBe(original);
    expect(getAuthSessionRevision()).toBe(revision);
    useAuthStore.getState().setToken("token-b");
    expect(useAuthStore.getState().sessionId).toBe(original);
    expect(getAuthSessionRevision()).toBeGreaterThan(revision);
    useAuthStore.getState().setAuth("token-a", user);
    const next = useAuthStore.getState().sessionId;
    useAuthStore.getState().logout();
    useAuthStore.getState().setAuth("token-a", user);
    expect(useAuthStore.getState().sessionId).not.toBe(next);
    const relogin = useAuthStore.getState().sessionId;
    useAuthStore.getState().setAuth("token-a", { id: "user-b" } as MeResponse);
    expect(useAuthStore.getState().sessionId).not.toBe(relogin);
  });

  it("migrates older persisted credentials once and preserves the generated namespace", async () => {
    localStorage.setItem("token", "legacy-token");
    localStorage.setItem(
      "auth-storage",
      JSON.stringify({ state: { token: "legacy-token", user }, version: 0 }),
    );
    useAuthStore.setState({ token: null, user: null, sessionId: null });
    // setState persists its state, so restore the legacy input after clearing memory.
    localStorage.setItem(
      "auth-storage",
      JSON.stringify({ state: { token: "legacy-token", user }, version: 0 }),
    );
    await useAuthStore.persist.rehydrate();
    const sessionId = useAuthStore.getState().sessionId;
    expect(sessionId).toBeTruthy();
    await useAuthStore.persist.rehydrate();
    expect(useAuthStore.getState().sessionId).toBe(sessionId);
  });
});
