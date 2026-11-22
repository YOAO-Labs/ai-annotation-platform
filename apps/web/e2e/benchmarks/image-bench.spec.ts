import { createHash } from "node:crypto";
import { writeFile } from "node:fs/promises";
import type { APIRequestContext, Locator, Page } from "@playwright/test";
import fixtureConfig from "../../scripts/image-bench/fixtures.json" with { type: "json" };
import { expect, test, type SeedAPI, type SeedTaskAnnotation } from "../fixtures/seed";
import { panAndZoomImage } from "../helpers/image-bench-gestures";

type Dimensions = { widthPx: number; heightPx: number };
type Scenario = Dimensions & { id: string; imageCanvas: string; annotationCount: number };
type FixtureEvidence = {
  taskId: string;
  asset: { contentId: string; sha256: string; byteSize: number };
  served: Dimensions;
  decoded: Dimensions;
  logical: Dimensions;
  apiBoxCount: number;
  stageBoxCount: number;
};
type OwnedImage = { stage: Locator; stageIdentity: string; observed: FixtureEvidence };

function requireMatchingFixture(selected: Scenario, observed: FixtureEvidence): void {
  for (const source of ["served", "decoded", "logical"] as const) {
    const { widthPx, heightPx } = observed[source];
    if (widthPx !== selected.widthPx || heightPx !== selected.heightPx) {
      throw new Error(
        `${source} image dimensions: expected ${selected.widthPx}×${selected.heightPx}, observed ${widthPx}×${heightPx}`,
      );
    }
  }
  for (const source of ["apiBoxCount", "stageBoxCount"] as const) {
    if (observed[source] !== selected.annotationCount) {
      throw new Error(
        `${source}: expected ${selected.annotationCount}, observed ${observed[source]}`,
      );
    }
  }
}

async function openOwnedImage(
  page: Page,
  request: APIRequestContext,
  seed: SeedAPI,
  selected: Scenario,
): Promise<OwnedImage> {
  const { imageCanvas, annotationCount } = selected;
  if (imageCanvas !== "720p" && imageCanvas !== "5k") {
    throw new Error(`Unsupported benchmark canvas: ${imageCanvas}`);
  }
  const data = await seed.owned(undefined, { imageCanvas });
  const taskId = data.task_ids[0];
  const taskPath = `/api/v1/tasks/${taskId}`;
  const annotationsPath = `${taskPath}/annotations`;
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.annotator_email)}` };
  const columns = Math.ceil(Math.sqrt(annotationCount));
  const rows = Math.ceil(annotationCount / columns);
  const seeded: { id: string; geometry: Record<string, unknown> }[] = [];
  for (let index = 0; index < annotationCount; index += 1) {
    const geometry = {
      type: "bbox",
      x: ((index % columns) + 0.2) / columns,
      y: (Math.floor(index / columns) + 0.2) / rows,
      w: 0.6 / columns,
      h: 0.6 / rows,
    };
    const response = await request.post(annotationsPath, {
      headers,
      data: { annotation_type: "bbox", tool_unit_id: "bbox", class_name: "car", geometry },
    });
    if (response.status() !== 201) {
      throw new Error(
        `Box ${index + 1} create failed: ${response.status()} ${await response.text()}`,
      );
    }
    const { id } = (await response.json()) as SeedTaskAnnotation;
    seeded.push({ id, geometry });
  }

  await seed.injectToken(page, data.annotator_email);
  const originalPath = `/api/v1/files/tasks/${taskId}/original`;
  const waitForGet = (path: string) =>
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET" && new URL(response.url()).pathname === path,
      { timeout: 30_000 },
    );
  const [identityResponse, originalResponse, annotationResponse] = await Promise.all([
    waitForGet(`${originalPath}-identity`),
    waitForGet(originalPath),
    waitForGet(annotationsPath),
    page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`),
  ]);
  expect(identityResponse.status()).toBe(200);
  expect(originalResponse.status()).toBe(200);
  expect(annotationResponse.status()).toBe(200);

  const identity = (await identityResponse.json()) as {
    owner_kind: string;
    verification_state: string;
    content_id: string;
    sha256: string;
    size_bytes: number;
    download_path: string;
  };
  const originalURL = new URL(originalResponse.url());
  const bytes = await originalResponse.body();
  expect(identity.owner_kind).toBe("dataset_asset");
  expect(identity.verification_state).toBe("verified");
  expect(identity.download_path).toBe(originalURL.pathname + originalURL.search);
  expect(originalURL.searchParams.get("content_id")).toBe(identity.content_id);
  expect(originalURL.searchParams.get("sha256")).toBe(identity.sha256);
  expect(bytes.length).toBe(identity.size_bytes);
  expect(createHash("sha256").update(bytes).digest("hex")).toBe(identity.sha256);
  expect(bytes.subarray(0, 16).toString("hex")).toBe("89504e470d0a1a0a0000000d49484452");

  const annotations = (await annotationResponse.json()) as SeedTaskAnnotation[];
  expect(annotations.map(({ id }) => id).sort()).toEqual(seeded.map(({ id }) => id).sort());
  const annotationById = new Map(annotations.map((annotation) => [annotation.id, annotation]));
  for (const { id, geometry } of seeded) {
    expect(annotationById.get(id)).toMatchObject({
      task_id: taskId,
      annotation_type: "bbox",
      class_name: "car",
      geometry,
    });
  }

  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-identity", /\/address\/\d+$/);
  await expect(stage).toHaveAttribute("data-user-box-count", String(annotations.length));
  const { stageIdentity, ...displayed } = await stage.evaluate(({ dataset }) => ({
    stageIdentity: dataset.imageIdentity ?? "",
    decoded: {
      widthPx: Number(dataset.imageDecodedWidth),
      heightPx: Number(dataset.imageDecodedHeight),
    },
    logical: {
      widthPx: Number(dataset.imageLogicalWidth),
      heightPx: Number(dataset.imageLogicalHeight),
    },
    stageBoxCount: Number(dataset.userBoxCount),
  }));
  const observed: FixtureEvidence = {
    taskId,
    asset: { contentId: identity.content_id, sha256: identity.sha256, byteSize: bytes.length },
    served: { widthPx: bytes.readUInt32BE(16), heightPx: bytes.readUInt32BE(20) },
    apiBoxCount: annotations.length,
    ...displayed,
  };
  return { stage, stageIdentity, observed };
}

async function measureMatchingFixture(page: Page, fixture: OwnedImage, selected: Scenario) {
  const { stage, stageIdentity, observed } = fixture;
  requireMatchingFixture(selected, observed);
  await expect(stage).toHaveAttribute("data-selected-box-count", "0");
  const recording = await page.evaluateHandle(() => {
    if (!PerformanceObserver.supportedEntryTypes.includes("longtask")) {
      throw new Error("This browser does not provide Long Tasks measurements");
    }
    let longTaskCount = 0;
    let longTaskMaxMs: number | null = null;
    const consume = (entries: PerformanceEntry[]) => {
      longTaskCount += entries.length;
      for (const { duration } of entries) longTaskMaxMs = Math.max(longTaskMaxMs ?? 0, duration);
    };
    const observer = new PerformanceObserver((list) => consume(list.getEntries()));
    observer.observe({ entryTypes: ["longtask"] });
    return {
      stop: async () => {
        const frame = Promise.withResolvers<void>();
        requestAnimationFrame(() => requestAnimationFrame(() => frame.resolve()));
        await frame.promise;
        consume(observer.takeRecords());
        observer.disconnect();
        return { longTaskCount, longTaskMaxMs };
      },
    };
  });
  try {
    const gestures = await panAndZoomImage(page, stage);
    await page.keyboard.press("ControlOrMeta+a");
    await expect(stage).toHaveAttribute("data-selected-box-count", String(observed.stageBoxCount));
    await expect(stage).toHaveAttribute("data-image-identity", stageIdentity);
    const metrics = await recording.evaluate(({ stop }) => stop());
    return { gestures: { ...gestures, selectedBoxCount: observed.stageBoxCount }, metrics };
  } finally {
    await recording.dispose();
  }
}

const scenarios: Scenario[] = fixtureConfig.fixtures.flatMap((image) =>
  fixtureConfig.annotationDensities.map((density) => ({
    id: `${image.id}-${density.id}`,
    imageCanvas: image.imageCanvas,
    widthPx: image.widthPx,
    heightPx: image.heightPx,
    annotationCount: density.count,
  })),
);

test.describe.configure({ timeout: 180_000 });

for (const selected of scenarios) {
  test(selected.id, async ({ browser, page, request, seed }, testInfo) => {
    const fixture = await openOwnedImage(page, request, seed, selected);
    const { gestures, metrics } = await measureMatchingFixture(page, fixture, selected);

    const { budgets } = fixtureConfig;
    const measurement = {
      kind: "measurement",
      fixtureConfigVersion: fixtureConfig.version,
      selected,
      observed: fixture.observed,
      browser: {
        version: browser.version(),
        viewport: page.viewportSize(),
        dpr: await page.evaluate(() => devicePixelRatio),
      },
      window: "post-load pan/zoom/select-all",
      gestures,
      metrics,
      softBudget: {
        ...budgets,
        met:
          (metrics.longTaskMaxMs === null || metrics.longTaskMaxMs <= budgets.longTaskMaxMs) &&
          metrics.longTaskCount <= budgets.longTaskCountUpper,
      },
    };
    const path = testInfo.outputPath("image-bench-measurement.json");
    await writeFile(path, `${JSON.stringify(measurement, null, 2)}\n`);
    await testInfo.attach("image-bench-measurement", { path, contentType: "application/json" });
  });
}

test("image-bench rejects fixture mismatches", async ({ page, request, seed }, testInfo) => {
  const actual = scenarios.find(({ id }) => id === "image-720p-boxes-10");
  const wrongSize = scenarios.find(({ id }) => id === "image-5k-boxes-10");
  const wrongDensity = scenarios.find(({ id }) => id === "image-720p-boxes-100");
  if (!actual || !wrongSize || !wrongDensity)
    throw new Error("Mismatch test scenarios are missing");
  const fixture = await openOwnedImage(page, request, seed, actual);
  requireMatchingFixture(actual, fixture.observed);
  await expect(measureMatchingFixture(page, fixture, wrongSize)).rejects.toThrow(
    "served image dimensions: expected 5120×2880, observed 1280×720",
  );
  await expect(measureMatchingFixture(page, fixture, wrongDensity)).rejects.toThrow(
    "apiBoxCount: expected 100, observed 10",
  );
  expect(testInfo.attachments).toEqual([]);
});
