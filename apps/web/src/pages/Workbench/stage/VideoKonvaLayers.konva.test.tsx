import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { VideoKonvaTrackShape } from "./VideoKonvaTrackShape";
import { VideoKonvaTracksLayer } from "./VideoKonvaTracksLayer";
import { VideoKonvaOverlayLayer } from "./VideoKonvaOverlayLayer";
import { VideoKonvaIssueLayer } from "./VideoKonvaIssueLayer";
import { DEFAULT_ANNOTATION_VISUAL } from "./annotationVisual";
import type { VideoEntryView, VideoTrackPreviewView } from "./videoFrameViews";
import type { AnnotationFeedback } from "@/api/feedbacks";

const size = { w: 1000, h: 500 };

function entry(over: Partial<VideoEntryView> = {}): VideoEntryView {
  return {
    key: "e1",
    id: "a1",
    geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
    color: "#ff0000",
    selected: false,
    dashed: false,
    occluded: false,
    predicted: false,
    labelText: "car",
    className: "car",
    ...over,
  };
}

describe("VideoKonvaTrackShape", () => {
  it("像素空间几何 + 线宽 /scale + 填充 rgba", () => {
    render(
      <VideoKonvaTrackShape
        geom={{ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }}
        color="#ff0000"
        dashed={false}
        selected={false}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    const rect = document.querySelector('[data-konva="Rect"]')!;
    expect(rect.getAttribute("data-x")).toBe("100");
    expect(rect.getAttribute("data-width")).toBe("300");
    expect(rect.getAttribute("data-y")).toBe("100");
    expect(rect.getAttribute("data-height")).toBe("200");
    expect(rect.getAttribute("data-strokewidth")).toBe("1.5");
    expect(rect.getAttribute("data-fill")).toMatch(/^rgba\(/);
    expect(rect.hasAttribute("data-dash")).toBe(false);
  });

  it("选中加粗 + 缩放 /scale", () => {
    render(
      <VideoKonvaTrackShape
        geom={{ x: 0, y: 0, w: 0.2, h: 0.2 }}
        color="#ff0000"
        dashed={false}
        selected
        size={size}
        scale={2}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-strokewidth")).toBe(
      "1",
    );
  });

  it("dashed(插值/遮挡)→ dash 数组 /scale", () => {
    render(
      <VideoKonvaTrackShape
        geom={{ x: 0, y: 0, w: 0.2, h: 0.2 }}
        color="#ff0000"
        dashed
        selected={false}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-dash")).toBe(
      JSON.stringify([6, 4]),
    );
  });

  it("points 存在 → 画 <Line closed> 而非 <Rect>, 像素空间顶点", () => {
    render(
      <VideoKonvaTrackShape
        geom={{ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }}
        points={[
          [0.1, 0.2],
          [0.4, 0.2],
          [0.4, 0.6],
        ]}
        color="#ff0000"
        dashed={false}
        selected={false}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Rect"]')).toBeNull();
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-points")).toBe(JSON.stringify([100, 100, 400, 100, 400, 300]));
    expect(line.getAttribute("data-closed")).toBe("true");
  });

  it("open (polyline) → <Line closed=false>, 无填充, 2 点即可", () => {
    render(
      <VideoKonvaTrackShape
        geom={{ x: 0.1, y: 0.2, w: 0.3, h: 0 }}
        points={[
          [0.1, 0.2],
          [0.4, 0.2],
        ]}
        open
        color="#ff0000"
        dashed={false}
        selected={false}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-closed")).toBe("false");
    expect(line.getAttribute("data-points")).toBe(JSON.stringify([100, 100, 400, 100]));
    expect(line.hasAttribute("data-fill")).toBe(false);
  });

  it("points < 3 → 回退 <Rect>", () => {
    render(
      <VideoKonvaTrackShape
        geom={{ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }}
        points={[
          [0.1, 0.2],
          [0.4, 0.2],
        ]}
        color="#ff0000"
        dashed={false}
        selected={false}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Rect"]')).not.toBeNull();
  });
});

describe("VideoKonvaTracksLayer", () => {
  const preview = (selected: boolean): VideoTrackPreviewView => ({
    key: "trk1",
    id: "trk1",
    color: "#00ff00",
    selected,
    points: [
      { frame: 0, x: 0.1, y: 0.1, occluded: false },
      { frame: 10, x: 0.5, y: 0.5, occluded: false },
    ],
  });

  it("未选中:画预览线、无关键帧圆点;选中:出圆点", () => {
    const { rerender } = render(
      <VideoKonvaTracksLayer
        entries={[entry()]}
        previews={[preview(false)]}
        ghost={null}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Layer"]')!.getAttribute("data-testid")).toBe(
      "tracks",
    );
    expect(document.querySelectorAll('[data-konva="Line"]').length).toBe(1);
    expect(document.querySelectorAll('[data-konva="Circle"]').length).toBe(0);
    expect(document.querySelectorAll('[data-konva="Rect"]').length).toBe(1);

    rerender(
      <VideoKonvaTracksLayer
        entries={[entry({ selected: true })]}
        previews={[preview(true)]}
        ghost={null}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelectorAll('[data-konva="Circle"]').length).toBe(2);
  });

  it("预览线点 = 归一化中心 × size(展平)", () => {
    render(
      <VideoKonvaTracksLayer
        entries={[]}
        previews={[preview(false)]}
        ghost={null}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-points")).toBe(JSON.stringify([100, 50, 500, 250]));
  });

  it("ghost → 虚线参考框", () => {
    render(
      <VideoKonvaTracksLayer
        entries={[]}
        previews={[]}
        ghost={{
          id: "g",
          geom: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
          color: "#0000ff",
          labelText: "x",
        }}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    const ghost = document.querySelector('[data-testid="video-track-ghost"]')!;
    expect(ghost).not.toBeNull();
    expect(ghost.getAttribute("data-dash")).toBe(JSON.stringify([6, 4]));
  });
});

describe("VideoKonvaOverlayLayer", () => {
  it("pending draft → 虚线框;标签 → Label + Text 文本", () => {
    render(
      <VideoKonvaOverlayLayer
        pendingDraft={{ geom: { x: 0.2, y: 0.2, w: 0.1, h: 0.1 }, className: "person" }}
        labels={[
          {
            key: "l1",
            geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
            color: "#ff0000",
            text: "car · 插值",
          },
        ]}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Layer"]')!.getAttribute("data-testid")).toBe(
      "overlay",
    );
    const draft = document.querySelector('[data-testid="video-pending-draft"]')!;
    expect(draft.getAttribute("data-dash")).toBe(JSON.stringify([6, 4]));
    expect(document.querySelector('[data-konva="Label"]')).not.toBeNull();
    const text = document.querySelector('[data-konva="Text"]')!;
    expect(text.getAttribute("data-text")).toBe("car · 插值");
    expect(text.getAttribute("data-fontsize")).toBe("12");
  });

  it("无 draft 无标签 → 空 overlay 层", () => {
    render(
      <VideoKonvaOverlayLayer
        pendingDraft={null}
        labels={[]}
        size={size}
        scale={1}
        visual={DEFAULT_ANNOTATION_VISUAL}
      />,
    );
    expect(document.querySelector('[data-konva="Rect"]')).toBeNull();
    expect(document.querySelector('[data-konva="Label"]')).toBeNull();
  });
});

describe("VideoKonvaIssueLayer", () => {
  const issue = (id: string, frame: number): AnnotationFeedback =>
    ({
      id,
      kind: "issue",
      anchor_type: "pixel",
      status: "open",
      anchor_position: { frame, x: 0.5, y: 0.5 },
    }) as unknown as AnnotationFeedback;

  it("只渲染当前帧的图钉", () => {
    render(
      <VideoKonvaIssueLayer
        pixelIssues={[issue("i1", 0), issue("i2", 5)]}
        frameIndex={0}
        size={size}
        scale={1}
      />,
    );
    expect(document.querySelector('[data-testid="video-issue-pin-i1"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="video-issue-pin-i2"]')).toBeNull();
  });

  it("无当前帧图钉 → 不渲染层", () => {
    render(
      <VideoKonvaIssueLayer pixelIssues={[issue("i1", 9)]} frameIndex={0} size={size} scale={1} />,
    );
    expect(document.querySelector('[data-konva="Layer"]')).toBeNull();
  });

  it("3D 质量锚点没有 x/y 时不进入二维图钉层", () => {
    const pointCloudIssue = {
      ...issue("quality-1", 0),
      anchor_type: "point_cloud",
      anchor_position: { frame: 0, point_cloud_quality_issue_id: "quality-1" },
    } as unknown as AnnotationFeedback;
    render(
      <VideoKonvaIssueLayer pixelIssues={[pointCloudIssue]} frameIndex={0} size={size} scale={1} />,
    );
    expect(document.querySelector('[data-konva="Layer"]')).toBeNull();
  });

  it("keeps pin radius and symbol screen-constant across zoom", () => {
    const view = render(
      <VideoKonvaIssueLayer
        pixelIssues={[{ ...issue("info", 0), severity: "info" }]}
        frameIndex={0}
        size={size}
        scale={1}
        highlightId="info"
      />,
    );
    expect(document.querySelector('[data-testid="video-issue-pin-info"]')).toHaveAttribute(
      "data-radius",
      "8",
    );
    expect(document.querySelector('[data-testid="video-issue-pin-ring-info"]')).toHaveAttribute(
      "data-radius",
      "11",
    );
    expect(document.querySelector('[data-konva="Text"]')).toHaveAttribute("data-text", "i");
    view.rerender(
      <VideoKonvaIssueLayer
        pixelIssues={[{ ...issue("info", 0), severity: "info" }]}
        frameIndex={0}
        size={size}
        scale={2}
        highlightId="info"
      />,
    );
    expect(document.querySelector('[data-testid="video-issue-pin-info"]')).toHaveAttribute(
      "data-radius",
      "4",
    );
    expect(document.querySelector('[data-konva="Text"]')).toHaveAttribute("data-fontsize", "5");
  });

  it("centers every pin symbol in a fixed box at zoom", () => {
    const symbols = [
      { id: "warn", symbol: "!", status: "open", severity: "warn", x: 0.1, y: 0.2 },
      { id: "info", symbol: "i", status: "open", severity: "info", x: 0.2, y: 0.3 },
      { id: "blocker", symbol: "×", status: "open", severity: "blocker", x: 0.3, y: 0.4 },
      { id: "resolved", symbol: "✓", status: "resolved", severity: "warn", x: 0.4, y: 0.5 },
      { id: "wont-fix", symbol: "–", status: "wont_fix", severity: "warn", x: 0.5, y: 0.6 },
    ] as const;
    render(
      <VideoKonvaIssueLayer
        pixelIssues={symbols.map(({ id, status, severity, x, y }) => ({
          ...issue(id, 0),
          status,
          severity,
          anchor_position: { frame: 0, x, y },
        }))}
        frameIndex={0}
        size={size}
        scale={2}
      />,
    );

    symbols.forEach(({ symbol, x, y }, index) => {
      const text = document.querySelectorAll('[data-konva="Text"]')[index];
      expect(text).toHaveAttribute("data-text", symbol);
      expect(text).toHaveAttribute("data-x", String(x * size.w - 4));
      expect(text).toHaveAttribute("data-y", String(y * size.h - 4));
      expect(text).toHaveAttribute("data-width", "8");
      expect(text).toHaveAttribute("data-height", "8");
      expect(text).toHaveAttribute("data-align", "center");
      expect(text).toHaveAttribute("data-verticalalign", "middle");
      expect(text).not.toHaveAttribute("data-offsetx");
      expect(text).not.toHaveAttribute("data-offsety");
    });
  });
});
