import { expect, type Locator, type Page } from "@playwright/test";

async function mediaWidth(stage: Locator): Promise<number> {
  return Number(await stage.getAttribute("data-media-width"));
}

export async function panAndZoomImage(page: Page, stage: Locator) {
  const box = await stage.boundingBox();
  if (!box) throw new Error("Image stage has no bounding box");
  const x = box.x + box.width * 0.5;
  const y = box.y + box.height * 0.5;
  const panDrags = 5;
  for (let i = 0; i < panDrags; i += 1) {
    const from = await stage.getAttribute("data-media-x");
    await page.mouse.move(x, y);
    await page.mouse.down({ button: "right" });
    await expect(stage).toHaveAttribute("data-drag-kind", "pan");
    await page.mouse.move(x + (i % 2 === 0 ? 1 : -1) * box.width * 0.1, y, { steps: 10 });
    await page.mouse.up({ button: "right" });
    await expect(stage).toHaveAttribute("data-drag-kind", "none");
    await expect(stage).not.toHaveAttribute("data-media-x", from ?? "");
  }

  const zoomSteps = 4;
  const initialWidth = await mediaWidth(stage);
  await page.mouse.move(x, y);
  await page.keyboard.down("Control");
  try {
    for (let i = 0; i < zoomSteps; i += 1) await page.mouse.wheel(0, -120);
    await expect.poll(() => mediaWidth(stage)).toBeGreaterThan(initialWidth);
    const zoomedWidth = await mediaWidth(stage);
    for (let i = 0; i < zoomSteps; i += 1) await page.mouse.wheel(0, 120);
    await expect.poll(() => mediaWidth(stage)).toBeLessThan(zoomedWidth);
  } finally {
    await page.keyboard.up("Control");
  }

  return { panDrags, zoomInSteps: zoomSteps, zoomOutSteps: zoomSteps };
}
