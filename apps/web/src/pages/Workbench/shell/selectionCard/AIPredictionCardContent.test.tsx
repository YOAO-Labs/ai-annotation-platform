// v0.16.14 · AIPredictionCardContent 单测:
// - 置信度条 + 来源/候选序号渲染
// - 采纳 / 忽略 回调透传整个 box
// - 精修按钮仅 polygon 几何渲染;readOnly 禁用动作

import { describe, expect, it, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { AiBox } from "../../state/transforms";
import { AIPredictionCardContent } from "./AIPredictionCardContent";

function makeAiBox(overrides: Partial<AiBox> = {}): AiBox {
  return {
    id: "pred-p1-2",
    cls: "car",
    conf: 0.82,
    x: 0.1,
    y: 0.1,
    w: 0.25,
    h: 0.2,
    source: "prediction_based",
    predictionId: "p1",
    shapeIndex: 2,
    predictionSource: "ml_backend",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.25, h: 0.2 },
    ...overrides,
  } as AiBox;
}

const noop = () => {};

describe("AIPredictionCardContent", () => {
  it("distinguishes unavailable content, loading and retryable errors for the original candidate", () => {
    const retry = vi.fn();
    const box = makeAiBox({
      geometry: {
        type: "raster_mask",
        mask: {
          encoding: "coco_rle_ref",
          size: [3, 3],
          sha256: "a".repeat(64),
          object_key: "mask.json",
          runs: 4,
          bytes: 20,
        },
      },
    });
    const props = {
      box,
      imageWidth: 3,
      imageHeight: 3,
      readOnly: false,
      onAccept: noop,
      onReject: noop,
      onRefine: noop,
      onRetryRasterMask: retry,
    };
    const view = render(<AIPredictionCardContent {...props} />);
    expect(view.getByRole("status")).toHaveTextContent("尚未加载");
    expect(view.queryByText("Mask 内容加载中…")).toBeNull();
    view.rerender(<AIPredictionCardContent {...props} rasterMaskStatus={{ state: "loading" }} />);
    expect(view.getByRole("status")).toHaveTextContent("加载中");
    view.rerender(
      <AIPredictionCardContent
        {...props}
        rasterMaskStatus={{
          state: "error",
          message: "像素读取失败",
          retryable: true,
          reason: "load_failed",
          backendReason: "mask_storage_unavailable",
        }}
      />,
    );
    expect(view.getByRole("status")).toHaveTextContent("像素读取失败");
    fireEvent.click(view.getByText("重试加载"));
    expect(retry).toHaveBeenCalledOnce();
    expect(retry).toHaveBeenCalledWith("pred-p1-2");
  });

  it("渲染置信度 + 来源 + 候选序号", () => {
    const { getByText } = render(
      <AIPredictionCardContent
        box={makeAiBox()}
        imageWidth={1920}
        imageHeight={1080}
        readOnly={false}
        onAccept={noop}
        onReject={noop}
        onRefine={noop}
      />,
    );
    expect(getByText("82%")).not.toBeNull();
    expect(getByText("模型")).not.toBeNull(); // predictionSourceLabel(ml_backend)
    expect(getByText("第 3 个候选")).not.toBeNull(); // shapeIndex 2 → 第 3
  });

  it("采纳 / 忽略 回调透传整个 box", () => {
    const onAccept = vi.fn();
    const onReject = vi.fn();
    const box = makeAiBox();
    const { getByTitle } = render(
      <AIPredictionCardContent
        box={box}
        imageWidth={1920}
        imageHeight={1080}
        readOnly={false}
        onAccept={onAccept}
        onReject={onReject}
        onRefine={noop}
      />,
    );
    fireEvent.click(getByTitle("采纳预测"));
    expect(onAccept).toHaveBeenCalledWith(box);
    fireEvent.click(getByTitle("忽略预测"));
    expect(onReject).toHaveBeenCalledWith(box);
  });

  it("bbox 不渲染精修按钮", () => {
    const { queryByTitle } = render(
      <AIPredictionCardContent
        box={makeAiBox()}
        imageWidth={1920}
        imageHeight={1080}
        readOnly={false}
        onAccept={noop}
        onReject={noop}
        onRefine={noop}
      />,
    );
    expect(queryByTitle("精修(Mask 笔刷)")).toBeNull();
  });

  it("polygon 渲染精修按钮并透传 box", () => {
    const onRefine = vi.fn();
    const box = makeAiBox({
      geometry: {
        type: "polygon",
        points: [
          [0, 0],
          [0.5, 0],
          [0.5, 0.5],
        ],
      },
    });
    const { getByTitle } = render(
      <AIPredictionCardContent
        box={box}
        imageWidth={1920}
        imageHeight={1080}
        readOnly={false}
        onAccept={noop}
        onReject={noop}
        onRefine={onRefine}
      />,
    );
    fireEvent.click(getByTitle("精修(Mask 笔刷)"));
    expect(onRefine).toHaveBeenCalledWith(box);
  });

  it("readOnly 时禁用采纳 / 忽略", () => {
    const { getByTitle } = render(
      <AIPredictionCardContent
        box={makeAiBox()}
        imageWidth={1920}
        imageHeight={1080}
        readOnly
        onAccept={noop}
        onReject={noop}
        onRefine={noop}
      />,
    );
    expect((getByTitle("采纳预测") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTitle("忽略预测") as HTMLButtonElement).disabled).toBe(true);
  });
});
