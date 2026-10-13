import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

export const TEST_USER_ID = "u1";
export const TEST_TOKEN = "test-token";

/** A realistic current-platform identity: employee, not the retired annotator role. */
export function createTestUser(overrides: Partial<MeResponse> = {}): MeResponse {
  return {
    id: TEST_USER_ID,
    email: "employee@example.com",
    name: "测试员工",
    role: "employee",
    group_name: null,
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/**
 * Seed the real auth store and token storage the way a completed `/auth/me`
 * handshake would, so API hooks resolve an owner id and the client attaches the
 * matching bearer token.
 */
export function seedAuthUser(user: MeResponse = createTestUser(), token = TEST_TOKEN): MeResponse {
  localStorage.setItem("token", token);
  useAuthStore.setState({ user, token });
  return user;
}

export function resetAuthUser(): void {
  useAuthStore.setState({ user: null, token: null });
}
