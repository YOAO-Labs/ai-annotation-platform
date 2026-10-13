import apiSchema from "../../../api/openapi.snapshot.json";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn((..._args: unknown[]) => Promise.resolve({}));
const post = vi.fn((..._args: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

import { invitationsApi } from "./invitations";
import { usersApi } from "./users";

beforeEach(() => {
  get.mockClear();
  post.mockClear();
});

describe("management API endpoint contracts", () => {
  it("串联用户分页、统计过滤条件与批量操作", () => {
    usersApi.page({
      role: "employee",
      project_id: "p1",
      group_id: "g1",
      status: "active",
      search: "alice@example.com",
      page: 2,
      page_size: 25,
    });
    expect(get).toHaveBeenCalledWith(
      "/users/query?role=employee&project_id=p1&group_id=g1&status=active&search=alice%40example.com&page=2&page_size=25",
    );

    usersApi.stats({ status: "inactive", search: "bob" });
    expect(get).toHaveBeenCalledWith("/users/stats?status=inactive&search=bob");

    usersApi.bulkInvite([{ email: "alice@example.com", role: "employee" }]);
    expect(post).toHaveBeenCalledWith("/users/bulk-invite", {
      items: [{ email: "alice@example.com", role: "employee" }],
    });

    usersApi.previewBulkGroup({ user_ids: ["u1"], group_id: "g1" });
    expect(post).toHaveBeenCalledWith("/users/groups/bulk/preview", {
      user_ids: ["u1"],
      group_id: "g1",
    });
    usersApi.bulkGroup({ user_ids: ["u1"], group_id: null });
    expect(post).toHaveBeenCalledWith("/users/groups/bulk", {
      user_ids: ["u1"],
      group_id: null,
    });

    usersApi.previewRoleChange("u/1", "reviewer");
    expect(get).toHaveBeenCalledWith("/users/u%2F1/role/preview?role=reviewer");
  });

  it("串联邀请分页、统计与显式邮件发送", () => {
    invitationsApi.page({
      status: "pending",
      scope: "all",
      search: "alice@example.com",
      page: 1,
      page_size: 25,
    });
    expect(get).toHaveBeenCalledWith(
      "/invitations/query?status=pending&scope=all&search=alice%40example.com&page=1&page_size=25",
    );

    invitationsApi.stats({ scope: "all", search: "alice" });
    expect(get).toHaveBeenCalledWith("/invitations/stats?scope=all&search=alice");

    invitationsApi.sendEmail("inv-1");
    expect(post).toHaveBeenCalledWith("/invitations/inv-1/send-email", {});
  });
});

it("management callers use paths and query keys declared by the real OpenAPI snapshot", () => {
  const contract = apiSchema as {
    paths: Record<string, Record<string, { parameters?: { in: string; name: string }[] }>>;
  };
  for (const [path, method, keys] of [
    [
      "/api/v1/users/query",
      "get",
      ["page", "page_size", "status", "role", "project_id", "group_id", "search"],
    ],
    [
      "/api/v1/invitations/query",
      "get",
      ["page", "page_size", "status", "role", "project_id", "scope", "search"],
    ],
    ["/api/v1/users/{user_id}/role/preview", "get", ["role"]],
    ["/api/v1/users/bulk-invite/preview", "post", []],
    ["/api/v1/users/bulk-invite", "post", []],
    ["/api/v1/users/groups/bulk/preview", "post", []],
    ["/api/v1/users/groups/bulk", "post", []],
  ] as const) {
    const operation = contract.paths[path]?.[method];
    expect(operation, path).toBeDefined();
    const queryKeys = (operation.parameters ?? [])
      .filter((parameter: { in: string }) => parameter.in === "query")
      .map((parameter: { name: string }) => parameter.name);
    expect(queryKeys).toEqual(expect.arrayContaining([...keys]));
  }
});
