import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";

import type { Psr } from "./geometry/triview";

import {
  clientRectToCanvasClipPath,
  clientRectToRendererScissors,
  clientRectToRendererViewport,
  setClientCameraViewport,
  subtractClientRect,
  PointCloudTriViewPass,
} from "./PointCloudTriViewPass";

const canvas = { left: 100, top: 50, width: 800, height: 600 };

describe("clientRectToRendererViewport", () => {
  it("uses a top-left origin for WebGPU and its WebGL2 fallback", () => {
    const rect = { left: 620, top: 140, width: 240, height: 120 };
    expect(clientRectToRendererViewport(rect, canvas, "webgpu")).toEqual({
      x: 520,
      y: 90,
      width: 240,
      height: 120,
    });
    expect(clientRectToRendererViewport(rect, canvas, "webgl2-fallback")).toEqual({
      x: 520,
      y: 90,
      width: 240,
      height: 120,
    });
  });

  it("converts the same client rect to WebGL's lower-left origin", () => {
    expect(
      clientRectToRendererViewport(
        { left: 620, top: 140, width: 240, height: 120 },
        canvas,
        "legacy-webgl2",
      ),
    ).toEqual({ x: 520, y: 390, width: 240, height: 120 });
  });

  it("clamps a floating panel to the renderer canvas", () => {
    expect(
      clientRectToRendererViewport(
        { left: 60, top: 20, width: 120, height: 100 },
        canvas,
        "webgpu",
      ),
    ).toEqual({ x: 0, y: 0, width: 80, height: 70 });
    expect(
      clientRectToRendererViewport(
        { left: 920, top: 700, width: 50, height: 50 },
        canvas,
        "webgpu",
      ),
    ).toBeNull();
  });
});

describe("clientRectToCanvasClipPath", () => {
  it("clips an elevated shared renderer canvas to the floating tri-view panel", () => {
    expect(
      clientRectToCanvasClipPath({ left: 620, top: 140, width: 240, height: 360 }, canvas),
    ).toBe("inset(90px 40px 150px 520px)");
  });

  it("returns null when the floating panel is outside the renderer canvas", () => {
    expect(
      clientRectToCanvasClipPath({ left: 920, top: 700, width: 50, height: 50 }, canvas),
    ).toBeNull();
  });
});

describe("PointCloudTriViewPass layout invalidation", () => {
  it("switches the legacy tri-view material to surface splats in place", () => {
    const pass = new PointCloudTriViewPass("legacy-webgl2", 1);
    const geometry = new THREE.BufferGeometry().setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array([0, 0, 0]), 3),
    );
    pass.setGeometry(geometry);
    const material = (pass as unknown as { material: THREE.PointsMaterial }).material;

    expect(pass.setDisplayStyle("surface")).toBe(true);
    expect(material.alphaMap).toBeInstanceOf(THREE.DataTexture);
    expect(material.alphaTest).toBeGreaterThan(0);
    expect(pass.setDisplayStyle("surface")).toBe(false);
    expect(pass.setDisplayStyle("points")).toBe(true);
    expect(material.alphaMap).toBeNull();

    pass.dispose();
    geometry.dispose();
  });

  it("keeps the complete view aspect when floating panels obscure part of a docked view", () => {
    const pass = new PointCloudTriViewPass("legacy-webgl2", 1);
    const geometry = new THREE.BufferGeometry().setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array([0, 0, 0]), 3),
    );
    pass.setGeometry(geometry);
    pass.setActive(true);
    pass.setBox({ center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] });
    const panel = { left: 120, top: 100, width: 300, height: 150 };
    const layout = {
      panel,
      views: [{ ...panel, view: "top" as const }],
      visibleRegions: [{ ...panel, width: 120 }],
    };
    pass.setLayout(layout);
    const render = vi.fn((_scene: THREE.Scene, camera: THREE.OrthographicCamera) => {
      expect((camera.right - camera.left) / (camera.top - camera.bottom)).toBeCloseTo(2);
    });
    const renderer = {
      setScissorTest: vi.fn(),
      setViewport: vi.fn(),
      setScissor: vi.fn(),
      render,
      clear: vi.fn(),
    };
    expect(pass.render(renderer as never, canvas)).toBe(1);
    expect(renderer.setViewport).toHaveBeenCalledWith(20, 400, 300, 150);
    expect(renderer.setScissor).toHaveBeenCalledWith(20, 400, 120, 150);
    expect(renderer.clear).not.toHaveBeenCalled();
    expect(pass.setLayout({ ...layout, visibleRegions: [] })).toBe(true);
    expect(pass.render(renderer as never, canvas)).toBe(0);
    expect(render).toHaveBeenCalledTimes(1);
    pass.dispose();
    geometry.dispose();
  });

  it("ignores subpixel DOM measurement jitter", () => {
    const pass = new PointCloudTriViewPass("legacy-webgl2", 1);
    const layout = {
      panel: { left: 100, top: 200, width: 320, height: 420 },
      views: [{ view: "top" as const, left: 108, top: 240, width: 304, height: 120 }],
    };

    expect(pass.setLayout(layout)).toBe(true);
    expect(
      pass.setLayout({
        panel: { ...layout.panel, top: layout.panel.top + 0.5 },
        views: [{ ...layout.views[0], top: layout.views[0].top + 0.5 }],
      }),
    ).toBe(false);
    expect(
      pass.setLayout({
        panel: { ...layout.panel, top: layout.panel.top + 1 },
        views: [{ ...layout.views[0], top: layout.views[0].top + 1 }],
      }),
    ).toBe(true);

    pass.dispose();
  });

  it("prewarms each WebGPU geometry once with the six-plane clipping topology", async () => {
    const pass = new PointCloudTriViewPass("webgpu", 1);
    const box: Psr = {
      center: [0, 0, 0],
      size: [4, 2, 2],
      rotation: [0, 0, 0],
    };
    let finishCompile: (() => void) | undefined;
    const compileAsync = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishCompile = resolve;
        }),
    );
    const firstGeometry = new THREE.BufferGeometry().setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array([0, 0, 0]), 3),
    );

    pass.setGeometry(firstGeometry);
    pass.setActive(true);
    const firstPrewarm = pass.prewarm({ compileAsync });
    expect(pass.prewarm({ compileAsync })).toBeNull();
    expect(compileAsync).toHaveBeenCalledTimes(1);
    expect(pass.render({ compileAsync } as never, canvas)).toBe(0);

    finishCompile?.();
    await firstPrewarm;
    expect(pass.prewarm({ compileAsync })).toBeNull();
    pass.setBox(box);
    expect(pass.prewarm({ compileAsync })).toBeNull();

    const secondGeometry = firstGeometry.clone();
    pass.setGeometry(secondGeometry);
    expect(pass.prewarm({ compileAsync })).not.toBeNull();
    expect(compileAsync).toHaveBeenCalledTimes(2);

    pass.dispose();
    firstGeometry.dispose();
    secondGeometry.dispose();
  });
});

describe("shared surface clipping", () => {
  it("subtracts overlapping occluders without reopening covered pixels", () => {
    const rect = { left: 0, top: 0, width: 100, height: 100 };
    const occluders = [
      { left: 20, top: 20, width: 40, height: 40 },
      { left: 40, top: 40, width: 40, height: 40 },
    ];
    const pieces = occluders.reduce(
      (regions, occluder) => regions.flatMap((region) => subtractClientRect(region, occluder)),
      [rect],
    );
    expect(pieces.reduce((sum, piece) => sum + piece.width * piece.height, 0)).toBe(7200);
    expect(clientRectToRendererScissors(rect, rect, "webgpu", [])).toEqual([]);
  });

  it("preserves pixel placement when WebGPU clips a viewport at the workspace edge", () => {
    const camera = new THREE.OrthographicCamera(-4, 4, 2, -2, 0.1, 100);
    camera.position.set(0, 0, 10);
    camera.updateMatrixWorld();
    const rect = { left: 60, top: 20, width: 240, height: 120 };
    const point = new THREE.Vector3(1, 1, 0);
    const projected = point.clone().project(camera);
    const expectedX = rect.left + ((projected.x + 1) / 2) * rect.width;
    const expectedY = rect.top + ((1 - projected.y) / 2) * rect.height;
    const viewport = setClientCameraViewport(camera, rect, canvas, "webgpu")!;
    const clipped = point.clone().project(camera);
    expect(canvas.left + viewport.x + ((clipped.x + 1) / 2) * viewport.width).toBeCloseTo(
      expectedX,
    );
    expect(canvas.top + viewport.y + ((1 - clipped.y) / 2) * viewport.height).toBeCloseTo(
      expectedY,
    );
  });
});
