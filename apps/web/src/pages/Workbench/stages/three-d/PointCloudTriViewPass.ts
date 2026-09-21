import * as THREE from "three";

import type { ColorAdjust } from "./geometry/colorize";
import { boxAxisWorldDir, boxLocalClipPlanes } from "./geometry/box3d";
import {
  clampTriZoom,
  frameOrtho,
  FRAME_MARGIN,
  TRI_ZOOM_DEFAULT,
  VIEW_AXES,
  type Psr,
  type TriView,
} from "./geometry/triview";
import type { GpuCameraTextureSample } from "./rendering/cameraTextureColorNode";
import type { PointCloudActualBackend, PointCloudRenderer } from "./rendering/pointCloudRenderer";
import {
  createWebGpuPointCloudLayer,
  type WebGpuPointCloudLayer,
} from "./rendering/webgpuPointCloudLayer";
import {
  applyLegacyPointCloudDisplayStyle,
  disposeLegacyPointCloudDisplayStyle,
  pointCloudDisplaySize,
  type PointCloudDisplayStyle,
} from "./rendering/pointCloudDisplayStyle";

export interface ClientRectSnapshot {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface TriViewClientRect extends ClientRectSnapshot {
  view: TriView;
}

export interface TriViewClientLayout {
  panel: ClientRectSnapshot;
  views: TriViewClientRect[];
  /** Disjoint client-coordinate pieces after Dockview visibility and floating occlusion. */
  visibleRegions?: readonly ClientRectSnapshot[];
}

export type PointCloudVisibleRegions = (element: HTMLElement) => readonly ClientRectSnapshot[];

export interface RendererViewportRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

function samePsr(a: Psr | null, b: Psr | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.center.every((value, index) => value === b.center[index]) &&
    a.size.every((value, index) => value === b.size[index]) &&
    a.rotation.every((value, index) => value === b.rotation[index])
  );
}

function sameRect(a: ClientRectSnapshot, b: ClientRectSnapshot): boolean {
  const epsilon = 0.75;
  return (
    Math.abs(a.left - b.left) <= epsilon &&
    Math.abs(a.top - b.top) <= epsilon &&
    Math.abs(a.width - b.width) <= epsilon &&
    Math.abs(a.height - b.height) <= epsilon
  );
}

function sameLayout(a: TriViewClientLayout | null, b: TriViewClientLayout | null): boolean {
  if (a === b) return true;
  if (!a || !b || !sameRect(a.panel, b.panel) || a.views.length !== b.views.length) return false;
  const aRegions = a.visibleRegions ?? [a.panel];
  const bRegions = b.visibleRegions ?? [b.panel];
  if (
    aRegions.length !== bRegions.length ||
    !aRegions.every((rect, index) => sameRect(rect, bRegions[index]))
  )
    return false;
  return a.views.every((rect, index) => {
    const other = b.views[index];
    return rect.view === other.view && sameRect(rect, other);
  });
}

export function intersectClientRects(
  a: ClientRectSnapshot,
  b: ClientRectSnapshot,
): ClientRectSnapshot | null {
  const left = Math.max(a.left, b.left);
  const top = Math.max(a.top, b.top);
  const right = Math.min(a.left + a.width, b.left + b.width);
  const bottom = Math.min(a.top + a.height, b.top + b.height);
  return right > left && bottom > top
    ? { left, top, width: right - left, height: bottom - top }
    : null;
}

/** Returns disjoint pieces; overlapping occluders cannot reopen an earlier hole. */
export function subtractClientRect(
  rect: ClientRectSnapshot,
  occluder: ClientRectSnapshot,
): ClientRectSnapshot[] {
  const overlap = intersectClientRects(rect, occluder);
  if (!overlap) return [rect];
  const right = rect.left + rect.width;
  const bottom = rect.top + rect.height;
  const overlapRight = overlap.left + overlap.width;
  const overlapBottom = overlap.top + overlap.height;
  return [
    { left: rect.left, top: rect.top, width: rect.width, height: overlap.top - rect.top },
    { left: rect.left, top: overlapBottom, width: rect.width, height: bottom - overlapBottom },
    { left: rect.left, top: overlap.top, width: overlap.left - rect.left, height: overlap.height },
    { left: overlapRight, top: overlap.top, width: right - overlapRight, height: overlap.height },
  ].filter((piece) => piece.width > 0 && piece.height > 0);
}

/** Scissors clip visibility while the camera continues to frame the complete content rect. */
export function clientRectToRendererScissors(
  rect: ClientRectSnapshot,
  canvasRect: ClientRectSnapshot,
  backend: PointCloudActualBackend,
  visibleRegions: readonly ClientRectSnapshot[] = [rect],
): RendererViewportRect[] {
  return visibleRegions.flatMap((region) => {
    const visible = intersectClientRects(rect, region);
    const scissor = visible && clientRectToRendererViewport(visible, canvasRect, backend);
    return scissor ? [scissor] : [];
  });
}

/** WebGPU requires an in-bounds viewport. A view offset preserves full-rect projection at edges. */
export function setClientCameraViewport(
  camera: THREE.PerspectiveCamera | THREE.OrthographicCamera,
  rect: ClientRectSnapshot,
  canvasRect: ClientRectSnapshot,
  backend: PointCloudActualBackend,
): RendererViewportRect | null {
  const visible = intersectClientRects(rect, canvasRect);
  if (!visible) return null;
  camera.clearViewOffset();
  if (
    rect.left !== visible.left ||
    rect.top !== visible.top ||
    rect.width !== visible.width ||
    rect.height !== visible.height
  ) {
    camera.setViewOffset(
      rect.width,
      rect.height,
      visible.left - rect.left,
      visible.top - rect.top,
      visible.width,
      visible.height,
    );
  }
  return clientRectToRendererViewport(visible, canvasRect, backend);
}

/** Returns a CSS clip-path that exposes only the floating tri-view panel on the shared canvas. */
export function clientRectToCanvasClipPath(
  rect: ClientRectSnapshot,
  canvasRect: ClientRectSnapshot,
): string | null {
  const canvasWidth = Math.round(canvasRect.width);
  const canvasHeight = Math.round(canvasRect.height);
  const left = Math.max(0, Math.round(rect.left - canvasRect.left));
  const top = Math.max(0, Math.round(rect.top - canvasRect.top));
  const right = Math.min(canvasWidth, Math.round(rect.left + rect.width - canvasRect.left));
  const bottom = Math.min(canvasHeight, Math.round(rect.top + rect.height - canvasRect.top));
  if (right <= left || bottom <= top) return null;
  return `inset(${top}px ${canvasWidth - right}px ${canvasHeight - bottom}px ${left}px)`;
}

/** Converts a DOM client rect into renderer logical pixels and clamps it to the main canvas. */
export function clientRectToRendererViewport(
  rect: ClientRectSnapshot,
  canvasRect: ClientRectSnapshot,
  backend: PointCloudActualBackend,
): RendererViewportRect | null {
  const left = Math.max(0, Math.round(rect.left - canvasRect.left));
  const top = Math.max(0, Math.round(rect.top - canvasRect.top));
  const right = Math.min(
    Math.round(canvasRect.width),
    Math.round(rect.left + rect.width - canvasRect.left),
  );
  const bottom = Math.min(
    Math.round(canvasRect.height),
    Math.round(rect.top + rect.height - canvasRect.top),
  );
  const width = right - left;
  const height = bottom - top;
  if (width <= 0 || height <= 0) return null;
  return {
    x: left,
    y: backend === "legacy-webgl2" ? Math.round(canvasRect.height) - bottom : top,
    width,
    height,
  };
}

const TRI_VIEW_PREWARM_BOX: Psr = {
  center: [0, 0, 0],
  size: [1, 1, 1],
  rotation: [0, 0, 0],
};

/** Orthographic point-cloud pass. Renderer, canvas and scheduling remain owned by PointCloudScene. */
export class PointCloudTriViewPass {
  private readonly scene = new THREE.Scene();
  private readonly cameras: Record<TriView, THREE.OrthographicCamera>;
  private points: THREE.Object3D | null = null;
  private geometry: THREE.BufferGeometry | null = null;
  private material: THREE.PointsMaterial | null = null;
  private webGpuPointLayer: WebGpuPointCloudLayer | null = null;
  private layout: TriViewClientLayout | null = null;
  private box: Psr | null = null;
  private cameraRef: Psr | null = null;
  private zoomByView: Record<TriView, number> = {
    top: TRI_ZOOM_DEFAULT,
    side: TRI_ZOOM_DEFAULT,
    front: TRI_ZOOM_DEFAULT,
  };
  private worldPointSize = 0.06;
  private displayStyle: PointCloudDisplayStyle = "points";
  private active = false;
  private visible = true;
  private geometryGeneration = 0;
  private prewarmedGeneration = -1;
  private prewarmPromise: Promise<void> | null = null;

  constructor(
    private readonly backend: PointCloudActualBackend,
    private readonly pixelRatio: number,
  ) {
    if (backend === "legacy-webgl2") {
      this.material = new THREE.PointsMaterial({
        size: 2,
        vertexColors: true,
        sizeAttenuation: false,
      });
      this.material.clippingPlanes = [];
    }
    const makeCamera = () => new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 1000);
    this.cameras = { top: makeCamera(), side: makeCamera(), front: makeCamera() };
  }

  setGeometry(geometry: THREE.BufferGeometry | null): boolean {
    if (this.geometry === geometry) {
      if (geometry && this.webGpuPointLayer) {
        const count = Number.isFinite(geometry.drawRange.count)
          ? geometry.drawRange.count
          : geometry.getAttribute("position").count;
        this.webGpuPointLayer.setPointCount(count);
      }
      return false;
    }
    this.removePoints();
    this.geometryGeneration += 1;
    this.prewarmPromise = null;
    this.geometry = geometry;
    if (!geometry) return true;
    if (this.material) {
      this.points = new THREE.Points(geometry, this.material);
    } else {
      this.webGpuPointLayer = createWebGpuPointCloudLayer(geometry, {
        pointSize: 2,
        sizeAttenuation: false,
        clipping: true,
        displayStyle: this.displayStyle,
      });
      this.points = this.webGpuPointLayer.object;
    }
    this.points.frustumCulled = false;
    this.scene.add(this.points);
    this.applyClippingPlanes();
    return true;
  }

  prewarm(renderer: Pick<PointCloudRenderer, "compileAsync">): Promise<void> | null {
    if (
      this.backend !== "webgpu" ||
      this.prewarmedGeneration === this.geometryGeneration ||
      this.prewarmPromise ||
      !this.points
    ) {
      return null;
    }
    const usesPlaceholderBox = this.box === null;
    const compileBox = this.box ?? TRI_VIEW_PREWARM_BOX;
    if (usesPlaceholderBox) {
      this.webGpuPointLayer?.setClippingPlanes(
        boxLocalClipPlanes(compileBox.center, compileBox.size, compileBox.rotation, FRAME_MARGIN),
      );
    }
    this.updateCamera("top", 1, compileBox);
    const generation = this.geometryGeneration;
    const promise = renderer
      .compileAsync(this.scene, this.cameras.top)
      .then(() => {
        if (generation === this.geometryGeneration) this.prewarmedGeneration = generation;
      })
      .catch(() => undefined)
      .finally(() => {
        if (usesPlaceholderBox && generation === this.geometryGeneration) {
          this.applyClippingPlanes();
        }
        if (this.prewarmPromise === promise) this.prewarmPromise = null;
      });
    this.prewarmPromise = promise;
    return promise;
  }

  setLayout(layout: TriViewClientLayout | null): boolean {
    if (sameLayout(this.layout, layout)) return false;
    this.layout = layout;
    return true;
  }

  setBox(box: Psr | null): boolean {
    if (samePsr(this.box, box)) return false;
    this.box = box;
    this.applyClippingPlanes();
    return true;
  }

  setCameraRef(cameraRef: Psr | null): boolean {
    if (samePsr(this.cameraRef, cameraRef)) return false;
    this.cameraRef = cameraRef;
    return true;
  }

  setZoomByView(zoomByView: Record<TriView, number>): boolean {
    const next = {
      top: clampTriZoom(zoomByView.top),
      side: clampTriZoom(zoomByView.side),
      front: clampTriZoom(zoomByView.front),
    };
    if (
      next.top === this.zoomByView.top &&
      next.side === this.zoomByView.side &&
      next.front === this.zoomByView.front
    ) {
      return false;
    }
    this.zoomByView = next;
    return true;
  }

  setPointSize(size: number): boolean {
    if (size === this.worldPointSize) return false;
    this.worldPointSize = size;
    return true;
  }

  setDisplayStyle(style: PointCloudDisplayStyle): boolean {
    if (style === this.displayStyle) return false;
    this.displayStyle = style;
    if (this.material) applyLegacyPointCloudDisplayStyle(this.material, style);
    this.webGpuPointLayer?.setDisplayStyle(style);
    return true;
  }

  setActive(active: boolean): boolean {
    if (active === this.active) return false;
    this.active = active;
    return true;
  }

  setVisible(visible: boolean): boolean {
    if (visible === this.visible) return false;
    this.visible = visible;
    return true;
  }

  setCameraTextureColorization(samples: readonly GpuCameraTextureSample[] | null): void {
    this.webGpuPointLayer?.setCameraColorization(samples);
  }

  setCameraTextureColorAdjust(adjust: ColorAdjust): void {
    this.webGpuPointLayer?.setColorAdjust(adjust);
  }

  getOccludingRegions(): readonly ClientRectSnapshot[] {
    return this.active && this.layout ? (this.layout.visibleRegions ?? [this.layout.panel]) : [];
  }

  render(renderer: PointCloudRenderer, canvasRect: ClientRectSnapshot): number {
    const layout = this.layout;
    const box = this.box;
    if (
      !this.active ||
      !this.visible ||
      this.prewarmPromise ||
      !layout ||
      !box ||
      !this.points ||
      layout.views.length === 0
    ) {
      return 0;
    }

    renderer.setScissorTest(true);
    let passCount = 0;
    for (const rect of layout.views) {
      const scissors = clientRectToRendererScissors(
        rect,
        canvasRect,
        this.backend,
        layout.visibleRegions,
      );
      if (scissors.length === 0) continue;
      const aspect = rect.width / rect.height;
      this.updateCamera(rect.view, aspect);
      const camera = this.cameras[rect.view];
      const viewport = setClientCameraViewport(camera, rect, canvasRect, this.backend);
      if (!viewport) continue;
      renderer.setViewport(viewport.x, viewport.y, viewport.width, viewport.height);
      const cameraBox = this.cameraRef ?? box;
      const { halfW } = frameOrtho(
        cameraBox.size,
        rect.view,
        aspect,
        FRAME_MARGIN,
        this.zoomByView[rect.view],
      );
      const cssPixelsPerMeter = rect.width / 2 / halfW;
      const pointSize = Math.max(1, this.worldPointSize * cssPixelsPerMeter * this.pixelRatio);
      if (this.material) this.material.size = pointCloudDisplaySize(pointSize, this.displayStyle);
      this.webGpuPointLayer?.setPointSize(pointSize);
      for (const scissor of scissors) {
        renderer.setScissor(scissor.x, scissor.y, scissor.width, scissor.height);
        renderer.render(this.scene, camera);
        passCount += 1;
      }
      camera.clearViewOffset();
    }
    renderer.setScissorTest(false);
    return passCount;
  }

  dispose(): void {
    this.removePoints();
    if (this.material) disposeLegacyPointCloudDisplayStyle(this.material);
    this.material?.dispose();
    this.material = null;
    this.layout = null;
    this.box = null;
    this.cameraRef = null;
  }

  private applyClippingPlanes(): void {
    const planes = this.box
      ? boxLocalClipPlanes(this.box.center, this.box.size, this.box.rotation, FRAME_MARGIN)
      : [];
    if (this.material) this.material.clippingPlanes = planes;
    this.webGpuPointLayer?.setClippingPlanes(planes);
  }

  private updateCamera(view: TriView, aspect: number, boxOverride?: Psr): void {
    const box = boxOverride ?? this.cameraRef ?? this.box;
    if (!box) return;
    const camera = this.cameras[view];
    const { u, v, normal } = VIEW_AXES[view];
    const uDirection = boxAxisWorldDir(box.rotation, u);
    const vDirection = boxAxisWorldDir(box.rotation, v);
    const normalDirection = uDirection.clone().cross(vDirection).normalize();
    const center = new THREE.Vector3(box.center[0], box.center[1], box.center[2]);
    const distance = box.size[normal] / 2 + FRAME_MARGIN + 10;
    camera.position.copy(center).addScaledVector(normalDirection, distance);
    camera.up.copy(vDirection);
    camera.lookAt(center);
    const { halfW, halfH } = frameOrtho(
      box.size,
      view,
      aspect,
      FRAME_MARGIN,
      this.zoomByView[view],
    );
    camera.left = -halfW;
    camera.right = halfW;
    camera.top = halfH;
    camera.bottom = -halfH;
    camera.near = 0.01;
    camera.far = distance * 2 + box.size[normal];
    camera.updateProjectionMatrix();
  }

  private removePoints(): void {
    if (this.points) this.scene.remove(this.points);
    this.points = null;
    this.geometry = null;
    this.webGpuPointLayer?.dispose();
    this.webGpuPointLayer = null;
  }
}
