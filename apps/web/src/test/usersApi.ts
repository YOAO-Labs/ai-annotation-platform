import { http, HttpResponse } from "msw";

import type { GroupResponse } from "@/api/groups";
import type { ProjectResponse } from "@/api/projects";
import type { UserPageResponse, UserResponse, UsersStats } from "@/api/users";
import { server } from "@/mocks/server";

export interface UsersApiOptions {
  /** Rows the API returns before request filters are applied. */
  users?: UserResponse[];
  total?: number;
  pages?: number;
  stats?: UsersStats;
  projects?: ProjectResponse[];
  groups?: GroupResponse[];
  /** Gate the `/users/query` response so a test can observe the loading state. */
  queryGate?: Promise<void>;
  queryStatus?: number;
  deleteStatus?: number;
  deleteBody?: unknown;
  transferUsers?: UserResponse[];
  exportBody?: string;
}

export interface CapturedUsersRequests {
  pageQueries: URLSearchParams[];
  statsQueries: URLSearchParams[];
  listQueries: URLSearchParams[];
  deletes: string[];
  exports: URLSearchParams[];
}

/** Minimal row with the lifecycle fields the page reads; other `UserOut` fields are unused. */
export function createUser(overrides: Partial<UserResponse> = {}): UserResponse {
  return {
    id: "u-base",
    name: "Base",
    email: "base@example.com",
    role: "employee",
    is_active: true,
    status: "offline",
    group_id: null,
    group_name: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as unknown as UserResponse;
}

export const DEFAULT_USERS: UserResponse[] = [
  createUser({ id: "u1", name: "Alice", email: "alice@example.com" }),
  createUser({ id: "u2", name: "Bob", email: "bob@example.com" }),
];

export const INACTIVE_USER: UserResponse = createUser({
  id: "u3",
  name: "Emergency Bob",
  email: "emergency@example.com",
  is_active: false,
  disabled_kind: "emergency_suspended",
  disabled_at: "2026-09-08T10:00:00Z",
  disabled_reason: "账号疑似泄露",
});

export const DEFAULT_PROJECTS: ProjectResponse[] = [
  { id: "project-1", name: "Test project", owner_id: "me-id" } as unknown as ProjectResponse,
];

/** Reject rows outside the requested status/role/search, mirroring the server filters. */
function filterUsers(users: UserResponse[], params: URLSearchParams): UserResponse[] {
  const search = params.get("search")?.toLowerCase() ?? "";
  const role = params.get("role") ?? "";
  const status = params.get("status") ?? "active";
  return users.filter((user) => {
    if (search && !`${user.name} ${user.email}`.toLowerCase().includes(search)) return false;
    if (role && user.role !== role) return false;
    if (status === "active" && user.is_active === false) return false;
    if (status === "inactive" && user.is_active !== false) return false;
    return true;
  });
}

/**
 * Describe the UsersPage responses at the API boundary the page already uses
 * (`/users/query`, `/users/stats`, `/users`, `/projects`, `/groups`, the delete
 * mutation and the CSV export fetch). Register in a `beforeEach`; the global
 * afterEach resets these runtime handlers.
 */
export function installUsersApi(options: UsersApiOptions = {}): CapturedUsersRequests {
  const captured: CapturedUsersRequests = {
    pageQueries: [],
    statsQueries: [],
    listQueries: [],
    deletes: [],
    exports: [],
  };
  const users = options.users ?? DEFAULT_USERS;

  server.use(
    http.get("*/api/v1/users/query", async ({ request }) => {
      const params = new URL(request.url).searchParams;
      captured.pageQueries.push(params);
      if (options.queryGate) await options.queryGate;
      if (options.queryStatus && options.queryStatus >= 400) {
        return HttpResponse.json(
          { detail: "user list forbidden" },
          { status: options.queryStatus },
        );
      }
      const items = filterUsers(users, params);
      const response: UserPageResponse = {
        items,
        total: options.total ?? items.length,
        page: Number(params.get("page") ?? 1),
        page_size: Number(params.get("page_size") ?? 25),
        pages: options.pages ?? 1,
      };
      return HttpResponse.json(response);
    }),
    http.get("*/api/v1/users/stats", ({ request }) => {
      captured.statsQueries.push(new URL(request.url).searchParams);
      return HttpResponse.json(
        options.stats ?? { total: users.length, online: 2, weekly_active: 5 },
      );
    }),
    http.get("*/api/v1/users", ({ request }) => {
      captured.listQueries.push(new URL(request.url).searchParams);
      return HttpResponse.json(options.transferUsers ?? users);
    }),
    http.delete("*/api/v1/users/:userId", ({ params }) => {
      const userId = String(params.userId);
      captured.deletes.push(userId);
      if (options.deleteStatus && options.deleteStatus >= 400) {
        return HttpResponse.json(options.deleteBody ?? { detail: "delete failed" }, {
          status: options.deleteStatus,
        });
      }
      return HttpResponse.json(users.find((user) => user.id === userId) ?? users[0]);
    }),
    http.post("*/api/v1/users/:userId/admin-reset-password", () =>
      HttpResponse.json({
        temp_password: "Tmp-1234",
        message: "已重置",
        target_email: "alice@example.com",
      }),
    ),
    http.get("*/api/v1/users/export", ({ request }) => {
      captured.exports.push(new URL(request.url).searchParams);
      return new HttpResponse(options.exportBody ?? "name,email\nAlice,alice@example.com\n", {
        headers: {
          "Content-Type": "text/csv",
          "Content-Disposition": 'attachment; filename="users.csv"',
        },
      });
    }),
    http.get("*/api/v1/projects", () => HttpResponse.json(options.projects ?? DEFAULT_PROJECTS)),
    http.get("*/api/v1/groups", () => HttpResponse.json(options.groups ?? [])),
  );

  return captured;
}
