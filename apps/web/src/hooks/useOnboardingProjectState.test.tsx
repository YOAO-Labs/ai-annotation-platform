import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse, UserPreferences } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useOnboardingProjectState } from "./useOnboardingProjectState";

const update = vi.hoisted(() => vi.fn());
vi.mock("@/api/auth", () => ({ authApi: { updatePreferences: update } }));
const user: MeResponse = {
  id: "u1",
  name: "User",
  email: "u@test.local",
  role: "employee",
  group_name: null,
  status: "active",
  created_at: "2026-09-09",
};
function deferred() {
  let resolve!: (value: Partial<UserPreferences>) => void;
  const promise = new Promise<Partial<UserPreferences>>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
describe("useOnboardingProjectState", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    useAuthStore.getState().setAuth("token", user);
  });

  it("requires successful readback and retries the failed patch", async () => {
    update.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({
      onboarding: {
        projects: { p1: { guide_version: "v1", guide_read: true, dismissed: false } },
      },
    });
    const { result } = renderHook(() => useOnboardingProjectState("p1", "v1"));
    await act(async () => {
      expect(await result.current.markGuideRead()).toBe(false);
    });
    expect(result.current.guideRead).toBe(false);
    expect(result.current.saveError).toContain("未持久化");
    await act(async () => {
      expect(await result.current.retry()).toBe(true);
    });
    expect(result.current.guideRead).toBe(true);
    expect(result.current.saveError).toBeNull();
  });

  it("resets old-version flags in its incremental request and preserves concurrent preferences", async () => {
    useAuthStore.getState().setUser({
      ...user,
      preferences: {
        onboarding: {
          projects: { p1: { guide_version: "old", guide_read: true, dismissed: true } },
        },
      },
    });
    const pending = deferred();
    update.mockReturnValue(pending.promise);
    const { result } = renderHook(() => useOnboardingProjectState("p1", "new"));
    expect(result.current.guideRead).toBe(false);
    let saving!: Promise<boolean>;
    act(() => {
      saving = result.current.dismiss();
    });
    expect(update).toHaveBeenCalledWith({
      onboarding: {
        projects: { p1: { guide_version: "new", guide_read: false, dismissed: true } },
      },
    });
    act(() => {
      useAuthStore.getState().setUser({ ...user, preferences: { ui: { theme: "dark" } } });
    });
    await act(async () => {
      pending.resolve({
        ui: { theme: "light" },
        onboarding: {
          projects: { p1: { guide_version: "new", guide_read: false, dismissed: true } },
        },
      });
      await saving;
    });
    expect(useAuthStore.getState().user?.preferences?.ui?.theme).toBe("dark");
    expect(result.current.dismissed).toBe(true);
  });

  it.each(["project", "account", "unmount"])(
    "ignores late readback after %s changes",
    async (change) => {
      const pending = deferred();
      update.mockReturnValue(pending.promise);
      const { result, rerender, unmount } = renderHook(
        ({ project }) => useOnboardingProjectState(project, "v1"),
        { initialProps: { project: "p1" } },
      );
      let saving!: Promise<boolean>;
      act(() => {
        saving = result.current.markGuideRead();
      });
      if (change === "project") rerender({ project: "p2" });
      if (change === "account")
        act(() => {
          useAuthStore.getState().setAuth("other", { ...user, id: "u2" });
        });
      if (change === "unmount") unmount();
      await act(async () => {
        pending.resolve({
          onboarding: {
            projects: { p1: { guide_version: "v1", guide_read: true, dismissed: false } },
          },
        });
        expect(await saving).toBe(false);
      });
      expect(useAuthStore.getState().user?.preferences?.onboarding).toBeUndefined();
    },
  );
});
