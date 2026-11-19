import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { VideoTrackGeometry, VideoTrackKeyframe } from "@/types";
import type { VideoTrackAnnotation } from "../../stage/videoStageTypes";
import { VideoTrackCardContent } from "./VideoTrackCardContent";

const box = { x: 0.1, y: 0.2, w: 0.3, h: 0.4 };

function kf(
  frame_index: number,
  source: VideoTrackKeyframe["source"] = "manual",
): VideoTrackKeyframe {
  return { frame_index, bbox: box, source };
}

function track(keyframes: VideoTrackKeyframe[]): VideoTrackAnnotation {
  const geometry: VideoTrackGeometry = {
    type: "video_track_bbox",
    track_id: "trk_1",
    keyframes,
  };
  return {
    id: "ann-1",
    task_id: "task-1",
    project_id: "project-1",
    user_id: "user-1",
    source: "manual",
    annotation_type: "video_track_bbox",
    class_name: "car",
    geometry,
    confidence: 1,
    parent_prediction_id: null,
    parent_annotation_id: null,
    lead_time: null,
    is_active: true,
    ground_truth: false,
    attributes: {},
    created_at: "2026-05-21T00:00:00Z",
    updated_at: null,
  };
}

function renderCard(overrides: Partial<React.ComponentProps<typeof VideoTrackCardContent>> = {}) {
  const selectedTrack = overrides.selectedTrack ?? track([kf(0), kf(5), kf(10, "prediction")]);
  const props: React.ComponentProps<typeof VideoTrackCardContent> = {
    selectedTrack,
    selectedTrackGhost: null,
    selectedTrackLocked: false,
    currentFrameOutside: false,
    frameIndex: 5,
    fps: null,
    imageWidth: 1920,
    imageHeight: 1080,
    readOnly: false,
    selectedTrackHidden: false,
    canCopyCurrentKeyframe: true,
    canPasteKeyframe: false,
    onMarkSelectedTrack: vi.fn(),
    onCopySelectedTrackToCurrentFrame: vi.fn(),
    onCopyCurrentKeyframe: vi.fn(),
    onPasteKeyframeToCurrentFrame: vi.fn(),
    onDeleteTrackKeyframe: vi.fn(),
    onToggleHidden: vi.fn(),
    onToggleLock: vi.fn(),
    ...overrides,
  };
  render(<VideoTrackCardContent {...props} />);
  return props;
}

describe("VideoTrackCardContent", () => {
  it("渲染轨迹整体 / 当前帧 / 关键帧 三层语义", () => {
    renderCard();
    expect(screen.getByText("轨迹整体")).toBeTruthy();
    expect(screen.getByText("当前帧")).toBeTruthy();
    // 关键帧层:导航按钮唯一标识(纯文本"关键帧"会与当前帧状态值重名)。
    expect(screen.getByTitle("上一可见关键帧")).toBeTruthy();
    expect(screen.getByTitle("下一可见关键帧")).toBeTruthy();
  });

  it("单轨 AI 入口明确为延展当前轨迹", () => {
    const onPropagateTrack = vi.fn();
    renderCard({ onPropagateTrack });
    fireEvent.click(screen.getByRole("button", { name: "延展此轨迹" }));
    expect(onPropagateTrack).toHaveBeenCalledWith(expect.objectContaining({ id: "ann-1" }));
  });

  it("关键帧表渲染全部关键帧, prediction 行带接受/拒绝", () => {
    renderCard({ onAcceptPredictionKeyframe: vi.fn(), onRejectPredictionKeyframe: vi.fn() });
    expect(screen.getAllByTestId(/video-(track|prediction)-keyframe-row/)).toHaveLength(3);
    const predRow = screen.getByTestId("video-prediction-keyframe-row");
    expect(within(predRow).getByLabelText("接受预测")).toBeTruthy();
    expect(within(predRow).getByLabelText("拒绝预测")).toBeTruthy();
  });

  it("下一关键帧按钮跳到下一可见关键帧", () => {
    const onSeekFrame = vi.fn();
    renderCard({ onSeekFrame });
    fireEvent.click(screen.getByTitle("下一可见关键帧"));
    expect(onSeekFrame).toHaveBeenCalledWith(10);
  });

  it("上一关键帧按钮跳到上一可见关键帧", () => {
    const onSeekFrame = vi.fn();
    renderCard({ onSeekFrame });
    fireEvent.click(screen.getByTitle("上一可见关键帧"));
    expect(onSeekFrame).toHaveBeenCalledWith(0);
  });

  it("标记消失把当前帧置 outside", () => {
    const onMarkSelectedTrack = vi.fn();
    renderCard({ onMarkSelectedTrack });
    fireEvent.click(screen.getByTitle("标记当前帧消失"));
    expect(onMarkSelectedTrack).toHaveBeenCalledWith({ outside: true, occluded: false });
  });

  it("接受预测关键帧回调带轨迹与帧号", () => {
    const onAcceptPredictionKeyframe = vi.fn();
    renderCard({ onAcceptPredictionKeyframe });
    const predRow = screen.getByTestId("video-prediction-keyframe-row");
    fireEvent.click(within(predRow).getByLabelText("接受预测"));
    expect(onAcceptPredictionKeyframe).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ann-1" }),
      10,
    );
  });

  it("底部操作栏含显隐 / 锁定 / 改类 / 删除, 点击隐藏与删除回调", () => {
    const onToggleHidden = vi.fn();
    const onDeleteTrack = vi.fn();
    renderCard({ onToggleHidden, onChangeClass: vi.fn(), onDeleteTrack });
    expect(screen.getByLabelText("锁定轨迹")).toBeTruthy();
    expect(screen.getByLabelText("修改类别")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("隐藏轨迹"));
    expect(onToggleHidden).toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("删除整条轨迹"));
    expect(onDeleteTrack).toHaveBeenCalled();
  });

  // 转框族锁语义: 锁定轨迹时「转独立框」全入口 (下拉触发 + 关键帧表行) 应禁用
  // —— 转框会删/改源轨迹, 锁定即冻结。回归此前浮卡入口只看 readOnly、绕过轨迹锁的 bug。
  it("锁定轨迹时「转换为独立框」下拉触发钮禁用", () => {
    renderCard({ selectedTrackLocked: true, onConvertToBboxes: vi.fn() });
    expect((screen.getByLabelText("转换为独立框") as HTMLButtonElement).disabled).toBe(true);
  });

  it("锁定轨迹时关键帧表行「转独立框」/「复制为独立框」按钮禁用", () => {
    renderCard({ selectedTrackLocked: true, onConvertToBboxes: vi.fn() });
    const convert = screen.getAllByLabelText("此关键帧转独立框");
    const copy = screen.getAllByLabelText("复制此关键帧为独立框");
    expect(convert.length).toBeGreaterThan(0);
    convert.forEach((b) => expect((b as HTMLButtonElement).disabled).toBe(true));
    copy.forEach((b) => expect((b as HTMLButtonElement).disabled).toBe(true));
  });

  it("未锁定轨迹时关键帧表行「转独立框」按钮可用", () => {
    renderCard({ selectedTrackLocked: false, onConvertToBboxes: vi.fn() });
    const convert = screen.getAllByLabelText("此关键帧转独立框");
    expect(convert.length).toBeGreaterThan(0);
    convert.forEach((b) => expect((b as HTMLButtonElement).disabled).toBe(false));
  });

  it("语义标签失焦提交去抖后的值", () => {
    const onUpdateSemanticLabel = vi.fn();
    renderCard({ onUpdateSemanticLabel });
    const input = screen.getByTestId("video-track-semantic-label-input");
    fireEvent.change(input, { target: { value: "car_3" } });
    fireEvent.blur(input);
    expect(onUpdateSemanticLabel).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ann-1" }),
      "car_3",
    );
  });
});
