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
type FixtureSuiteFamily = "owned" | "scale";
export function prepareFixtureSuite(
  suiteId: string,
  family: FixtureSuiteFamily,
): Promise<{ suite_id: string; family: FixtureSuiteFamily }>;
export function verifyFixtureSuite(
  suiteId: string,
  family: FixtureSuiteFamily,
): Promise<{ suite_id: string; family: FixtureSuiteFamily }>;
export function runMaskSliceLifecycle(data: {
  action: "gc" | "drop_revision" | "expire_revision";
  task_id: string;
  operation_id: string;
  run_id: string;
}): Promise<{ orphan_deleted?: boolean }>;
export function postFixture(
  url: string,
  options?: Parameters<APIRequestContext["post"]>[1] & { operationId?: string },
): Promise<SeedResponse>;
