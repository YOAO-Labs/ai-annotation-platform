import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthStore } from "@/stores/authStore";
import type {
  BoxPsr,
  PointCloudViewState,
  SceneBox,
  SceneMeasurementPath,
} from "./PointCloudScene";

const mockState = vi.hoisted(() => {
  const initialView = {
    position: [0, -20, 12],
    target: [0, 0, 0],
    up: [0, 0, 1],
    mode: "orbit",
  } as PointCloudViewState;
  const instances: MockScene[] = [];
  const createOptions: Array<{
    rendererMode?: string;
    onDeviceLost?: (reason: string) => void;
  }> = [];
  const createControl = { error: null as Error | null };
  class MockScene {
    currentView: PointCloudViewState = initialView;
    viewChangeHandler: ((view: PointCloudViewState) => void) | null = null;
    loadPcd = vi.fn(async (_url?: string, _convention?: unknown, _options?: unknown) => {
      this.viewChangeHandler?.(initialView);
      return { totalPoints: 10, renderedPoints: 10, decimated: false, decimateStride: 1 };
    });
    getViewState = vi.fn(() => this.currentView);
    applyViewState = vi.fn((view: PointCloudViewState | null | undefined) => {
      if (!view) return false;
      this.currentView = view;
      this.viewChangeHandler?.(view);
      return true;
    });
    setViewChangeHandler = vi.fn((handler: ((view: PointCloudViewState) => void) | null) => {
      this.viewChangeHandler = handler;
    });
    setPointSize = vi.fn();
    setGridVisible = vi.fn();
    setAxisGizmoVisible = vi.fn();
    setCameraDamping = vi.fn();
    setTransformHandler = vi.fn();
    setDecimateThreshold = vi.fn();
    clearPointCloud = vi.fn();
    setPointCloudVisible = vi.fn();
    setBoxes = vi.fn();
    setMeasurementPaths = vi.fn();
    attachTransform = vi.fn();
    detachTransform = vi.fn();
    setTransformMode = vi.fn();
    resize = vi.fn();
    setRenderSurface = vi.fn();
    dispose = vi.fn();

    static async create(
      _container: HTMLElement,
      options: { rendererMode?: string; onDeviceLost?: (reason: string) => void } = {},
    ) {
      createOptions.push(options);
      if (createControl.error) throw createControl.error;
      return new MockScene();
    }

    getRendererStatus = vi.fn(() => ({
      requestedMode: "legacy",
      actualBackend: "legacy-webgl2",
      initMs: 0,
      fallbackReason: null,
    }));

    constructor() {
      instances.push(this);
    }
  }
  return { createControl, createOptions, instances, MockScene };
});

vi.mock("./PointCloudScene", () => ({ PointCloudScene: mockState.MockScene }));

import { usePointCloudScene } from "./usePointCloudScene";

const RUNTIME_VIEW: PointCloudViewState = {
  position: [7, -8, 9],
  target: [1, 2, 3],
  up: [0, 0, 1],
  mode: "orbit",
};
const ACCOUNT_VIEW: PointCloudViewState = {
  position: [0, 0, 40],
  target: [0, 0, 0],
  up: [0, 1, 0],
  mode: "bev",
};

describe("usePointCloudScene camera continuity", () => {
  beforeEach(() => {
    mockState.createOptions.length = 0;
    mockState.instances.length = 0;
    mockState.createControl.error = null;
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  });

  it("retries a failed point cloud without recreating its renderer", async () => {
    const sceneRef = { current: null };
    const view = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: document.createElement("div") },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: "retry.pcd",
        continuityKey: "retry-scene",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(view.result.current.loadedPointCloudUrl).toBe("retry.pcd"));
    const scene = mockState.instances[0];
    scene.loadPcd.mockRejectedValueOnce(new Error("decode failed"));
    act(() => view.result.current.retryLoad());
    await waitFor(() => expect(view.result.current.loadError).toBe("decode failed"));
    act(() => view.result.current.retryLoad());
    await waitFor(() => expect(view.result.current.loadedPointCloudUrl).toBe("retry.pcd"));
    expect(view.result.current.loadError).toBeNull();
    expect(mockState.instances).toHaveLength(1);
    expect(scene.dispose).not.toHaveBeenCalled();
  });

  it("moves the render surface and refreshes layout without recreating or reloading the scene", async () => {
    const container = document.createElement("div");
    const surface = document.createElement("div");
    const sceneRef = { current: null };
    const viewportRef = { current: container };
    const render = renderHook(
      ({ renderSurface, layoutKey }) =>
        usePointCloudScene({
          viewportRef,
          sceneRef: sceneRef as never,
          renderSurface,
          layoutKey,
          pcdDecimate: 100,
          pointSize: 0.06,
          showGrid: true,
          showAxisGizmo: true,
          cameraDamping: 0.1,
          persistCameraView: false,
          pointCloudUrl: "frame-shared.pcd",
          continuityKey: "shared-surface",
          axisConvention: "iso_8855",
          boxes: [],
          selectedId: null,
          selectedPsrEditable: false,
          pointcloudCamera: null,
          onWorkbenchLayoutChange: vi.fn(),
          onViewModeChange: vi.fn(),
          onTransformPreview: vi.fn(),
          onTransformCommit: vi.fn(),
        }),
      { initialProps: { renderSurface: null as HTMLElement | null, layoutKey: 0 } },
    );
    await waitFor(() => expect(render.result.current.stats?.totalPoints).toBe(10));
    const scene = mockState.instances[0];
    const loadCount = scene.loadPcd.mock.calls.length;
    render.rerender({ renderSurface: surface, layoutKey: 1 });
    render.rerender({ renderSurface: surface, layoutKey: 2 });
    expect(mockState.instances).toHaveLength(1);
    expect(scene.setRenderSurface).toHaveBeenLastCalledWith(surface);
    expect(scene.resize).toHaveBeenCalled();
    expect(scene.loadPcd).toHaveBeenCalledTimes(loadCount);
    expect(scene.dispose).not.toHaveBeenCalled();
  });

  it("Renderer 创建失败时结束加载并暴露明确错误", async () => {
    mockState.createControl.error = new Error("Error creating WebGL context.");
    const container = document.createElement("div");
    const sceneRef = { current: null };
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: "frame-1.pcd",
        continuityKey: "scene-renderer-error",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );

    await waitFor(() =>
      expect(render.result.current.rendererError).toBe("Error creating WebGL context."),
    );
    expect(render.result.current.isLoading).toBe(false);
    expect(render.result.current.rendererStatus).toBeNull();
    expect(sceneRef.current).toBeNull();
  });

  it("将 gizmo 拖动帧路由到本地预览，仅在松手后提交", async () => {
    const container = document.createElement("div");
    const sceneRef = { current: null };
    const onTransformPreview = vi.fn();
    const onTransformCommit = vi.fn();
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: undefined,
        continuityKey: "scene-transform-preview",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview,
        onTransformCommit,
      }),
    );
    await waitFor(() => expect(mockState.instances).toHaveLength(1));
    const handler = mockState.instances[0].setTransformHandler.mock.calls[0]?.[0] as (
      id: string,
      psr: BoxPsr,
      commit: boolean,
    ) => void;
    const psr: BoxPsr = {
      center: [3, 4, 5],
      size: [6, 2, 1.5],
      rotation: [0, 0, 0.2],
    };

    act(() => handler("box-1", psr, false));
    expect(onTransformPreview).toHaveBeenCalledWith("box-1", psr);
    expect(onTransformCommit).not.toHaveBeenCalled();

    act(() => handler("box-1", psr, true));
    expect(onTransformCommit).toHaveBeenCalledWith("box-1", psr);
    render.unmount();
  });

  it("选中框数据延迟到达时重新挂载变换 gizmo", async () => {
    const container = document.createElement("div");
    const sceneRef = { current: null };
    let boxes: SceneBox[] = [];
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: undefined,
        continuityKey: "scene-late-selected-box",
        axisConvention: "iso_8855",
        boxes,
        selectedId: "box-1",
        selectedPsrEditable: true,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(mockState.instances).toHaveLength(1));
    const scene = mockState.instances[0];
    await waitFor(() => expect(scene.attachTransform).toHaveBeenCalledWith("box-1"));
    scene.attachTransform.mockClear();

    boxes = [
      {
        id: "box-1",
        center: [1, 2, 3],
        size: [4, 2, 1.5],
        rotation: [0, 0, 0.2],
        color: "#22c55e",
        selected: true,
      },
    ];
    render.rerender();

    await waitFor(() => expect(scene.setBoxes).toHaveBeenLastCalledWith(boxes));
    expect(scene.attachTransform).toHaveBeenCalledWith("box-1");
    render.unmount();
  });

  it("同 continuity key 重载时在 loadPcd 后恢复运行时视角", async () => {
    const container = document.createElement("div");
    const sceneRef = { current: null };
    const onWorkbenchLayoutChange = vi.fn();
    const onViewModeChange = vi.fn();
    let pointCloudUrl = "frame-1.pcd";
    let continuityKey = "scene-a";
    let persistCameraView = false;
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView,
        pointCloudUrl,
        continuityKey,
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: ACCOUNT_VIEW,
        onWorkbenchLayoutChange,
        onViewModeChange,
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(render.result.current.stats?.totalPoints).toBe(10));
    const scene = mockState.instances[0];
    scene.currentView = RUNTIME_VIEW;

    pointCloudUrl = "frame-2.pcd";
    render.rerender();
    await waitFor(() => expect(scene.loadPcd).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(scene.applyViewState).toHaveBeenCalledWith(RUNTIME_VIEW));
    expect(scene.loadPcd.mock.invocationCallOrder[1]).toBeLessThan(
      scene.applyViewState.mock.invocationCallOrder[0],
    );
    expect(onWorkbenchLayoutChange).not.toHaveBeenCalled();

    continuityKey = "scene-b";
    pointCloudUrl = "scene-b-frame-1.pcd";
    render.rerender();
    await waitFor(() => expect(scene.loadPcd).toHaveBeenCalledTimes(3));
    expect(scene.applyViewState).toHaveBeenCalledTimes(1);

    persistCameraView = true;
    continuityKey = "scene-c";
    pointCloudUrl = "scene-c-frame-1.pcd";
    render.rerender();
    await waitFor(() => expect(scene.applyViewState).toHaveBeenLastCalledWith(ACCOUNT_VIEW));
    expect(onViewModeChange).toHaveBeenLastCalledWith("bev");
    expect(onWorkbenchLayoutChange).not.toHaveBeenCalled();

    render.unmount();
    const remountedSceneRef = { current: null };
    const remounted = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: remountedSceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: "scene-c-frame-2.pcd",
        continuityKey: "scene-c",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange,
        onViewModeChange,
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(mockState.instances).toHaveLength(2));
    const remountedScene = mockState.instances[1];
    await waitFor(() => expect(remountedScene.applyViewState).toHaveBeenCalledWith(ACCOUNT_VIEW));
    expect(onWorkbenchLayoutChange).not.toHaveBeenCalled();
    remounted.unmount();
  });

  it("换帧保留旧点云直到新帧原子提交，且过期加载不再具备提交资格", async () => {
    const container = document.createElement("div");
    const sceneRef = { current: null };
    let pointCloudUrl = "frame-1.pcd";
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl,
        continuityKey: "scene-a",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        deferPointCloudDisplay: true,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(render.result.current.loadedPointCloudUrl).toBe("frame-1.pcd"));
    const scene = mockState.instances[0];

    let finishFrame2!: (value: {
      totalPoints: number;
      renderedPoints: number;
      decimated: boolean;
      decimateStride: number;
    }) => void;
    scene.loadPcd.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishFrame2 = resolve;
        }),
    );
    pointCloudUrl = "frame-2.pcd";
    render.rerender();

    await waitFor(() => expect(scene.loadPcd).toHaveBeenCalledTimes(2));
    expect(render.result.current.stats?.totalPoints).toBe(10);
    expect(render.result.current.loadedPointCloudUrl).toBe("frame-1.pcd");
    expect(render.result.current.isLoading).toBe(true);
    expect(scene.clearPointCloud).not.toHaveBeenCalled();
    const frame2Options = scene.loadPcd.mock.calls[1][2] as {
      shouldCommit: () => boolean;
      visible: boolean;
    };
    expect(frame2Options.shouldCommit()).toBe(true);
    expect(frame2Options.visible).toBe(false);

    pointCloudUrl = "frame-3.pcd";
    render.rerender();
    await waitFor(() => expect(scene.loadPcd).toHaveBeenCalledTimes(3));
    expect(frame2Options.shouldCommit()).toBe(false);

    finishFrame2({ totalPoints: 20, renderedPoints: 20, decimated: false, decimateStride: 1 });
    await waitFor(() => expect(render.result.current.loadedPointCloudUrl).toBe("frame-3.pcd"));
  });

  it("把会话态测量路径同步到场景，并响应隐藏或删除", async () => {
    const container = document.createElement("div");
    const sceneRef = { current: null };
    let measurementPaths: SceneMeasurementPath[] = [
      {
        id: "measurement-1",
        positions: [
          [1, 2, 3],
          [4, 6, 3],
        ],
        active: false,
      },
    ];
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: undefined,
        continuityKey: "scene-measurement",
        axisConvention: "iso_8855",
        boxes: [],
        measurementPaths,
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );

    await waitFor(() => expect(mockState.instances).toHaveLength(1));
    const scene = mockState.instances[0];
    await waitFor(() =>
      expect(scene.setMeasurementPaths).toHaveBeenLastCalledWith(measurementPaths),
    );

    measurementPaths = [];
    render.rerender();
    await waitFor(() => expect(scene.setMeasurementPaths).toHaveBeenLastCalledWith([]));
    render.unmount();
  });

  it("WebGPU device lost 后熔断当前页面并用 Legacy renderer 重建场景", async () => {
    const container = document.createElement("div");
    const sceneRef = { current: null };
    const render = renderHook(() =>
      usePointCloudScene({
        viewportRef: { current: container },
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        rendererMode: "webgpu-experimental",
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: undefined,
        continuityKey: "scene-device-lost",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(mockState.instances).toHaveLength(1));
    expect(mockState.createOptions[0]?.rendererMode).toBe("webgpu-experimental");

    act(() => mockState.createOptions[0]?.onDeviceLost?.("adapter reset"));

    await waitFor(() => expect(mockState.instances).toHaveLength(2));
    expect(mockState.instances[0].dispose).toHaveBeenCalledTimes(1);
    expect(mockState.createOptions[1]?.rendererMode).toBe("legacy");
    await waitFor(() =>
      expect(render.result.current.rendererStatus).toMatchObject({
        requestedMode: "webgpu-experimental",
        actualBackend: "legacy-webgl2",
        fallbackReason: "device-lost: adapter reset",
      }),
    );
  });

  it("settings buttons do not switch the gizmo and closing restores W/E/R", async () => {
    const viewportRef = { current: document.createElement("div") };
    const sceneRef = { current: null };
    const view = renderHook(() =>
      usePointCloudScene({
        viewportRef,
        sceneRef,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: undefined,
        continuityKey: "settings-input",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: "box",
        selectedPsrEditable: true,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(mockState.instances).toHaveLength(1));
    const scene = mockState.instances[0];
    const settings = document.createElement("button");
    settings.dataset.workbenchSettings = "";
    settings.dataset.state = "open";
    document.body.append(settings);
    act(() => {
      for (const key of ["w", "e", "r"]) {
        settings.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
      }
    });
    expect(scene.setTransformMode).not.toHaveBeenCalled();
    settings.remove();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "r" })));
    expect(scene.setTransformMode).toHaveBeenCalledWith("scale");
    view.unmount();
  });
  it("clears old-session geometry and fences a decoder even before its late result resolves", async () => {
    useAuthStore.getState().setAuth("owner-first", { id: "point-cloud-owner" } as never);
    const viewportRef = { current: document.createElement("div") };
    const sceneRef = { current: null };
    const view = renderHook(() =>
      usePointCloudScene({
        viewportRef,
        sceneRef: sceneRef as never,
        pcdDecimate: 100,
        pointSize: 0.06,
        showGrid: true,
        showAxisGizmo: true,
        cameraDamping: 0.1,
        persistCameraView: false,
        pointCloudUrl: "owned-frame.pcd",
        continuityKey: "owned-scene",
        axisConvention: "iso_8855",
        boxes: [],
        selectedId: null,
        selectedPsrEditable: false,
        pointcloudCamera: null,
        onWorkbenchLayoutChange: vi.fn(),
        onViewModeChange: vi.fn(),
        onTransformPreview: vi.fn(),
        onTransformCommit: vi.fn(),
      }),
    );
    await waitFor(() => expect(view.result.current.loadedPointCloudUrl).toBe("owned-frame.pcd"));
    const scene = mockState.instances[0];
    const oldOptions = scene.loadPcd.mock.calls[0][2] as { shouldCommit(): boolean };
    expect(oldOptions.shouldCommit()).toBe(true);
    const loads = scene.loadPcd.mock.calls.length;
    act(() => useAuthStore.getState().setToken("owner-refreshed"));
    expect(scene.clearPointCloud).not.toHaveBeenCalled();
    expect(scene.loadPcd).toHaveBeenCalledTimes(loads);
    act(() =>
      useAuthStore.getState().setAuth("owner-second-login", { id: "point-cloud-owner" } as never),
    );
    expect(scene.clearPointCloud).toHaveBeenCalled();
    expect(oldOptions.shouldCommit()).toBe(false);
    view.unmount();
  });
});
