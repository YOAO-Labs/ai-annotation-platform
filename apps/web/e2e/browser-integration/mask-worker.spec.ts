import { expect, test } from "@playwright/test";

// Browser runtime integration: direct Vite source imports, not built-product E2E.
test.describe("Mask Worker browser integration", () => {
  test("4. 1080p 实际 Worker 可完成、可取消且失败不修改输入", async ({ page }) => {
    test.setTimeout(90_000);
    await page.goto("/e2e/fixtures/mask-worker.html");

    const result = await page.evaluate(async () => {
      const compute = await import("/src/pages/Workbench/stage/shared/rasterMaskCompute.ts");
      const codec = await import("/src/pages/Workbench/stage/shared/geometry/maskRle.ts");
      const width = 1920;
      const height = 1080;
      const alpha = new Uint8Array(width * height);
      for (let y = 400; y < 680; y += 1) {
        alpha.fill(255, y * width + 700, y * width + 1220);
      }
      const rle = codec.encodeCocoRle(alpha, width, height);
      const originalCounts = [...rle.counts];
      const completed = await compute.executeRasterMaskOperationAsync(
        rle,
        { type: "morphology", operation: "erode", kernelShape: "disk", radius: 2 },
        { sessionId: "e2e-worker", generation: 1, operationId: 1 },
      );

      const controller = new AbortController();
      const cancelledPromise = compute.executeRasterMaskOperationAsync(
        rle,
        { type: "morphology", operation: "close", kernelShape: "disk", radius: 32 },
        { sessionId: "e2e-worker", generation: 1, operationId: 2 },
        { signal: controller.signal },
      );
      controller.abort();
      let cancelledName = "";
      try {
        await cancelledPromise;
      } catch (error) {
        cancelledName = error instanceof Error ? error.name : String(error);
      }

      let failingWorker: {
        onmessage: ((event: MessageEvent) => void) | null;
        onerror: ((event: { message: string }) => void) | null;
        postMessage: () => void;
        terminate: () => void;
      };
      const failurePromise = compute.executeRasterMaskOperationAsync(
        rle,
        { type: "fill_holes", mode: "all" },
        { sessionId: "e2e-worker", generation: 1, operationId: 3 },
        {
          createWorker: () => {
            failingWorker = {
              onmessage: null,
              onerror: null,
              postMessage: () =>
                queueMicrotask(() => failingWorker.onerror?.({ message: "forced worker failure" })),
              terminate: () => undefined,
            };
            return failingWorker as unknown as Worker;
          },
        },
      );
      let failureName = "";
      try {
        await failurePromise;
      } catch (error) {
        failureName = error instanceof Error ? error.name : String(error);
      }
      return {
        completedArea: completed.result.report.afterArea,
        cancelledName,
        failureName,
        inputUnchanged: JSON.stringify(rle.counts) === JSON.stringify(originalCounts),
      };
    });

    expect(result.completedArea).toBe(142416);
    expect(result.cancelledName).toBe("RasterMaskWorkerCancelledError");
    expect(result.failureName).toBe("RasterMaskWorkerError");
    expect(result.inputUnchanged).toBe(true);
  });
});
