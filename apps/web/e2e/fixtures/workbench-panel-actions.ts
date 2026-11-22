import { expect, type Page } from "@playwright/test";

export async function canvasBottomDivider(
  page: Page,
): Promise<{ x: number; y: number; top: number; height: number }> {
  let divider: { x: number; y: number; top: number; height: number } | null = null;
  await expect
    .poll(async () => {
      divider = await page.evaluate(() => {
        const canvas = document
          .querySelector('[data-workbench-panel="canvas"]')
          ?.getBoundingClientRect();
        if (!canvas) return null;
        const x = canvas.x + canvas.width / 2;
        const sash = Array.from(document.querySelectorAll(".dv-sash:not(.dv-disabled)"))
          .map((node) => node.getBoundingClientRect())
          .find(
            (rect) =>
              rect.width > 40 &&
              rect.height <= 5 &&
              rect.left < x &&
              rect.right > x &&
              Math.abs(rect.y - canvas.bottom) < 6,
          );
        return sash
          ? { x, y: sash.y + sash.height / 2, top: canvas.y, height: canvas.height }
          : null;
      });
      return divider !== null;
    }, "canvas bottom divider is ready")
    .toBe(true);
  return divider!;
}

export async function panelCommand(page: Page, title: string, name: string): Promise<void> {
  const tab = page.getByRole("tab", { name: title, exact: true });
  if (name === "隐藏面板") {
    await tab.getByRole("button", { name: `隐藏${title}`, exact: true }).click();
    return;
  }
  await tab.scrollIntoViewIfNeeded();
  const source = tab.locator("span").first();
  if (name === "浮动面板") {
    const box = await source.boundingBox();
    expect(box).not.toBeNull();
    await page.keyboard.down("Shift");
    try {
      await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
      await page.mouse.down();
      await page.mouse.move(box!.x + box!.width / 2 - 40, box!.y + 100, { steps: 10 });
      await page.mouse.up();
    } finally {
      await page.keyboard.up("Shift");
    }
    return;
  }
  if (name.startsWith("与") && name.endsWith("合并为标签")) {
    const target = page.getByRole("tab", { name: name.slice(1, -5), exact: true });
    const box = await target.boundingBox();
    expect(box).not.toBeNull();
    await source.dragTo(target, {
      targetPosition: { x: box!.width * 0.75, y: box!.height / 2 },
    });
    return;
  }
  const canvas = page.locator('[data-workbench-panel="canvas"]');
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  const positions: Record<string, { x: number; y: number }> = {
    停靠到左侧: { x: 8, y: box!.height / 2 },
    停靠到右侧: { x: box!.width - 8, y: box!.height / 2 },
    停靠到底部: { x: box!.width / 2, y: box!.height - 8 },
  };
  const targetPosition = positions[name];
  if (!targetPosition) throw new Error(`Unknown panel action: ${name}`);
  await source.dragTo(canvas, { targetPosition });
}
