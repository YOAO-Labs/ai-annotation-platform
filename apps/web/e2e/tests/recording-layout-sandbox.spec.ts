import { expect, test } from "@playwright/test";
import { DEFAULT_WORKBENCH_PREFERENCES, type UserPreferences } from "../../src/api/auth";
import { installRecordingWorkbenchLayout } from "../screenshots/flows/_workbench-layout";

test("recording layout keeps expanded selection preferences inside its browser sandbox", async ({
  page,
}) => {
  const origin = "https://recording-layout.test";
  const backendMethods: string[] = [];
  const browserMethods: string[] = [];
  const preferences = {
    workbench: structuredClone(DEFAULT_WORKBENCH_PREFERENCES),
    ai: {},
    ui: {},
  } as UserPreferences;
  await page.route(`${origin}/**`, async (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/auth/me/preferences") {
      backendMethods.push(route.request().method());
      await route.fulfill({ status: 200, json: preferences });
    } else {
      await route.fulfill({
        contentType: "text/html",
        body: "<!doctype html><title>Sandbox</title>",
      });
    }
  });
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/auth/me/preferences")
      browserMethods.push(request.method());
  });
  await page.goto(origin);
  await page.evaluate(() => {
    localStorage.setItem("token", "isolated-recording-token");
    localStorage.setItem(
      "auth-storage",
      JSON.stringify({ state: { user: { id: "recording-user" } } }),
    );
  });
  await installRecordingWorkbenchLayout(page, "both");
  const result = await page.evaluate(async () => {
    const endpoint = "/api/v1/auth/me/preferences";
    const initial = await (await fetch(endpoint)).json();
    const patched = await (
      await fetch(endpoint, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workbench: {
            layout: {
              floatingSelection: {
                ...initial.workbench.layout.floatingSelection,
                collapsed: false,
              },
            },
          },
        }),
      })
    ).json();
    const reloaded = await (await fetch(endpoint)).json();
    return { initial, patched, reloaded };
  });
  expect(result.initial.workbench.layout.floatingSelection.collapsed).toBe(true);
  expect(result.patched.workbench.layout.floatingSelection.collapsed).toBe(false);
  expect(result.reloaded.workbench.layout.floatingSelection.collapsed).toBe(false);
  expect(result.reloaded.workbench.layout.floatingSelection.x).toBe(
    result.initial.workbench.layout.floatingSelection.x,
  );
  expect(browserMethods).toEqual(["GET", "GET", "PATCH", "GET"]);
  expect(backendMethods, "Only initial preferences may reach the backing route").toEqual(["GET"]);
});
