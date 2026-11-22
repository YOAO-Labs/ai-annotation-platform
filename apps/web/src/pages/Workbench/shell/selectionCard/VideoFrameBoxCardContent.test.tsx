import { describe, expect, it, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { AnnotationResponse } from "@/types";
import { VideoFrameBoxCardContent } from "./VideoFrameBoxCardContent";

function makeVideoBbox(overrides: Partial<AnnotationResponse> = {}): AnnotationResponse {
  return {
    id: "vb-1",
    class_name: "car",
    geometry: { type: "video_bbox", frame_index: 48, x: 0.1, y: 0.1, w: 0.25, h: 0.2 },
    source: "manual",
    attributes: {},
    ...overrides,
  } as AnnotationResponse;
}

const noop = () => {};

function renderCard(props: Partial<Parameters<typeof VideoFrameBoxCardContent>[0]> = {}) {
  return render(
    <VideoFrameBoxCardContent
      annotation={makeVideoBbox()}
      imageWidth={1920}
      imageHeight={1080}
      fps={24}
      attributeSchema={undefined}
      readOnly={false}
      onSeekFrame={noop}
      onChangeClass={noop}
      onDelete={noop}
      onUpdateAttributes={noop}
      {...props}
    />,
  );
}

describe("VideoFrameBoxCardContent", () => {
  it("渲染帧定位 chip(F48 + 时间)与指标", () => {
    const { getByText } = renderCard();
    expect(getByText(/F48/)).not.toBeNull();
    expect(getByText("· 0:02")).not.toBeNull();
    expect(getByText("480×216 px")).not.toBeNull();
  });

  it("缺 fps 时只显示帧号,不显示时间", () => {
    const { getByText, queryByText } = renderCard({ fps: null });
    expect(getByText(/F48/)).not.toBeNull();
    expect(queryByText(/· \d+:\d+/)).toBeNull();
  });

  it("跳到该帧 / 改类 / 删除 回调透传", () => {
    const onSeekFrame = vi.fn();
    const onChangeClass = vi.fn();
    const onDelete = vi.fn();
    const { getByTitle } = renderCard({ onSeekFrame, onChangeClass, onDelete });
    fireEvent.click(getByTitle("跳到该帧"));
    expect(onSeekFrame).toHaveBeenCalledWith(48);
    fireEvent.click(getByTitle("修改类别"));
    expect(onChangeClass).toHaveBeenCalledWith("vb-1");
    fireEvent.click(getByTitle("删除标注"));
    expect(onDelete).toHaveBeenCalledWith("vb-1");
  });

  it("readOnly 禁用改类/删除,跳帧仍可用", () => {
    const { getByTitle } = renderCard({ readOnly: true });
    expect((getByTitle("修改类别") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTitle("删除标注") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTitle("跳到该帧") as HTMLButtonElement).disabled).toBe(false);
  });
});
