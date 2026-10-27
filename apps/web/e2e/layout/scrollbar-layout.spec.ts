import { expect, test, type Locator, type Page } from "@playwright/test";

const fixture = "/e2e/fixtures/scrollbar-layout.html";
const nativePanels = ["utility-y", "utility-both", "module-history", "module-command", "textarea"];

async function geometry(target: Locator) {
  return target.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return {
      x: box.x,
      width: box.width,
      clientWidth: element.clientWidth,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      lane: (element as HTMLElement).offsetWidth - element.clientWidth,
    };
  });
}

async function toggle(page: Page, long: boolean) {
  const button = page.getByRole("button", { name: "切换内容长度", exact: true });
  await button.click();
  await expect(button).toHaveAttribute("aria-pressed", String(long));
}

test.beforeEach(async ({ page }) => {
  // The fixtures must remain independent of API, auth and mutable seed data.
  await page.route("**/api/**", (route) => route.abort());
});

test("native panels and textareas keep content width when scrolling starts and stops", async ({
  page,
}) => {
  await page.goto(fixture);
  await expect(page.getByTestId("utility-y")).toBeVisible();
  const initial = await Promise.all(nativePanels.map((id) => geometry(page.getByTestId(id))));
  // A real classic scrollbar is required; hidden/overlay bars cannot reproduce the defect.
  expect(initial[0].lane).toBeGreaterThan(0);
  await toggle(page, true);
  for (const [index, id] of nativePanels.entries()) {
    const long = await geometry(page.getByTestId(id));
    expect(long.clientWidth, id).toBe(initial[index].clientWidth);
    expect(long.scrollHeight, id).toBeGreaterThan(long.clientHeight);
    expect(
      await page.getByTestId(id).evaluate((element) => {
        element.scrollTop = 60;
        return element.scrollTop;
      }),
      id,
    ).toBeGreaterThan(0);
  }
  await toggle(page, false);
  for (const [index, id] of nativePanels.entries()) {
    expect((await geometry(page.getByTestId(id))).clientWidth, id).toBe(initial[index].clientWidth);
  }
});

test("horizontal and custom scrollbar surfaces keep their full width", async ({ page }) => {
  await page.goto(fixture);
  const overlay = page.locator('[data-slot="scroll-area-viewport"]');
  await expect(overlay).toBeVisible();
  const before = await geometry(overlay);
  expect(before.lane).toBe(0);
  expect((await geometry(page.getByTestId("hidden-scroll"))).lane).toBe(0);
  expect((await geometry(page.getByTestId("horizontal"))).lane).toBe(0);
  await toggle(page, true);
  expect((await geometry(overlay)).clientWidth).toBe(before.clientWidth);
  expect((await geometry(overlay)).lane).toBe(0);
  expect((await geometry(page.getByTestId("hidden-scroll"))).lane).toBe(0);
  expect(
    await page.getByTestId("horizontal").evaluate((element) => {
      element.scrollLeft = 80;
      return element.scrollLeft;
    }),
  ).toBeGreaterThan(0);
});

test("standalone document width stays stable with root scrolling and modal scroll lock", async ({
  page,
}) => {
  await page.goto(`${fixture}?surface=document`);
  const document = page.getByTestId("document");
  await expect(document).toBeVisible();
  const before = await geometry(document);
  await toggle(page, true);
  expect((await geometry(document)).width).toBe(before.width);
  expect((await geometry(document)).x).toBe(before.x);
  await toggle(page, false);
  await page.getByRole("button", { name: "打开弹窗", exact: true }).click();
  await expect(page.locator("body")).toHaveAttribute("data-scroll-locked", "1");
  expect((await geometry(document)).width).toBe(before.width);
  expect((await geometry(document)).x).toBe(before.x);
  const modalBefore = await geometry(page.getByTestId("modal-content"));
  await page.getByRole("button", { name: "切换弹窗内容长度", exact: true }).click();
  await expect(page.getByRole("button", { name: "切换弹窗内容长度" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect((await geometry(page.getByTestId("modal-content"))).clientWidth).toBe(
    modalBefore.clientWidth,
  );
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  expect((await geometry(document)).width).toBe(before.width);
  expect((await geometry(document)).x).toBe(before.x);
  // Reopen after the document itself has grown. Radix now measures an actual
  // root scrollbar, rather than the zero gap of the initial short document.
  await page.getByRole("button", { name: "打开弹窗", exact: true }).click();
  await expect(page.locator("body")).toHaveAttribute("data-scroll-locked", "1");
  expect((await geometry(document)).width).toBe(before.width);
  expect((await geometry(document)).x).toBe(before.x);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
});

for (const surface of ["shell", "workbench"] as const) {
  test(`${surface} keeps the browser viewport and canvas free of an extra outer gutter`, async ({
    page,
  }) => {
    await page.goto(`${fixture}?surface=${surface}`);
    const panel = page.getByTestId(surface === "shell" ? "shell-main" : "workbench-panel");
    await expect(panel).toBeVisible();
    const before = await geometry(panel);
    const canvasBefore =
      surface === "workbench" ? await geometry(page.getByTestId("canvas")) : null;
    expect(
      await page.evaluate(
        () => innerWidth - document.documentElement.getBoundingClientRect().width,
      ),
    ).toBe(0);
    await toggle(page, true);
    const long = await geometry(panel);
    expect(long.clientWidth).toBe(before.clientWidth);
    expect(long.scrollHeight).toBeGreaterThan(long.clientHeight);
    if (canvasBefore) expect(await geometry(page.getByTestId("canvas"))).toEqual(canvasBefore);
    await page.getByRole("button", { name: "打开弹窗", exact: true }).click();
    await expect(page.locator("body")).toHaveAttribute("data-scroll-locked", "1");
    expect((await geometry(panel)).clientWidth).toBe(before.clientWidth);
    if (canvasBefore) expect(await geometry(page.getByTestId("canvas"))).toEqual(canvasBefore);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
    await toggle(page, false);
    expect((await geometry(panel)).clientWidth).toBe(before.clientWidth);
  });
}
