import { expect, test, type Locator } from "@playwright/test";

async function expectCover(card: Locator, media: Locator, { fills = true } = {}) {
  await expect(media).toBeVisible();
  const bounds = await media.evaluate((element) => {
    const thumbnail = element.parentElement!;
    const cover = thumbnail.parentElement!;
    const body = cover.nextElementSibling!;
    const { x, y, width, height } = cover.getBoundingClientRect();
    const thumbnailBounds = thumbnail.getBoundingClientRect();
    const mediaBounds = element.getBoundingClientRect();
    return {
      height,
      bodyHeight: body.getBoundingClientRect().height,
      thumbnailMatches:
        thumbnailBounds.x === x &&
        thumbnailBounds.y === y &&
        thumbnailBounds.width === width &&
        thumbnailBounds.height === height,
      mediaMatches:
        mediaBounds.x === x &&
        mediaBounds.y === y &&
        mediaBounds.width === width &&
        mediaBounds.height === height,
      mediaCentered:
        Math.abs(mediaBounds.x + mediaBounds.width / 2 - (x + width / 2)) < 1 &&
        Math.abs(mediaBounds.y + mediaBounds.height / 2 - (y + height / 2)) < 1,
    };
  });
  expect(bounds.height, (await card.textContent()) ?? "").toBeGreaterThan(0);
  expect(bounds.height).toBe(bounds.bodyHeight);
  expect(bounds.thumbnailMatches).toBe(true);
  if (fills) expect(bounds.mediaMatches).toBe(true);
  else expect(bounds.mediaCentered).toBe(true);
  return bounds.height;
}

for (const area of ["annotate", "review"]) {
  test(`${area} cards fill the cover before and after the image response`, async ({ page }) => {
    const { promise: gate, resolve: release } = Promise.withResolvers<void>();
    await page.route("**/test-cover/*.svg", async (route) => {
      await gate;
      if (route.request().url().endsWith("/success.svg")) {
        await route.fulfill({
          contentType: "image/svg+xml",
          body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"><rect width="64" height="32" fill="green"/></svg>',
        });
      } else {
        await route.fulfill({ status: 404 });
      }
    });
    await page.goto(`/e2e/fixtures/batch-card-covers.html?area=${area}`, {
      waitUntil: "domcontentloaded",
    });

    const success = page.getByRole("button", { name: /图片成功/ });
    const withPreview = page.getByRole("button", { name: /预览保留/ });
    const failure = page.getByRole("button", { name: /图片失败/ });
    const noImage = page.getByRole("button", { name: /没有封面/ });
    const icon = "svg.lucide-image";
    const annotatorToggle = page.getByRole("button", { name: "显示标注员" });
    const checkLoadingCovers = async () => {
      const canvas = success.locator("canvas");
      const height = await expectCover(success, canvas);
      await expect(canvas).toHaveAttribute("width", "48");
      await expect(canvas).toHaveAttribute("height", "48");
      await expect
        .poll(() =>
          canvas.evaluate(
            (element: HTMLCanvasElement) =>
              element.getContext("2d")!.getImageData(0, 0, 1, 1).data[3],
          ),
        )
        .toBe(255);
      await expect(success.locator("img")).toHaveCSS("opacity", "0");
      await expectCover(withPreview, withPreview.locator("canvas"));
      await expectCover(failure, failure.locator("img"));
      await expect(failure.locator("img")).toHaveCSS("opacity", "0");
      await expectCover(noImage, noImage.locator(icon), { fills: false });
      return height;
    };
    try {
      const originalHeight = await checkLoadingCovers();
      if (area === "review") {
        await annotatorToggle.click();
        expect(await checkLoadingCovers()).toBeGreaterThan(originalHeight);
      }
    } finally {
      release();
    }

    const checkSettledCovers = async () => {
      await expect(success.locator("img")).toHaveCSS("opacity", "1");
      await expect(success.locator("canvas")).toHaveCount(0);
      await expectCover(success, success.locator("img"));
      await expect(withPreview.locator("img")).toHaveCount(0);
      await expectCover(withPreview, withPreview.locator("canvas"));
      await expect(failure.locator("img")).toHaveCount(0);
      await expectCover(failure, failure.locator(icon), { fills: false });
      await expectCover(noImage, noImage.locator(icon), { fills: false });
    };
    await checkSettledCovers();
    if (area === "review") {
      await annotatorToggle.click();
      await checkSettledCovers();
    }

    for (const [id, size] of [
      ["table-thumbnail", 40],
      ["default-thumbnail", 48],
    ] as const) {
      const thumbnail = page.getByTestId(id).locator(icon).locator("..");
      const box = await thumbnail.boundingBox();
      expect(box).toMatchObject({ width: size, height: size });
    }
  });
}
