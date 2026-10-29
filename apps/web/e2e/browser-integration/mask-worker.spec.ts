import { expect, test } from "../fixtures/seed";

// Browser runtime integration: direct Vite source imports, not built-product E2E.
test.describe("Mask Worker browser integration", () => {
  test("4. 1080p 实际 Worker 可完成、可取消且失败不修改输入", async ({ page, seed }) => {
    test.skip(
      Boolean(process.env.CI) && !process.env.PLAYWRIGHT_RASTER_MASK_MATRIX,
      "源码 Worker 模块需由 Vite 开发服务提供，在 native Mask 矩阵执行",
    );
    test.setTimeout(90_000);
    const data = await seed.owned();
    await seed.injectToken(page, data.admin_email);
    await page.goto("/dashboard");

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
      let failureMessage = "";
      try {
        await failurePromise;
      } catch (error) {
        failureName = error instanceof Error ? error.name : String(error);
        failureMessage = error instanceof Error ? error.message : String(error);
      }
      return {
        completedContext: completed.context,
        completedArea: completed.result.report.afterArea,
        cancelledName,
        failureName,
        failureMessage,
        inputUnchanged: JSON.stringify(rle.counts) === JSON.stringify(originalCounts),
      };
    });

    expect(result.completedContext).toEqual({
      sessionId: "e2e-worker",
      generation: 1,
      operationId: 1,
    });
    expect(result.completedArea).toBeGreaterThan(0);
    expect(result.cancelledName).toBe("RasterMaskWorkerCancelledError");
    expect(result.failureName).toBe("RasterMaskWorkerError");
    expect(result.failureMessage).toContain("forced worker failure");
    expect(result.inputUnchanged).toBe(true);
  });
});
