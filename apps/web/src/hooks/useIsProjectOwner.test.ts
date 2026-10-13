import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProjectResponse } from "@/api/projects";

const state = vi.hoisted(() => ({ user: { id: "u", role: "employee" } }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: typeof state) => unknown) => select(state),
}));

import { useIsProjectOwner } from "./useIsProjectOwner";

describe("useIsProjectOwner", () => {
  it.each([
    ["employee", "u", false],
    ["viewer", "u", false],
    ["project_admin", "u", true],
    ["project_admin", "other", false],
    ["super_admin", "other", true],
  ] as const)("%s with owner %s has management=%s", (role, ownerId, expected) => {
    state.user.role = role;
    const { result } = renderHook(() =>
      useIsProjectOwner({ owner_id: ownerId } as ProjectResponse),
    );
    expect(result.current).toBe(expected);
  });
});
