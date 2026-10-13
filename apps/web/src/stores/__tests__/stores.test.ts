/**
 * v0.8.3 · zustand stores 单测：authStore / appStore / bugDrawerStore。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useAuthStore } from "../authStore";
import { useAppStore } from "../appStore";
import { useBugDrawerStore } from "../bugDrawerStore";
import type { MeResponse } from "@/api/auth";

const fakeUser: MeResponse = {
  id: "1",
  email: "u@x.com",
  name: "U",
  role: "employee",
  status: "online",
} as unknown as MeResponse;

describe("authStore", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({ token: null, user: null });
  });

  it("setToken 写 localStorage 与 store", () => {
    useAuthStore.getState().setToken("t1");
    expect(useAuthStore.getState().token).toBe("t1");
    expect(localStorage.getItem("token")).toBe("t1");
  });

  it("setUser 仅写 user 不触 localStorage 写 token", () => {
    useAuthStore.getState().setUser(fakeUser);
    expect(useAuthStore.getState().user?.email).toBe("u@x.com");
    expect(localStorage.getItem("token")).toBeNull();
  });

  it("setAuth 同时写 token + user", () => {
    useAuthStore.getState().setAuth("t2", fakeUser);
    expect(useAuthStore.getState().token).toBe("t2");
    expect(useAuthStore.getState().user?.email).toBe("u@x.com");
    expect(localStorage.getItem("token")).toBe("t2");
  });

  it("logout 清空 token+user 与 localStorage", () => {
    useAuthStore.getState().setAuth("t3", fakeUser);
    useAuthStore.getState().logout();
    expect(useAuthStore.getState().token).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
    expect(localStorage.getItem("token")).toBeNull();
  });
});

describe("appStore", () => {
  it("默认 workspace 已设", () => {
    expect(useAppStore.getState().workspace).toBe("默认工作区");
  });
});

describe("bugDrawerStore", () => {
  beforeEach(() => {
    useBugDrawerStore.setState({ open: false, focusBugId: null });
  });

  it("默认关闭", () => {
    expect(useBugDrawerStore.getState().open).toBe(false);
  });

  it("openDrawer 不带 id → 仅设 open", () => {
    useBugDrawerStore.getState().openDrawer();
    expect(useBugDrawerStore.getState().open).toBe(true);
    expect(useBugDrawerStore.getState().focusBugId).toBeNull();
  });

  it("openDrawer 带 id → focusBugId 同步", () => {
    useBugDrawerStore.getState().openDrawer("B-42");
    expect(useBugDrawerStore.getState().focusBugId).toBe("B-42");
  });

  it("close → 全部清", () => {
    useBugDrawerStore.getState().openDrawer("B-1");
    useBugDrawerStore.getState().close();
    expect(useBugDrawerStore.getState().open).toBe(false);
    expect(useBugDrawerStore.getState().focusBugId).toBeNull();
  });
});
