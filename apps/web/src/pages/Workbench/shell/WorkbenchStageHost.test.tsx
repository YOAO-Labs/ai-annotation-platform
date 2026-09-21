// v0.10.18 · WorkbenchStageHost focused render tests.
// 验证按 stageKind 分发到唯一对应的舞台组件 (image / video / 3d).
// 不验证舞台组件内部行为 — 那些有各自单测 (VideoStage / ImageWorkbench).

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createRef, forwardRef } from "react";

const threeDWorkbenchMock = vi.hoisted(() => vi.fn());
const videoWorkbenchMock = vi.hoisted(() => vi.fn());
const imageWorkbenchMock = vi.hoisted(() => vi.fn());

vi.mock("../stages/image/ImageWorkbench", () => ({
  ImageWorkbench: (props: Record<string, unknown>) => {
    imageWorkbenchMock(props);
    return <div data-testid="image-workbench" />;
  },
}));
vi.mock("../stages/video/VideoWorkbench", () => ({
  VideoWorkbench: forwardRef(function VideoWorkbench(props: { overlays?: React.ReactNode }, _ref) {
    videoWorkbenchMock(props);
    return <div data-testid="video-workbench">{props.overlays}</div>;
  }),
}));
vi.mock("../stages/three-d/ThreeDWorkbench", () => ({
  default: (props: { selectedIds?: string[]; petDock?: unknown }) => {
    threeDWorkbenchMock(props);
    return <div data-testid="three-d-workbench" />;
  },
}));

import { WorkbenchStageHost } from "./WorkbenchStageHost";

// 全字段 mock; 大多数 prop 仅 image/video 各自子组件消费, 这里只关心 stageKind 分发.
// 子组件被 vi.mock 替换为空 div 后, 实际不消费任何 prop;
// 用 `as unknown as Parameters<typeof WorkbenchStageHost>[0]` 绕开 70+ 字段的精确 mock.
type StageHostProps = React.ComponentProps<typeof WorkbenchStageHost>;
const baseProps = {
  common: {
    overlays: <div data-testid="overlays-content">overlays</div>,
    readOnly: false,
    activeClass: "person",
    selectedId: null,
    selectedIds: [],
    annotations: [],
    pendingDrawing: null,
    onSelectBox: vi.fn(),
    onCursorMove: vi.fn(),
    onDeleteUserBox: vi.fn(),
    onChangeUserBoxClass: vi.fn(),
    fitTick: 0,
    threeDTool: "select",
    onSetThreeDTool: vi.fn(),
    onNavigateSceneFrame: vi.fn(),
    rightSidebarOpen: true,
    rightSidebarWidth: 280,
    workbenchLayout: {
      leftOpen: true,
      rightOpen: true,
      floatingTaskQueue: { detached: false, x: null, y: null, w: null, h: null },
      floatingClassPalette: { detached: false, x: null, y: null, w: null, h: null },
      floatingInspector: { detached: false, x: null, y: null, w: null, h: null },
      floatingDiscussion: { detached: false, x: null, y: null, w: null, h: null },
      triViewFloat: { collapsed: false, x: null, y: null, w: null, h: null },
      cameraPanels: {},
      pointcloudCamera: null,
    },
    onWorkbenchLayoutChange: vi.fn(),
    workbenchCommon: {
      longTaskSampleRate: 0.05,
      crossFrameOverlayEnabled: false,
      crossFrameOverlayK: 1,
      crossFrameOverlayScope: "selected",
      confirmDelete: "never",
      recentClassesLimit: 5,
      performanceTier: "standard",
    },
    workbenchVideo: { defaultPlaybackRate: 1, largeFrameStep: 10 },
    workbenchPointcloud: {
      displayStyle: "points",
      pointSize: 0.06,
      persistCameraView: false,
      colorizeWithCamera: false,
      colorizeContrast: 1,
      colorizeBrightness: 0,
      colorizeGamma: 1,
      showDepthHint: false,
      pointMaskSelectMode: "rect",
      showGrid: true,
      showAxisGizmo: true,
      cameraDamping: 0.1,
      neighborPointOverlay: false,
      neighborPointOverlayK: 1,
    },
    workbenchConfigLoaded: true,
    onWorkbenchConfigChange: vi.fn(),
    onWorkbenchConfigUpdate: vi.fn(),
  },
  video: {
    videoManifest: undefined,
    videoTool: "box",
    videoFrameIndex: 0,
    hiddenVideoTrackIds: new Set<string>(),
    lockedVideoTrackIds: new Set<string>(),
    onVideoFrameIndexChange: vi.fn(),
    onVideoCreate: vi.fn(),
    onVideoPendingDraw: vi.fn(),
    onVideoUpdate: vi.fn(),
    onVideoRename: vi.fn(),
    onVideoConvertToBboxes: vi.fn(),
  },
  image: {
    fileUrl: null,
    thumbnailUrl: null,
    tool: "box",
    fadedAiIds: new Set<string>(),
    nudgeMap: new Map(),
    userBoxes: [],
    aiBoxes: [],
    spacePan: false,
    vp: { scale: 1, x: 0, y: 0 },
    setVp: vi.fn(),
    fitTick: 0,
    setFitTick: vi.fn(),
    onAcceptPrediction: vi.fn(),
    onRejectPrediction: vi.fn(),
    onCommitDrawing: vi.fn(),
    onSamPrompt: vi.fn(),
    onCommitMove: vi.fn(),
    onCommitResize: vi.fn(),
    onCommitPolygonGeometry: vi.fn(),
    onStageGeometry: vi.fn(),
  },
  ai: {
    samCandidates: [],
    samActiveIdx: -1,
    samSessionPoints: [],
    samSessionExemplars: [],
    samSubTool: null,
    samPolarity: "fg",
  },
  editors: {
    canvasShapes: [],
    canvasEditable: false,
    canvasStroke: "#fff",
    onCanvasStrokeCommit: vi.fn(),
    canUndo: false,
    canRedo: false,
    onUndo: vi.fn(),
    onRedo: vi.fn(),
    onSetCanvasStroke: vi.fn(),
    canvasShapeCount: 0,
    onUndoCanvasShape: vi.fn(),
    onClearCanvasShapes: vi.fn(),
    onCancelCanvasDraft: vi.fn(),
    onDoneCanvasDraft: vi.fn(),
    stageGeom: { imgW: 0, imgH: 0, vpSize: { w: 0, h: 0 } },
  },
};

function propsFor(stageKind: "image" | "video" | "3d"): StageHostProps {
  return {
    ...baseProps,
    common: { ...baseProps.common, stageKind },
  } as unknown as StageHostProps;
}

describe("WorkbenchStageHost", () => {
  it("stageKind=image: renders ImageWorkbench, not Video / 3d", () => {
    const props = propsFor("image");
    const annotationCommentCounts = { a1: 3 };
    const onOpenAnnotationComments = vi.fn();
    props.editors!.annotationCommentCounts = annotationCommentCounts;
    props.editors!.onOpenAnnotationComments = onOpenAnnotationComments;
    render(<WorkbenchStageHost ref={createRef()} {...props} />);

    expect(screen.getByTestId("image-workbench")).toBeTruthy();
    expect(screen.queryByTestId("video-workbench")).toBeNull();
    expect(screen.queryByTestId("three-d-workbench")).toBeNull();
    expect(imageWorkbenchMock).toHaveBeenCalledWith(
      expect.objectContaining({ annotationCommentCounts, onOpenAnnotationComments }),
    );
  });

  it("stageKind=video: forwards overlays to the video viewport", () => {
    const props = propsFor("video");
    const annotationCommentCounts = { track: 3 };
    const onOpenAnnotationComments = vi.fn();
    props.editors!.annotationCommentCounts = annotationCommentCounts;
    props.editors!.onOpenAnnotationComments = onOpenAnnotationComments;
    props.editors!.issuePinDropArmed = true;
    props.editors!.issueNavigationPending = true;
    props.editors!.onIssuePinDrop = vi.fn();
    props.editors!.onSeekIssueFrame = vi.fn();
    props.video!.keypointSchema = {
      nodes: [{ name: "nose", color: "#fff", x: 0.5, y: 0.5 }],
      edges: [],
    };
    render(<WorkbenchStageHost ref={createRef()} {...props} />);

    expect(screen.getByTestId("video-workbench")).toBeTruthy();
    expect(screen.queryByTestId("image-workbench")).toBeNull();
    expect(screen.queryByTestId("three-d-workbench")).toBeNull();
    expect(screen.getByTestId("overlays-content")).toBeTruthy();
    expect(
      screen.getByTestId("video-workbench").contains(screen.getByTestId("overlays-content")),
    ).toBe(true);
    expect(videoWorkbenchMock).toHaveBeenCalledWith(
      expect.objectContaining({
        keypointSchema: props.video!.keypointSchema,
        issuePinDropArmed: true,
        issueNavigationPending: true,
        onIssuePinDrop: props.editors!.onIssuePinDrop,
        onSeekIssueFrame: props.editors!.onSeekIssueFrame,
        annotationCommentCounts,
        onOpenAnnotationComments,
      }),
    );
  });

  it("stageKind=3d: renders ThreeDWorkbench only (lazy)", async () => {
    const props = propsFor("3d");
    const onNavigateSceneFrame = vi.fn();
    props.common.selectedIds = ["a1", "a2"];
    props.common.onNavigateSceneFrame = onNavigateSceneFrame;
    render(<WorkbenchStageHost ref={createRef()} {...props} />);

    // lazy + Suspense: 组件异步解析,用 findBy 等待。
    expect(await screen.findByTestId("three-d-workbench")).toBeTruthy();
    expect(screen.queryByTestId("image-workbench")).toBeNull();
    expect(screen.queryByTestId("video-workbench")).toBeNull();
    expect(threeDWorkbenchMock).toHaveBeenCalledWith(
      expect.objectContaining({ selectedIds: ["a1", "a2"], onNavigateSceneFrame }),
    );
  });

  it("forwards the shared pet dock to the 3d workbench", async () => {
    const petDock = {
      enabled: true,
      position: { x: 480, y: 620 },
      onPositionChange: vi.fn(),
    };

    render(<WorkbenchStageHost ref={createRef()} {...propsFor("3d")} petDock={petDock} />);

    expect(await screen.findByTestId("three-d-workbench")).toBeTruthy();
    expect(threeDWorkbenchMock).toHaveBeenLastCalledWith(expect.objectContaining({ petDock }));
  });
});
