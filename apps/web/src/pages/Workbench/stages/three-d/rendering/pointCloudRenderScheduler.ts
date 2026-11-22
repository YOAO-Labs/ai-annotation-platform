export const POINT_CLOUD_RENDER_MAIN = 1 << 0;
export const POINT_CLOUD_RENDER_TRI = 1 << 1;
export const POINT_CLOUD_RENDER_ALL = POINT_CLOUD_RENDER_MAIN | POINT_CLOUD_RENDER_TRI;

export interface PointCloudRenderPlan {
  renderMain: boolean;
  renderTri: boolean;
}

export function resolvePointCloudRenderPlan(
  dirtyMask: number,
  controlsChanged: boolean,
): PointCloudRenderPlan {
  const renderMain = dirtyMask !== 0 || controlsChanged;
  return {
    renderMain,
    renderTri: renderMain || (dirtyMask & POINT_CLOUD_RENDER_TRI) !== 0,
  };
}

type RequestFrame = (callback: FrameRequestCallback) => number;
type CancelFrame = (handle: number) => void;

export class PointCloudRenderScheduler {
  private frameHandle = 0;
  private dirtyMask = 0;
  private disposed = false;

  constructor(
    private readonly render: (dirtyMask: number) => boolean | void,
    private readonly requestFrame: RequestFrame = window.requestAnimationFrame.bind(window),
    private readonly cancelFrame: CancelFrame = window.cancelAnimationFrame.bind(window),
  ) {}

  invalidate(mask = POINT_CLOUD_RENDER_ALL): void {
    if (this.disposed) return;
    this.dirtyMask |= mask;
    if (this.frameHandle !== 0) return;
    this.frameHandle = this.requestFrame(this.flush);
  }

  dispose(): void {
    this.disposed = true;
    this.dirtyMask = 0;
    if (this.frameHandle !== 0) this.cancelFrame(this.frameHandle);
    this.frameHandle = 0;
  }

  private readonly flush: FrameRequestCallback = () => {
    this.frameHandle = 0;
    if (this.disposed) return;
    const dirtyMask = this.dirtyMask;
    this.dirtyMask = 0;
    const keepRendering = this.render(dirtyMask) === true;
    if (keepRendering) this.invalidate(POINT_CLOUD_RENDER_MAIN);
  };
}
