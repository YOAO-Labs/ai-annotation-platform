import type { Locator } from "@playwright/test";

export function annotationPaintedPixels(stage: Locator) {
  return stage.locator(".konvajs-content > canvas").evaluateAll((canvases) => {
    const annotationCanvases = canvases.slice(1);
    if (!annotationCanvases.length) throw new Error("annotation canvas is not ready");
    let painted = 0;
    for (const node of annotationCanvases) {
      const canvas = node as HTMLCanvasElement;
      if (!canvas.width || !canvas.height) throw new Error("annotation canvas is not ready");
      const context = canvas.getContext("2d");
      if (!context) throw new Error("annotation canvas has no 2D context");
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      for (let index = 3; index < pixels.length; index += 4) if (pixels[index] > 0) painted += 1;
    }
    return painted;
  });
}
