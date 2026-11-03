import type { APIRequestContext, APIResponse } from "@playwright/test";

export type SeedResponse = Pick<APIResponse, "ok" | "status" | "text" | "json">;

export function closeFixtureTransport(): Promise<void>;
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
