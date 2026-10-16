import { render, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BoxListItem } from "./BoxListItem";
import type { Annotation } from "@/types";
import type { AiBox } from "../state/transforms";

const base: Annotation = {
  id: "a1",
  cls: "car",
  conf: 1,
  source: "manual",
  x: 0.1,
  y: 0.2,
  w: 0.3,
  h: 0.4,
};

describe("BoxListItem", () => {
  it("pins row actions without hover and retires them when the row owner changes", () => {
    const onSelect = vi.fn();
    const onAccept = vi.fn();
    const props = {
      selected: false,
      imageWidth: 1000,
      imageHeight: 500,
      onSelect,
      onAccept,
      isAi: true,
    };
    const { getByLabelText, rerender } = render(<BoxListItem {...props} b={base} />);
    fireEvent.click(getByLabelText("更多操作"));
    expect(getByLabelText("更多操作")).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(getByLabelText("采纳预测"));
    expect(onAccept).toHaveBeenCalledOnce();
    expect(onSelect).not.toHaveBeenCalled();
    rerender(<BoxListItem {...props} b={{ ...base, id: "another-prediction" }} />);
    expect(getByLabelText("更多操作")).toHaveAttribute("aria-expanded", "false");
  });
  it("shows bbox tool metadata", () => {
    const b: Annotation = {
      ...base,
      annotation_type: "bbox",
      geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
    };
    const { getByText } = render(
      <BoxListItem b={b} selected={false} imageWidth={1000} imageHeight={500} onSelect={vi.fn()} />,
    );

    expect(getByText("矩形框")).toBeInTheDocument();
    expect(getByText("(100, 100) · 300×200")).toBeInTheDocument();
  });

  it("v0.10.9 · 渲染 onRefine 按钮并触发回调（AI 行）", () => {
    const b: Annotation = {
      ...base,
      annotation_type: "polygon",
      geometry: {
        type: "polygon",
        points: [
          [0, 0],
          [1, 0],
          [0.5, 1],
        ],
      },
    };
    const onRefine = vi.fn();
    const { getByLabelText } = render(
      <BoxListItem
        b={b}
        isAi
        selected={false}
        imageWidth={1000}
        imageHeight={500}
        onSelect={vi.fn()}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onRefine={onRefine}
      />,
    );
    fireEvent.click(getByLabelText("精修"));
    expect(onRefine).toHaveBeenCalledOnce();
  });

  it("AI 行展示预测来源", () => {
    const b: AiBox = {
      ...base,
      id: "pred-imported",
      predictionId: "p-imported",
      shapeIndex: 0,
      predictionSource: "external_import",
      annotation_type: "bbox",
      geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      conf: 0.91,
      source: "prediction_based",
    };
    const { getByText } = render(
      <BoxListItem
        b={b}
        isAi
        selected={false}
        imageWidth={1000}
        imageHeight={500}
        onSelect={vi.fn()}
      />,
    );

    expect(getByText("导入 · 91%")).toBeInTheDocument();
  });

  it("marks orphan user annotations", () => {
    const { getByText } = render(
      <BoxListItem
        b={base}
        selected={false}
        orphan
        imageWidth={1000}
        imageHeight={500}
        onSelect={vi.fn()}
      />,
    );

    expect(getByText("已删除")).toBeInTheDocument();
  });

  it("v0.10.9 · 渲染 onRefine 按钮并触发回调（user polygon 行）", () => {
    const b: Annotation = {
      ...base,
      annotation_type: "polygon",
      geometry: {
        type: "polygon",
        points: [
          [0, 0],
          [1, 0],
          [0.5, 1],
        ],
      },
    };
    const onRefine = vi.fn();
    const { getByLabelText } = render(
      <BoxListItem
        b={b}
        selected={false}
        imageWidth={1000}
        imageHeight={500}
        onSelect={vi.fn()}
        onRefine={onRefine}
      />,
    );
    fireEvent.click(getByLabelText("精修"));
    expect(onRefine).toHaveBeenCalledOnce();
  });

  it("shows track-specific metadata", () => {
    const b: Annotation = {
      ...base,
      annotation_type: "video_track_bbox",
      geometry: {
        type: "video_track_bbox",
        track_id: "trk_abcdefgh12345678",
        keyframes: [
          { frame_index: 0, bbox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 }, source: "manual" },
          {
            frame_index: 10,
            bbox: { x: 0.3, y: 0.2, w: 0.3, h: 0.4 },
            source: "manual",
            occluded: true,
          },
          { frame_index: 12, bbox: { x: 0.3, y: 0.2, w: 0.3, h: 0.4 }, source: "manual" },
        ],
        outside: [{ from: 12, to: 12 }],
      },
    };
    const { getByText } = render(
      <BoxListItem b={b} selected={false} imageWidth={1000} imageHeight={500} onSelect={vi.fn()} />,
    );

    expect(getByText("轨迹")).toBeInTheDocument();
    expect(getByText(/3 关键帧/)).toHaveTextContent("F0-F12");
    expect(getByText(/3 关键帧/)).toHaveTextContent("1 消失");
    expect(getByText(/3 关键帧/)).toHaveTextContent("1 遮挡");
  });
});
