/**
 * M2 · 网络状态模拟：用 page.route() 拦截 /api/v1/** 返回指定状态。
 *
 * 使用方式（driver 内部调用）：
 *   const cleanup = await setupMockState(page, scene.mockState);
 *   await page.goto(route);
 *   ...
 *   await cleanup();
 */
import type { Page } from "@playwright/test";

export type MockState = "happy" | "empty" | "error" | "loading" | "rate-limited";

const API_PATTERN = "**/api/v1/**";

const EMPTY_PROJECT_STATS = {
  total_data: 0,
  completed: 0,
  ai_rate: 0,
  pending_review: 0,
  total_annotations: 0,
  ai_derived_annotations: 0,
  total_data_series: Array(14).fill(0),
  completed_series: Array(14).fill(0),
  ai_rate_series: Array(14).fill(0),
  pending_review_series: Array(14).fill(0),
};

function apiPath(url: string): string {
  return new URL(url).pathname;
}

function isProjectsListRequest(method: string, path: string): boolean {
  return method === "GET" && path === "/api/v1/projects/query";
}

function isProjectStatsRequest(method: string, path: string): boolean {
  return method === "GET" && path === "/api/v1/projects/stats";
}

/** 激活网络 mock；返回清除函数。 */
export async function setupMockState(
  page: Page,
  state: MockState | undefined,
): Promise<() => Promise<void>> {
  if (!state || state === "happy") return async () => {};

  switch (state) {
    case "empty": {
      await page.route(API_PATTERN, (route) => {
        const method = route.request().method();
        const url = new URL(route.request().url());
        const path = url.pathname;
        if (isProjectsListRequest(method, path)) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              items: [],
              page: Number(url.searchParams.get("page") ?? "1"),
              page_size: Number(url.searchParams.get("page_size") ?? "20"),
              pages: 0,
              total: 0,
            }),
          });
        }
        if (isProjectStatsRequest(method, path)) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(EMPTY_PROJECT_STATS),
          });
        }
        return route.continue();
      });
      break;
    }

    case "error": {
      await page.route(API_PATTERN, (route) => {
        const method = route.request().method();
        const path = apiPath(route.request().url());
        if (isProjectsListRequest(method, path)) {
          return route.fulfill({
            status: 500,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Internal Server Error (mock)" }),
          });
        }
        if (isProjectStatsRequest(method, path)) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(EMPTY_PROJECT_STATS),
          });
        }
        return route.continue();
      });
      break;
    }

    case "loading": {
      // 延迟响应 —— 截图时页面处于 loading skeleton 状态
      await page.route(API_PATTERN, async (route) => {
        await new Promise((r) => setTimeout(r, 30_000));
        return route.continue();
      });
      break;
    }

    case "rate-limited": {
      await page.route(API_PATTERN, (route) => {
        return route.fulfill({
          status: 429,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Rate limit exceeded (mock)" }),
          headers: { "Retry-After": "60" },
        });
      });
      break;
    }
  }

  return async () => {
    await page.unrouteAll();
  };
}
