import type { APIRequestContext, APIResponse } from "@playwright/test";

export type SeedResponse = Pick<APIResponse, "ok" | "status" | "text" | "json">;

export function closeFixtureTransport(): Promise<void>;
export function recoverFixtureOperation(operationId: string): Promise<{
  operation_id: string;
  state?: "unknown" | "pending";
  status?: number;
  body?: string;
  owner_error?: string;
  disposal_error?: string;
  operation_response?: { status: number; body: string };
  readonly privateOperationResponse?: { status: number; body: string };
  journal_error?: string;
}>;
export function acknowledgeFixtureOperation(operationId: string): Promise<void>;
export function shutdownFixtureTransport(suiteId: string): Promise<{ state: "stopped" | "shared" }>;
export function prepareFixtureSuite(
  suiteId: string,
  family: "owned" | "screenshot" | "scale",
): Promise<{ suite_id: string; family: "owned" | "screenshot" | "scale" }>;
export function prepareScreenshotNative(
  runId: string,
  apiPort: number,
  operationId: string,
): Promise<{ operationId: string; apiOrigin: string }>;
export function runMaskSliceLifecycle(data: {
  action: "gc" | "drop_revision" | "expire_revision";
  task_id: string;
  operation_id: string;
  namespace: string;
}): Promise<{ orphan_deleted?: boolean }>;
export function postFixture(
  url: string,
  options?: Parameters<APIRequestContext["post"]>[1] & { operationId?: string },
): Promise<SeedResponse>;

export function getScreenshotCatalog(
  url: string,
  options?: { timeout?: number },
): Promise<SeedResponse>;
