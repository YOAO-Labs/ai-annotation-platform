import { describe, expect, it, vi } from "vitest";

import { apiClient, apiErrorDetailMessage } from "./client";
import { useAuthStore } from "../stores/authStore";
import type { MeResponse } from "./auth";

describe("apiErrorDetailMessage", () => {
  it("提取 FastAPI 请求体校验错误的字段路径", () => {
    expect(
      apiErrorDetailMessage([
        {
          type: "string_type",
          loc: ["body", "candidate", "candidate", "value", "masklabels", 0],
          msg: "Input should be a valid string",
        },
      ]),
    ).toBe("candidate.candidate.value.masklabels.0：Input should be a valid string");
  });

  it("保持字符串和结构化 message 错误兼容", () => {
    expect(apiErrorDetailMessage("plain error")).toBe("plain error");
    expect(apiErrorDetailMessage({ message: "structured error" })).toBe("structured error");
  });
});

it("never sends another tab's credentials from a stale account UI", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  useAuthStore.getState().setAuth("alice-token", { id: "alice" } as MeResponse);
  localStorage.setItem("token", "bob-token");
  await expect(apiClient.post("/tasks/task/submit")).rejects.toMatchObject({ status: 401 });
  expect(fetch).not.toHaveBeenCalled();
  expect(localStorage.getItem("token")).toBe("bob-token");
  vi.unstubAllGlobals();
  useAuthStore.getState().logout();
});

it("a late 401 does not clear newer credentials", async () => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    ),
  );
  localStorage.setItem("token", "old-token");
  const request = apiClient.get("/auth/me").catch((error: unknown) => error);
  localStorage.setItem("token", "new-token");
  useAuthStore.setState({ token: "new-token" });
  resolve(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }));
  await request;
  expect(localStorage.getItem("token")).toBe("new-token");
  expect(useAuthStore.getState().token).toBe("new-token");
  vi.unstubAllGlobals();
  useAuthStore.getState().logout();
});

it("reads authenticated binary responses with abort support and preserves structured errors", async () => {
  const controller = new AbortController();
  const blob = new Blob(["snapshot bytes"], { type: "image/png" });
  const binary = vi.fn().mockResolvedValue(blob);
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200, blob: binary });
  vi.stubGlobal("fetch", fetch);
  useAuthStore.getState().setAuth("binary-token", { id: "binary-user" } as MeResponse);
  try {
    expect(await apiClient.getBlob("/frozen-media", { signal: controller.signal })).toBe(blob);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/frozen-media",
      expect.objectContaining({
        signal: controller.signal,
        headers: expect.objectContaining({ Authorization: "Bearer binary-token" }),
      }),
    );
    fetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Frozen media digest mismatch" }), { status: 409 }),
    );
    await expect(apiClient.getBlob("/frozen-media")).rejects.toMatchObject({
      status: 409,
      message: "Frozen media digest mismatch",
    });
  } finally {
    vi.unstubAllGlobals();
    useAuthStore.getState().logout();
  }
});

it("DELETE preserves JSON bodies and sends RequestInit headers without a fake body", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  useAuthStore.getState().setAuth("delete-token", { id: "delete-user" } as MeResponse);
  const idempotencyKey = "35d9bbf7-2410-4d34-9ca0-6cb9b13ea83b";
  try {
    await apiClient.delete<void>("/items/item", { purge: true });
    await apiClient.delete<void>("/datasets/d1", undefined, {
      headers: { "Idempotency-Key": idempotencyKey },
    });

    const bodyRequest = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(bodyRequest.method).toBe("DELETE");
    expect(bodyRequest.body).toBe(JSON.stringify({ purge: true }));

    const headerRequest = fetch.mock.calls[1]?.[1] as RequestInit;
    expect(headerRequest.method).toBe("DELETE");
    expect(headerRequest.body).toBeUndefined();
    expect(new Headers(headerRequest.headers).get("Idempotency-Key")).toBe(idempotencyKey);
  } finally {
    vi.unstubAllGlobals();
    useAuthStore.getState().logout();
  }
});
