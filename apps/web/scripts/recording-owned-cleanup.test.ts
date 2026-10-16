import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@playwright/test", async () => {
  const { expect: assertions } = await import("vitest");
  const locatorAssertions = new Proxy({}, { get: () => () => Promise.resolve() });
  const recordingExpect = Object.assign(
    (value: unknown) =>
      (value as { mockLocator?: boolean })?.mockLocator ? locatorAssertions : assertions(value),
    { poll: () => locatorAssertions },
  );
  return { expect: recordingExpect };
});
vi.mock("../e2e/screenshots/flows/_canvas", () => ({
  openImageAnnotate: vi.fn(),
  hidePredictions: vi.fn(),
}));
vi.mock("../e2e/screenshots/flows/_workbench-layout", () => ({ waitForRecordingPanels: vi.fn() }));
import { runProjectInvitationJoin } from "../e2e/screenshots/flows/project-invitation-join";
import { runDiscussionTargetDrafts } from "../e2e/screenshots/flows/discussion-target-drafts";
import type { Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../e2e/fixtures/seed";

const catalog = {
  projects: {
    video_demo: { id: "project", name: "Demo", display_id: "P1" },
    image_demo: { id: "image", tasks: { annotating: { id: "task" } } },
  },
} as unknown as ScreenshotSeedCatalog;
function locator() {
  const target = {
    mockLocator: true,
    innerText: async () => "轮廓与影像边界不一致",
    count: async () => 0,
  };
  const proxy: unknown = new Proxy(target, {
    get(obj, key) {
      if (key === "then") return undefined;
      if (key in obj) return obj[key as keyof typeof obj];
      return () => proxy;
    },
  });
  return proxy;
}
function response(payload: unknown, url = "") {
  return {
    ok: () => true,
    status: () => 200,
    json: async () => payload,
    text: async () => "",
    url: () => url,
  };
}
function pageBase() {
  return {
    getByRole: () => locator(),
    getByTestId: () => locator(),
    getByTitle: () => locator(),
    getByText: () => locator(),
    getByPlaceholder: () => locator(),
    locator: () => locator(),
    goto: async () => {},
    waitForTimeout: async () => {},
    waitForURL: async () => {},
    keyboard: { insertText: async () => {} },
  };
}
afterEach(() => vi.restoreAllMocks());

describe("invitation capture owns the account as well as the membership", () => {
  for (const failure of [
    "none",
    "acceptance",
    "registration-json",
    "membership-delete",
    "existing-user",
    "foreign-id",
  ] as const) {
    it(`retires its new account with ${failure} failure`, async () => {
      vi.spyOn(Date, "now").mockReturnValue(123);
      const email = "invited-123@capture.local";
      let created = false;
      let active = true;
      let membership = true;
      let wait = 0;
      const actions: string[] = [];
      const user = () => ({ id: "owned-user", email, role: "employee", is_active: active });
      const member = {
        id: "owned-member",
        user_id: "owned-user",
        user_email: email,
        role: "reviewer",
        platform_role: "employee",
      };
      const page = {
        ...pageBase(),
        evaluate: async () => "admin-token",
        waitForResponse: async () => {
          if (wait++ === 0)
            return {
              ...response({
                invite_url: "https://example.test/register?token=secret",
                token: "secret",
                project_id: "project",
                project_member_role: "reviewer",
              }),
              request: () => ({
                postDataJSON: () => ({
                  email,
                  role: "employee",
                  project_id: "project",
                  project_member_role: "reviewer",
                }),
              }),
            };
          created = true;
          return {
            ...response({
              user: user(),
              acceptance: {
                project_id: "project",
                project_name: "Demo",
                project_member_role: "reviewer",
                next_action: failure === "acceptance" ? "start_work" : "wait_for_allocation",
                active_batch_count: 0,
              },
            }),
            json: async () => {
              if (failure === "registration-json") throw Error("registration read failed");
              return {
                user: failure === "foreign-id" ? { ...user(), id: "foreign-user" } : user(),
                acceptance: {
                  project_id: "project",
                  project_name: "Demo",
                  project_member_role: "reviewer",
                  next_action: failure === "acceptance" ? "start_work" : "wait_for_allocation",
                  active_batch_count: 0,
                },
              };
            },
          };
        },
        request: {
          get: async (path: string) =>
            response(
              path === "/api/v1/users"
                ? created || failure === "existing-user"
                  ? [user()]
                  : []
                : path.includes("members")
                  ? membership
                    ? [member]
                    : []
                  : [
                      {
                        id: "invitation",
                        email,
                        role: "employee",
                        project_id: "project",
                        project_member_role: "reviewer",
                        status: "pending",
                      },
                    ],
            ),
          post: async (path: string) => {
            actions.push(path);
            if (path.endsWith("deactivate")) {
              active = false;
              return response(user());
            }
            return response({ access_token: "new-admin-token" });
          },
          delete: async (path: string) => {
            actions.push(path);
            if (path.includes("members")) {
              if (failure === "membership-delete") throw Error("membership deletion failed");
              membership = false;
            }
            return response(null);
          },
        },
      } as unknown as Page;
      const run = runProjectInvitationJoin(page, catalog, () => {});
      if (failure === "none") await run;
      else await expect(run).rejects.toThrow();
      if (failure === "existing-user" || failure === "foreign-id") {
        expect(active).toBe(true);
        expect(
          actions.some((path) => path.endsWith("deactivate") || path.includes("members/")),
        ).toBe(false);
        return;
      }
      expect(created).toBe(true);
      expect(active).toBe(false);
      expect(actions).toContain("/api/v1/users/owned-user/deactivate");
      if (failure !== "membership-delete") expect(membership).toBe(false);
    });
  }
});

describe("discussion capture deletes its feedback even when readback fails", () => {
  for (const failure of ["none", "api", "duplicate"] as const) {
    it(`cleans up after ${failure} readback`, async () => {
      const deleted: string[] = [];
      const page = {
        ...pageBase(),
        waitForResponse: async () =>
          response({
            id: "owned-feedback",
            project_id: "image",
            task_id: "task",
            body: "整体边界待复核",
            annotation_id: null,
          }),
        evaluate: async (_callback: unknown, args: { path: string; method: string }) => {
          if (args.method === "DELETE") {
            deleted.push(args.path);
            return null;
          }
          if (failure === "api") throw Error("readback unavailable");
          if (failure === "duplicate")
            return {
              items: [1, 2].map((id) => ({
                source: "feedback",
                data: {
                  id: `old-${id}`,
                  body: "整体边界待复核",
                  task_id: "task",
                  thread_parent_id: null,
                },
              })),
            };
          return {
            items: args.path.includes("scope=annotation")
              ? []
              : [
                  {
                    source: "feedback",
                    data: { body: "整体边界待复核", task_id: "task", thread_parent_id: null },
                  },
                ],
          };
        },
      } as unknown as Page;
      const run = runDiscussionTargetDrafts(page, catalog, { annotationId: "annotation" });
      if (failure === "api") await expect(run).rejects.toThrow("readback unavailable");
      else if (failure === "duplicate") await expect(run).rejects.toThrow();
      else await run;
      expect(deleted).toEqual(["/api/v1/feedbacks/owned-feedback"]);
    });
  }
});
