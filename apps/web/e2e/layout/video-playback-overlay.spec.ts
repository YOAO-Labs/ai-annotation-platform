import { expect, test, type Locator } from "@playwright/test";

const fixture = "/e2e/fixtures/video-playback-overlay.html";

async function bounds(element: Locator) {
  await expect(element).toBeVisible();
  const box = await element.boundingBox();
  if (!box) throw new Error("Visible timeline control has no layout box");
  return box;
}

test("collapsed metadata leaves the track clear; expanded transport remains anchored and usable", async ({
  page,
}) => {
  await page.goto(fixture);
  const overlay = page.getByTestId("video-playback-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay).not.toHaveCSS("view-transition-name", "none");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const track = await bounds(page.getByTestId("video-timeline-shell"));
  const readout = await bounds(page.getByTestId("video-time-readout"));
  expect(readout.y).toBeGreaterThanOrEqual(track.y + track.height);
  expect(track.width).toBeGreaterThanOrEqual(280);
  const collapsed = await bounds(overlay);
  const stage = await bounds(page.getByRole("main"));
  expect(collapsed.y + collapsed.height).toBeLessThan(stage.y + stage.height);
  await expect(overlay).toHaveCSS("view-transition-name", "none");

  await page.getByRole("button", { name: "展开时间轴详情" }).click();
  const collapse = page.getByRole("button", { name: "收起时间轴详情" });
  await expect(collapse).toBeFocused();
  const expanded = await bounds(overlay);
  expect(expanded.y + expanded.height).toBeCloseTo(collapsed.y + collapsed.height, 0);
  const bar = page.getByTestId("video-timeline-bottom-bar");
  const next = bar.getByRole("button", { name: "下一帧", exact: true });
  const play = bar.getByRole("button", { name: "播放 / 暂停" });
  const nextBox = await bounds(next);
  const playBox = await bounds(play);
  expect(playBox.x + playBox.width).toBeLessThanOrEqual(nextBox.x);
  expect(nextBox.width).toBeGreaterThanOrEqual(24);
  expect(nextBox.height).toBeGreaterThanOrEqual(24);
  await next.click();
  await expect(bar.getByText("F 51 / 100", { exact: true })).toBeVisible();
  await collapse.click();
  await expect(page.getByRole("button", { name: "展开时间轴详情" })).toBeFocused();
});

for (const [purpose, draft] of [
  ["chapter-draft", "video-timeline-chapter-draft"],
  ["propagate-range", "video-timeline-propagate-draft"],
] as const) {
  test(`collapsed ${purpose} drag exposes the live range and submits the selected frames`, async ({
    page,
  }) => {
    await page.goto(purpose === "chapter-draft" ? `${fixture}?chapter` : fixture);
    const track = await bounds(page.getByTestId("video-timeline-shell"));
    const loop = await bounds(page.getByTestId("video-loop-region"));
    const propagation = await bounds(page.getByTestId("video-propagate-range"));
    expect(loop.x).toBeGreaterThanOrEqual(track.x);
    expect(loop.x + loop.width).toBeLessThan(propagation.x);
    expect(propagation.x + propagation.width).toBeLessThanOrEqual(track.x + track.width);

    const y = track.y + track.height / 2;
    await page.mouse.move(track.x + track.width * 0.3, y);
    if (purpose === "propagate-range") await page.keyboard.down("Shift");
    await page.mouse.down();
    await page.mouse.move(track.x + track.width * 0.6, y);
    const selected = await bounds(page.getByTestId(draft));
    expect(selected.x).toBeGreaterThan(loop.x + loop.width);
    expect(selected.x + selected.width).toBeLessThan(propagation.x);
    expect(selected.width).toBeGreaterThan(track.width / 4);
    await page.mouse.up();
    if (purpose === "propagate-range") await page.keyboard.up("Shift");
    await expect(page.getByTestId(draft)).toHaveCount(0);
    await expect(page.getByRole("status", { name: "提交的区间" })).toHaveText(`${purpose} F30–60`);
  });
}
