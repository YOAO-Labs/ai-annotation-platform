import { expect, type Page } from "@playwright/test";

const FIXED_TIME = new Date("2026-07-13T10:00:00+08:00");

/** 必须在首次导航前调用，避免动画、系统时区和页面时间造成截图漂移。 */
export async function installScreenshotEnvironment(
  page: Page,
  options: { clock?: "fixed" | "live" } = {},
): Promise<"fixed" | "live"> {
  // Live API leases use server time. Freezing a recording months in the past
  // creates false lock durations and stale-job ages in otherwise real workflows.
  const clock =
    options.clock ??
    (process.env.SCREENSHOT_RECORDING_PROFILE || process.env.MARKETING_RUN_ID ? "live" : "fixed");
  if (clock === "fixed") await page.clock.setFixedTime(FIXED_TIME);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(() => {
    const installStyle = () => {
      if (document.getElementById("screenshot-environment-style")) return;
      const style = document.createElement("style");
      style.id = "screenshot-environment-style";
      style.textContent =
        "*,*::before,*::after{" +
        "animation-delay:0s!important;animation-duration:0s!important;" +
        "caret-color:transparent!important;transition-delay:0s!important;" +
        "transition-duration:0s!important;scroll-behavior:auto!important}";
      (document.head ?? document.documentElement).appendChild(style);
    };
    if (document.documentElement) installStyle();
    document.addEventListener("DOMContentLoaded", installStyle, { once: true });
  });
  return clock;
}

export async function applyScreenshotTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.evaluate((nextTheme) => {
    localStorage.setItem("anno.theme", nextTheme);
    document.documentElement.dataset.theme = nextTheme;

    const raw = localStorage.getItem("auth-storage");
    if (!raw) return;
    const parsed = JSON.parse(raw) as {
      state?: { user?: { preferences?: { ui?: Record<string, unknown> } | null } | null };
    };
    const user = parsed.state?.user;
    if (!user) return;
    const preferences = user.preferences ?? {};
    const ui = preferences.ui ?? {};
    user.preferences = { ...preferences, ui: { ...ui, theme: nextTheme } };
    localStorage.setItem("auth-storage", JSON.stringify(parsed));
  }, theme);
}

/** A mounted canvas can still be hidden by layout restoration or loading its media. */
export async function waitForScreenshotImage(page: Page): Promise<void> {
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toBeVisible({ timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
}

export async function waitForScreenshotVideo(page: Page): Promise<void> {
  const stage = page.getByTestId("video-konva-stage");
  await expect(stage).toBeVisible({ timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-video-view-ready", "true", { timeout: 15_000 });
  await expect
    .poll(
      () =>
        page.getByTestId("video-konva-source").evaluate((video: HTMLVideoElement) => {
          const stage = video.closest('[data-testid="video-konva-stage"]');
          if (!stage || video.seeking || video.readyState < 2 || video.videoWidth === 0) {
            return false;
          }
          return (
            stage.getAttribute("data-video-frame-source") !== "webcodecs" ||
            stage.getAttribute("data-video-painted-frame-index") ===
              stage.getAttribute("data-video-frame-index")
          );
        }),
      { timeout: 15_000 },
    )
    .toBe(true);
}

/** 等字体、已挂载图片和两个渲染帧稳定；业务控件仍由各 scene 显式等待。 */
export async function waitForScreenshotReady(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await document.fonts.ready;
    const images = Array.from(document.images).filter((image) => image.isConnected);
    await Promise.all(
      images.map(async (image) => {
        if (image.complete) {
          if (image.naturalWidth === 0) {
            throw new Error(`图片加载失败: ${image.src}`);
          }
          await image.decode();
          return;
        }
        await new Promise<void>((resolve, reject) => {
          image.addEventListener("load", () => resolve(), { once: true });
          image.addEventListener("error", () => reject(new Error(`图片加载失败: ${image.src}`)), {
            once: true,
          });
        });
      }),
    );
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
  });
}
