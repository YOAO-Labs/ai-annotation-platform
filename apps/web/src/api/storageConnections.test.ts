import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn((..._args: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    post: (...args: unknown[]) => post(...args),
  },
}));

import { storageConnectionsApi } from "./storageConnections";

beforeEach(() => post.mockClear());

describe("storageConnectionsApi import retry contract", () => {
  it("preserves the JSON request and passes the caller's saved key as a header", () => {
    const payload = {
      connection_id: "c1",
      source_path: "incoming/images",
      recursive: false,
      include_globs: ["*.jpg"],
    };
    const idempotencyKey = "bdffeb02-4e76-4eae-aed3-9ca184db4110";

    storageConnectionsApi.importFromConnection("d1", payload, idempotencyKey);

    expect(post).toHaveBeenCalledWith("/datasets/d1/import-from-connection", payload, {
      headers: { "Idempotency-Key": idempotencyKey },
    });
  });
});
