import type { APIResponse, Page, Response } from "@playwright/test";

export function waitForApiWrite(
  page: Page,
  method: "POST" | "PATCH" | "PUT" | "DELETE",
  path: `/${string}`,
  options?: { timeout: number },
): Promise<Response> {
  const pathname = `/api/v1${path}`;
  return page.waitForResponse(
    (response) =>
      response.request().method() === method && new URL(response.url()).pathname === pathname,
    options,
  );
}

export async function json<T>(
  response: APIResponse | Response,
  expectedStatus?: number,
): Promise<T> {
  const status = response.status();
  if (expectedStatus === undefined ? !response.ok() : status !== expectedStatus) {
    throw new Error(`${response.url()} returned HTTP ${status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}
