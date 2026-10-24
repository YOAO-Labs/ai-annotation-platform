import { describe, expect, it, vi } from "vitest";

import { apiClient, apiErrorDetailMessage } from "./client";
import { useAuthStore } from "../stores/authStore";
import type { MeResponse } from "./auth";
import { useToastStore } from "@/components/ui/Toast";

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

it.each([403, 500])(
  "keeps silent binary and raw POST %s failures in the caller",
  async (status) => {
    const push = vi.spyOn(useToastStore.getState(), "push").mockImplementation(() => {});
    const fetch = vi
      .fn()
      .mockImplementation(
        async () => new Response(JSON.stringify({ detail: "Attachment unavailable" }), { status }),
      );
    vi.stubGlobal("fetch", fetch);
    useAuthStore.getState().setAuth("scoped-token", { id: "owner" } as MeResponse);
    const controller = new AbortController();
    const image = new Blob(["raw image bytes"], { type: "image/png" });
    try {
      await expect(
        apiClient.silentGetBlob("/attachment", { signal: controller.signal }),
      ).rejects.toMatchObject({
        status,
        message: "Attachment unavailable",
      });
      await expect(
        apiClient.silentPost("/upload", undefined, {
          signal: controller.signal,
          body: image,
          headers: { "Content-Type": "image/png" },
        }),
      ).rejects.toMatchObject({ status, message: "Attachment unavailable" });
      expect(push).not.toHaveBeenCalled();
      expect(fetch).toHaveBeenNthCalledWith(
        1,
        "/api/v1/attachment",
        expect.objectContaining({
          signal: controller.signal,
          headers: expect.objectContaining({ Authorization: "Bearer scoped-token" }),
        }),
      );
      expect(fetch).toHaveBeenNthCalledWith(
        2,
        "/api/v1/upload",
        expect.objectContaining({
          method: "POST",
          body: image,
          signal: controller.signal,
          headers: { Authorization: "Bearer scoped-token", "Content-Type": "image/png" },
        }),
      );
      await expect(apiClient.getBlob("/unscoped")).rejects.toMatchObject({ status });
      expect(push).toHaveBeenCalledTimes(1);
    } finally {
      push.mockRestore();
      vi.unstubAllGlobals();
      useAuthStore.getState().logout();
    }
  },
);

it("keeps silent helpers authenticated for successful blobs, raw bodies and JSON writes", async () => {
  const image = new Blob(["image bytes"], { type: "image/webp" });
  const controller = new AbortController();
  const receipt = { storageKey: "owned-key" };
  const fetch = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, status: 200, blob: async () => image })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => receipt })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ id: "report" }) });
  vi.stubGlobal("fetch", fetch);
  useAuthStore.getState().setAuth("scoped-token", { id: "owner" } as MeResponse);
  try {
    expect(await apiClient.silentGetBlob("/attachment", { signal: controller.signal })).toBe(image);
    expect(
      await apiClient.silentPost("/upload", undefined, {
        body: image,
        signal: controller.signal,
        headers: { "Content-Type": "image/webp" },
      }),
    ).toBe(receipt);
    expect(await apiClient.silentPost("/report", { title: "Issue" })).toEqual({ id: "report" });
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/upload",
      expect.objectContaining({
        method: "POST",
        body: image,
        signal: controller.signal,
        headers: { Authorization: "Bearer scoped-token", "Content-Type": "image/webp" },
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      "/api/v1/report",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ title: "Issue" }),
        headers: { Authorization: "Bearer scoped-token", "Content-Type": "application/json" },
      }),
    );
  } finally {
    vi.unstubAllGlobals();
    useAuthStore.getState().logout();
  }
});
