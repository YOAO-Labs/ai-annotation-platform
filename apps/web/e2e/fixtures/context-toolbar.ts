import { expect, type Page } from "@playwright/test";

const labels = { interactive: "AI", secondary: "二次推理", "tracker-review": "追踪审阅" } as const;
type ToolbarId = keyof typeof labels;

export async function openContextToolbar(page: Page, id: ToolbarId) {
  const capsule = page.getByTestId(`${id}-tool-capsule`);
  await expect(capsule).toBeAttached({ timeout: 20_000 });
  if ((await capsule.getAttribute("data-panel-open")) !== "true") {
    if ((await capsule.getAttribute("data-expanded")) !== "true")
      await page.getByTestId(`${id}-settings-trigger`).click();
    await expect(capsule).toHaveAttribute("data-expanded", "true");
    // The disclosure animates a clipped grid. Clicking during that transition
    // can scroll its contents and move the More button underneath the pointer.
    await capsule.evaluate((element) =>
      Promise.allSettled(
        element
          .getAnimations({ subtree: true })
          .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
          .map((animation) => animation.finished),
      ),
    );
    await page.getByRole("button", { name: `更多 ${labels[id]} 工具`, exact: true }).click();
    await expect(capsule).toHaveAttribute("data-panel-open", "true");
  }
  await expect(page.getByTestId(`${id}-toolbar`)).toBeVisible();
}

export async function closeContextToolbar(page: Page, id: ToolbarId) {
  if ((await page.getByTestId(`${id}-tool-capsule`).getAttribute("data-panel-open")) === "true") {
    await page
      .getByTestId(`${id}-toolbar`)
      .getByRole("button", {
        name:
          id === "interactive" ? "收起 AI 设置" : id === "secondary" ? "收起二次推理设置" : "收起",
        exact: true,
      })
      .click();
    await expect(page.getByTestId(`${id}-toolbar`)).toBeHidden();
  }
}

/** Sample the rendered capsule throughout pointer and settings transitions. */
export async function expectStableContextCapsule(page: Page, id: ToolbarId) {
  const capsule = page.getByTestId(`${id}-tool-capsule`);
  await expect(capsule).toBeVisible();
  await page.mouse.move(0, 0);
  await page.getByTestId(`${id}-settings-trigger`).press("Escape");
  await expect(capsule).toHaveAttribute("data-expanded", "false");
  await capsule.evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished));
  });
  const initial = await capsule.boundingBox();
  expect(initial).not.toBeNull();
  expect(initial!.width).toBeGreaterThan(26);
  expect(initial!.width).toBeLessThanOrEqual(240);
  expect(initial!.height).toBe(36);
  const samples = await capsule.evaluate(async (element) => {
    const values: Array<{ width: number; height: number; x: number; y: number }> = [];
    for (let frame = 0; frame < 30; frame++) {
      element.dispatchEvent(
        new PointerEvent(frame % 2 ? "pointerout" : "pointerover", {
          bubbles: true,
          buttons: 0,
          pointerType: "mouse",
        }),
      );
      await new Promise(requestAnimationFrame);
      const { width, height, x, y } = element.getBoundingClientRect();
      values.push({ width, height, x, y });
    }
    return values;
  });
  for (const sample of samples) {
    expect(sample.x).toBe(initial!.x);
    expect(sample.y).toBe(initial!.y);
    expect(sample.height).toBe(initial!.height);
    expect(sample.width).toBeGreaterThanOrEqual(initial!.width - 1);
    expect(sample.width).toBeLessThanOrEqual(Math.max(160, initial!.width + 22) + 1);
  }
  await page.getByTestId(`${id}-settings-trigger`).click();
  const quick = await page.getByTestId(`${id}-quick-disclosure`).boundingBox();
  expect(quick!.y).toBeGreaterThanOrEqual(initial!.y + initial!.height - 1);
  await openContextToolbar(page, id);
  await closeContextToolbar(page, id);
  await expect.poll(() => capsule.boundingBox()).toEqual(initial);
}
