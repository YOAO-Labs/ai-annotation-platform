import { request, type APIRequestContext, type TestInfo } from "@playwright/test";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { SeedAPI } from "../../e2e/fixtures/seed";
import { postFixture, type SeedResponse } from "../../e2e/helpers/seed-owner-transport.mjs";

vi.mock("../../e2e/helpers/seed-owner-transport.mjs", () => ({ postFixture: vi.fn() }));

const suiteId = "84ebe1dd-9e94-451f-b7bc-aabf90b46b35";
const busy = '{"detail":{"code":"fixture_scope_busy"}}';
const notFound = '{"detail":{"code":"fixture_run_not_found"}}';
const post = vi.mocked(postFixture);
let context: APIRequestContext;

beforeEach(async () => {
  vi.stubEnv("PLAYWRIGHT_FIXTURE_RUN_ID", suiteId);
  post.mockReset();
  context = await request.newContext();
});

afterEach(async () => {
  vi.unstubAllEnvs();
  await context.dispose();
});

function response(status: number, body = "{}"): SeedResponse {
  return {
    ok: () => status >= 200 && status < 300,
    status: () => status,
    text: async () => body,
    json: async () => JSON.parse(body),
  };
}

function owner(retry = 0): SeedAPI {
  return new SeedAPI(context, {
    file: "workbench-pet.spec.ts",
    titlePath: ["a workbench test"],
    retry,
  } as TestInfo);
}

function scope(call: number) {
  const { namespace, suite_id, run_id } = post.mock.calls[call][1]?.data as {
    namespace: string;
    suite_id: string;
    run_id: string;
  };
  return { namespace, suite_id, run_id };
}

function routes(): string[] {
  return post.mock.calls.map(([url]) => new URL(url).pathname.replace("/api/v1/__test/seed/", ""));
}

test("a scope busy retry does not clean a refused run or the prior unsettled run", async () => {
  const cause = Object.assign(new Error("Fixture operation outcome remains pending"), {
    operationId: "pending-owned-operation",
  });
  post
    .mockRejectedValueOnce(cause)
    .mockResolvedValueOnce(response(409, '{"detail":{"code":"fixture_run_unsettled"}}'))
    .mockResolvedValueOnce(response(200))
    .mockResolvedValueOnce(response(409, busy))
    .mockResolvedValueOnce(response(200));

  const first = owner();
  await expect(first.owned()).rejects.toBe(cause);
  await expect(first.cleanupOwnedFixtures()).rejects.toThrow("fixture_run_unsettled");

  const retry = owner(1);
  await retry.filtering({ namespace: "neighbor", videoReady: true });
  await expect(retry.owned()).rejects.toThrow(`seed/owned failed: 409 ${busy}`);
  await retry.cleanupOwnedFixtures();

  expect(routes()).toEqual(["owned", "owned-cleanup", "filtering", "owned", "owned-cleanup"]);
  expect(scope(1)).toEqual(scope(0));
  expect(scope(3)).toEqual({ ...scope(0), run_id: expect.any(String) });
  expect(scope(3).run_id).not.toBe(scope(0).run_id);
  expect(scope(4)).toEqual(scope(2));
  expect(scope(4).namespace).not.toBe(scope(3).namespace);
});

test.each<[number, string]>([
  [409, '{"detail":{"code":"fixture_scope_changed"}}'],
  [409, '{"detail":{"reason":"fixture_scope_busy"}}'],
  [500, busy],
  [409, "invalid response"],
])("retains run ownership for a non-matching HTTP %i response: %s", async (status, body) => {
  post.mockResolvedValueOnce(response(status, body)).mockResolvedValueOnce(response(404, notFound));
  const seed = owner();

  await expect(seed.owned()).rejects.toThrow(`seed/owned failed: ${status} ${body}`);
  await expect(seed.cleanupOwnedFixtures()).rejects.toThrow(
    `seed/owned-cleanup failed: 404 ${notFound}`,
  );
  expect(routes()).toEqual(["owned", "owned-cleanup"]);
  expect(scope(1)).toEqual(scope(0));
});

test("preserves the run identity created by raster preparation", async () => {
  post
    .mockResolvedValueOnce(response(200))
    .mockResolvedValueOnce(response(409, busy))
    .mockResolvedValueOnce(response(200));
  const seed = owner();

  await seed.prepareRasterOwned();
  await expect(seed.owned()).rejects.toThrow(`seed/owned failed: 409 ${busy}`);
  await seed.cleanupOwnedFixtures();
  expect(routes()).toEqual(["owned-prepare", "owned", "owned-cleanup"]);
  expect(scope(1)).toEqual(scope(0));
  expect(scope(2)).toEqual(scope(0));
});

test("reset cleans the built run once and forgets only the refused replacement", async () => {
  const built = response(200, '{"project_id":"project","task_ids":["task"]}');
  post
    .mockResolvedValueOnce(built)
    .mockResolvedValueOnce(response(200))
    .mockResolvedValueOnce(response(409, busy))
    .mockResolvedValueOnce(built)
    .mockResolvedValueOnce(response(200));
  const seed = owner();

  await seed.owned("reset");
  await expect(seed.owned("reset")).rejects.toThrow(`seed/owned failed: 409 ${busy}`);
  await seed.cleanupOwnedFixtures();
  expect(routes()).toEqual(["owned", "owned-cleanup", "owned"]);

  await seed.owned("reset");
  await seed.cleanupOwnedFixtures();
  expect(routes()).toEqual(["owned", "owned-cleanup", "owned", "owned", "owned-cleanup"]);
  expect(scope(1)).toEqual(scope(0));
  expect(scope(4)).toEqual(scope(3));
  expect(scope(2)).toEqual({ ...scope(0), run_id: expect.any(String) });
  expect(scope(3)).toEqual({ ...scope(0), run_id: expect.any(String) });
  expect(new Set([scope(0).run_id, scope(2).run_id, scope(3).run_id]).size).toBe(3);
});

test("a refusal cannot withdraw a run also used by another in-flight seed call", async () => {
  post
    .mockResolvedValueOnce(response(409, busy))
    .mockResolvedValueOnce(response(200))
    .mockResolvedValueOnce(response(200));
  const seed = owner();

  const attempt = seed.owned();
  const reuse = seed.projectRoles();
  await expect(attempt).rejects.toThrow(`seed/owned failed: 409 ${busy}`);
  await reuse;
  await seed.cleanupOwnedFixtures();
  expect(routes()).toEqual(["owned", "project-roles", "owned-cleanup"]);
  expect(scope(1)).toEqual(scope(0));
  expect(scope(2)).toEqual(scope(0));
});
