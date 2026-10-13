import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeResponse } from "../api/auth";
import { useAuthStore } from "../stores/authStore";

const { setAvatarRefMock, clearAvatarMock, uploadAvatarMock } = vi.hoisted(() => ({
  setAvatarRefMock: vi.fn(),
  clearAvatarMock: vi.fn(),
  uploadAvatarMock: vi.fn(),
}));

vi.mock("../api/me", () => ({
  meApi: {
    setAvatarRef: setAvatarRefMock,
    clearAvatar: clearAvatarMock,
    uploadAvatar: uploadAvatarMock,
    updateProfile: vi.fn(),
    changePassword: vi.fn(),
    requestDeactivation: vi.fn(),
    cancelDeactivation: vi.fn(),
  },
}));

import { useSetAvatarRef } from "./useMe";

function meUser(id: string, avatarRef: string | null): MeResponse {
  return {
    id,
    name: "Ann",
    email: "ann@example.com",
    avatar_ref: avatarRef,
  } as MeResponse;
}

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

function newClient() {
  return new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
}

describe("useAvatarMutation 完成时的账号归属校验", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({ token: "t1", user: meUser("u1", null) });
    localStorage.setItem("token", "t1");
    setAvatarRefMock.mockReset();
    clearAvatarMock.mockReset();
    uploadAvatarMock.mockReset();
  });

  it("登出后迟到完成不把旧凭据写回 auth store", async () => {
    let resolveRequest!: (value: MeResponse) => void;
    setAvatarRefMock.mockReturnValue(
      new Promise<MeResponse>((resolve) => {
        resolveRequest = resolve;
      }),
    );

    const { result } = renderHook(() => useSetAvatarRef(), { wrapper: makeWrapper(newClient()) });
    act(() => {
      void result.current.mutate("preset:pixel-01");
    });
    act(() => {
      useAuthStore.getState().logout();
    });

    await act(async () => {
      resolveRequest(meUser("u1", "preset:pixel-01"));
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // 写回会把旧 token / 旧用户重新塞进 store,等于撤销登出。
    expect(useAuthStore.getState().token).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("跨标签页换成别的账号后不覆盖新账号状态", async () => {
    let resolveRequest!: (value: MeResponse) => void;
    setAvatarRefMock.mockReturnValue(
      new Promise<MeResponse>((resolve) => {
        resolveRequest = resolve;
      }),
    );

    const { result } = renderHook(() => useSetAvatarRef(), { wrapper: makeWrapper(newClient()) });
    act(() => {
      void result.current.mutate("preset:pixel-01");
    });
    // 另一个标签页已把共享凭据换成 u2。
    act(() => {
      useAuthStore.setState({ token: "t2", user: meUser("u2", null) });
      localStorage.setItem("token", "t2");
    });

    await act(async () => {
      resolveRequest(meUser("u1", "preset:pixel-01"));
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(useAuthStore.getState().user?.id).toBe("u2");
    expect(useAuthStore.getState().token).toBe("t2");
  });

  it("仍是同一账号时正常写回", async () => {
    setAvatarRefMock.mockResolvedValue(meUser("u1", "preset:pixel-01"));
    const { result } = renderHook(() => useSetAvatarRef(), { wrapper: makeWrapper(newClient()) });

    await act(async () => {
      await result.current.mutateAsync("preset:pixel-01");
    });

    expect(useAuthStore.getState().user?.avatar_ref).toBe("preset:pixel-01");
    expect(useAuthStore.getState().token).toBe("t1");
  });
});
