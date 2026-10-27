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

it.each(["alice", "bob"])(
  "a late 401 cannot log out a new %s session reusing the token",
  async (userId) => {
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
    useAuthStore.getState().setAuth("reused-token", { id: "alice" } as MeResponse);
    const oldSession = useAuthStore.getState().sessionId;
    const request = apiClient.get("/auth/me").catch((error: unknown) => error);
    useAuthStore.getState().logout();
    useAuthStore.getState().setAuth("reused-token", { id: userId } as MeResponse);
    const newSession = useAuthStore.getState().sessionId;
    expect(newSession).not.toBe(oldSession);
    try {
      resolve(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }));
      expect(await request).toMatchObject({ status: 401 });
      expect(useAuthStore.getState().sessionId).toBe(newSession);
      expect(useAuthStore.getState().user?.id).toBe(userId);
      expect(localStorage.getItem("token")).toBe("reused-token");
    } finally {
      vi.unstubAllGlobals();
      useAuthStore.getState().logout();
    }
  },
);

it("a current session's 401 still logs out its expired credentials", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 401 })),
  );
  useAuthStore.getState().setAuth("expired-token", { id: "alice" } as MeResponse);
  try {
    await expect(apiClient.get("/auth/me")).rejects.toMatchObject({ status: 401 });
    expect(useAuthStore.getState().user).toBeNull();
    expect(localStorage.getItem("token")).toBeNull();
  } finally {
    vi.unstubAllGlobals();
    useAuthStore.getState().logout();
  }
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

it("dataset POST/PUT send Idempotency-Key headers without changing JSON bodies", async () => {
  const fetch = vi
    .fn()
    .mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify({ id: "d1" }), { status: 200 })),
    );
  vi.stubGlobal("fetch", fetch);
  useAuthStore.getState().setAuth("dataset-token", { id: "dataset-user" } as MeResponse);
  const createKey = "35d9bbf7-2410-4d34-9ca0-6cb9b13ea83b";
  const updateKey = "7b14b573-3203-4d64-8b51-6ab7b5fbe4cb";
  try {
    await apiClient.post(
      "/datasets",
      { name: "ds", data_type: "image" },
      {
        headers: { "Idempotency-Key": createKey },
      },
    );
    await apiClient.put(
      "/datasets/d1",
      { axis_convention: null },
      {
        headers: { "Idempotency-Key": updateKey },
      },
    );

    const createRequest = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(createRequest.body).toBe(JSON.stringify({ name: "ds", data_type: "image" }));
    expect(new Headers(createRequest.headers).get("Idempotency-Key")).toBe(createKey);
    const updateRequest = fetch.mock.calls[1]?.[1] as RequestInit;
    expect(updateRequest.body).toBe(JSON.stringify({ axis_convention: null }));
    expect(new Headers(updateRequest.headers).get("Idempotency-Key")).toBe(updateKey);
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

it("keeps authenticated delete cancellation and errors scoped to its caller", async () => {
  const signal = new AbortController().signal;
  const fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, status: 200, json: async () => ({ deleted: "key" }) });
  vi.stubGlobal("fetch", fetch);
  const toast = vi.spyOn(useToastStore.getState(), "push");
  useAuthStore.getState().setAuth("delete-token", { id: "delete-user" } as MeResponse);
  try {
    await expect(apiClient.silentDelete("/guide", { signal })).resolves.toEqual({ deleted: "key" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/guide",
      expect.objectContaining({
        method: "DELETE",
        signal,
        headers: expect.objectContaining({ Authorization: "Bearer delete-token" }),
      }),
    );
    fetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "retired" }), { status: 403 }),
    );
    await expect(apiClient.silentDelete("/guide", { signal })).rejects.toMatchObject({
      status: 403,
    });
    expect(toast).not.toHaveBeenCalled();
  } finally {
    toast.mockRestore();
    vi.unstubAllGlobals();
    useAuthStore.getState().logout();
  }
});
