/**
 * 视频底图层(VideoKonvaMediaLayer)测试。
 *
 * 两层分工(对齐 ADR-0041 决策 C):
 *   - 纯函数 pickMediaImageSource:播放/暂停下的 image source 选取矩阵;
 *   - konva mock:断言渲染出 media-bg Layer + Konva.Image,几何 props(world 尺寸)透传。
 * 真实 canvas blit / 播放重绘回归交给 Playwright,不在此验证。
 */
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import type { VideoFramePresentation } from "./videoStageControls";

const drawing = vi.hoisted(() => {
  const listeners = new Map<string, () => void>();
  return {
    listeners,
    layer: {
      batchDraw: vi.fn(),
      isVisible: vi.fn(() => true),
      on: vi.fn((event: string, listener: () => void) => {
        listeners.set(event, listener);
      }),
      off: vi.fn((event: string, listener: () => void) => {
        if (listeners.get(event) === listener) listeners.delete(event);
      }),
    },
  };
});

vi.mock("react-konva", async () => {
  const React = await import("react");
  return {
    Layer: React.forwardRef((props: { children?: ReactNode; name?: string }, ref) => {
      React.useImperativeHandle(ref, () => drawing.layer);
      return React.createElement(
        "div",
        { "data-konva": "Layer", "data-testid": props.name },
        props.children,
      );
    }),
    Image: React.forwardRef(
      (
        props: { image?: CanvasImageSource; width?: number; height?: number; listening?: boolean },
        ref,
      ) => {
        React.useImperativeHandle(ref, () => ({ image: () => props.image }), [props.image]);
        return React.createElement("div", {
          "data-konva": "Image",
          "data-width": props.width,
          "data-height": props.height,
          "data-listening": String(props.listening),
        });
      },
    ),
  };
});

import { VideoKonvaMediaLayer, pickMediaImageSource } from "./VideoKonvaMediaLayer";

beforeEach(() => {
  drawing.listeners.clear();
  vi.clearAllMocks();
  drawing.layer.isVisible.mockReturnValue(true);
});

function completeDraw() {
  for (const callback of [...drawing.listeners.values()]) callback();
}

describe("pickMediaImageSource", () => {
  const video = { tagName: "VIDEO" } as unknown as HTMLVideoElement;
  const bitmap = { width: 1920, height: 1080 } as unknown as ImageBitmap;

  it("播放态用 <video> 实时帧(忽略 bitmap)", () => {
    expect(pickMediaImageSource(true, video, bitmap)).toBe(video);
  });

  it("暂停态优先精确 bitmap", () => {
    expect(pickMediaImageSource(false, video, bitmap)).toBe(bitmap);
  });

  it("暂停态无 bitmap → 回退 <video>", () => {
    expect(pickMediaImageSource(false, video, null)).toBe(video);
  });

  it("无任何源 → undefined(不渲染 Image)", () => {
    expect(pickMediaImageSource(false, null, null)).toBeUndefined();
    expect(pickMediaImageSource(true, null, bitmap)).toBeUndefined();
  });
});

describe("VideoKonvaMediaLayer · konva mock", () => {
  const size = { w: 1920, h: 1080 };
  const viewport = { w: 1280, h: 720 };
  const bitmap = { width: 1920, height: 1080 } as unknown as ImageBitmap;

  it("渲染 media-bg Layer + Konva.Image,world 尺寸按 size 透传", () => {
    render(
      <VideoKonvaMediaLayer
        videoEl={null}
        bitmap={bitmap}
        size={size}
        viewport={viewport}
        isPlaybackActive={false}
      />,
    );
    const layer = document.querySelector('[data-konva="Layer"]');
    expect(layer?.getAttribute("data-testid")).toBe("media-bg");
    const image = document.querySelector('[data-konva="Image"]');
    expect(image).not.toBeNull();
    expect(image!.getAttribute("data-width")).toBe("1920");
    expect(image!.getAttribute("data-height")).toBe("1080");
    // 底图层不参与 hit-test。
    expect(image!.getAttribute("data-listening")).toBe("false");
  });

  it("无可画源时只渲染空 Layer,不渲染 Image", () => {
    render(
      <VideoKonvaMediaLayer
        videoEl={null}
        bitmap={null}
        size={size}
        viewport={viewport}
        isPlaybackActive={false}
      />,
    );
    expect(document.querySelector('[data-konva="Layer"]')).not.toBeNull();
    expect(document.querySelector('[data-konva="Image"]')).toBeNull();
  });

  const presentation = (requestId: number, image = bitmap): VideoFramePresentation => ({
    requestId,
    frameIndex: 17,
    source: "webcodecs",
    image,
    isCurrent: () => true,
  });

  it("acknowledges exact source pixels only after the media layer draw completes", () => {
    const request = presentation(1);
    const onFramePresented = vi.fn();
    const onPreciseFramePainted = vi.fn();
    render(
      <VideoKonvaMediaLayer
        videoEl={null}
        bitmap={bitmap}
        frameIndex={17}
        preciseFrameIndex={17}
        framePresentation={request}
        onFramePresented={onFramePresented}
        onPreciseFramePainted={onPreciseFramePainted}
        size={size}
        viewport={viewport}
        isPlaybackActive={false}
      />,
    );
    expect(drawing.layer.batchDraw).toHaveBeenCalledOnce();
    expect(onFramePresented).not.toHaveBeenCalled();
    completeDraw();
    expect(onFramePresented).toHaveBeenCalledOnce();
    expect(onFramePresented).toHaveBeenCalledWith(request);
    expect(onPreciseFramePainted).toHaveBeenCalledWith(17);
  });

  it("a cached same-image request still requires a new draw and cancels the previous listener", () => {
    const onFramePresented = vi.fn();
    const first = presentation(1);
    const second = presentation(2);
    const props = {
      videoEl: null,
      bitmap,
      frameIndex: 17,
      onFramePresented,
      size,
      viewport,
      isPlaybackActive: false,
    };
    const { rerender } = render(<VideoKonvaMediaLayer {...props} framePresentation={first} />);
    const staleCallback = [...drawing.listeners.values()][0];
    rerender(<VideoKonvaMediaLayer {...props} framePresentation={second} />);
    expect(drawing.layer.batchDraw).toHaveBeenCalledTimes(2);
    staleCallback();
    expect(onFramePresented).not.toHaveBeenCalled();
    completeDraw();
    expect(onFramePresented).toHaveBeenCalledOnce();
    expect(onFramePresented).toHaveBeenCalledWith(second);
  });

  it("does not acknowledge a stale native source even if a queued draw finishes", () => {
    const video = document.createElement("video");
    let current = true;
    const request: VideoFramePresentation = {
      requestId: 1,
      frameIndex: 17,
      source: "video-element",
      image: video,
      isCurrent: () => current,
    };
    const onFramePresented = vi.fn();
    render(
      <VideoKonvaMediaLayer
        videoEl={video}
        bitmap={null}
        frameIndex={17}
        framePresentation={request}
        onFramePresented={onFramePresented}
        size={size}
        viewport={viewport}
        isPlaybackActive={false}
      />,
    );
    current = false;
    completeDraw();
    expect(onFramePresented).not.toHaveBeenCalled();
  });

  it("does not certify a different image or a hidden layer", () => {
    const onFramePresented = vi.fn();
    const otherBitmap = { ...bitmap } as ImageBitmap;
    const props = {
      videoEl: null,
      bitmap,
      frameIndex: 17,
      onFramePresented,
      size,
      viewport,
      isPlaybackActive: false,
    };
    const { rerender } = render(
      <VideoKonvaMediaLayer {...props} framePresentation={presentation(1, otherBitmap)} />,
    );
    completeDraw();
    expect(onFramePresented).not.toHaveBeenCalled();
    rerender(<VideoKonvaMediaLayer {...props} framePresentation={presentation(2)} />);
    drawing.layer.isVisible.mockReturnValue(false);
    completeDraw();
    expect(onFramePresented).not.toHaveBeenCalled();
  });

  it("redraws ordinary native fallback on loadeddata and seeked without inventing readiness", () => {
    const video = document.createElement("video");
    const onFramePresented = vi.fn();
    render(
      <VideoKonvaMediaLayer
        videoEl={video}
        bitmap={null}
        frameIndex={17}
        onFramePresented={onFramePresented}
        size={size}
        viewport={viewport}
        isPlaybackActive={false}
      />,
    );
    expect(drawing.layer.batchDraw).toHaveBeenCalledOnce();
    fireEvent.loadedData(video);
    fireEvent.seeked(video);
    expect(drawing.layer.batchDraw).toHaveBeenCalledTimes(3);
    completeDraw();
    expect(onFramePresented).not.toHaveBeenCalled();
  });

  it("cleans native redraw events and pending draw receipts when the media owner changes", () => {
    const first = document.createElement("video");
    const second = document.createElement("video");
    const props = { bitmap: null, frameIndex: 17, size, viewport, isPlaybackActive: false };
    const { rerender, unmount } = render(<VideoKonvaMediaLayer {...props} videoEl={first} />);
    rerender(<VideoKonvaMediaLayer {...props} videoEl={second} />);
    const draws = drawing.layer.batchDraw.mock.calls.length;
    fireEvent.seeked(first);
    expect(drawing.layer.batchDraw).toHaveBeenCalledTimes(draws);
    unmount();
    fireEvent.loadedData(second);
    expect(drawing.layer.batchDraw).toHaveBeenCalledTimes(draws);
    expect(drawing.listeners.size).toBe(0);
  });
});
