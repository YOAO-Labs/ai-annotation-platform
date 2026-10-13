import { QueryClient } from "@tanstack/react-query";
import { expect, it } from "vitest";
import type { MeResponse } from "@/api/auth";
import { clearProactiveLogout, isProactiveLogout } from "@/utils/authRedirect";
import { bindAuthQueryCache } from "./authQueryCache";
import { bindAuthStorage, useAuthStore } from "./authStore";

it("drops private cached queries and ignores late results across account changes", async () => {
  const client = new QueryClient();
  const alice = { id: "alice" } as MeResponse;
  useAuthStore.getState().setAuth("alice-token", alice);
  const unbind = bindAuthQueryCache(client);
  let resolve!: (value: string) => void;
  const pending = client
    .fetchQuery({
      queryKey: ["tasks"],
      queryFn: () =>
        new Promise<string>((done) => {
          resolve = done;
        }),
    })
    .catch(() => undefined);
  client.setQueryData(["projects"], ["private-alice-project"]);
  useAuthStore.getState().logout();
  useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse);
  expect(client.getQueryData(["projects"])).toBeUndefined();
  resolve("late-alice-task");
  await pending;
  expect(client.getQueryData(["tasks"])).toBeUndefined();
  unbind();
  client.clear();
  useAuthStore.getState().logout();
});

it("adopts another tab's complete identity and clears the previous account cache", async () => {
  const client = new QueryClient();
  useAuthStore.getState().setAuth("alice-token", { id: "alice" } as MeResponse);
  const unbindCache = bindAuthQueryCache(client);
  const unbindStorage = bindAuthStorage();
  client.setQueryData(["projects"], ["alice-private"]);
  const bob = { token: "bob-token", user: { id: "bob" } };
  localStorage.setItem("token", bob.token);
  localStorage.setItem("auth-storage", JSON.stringify({ state: bob, version: 0 }));
  window.dispatchEvent(
    new StorageEvent("storage", { key: "auth-storage", storageArea: localStorage }),
  );
  expect(useAuthStore.getState()).toMatchObject(bob);
  expect(client.getQueryData(["projects"])).toBeUndefined();
  expect(JSON.parse(localStorage.getItem("auth-storage")!).state).toEqual(bob);
  unbindCache();
  unbindStorage();
  client.clear();
  useAuthStore.getState().logout();
});

it("adopts another tab's proactive logout intent (Issue #123)", () => {
  useAuthStore.getState().setAuth("alice-token", { id: "alice" } as MeResponse);
  const unbindStorage = bindAuthStorage();
  clearProactiveLogout();

  localStorage.removeItem("token");
  localStorage.setItem(
    "auth-storage",
    JSON.stringify({ state: { token: null, user: null }, version: 0 }),
  );
  window.dispatchEvent(
    new StorageEvent("storage", { key: "auth-storage", storageArea: localStorage }),
  );

  expect(useAuthStore.getState().token).toBeNull();
  // 其它标签页主动退出 → 本标签页兜底跳转不带 from。
  expect(isProactiveLogout()).toBe(true);

  unbindStorage();
  useAuthStore.getState().logout();
  clearProactiveLogout();
});

it("clears and cancels private queries when the same account rotates its token", async () => {
  const client = new QueryClient();
  useAuthStore.getState().setAuth("alice-token-a", { id: "alice" } as MeResponse);
  const unbind = bindAuthQueryCache(client);
  let resolve!: (value: string) => void;
  let signal!: AbortSignal;
  const pending = client
    .fetchQuery({
      queryKey: ["projects", { search: "car" }, "alice", "alice-token-a"],
      queryFn: ({ signal: requestSignal }) => {
        signal = requestSignal;
        return new Promise<string>((done) => {
          resolve = done;
        });
      },
    })
    .catch(() => undefined);
  client.setQueryData(["projects", { search: "old" }, "alice", "alice-token-a"], ["old"]);

  useAuthStore.getState().setToken("alice-token-b");

  expect(signal.aborted).toBe(true);
  expect(client.getQueryData(["projects", { search: "old" }, "alice", "alice-token-a"])).toBe(
    undefined,
  );
  resolve("late-alice-token-a");
  await pending;
  expect(client.getQueryData(["projects", { search: "car" }, "alice", "alice-token-a"])).toBe(
    undefined,
  );
  unbind();
  client.clear();
  useAuthStore.getState().logout();
});
