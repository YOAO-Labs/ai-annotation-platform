import { expect } from "vitest";

/** Requests to the product API are the ones a test must consciously handle. */
const TARGET_API = /\/api\/v\d+\//;

export interface RecordedRequest {
  method: string;
  url: string;
}

type UnhandledPrint = { warning: () => void; error: () => void };

let unexpected: RecordedRequest[] = [];

function isTargetApi(url: string): boolean {
  return TARGET_API.test(url);
}

/**
 * MSW `onUnhandledRequest` callback.
 *
 * Every request with no matching handler is recorded. Target API requests are
 * later turned into test failures by {@link expectNoUnexpectedApiRequests};
 * non-API traffic (avatars, fonts, health probes) only keeps the legacy warning
 * so existing suites can migrate in batches instead of all at once.
 */
export function handleUnhandledRequest(request: Request, print: UnhandledPrint): void {
  if (isTargetApi(request.url)) {
    unexpected.push({ method: request.method, url: request.url });
  }
  print.warning();
}

export function resetUnexpectedApiRequests(): void {
  unexpected = [];
}

function formatRequest({ method, url }: RecordedRequest): string {
  const parsed = new URL(url);
  return `${method} ${parsed.pathname}${parsed.search}`;
}

/**
 * Fail when a target API request reached the network without an explicit MSW
 * handler. Refactored integration suites call this after each test; it consumes
 * the recorded list so a later test cannot inherit an earlier failure.
 */
export function expectNoUnexpectedApiRequests(): void {
  const recorded = unexpected;
  unexpected = [];
  expect(recorded.map(formatRequest)).toEqual([]);
}
