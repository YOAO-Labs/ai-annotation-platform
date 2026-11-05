import type { APIRequestContext, APIResponse } from "@playwright/test";

export type SeedResponse = Pick<APIResponse, "ok" | "status" | "text" | "json">;

export function closeFixtureTransport(): Promise<void>;
export function shutdownFixtureTransport(suiteId: string): Promise<{ state: "stopped" | "shared" }>;
export function prepareFixtureSuite(
  suiteId: string,
  family: "owned" | "screenshot" | "scale",
): Promise<{ suite_id: string; family: "owned" | "screenshot" | "scale" }>;
export function runMaskSliceLifecycle(data: {
  action: "gc" | "drop_revision" | "expire_revision";
  task_id: string;
  operation_id: string;
  namespace: string;
}): Promise<{ orphan_deleted?: boolean }>;
export function postFixture(
  url: string,
  options?: Parameters<APIRequestContext["post"]>[1],
): Promise<SeedResponse>;

export function getScreenshotCatalog(
  url: string,
  options?: { timeout?: number },
): Promise<SeedResponse>;
