/**
 * 流程录制：大图概览层稳定显示后，缩放与平移触发高清切片渐进替换。
 */
import type { Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

interface PyramidManifestSummary {
  levels: Array<{ level: number; scaleFactor: number }>;
}

interface ImageTileDiagnosticResources {
  currentLevel?: number;
  targetCoverageRatio?: number;
  retryingVisibleTiles?: number;
  queued?: number;
  fetching?: number;
}

interface WindowWithImageTileDiagnostics extends Window {
  __imageTileDiagnostics?: { resources?: ImageTileDiagnosticResources };
}

interface PyramidTileCoordinates {
  level: number;
  x: number;
  y: number;
}

function parsePyramidTileCoordinates(requestUrl: string): PyramidTileCoordinates | null {
  const match = new URL(requestUrl).pathname.match(
    /\/image-pyramids\/.+\/tiles\/(\d+)\/(\d+)\/(\d+)\.webp$/,
  );
  if (!match) return null;
  return { level: Number(match[1]), x: Number(match[2]), y: Number(match[3]) };
}

interface PyramidTileResponse extends PyramidTileCoordinates {
  ok: boolean;
}

async function waitForStablePyramidView(page: Page, expectedLevel: number | null): Promise<void> {
  await page.waitForFunction(
    (level) => {
      const diagnosticsWindow = window as WindowWithImageTileDiagnostics;
      const diagnostics = diagnosticsWindow.__imageTileDiagnostics?.resources;
      const stage = document.querySelector('[data-testid="workbench-stage"]');
      const loading = Array.from(document.querySelectorAll('[role="status"]')).some((node) =>
        node.textContent?.includes("正在恢复图像清晰度"),
      );
      return (
        stage?.getAttribute("data-image-ready") === "true" &&
        diagnostics?.targetCoverageRatio === 1 &&
        diagnostics.retryingVisibleTiles === 0 &&
        diagnostics.queued === 0 &&
        diagnostics.fetching === 0 &&
        (level === null || diagnostics.currentLevel === level) &&
        !loading
      );
    },
    expectedLevel,
    { timeout: 20_000 },
  );
}

export async function runLargeImageProgressive(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.large_image_demo;
  if (!project) {
    throw new Error(
      "[large-image-progressive] 截图库缺少 large_image_demo，请先生成 P-LARGE-IMG 的 Cosmic Cliffs 图像金字塔",
    );
  }
  const task = project.tasks.cosmic_cliffs;
  if (!task) throw new Error("[large-image-progressive] large_image_demo 缺少 cosmic_cliffs 任务");

  const tileResponses: PyramidTileResponse[] = [];
  page.on("response", (response) => {
    if (response.request().method() !== "GET") return;
    const tile = parsePyramidTileCoordinates(response.url());
    if (tile) tileResponses.push({ ...tile, ok: response.ok() });
  });

  const pyramidReady = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname.endsWith(`/tasks/${task.id}/image-pyramid`) &&
      response.ok(),
    { timeout: 20_000 },
  );
  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const pyramidPayload: unknown = await (await pyramidReady).json();
  if (
    typeof pyramidPayload !== "object" ||
    pyramidPayload === null ||
    !("task_id" in pyramidPayload) ||
    pyramidPayload.task_id !== task.id ||
    !("status" in pyramidPayload) ||
    pyramidPayload.status !== "ready" ||
    !("manifest" in pyramidPayload) ||
    typeof pyramidPayload.manifest !== "object" ||
    pyramidPayload.manifest === null ||
    !("levels" in pyramidPayload.manifest) ||
    !Array.isArray(pyramidPayload.manifest.levels)
  ) {
    throw new Error("[large-image-progressive] 大图接口未返回当前任务的 ready manifest");
  }
  const manifestLevels: PyramidManifestSummary["levels"] = [];
  for (const candidate of pyramidPayload.manifest.levels) {
    if (
      typeof candidate !== "object" ||
      candidate === null ||
      !("level" in candidate) ||
      typeof candidate.level !== "number" ||
      !("scaleFactor" in candidate) ||
      typeof candidate.scaleFactor !== "number"
    ) {
      throw new Error("[large-image-progressive] 大图 manifest 包含无效层级");
    }
    manifestLevels.push({ level: candidate.level, scaleFactor: candidate.scaleFactor });
  }
  const manifest: PyramidManifestSummary = { levels: manifestLevels };
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 15_000 });
  await waitForStablePyramidView(page, null);

  const overviewLevel = await page.evaluate(() => {
    const diagnosticsWindow = window as WindowWithImageTileDiagnostics;
    const diagnostics = diagnosticsWindow.__imageTileDiagnostics?.resources;
    return diagnostics?.currentLevel ?? null;
  });
  if (overviewLevel == null || !manifest.levels.some((level) => level.level === overviewLevel)) {
    throw new Error(`[large-image-progressive] 无法测量稳定概览层级 L${overviewLevel ?? "?"}`);
  }
  const overviewScaleFactor = manifest.levels.find(
    (level) => level.level === overviewLevel,
  )?.scaleFactor;
  const overviewTiles = tileResponses.filter((tile) => tile.ok && tile.level === overviewLevel);
  if (!overviewScaleFactor || overviewTiles.length === 0) {
    throw new Error(`[large-image-progressive] 稳定概览 L${overviewLevel} 未加载真实高清切片`);
  }

  const stageBox = await stage.boundingBox();
  if (!stageBox) throw new Error("[large-image-progressive] 大图画布不可见");
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_200);

  const targetLevel = Math.max(0, overviewLevel - 2);
  const targetScaleFactor = manifest.levels.find(
    (level) => level.level === targetLevel,
  )?.scaleFactor;
  if (
    targetLevel >= overviewLevel ||
    !targetScaleFactor ||
    targetScaleFactor >= overviewScaleFactor
  ) {
    throw new Error(`[large-image-progressive] 概览 L${overviewLevel} 没有可验证的更高清金字塔层`);
  }

  const zoomFocus = {
    x: stageBox.x + stageBox.width * 0.62,
    y: stageBox.y + stageBox.height * 0.48,
  };
  const zoomTileStart = tileResponses.length;
  await page.mouse.move(zoomFocus.x, zoomFocus.y);
  await page.keyboard.down("Control");
  let fineLevel = overviewLevel;
  for (let index = 0; index < 18; index += 1) {
    await page.mouse.wheel(0, -260);
    await page.waitForTimeout(260);
    const currentLevel = await page.evaluate(() => {
      const diagnosticsWindow = window as WindowWithImageTileDiagnostics;
      const diagnostics = diagnosticsWindow.__imageTileDiagnostics?.resources;
      return diagnostics?.currentLevel ?? null;
    });
    if (currentLevel != null && currentLevel <= targetLevel) {
      fineLevel = currentLevel;
      break;
    }
  }
  await page.keyboard.up("Control");
  if (fineLevel >= overviewLevel) {
    throw new Error(
      `[large-image-progressive] 缩放未从概览 L${overviewLevel} 到达更高清目标 L${targetLevel}`,
    );
  }
  await waitForStablePyramidView(page, fineLevel);
  const zoomedTiles = tileResponses
    .slice(zoomTileStart)
    .filter((tile) => tile.ok && tile.level === fineLevel);
  if (zoomedTiles.length === 0) {
    throw new Error(
      `[large-image-progressive] 缩放后未收到真实更高清切片：概览 L${overviewLevel}，缩放 L${fineLevel}`,
    );
  }
  const fineScaleFactor = manifest.levels.find((level) => level.level === fineLevel)?.scaleFactor;
  if (!fineScaleFactor || fineScaleFactor >= overviewScaleFactor) {
    throw new Error(
      `[large-image-progressive] 缩放切片 L${fineLevel} 未比概览 L${overviewLevel} 更清晰`,
    );
  }
  await page.waitForTimeout(1_200);

  const firstRegionTileKeys = new Set(
    tileResponses
      .filter((tile) => tile.ok && tile.level === fineLevel)
      .map((tile) => `${tile.level}/${tile.x}/${tile.y}`),
  );
  const firstPanTileResponse = page.waitForResponse(
    (response) => {
      if (response.request().method() !== "GET" || !response.ok()) return false;
      const tile = parsePyramidTileCoordinates(response.url());
      return (
        tile?.level === fineLevel && !firstRegionTileKeys.has(`${tile.level}/${tile.x}/${tile.y}`)
      );
    },
    { timeout: 20_000 },
  );
  const panStartX = stageBox.x + stageBox.width * 0.62;
  const panStartY = stageBox.y + stageBox.height * 0.52;
  const mediaPositionBeforePan = await stage.evaluate((element) => ({
    x: Number(element.getAttribute("data-media-x")),
    y: Number(element.getAttribute("data-media-y")),
  }));
  await page.keyboard.down("Space");
  await page.mouse.move(panStartX, panStartY);
  await page.mouse.down();
  await movePointerAtRefreshRate(
    page,
    { x: panStartX, y: panStartY },
    { x: panStartX - stageBox.width * 0.3, y: panStartY + stageBox.height * 0.06 },
    1_300,
  );
  await page.mouse.up();
  await page.keyboard.up("Space");
  await firstPanTileResponse;
  await waitForStablePyramidView(page, fineLevel);
  const mediaPositionAfterPan = await stage.evaluate((element) => ({
    x: Number(element.getAttribute("data-media-x")),
    y: Number(element.getAttribute("data-media-y")),
  }));
  if (Math.abs(mediaPositionAfterPan.x - mediaPositionBeforePan.x) < stageBox.width * 0.2) {
    throw new Error("[large-image-progressive] 平移未移动到新的可视区域");
  }
  const panTiles = tileResponses.filter(
    (tile) =>
      tile.ok &&
      tile.level === fineLevel &&
      !firstRegionTileKeys.has(`${tile.level}/${tile.x}/${tile.y}`),
  );
  if (panTiles.length === 0) {
    throw new Error(`[large-image-progressive] 平移后未加载新的 L${fineLevel} 高清切片`);
  }
  // 留出已加载相邻区域的稳定细节画面；下一层仍由真实滚轮缩放和切片请求触发。
  await page.waitForTimeout(1_200);

  const deeperTargetLevel = Math.max(0, fineLevel - 1);
  let finalLevel = fineLevel;
  if (deeperTargetLevel < fineLevel) {
    const deeperZoomTileStart = tileResponses.length;
    await page.mouse.move(stageBox.x + stageBox.width * 0.48, stageBox.y + stageBox.height * 0.46);
    await page.keyboard.down("Control");
    for (let index = 0; index < 10; index += 1) {
      await page.mouse.wheel(0, -260);
      await page.waitForTimeout(260);
      const currentLevel = await page.evaluate(() => {
        const diagnosticsWindow = window as WindowWithImageTileDiagnostics;
        const diagnostics = diagnosticsWindow.__imageTileDiagnostics?.resources;
        return diagnostics?.currentLevel ?? null;
      });
      if (currentLevel != null && currentLevel <= deeperTargetLevel) {
        finalLevel = currentLevel;
        break;
      }
    }
    await page.keyboard.up("Control");
    if (finalLevel >= fineLevel) {
      throw new Error(`[large-image-progressive] 新区域缩放未进入更高清层 L${deeperTargetLevel}`);
    }
    await waitForStablePyramidView(page, finalLevel);
    const deeperTiles = tileResponses
      .slice(deeperZoomTileStart)
      .filter((tile) => tile.ok && tile.level === finalLevel);
    if (deeperTiles.length === 0) {
      throw new Error(`[large-image-progressive] 新区域缩放后未加载真实 L${finalLevel} 金字塔切片`);
    }
    await page.waitForTimeout(1_200);
  }

  const finalRegionTileKeys = new Set(
    tileResponses
      .filter((tile) => tile.ok && tile.level === finalLevel)
      .map((tile) => `${tile.level}/${tile.x}/${tile.y}`),
  );
  const finalPanTileResponse = page.waitForResponse(
    (response) => {
      if (response.request().method() !== "GET" || !response.ok()) return false;
      const tile = parsePyramidTileCoordinates(response.url());
      return (
        tile?.level === finalLevel && !finalRegionTileKeys.has(`${tile.level}/${tile.x}/${tile.y}`)
      );
    },
    { timeout: 20_000 },
  );
  const secondPanStartX = stageBox.x + stageBox.width * 0.62;
  const secondPanStartY = stageBox.y + stageBox.height * 0.52;
  await page.keyboard.down("Space");
  await page.mouse.move(secondPanStartX, secondPanStartY);
  await page.mouse.down();
  await movePointerAtRefreshRate(
    page,
    { x: secondPanStartX, y: secondPanStartY },
    { x: secondPanStartX - stageBox.width * 0.18, y: secondPanStartY - stageBox.height * 0.05 },
    1_150,
  );
  await page.mouse.up();
  await page.keyboard.up("Space");
  await finalPanTileResponse;
  await waitForStablePyramidView(page, finalLevel);
  const finalPanTiles = tileResponses.filter(
    (tile) =>
      tile.ok &&
      tile.level === finalLevel &&
      !finalRegionTileKeys.has(`${tile.level}/${tile.x}/${tile.y}`),
  );
  if (finalPanTiles.length === 0) {
    throw new Error(`[large-image-progressive] 最终平移没有落到新的 L${finalLevel} 高清切片`);
  }
  await page.waitForTimeout(1_200);

  return { drawStartMs, drawEndMs: Date.now() };
}
