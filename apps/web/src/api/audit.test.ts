import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn((..._args: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
  },
}));

import { auditApi } from "./audit";

beforeEach(() => get.mockClear());

describe("auditApi", () => {
  it("preserves an empty detail value when a detail key is present", () => {
    auditApi.list({ detail_key: "role", detail_value: "", page: 1 });

    expect(get).toHaveBeenCalledWith("/audit-logs?detail_key=role&detail_value=&page=1");
  });
});
