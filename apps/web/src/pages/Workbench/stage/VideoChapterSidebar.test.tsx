import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { VideoChapter } from "@/api/videoChapters";

const chaptersRef: { current: VideoChapter[] } = { current: [] };

vi.mock("@/hooks/useVideoChapters", () => ({
  useVideoChapters: () => ({ data: chaptersRef.current, isLoading: false }),
  useCreateVideoChapter: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateVideoChapter: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteVideoChapter: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import { VideoChapterSidebar } from "./VideoChapterSidebar";

function chapter(overrides: Partial<VideoChapter>): VideoChapter {
  return {
    id: overrides.id ?? "c1",
    dataset_item_id: "item-1",
    start_frame: 0,
    end_frame: 10,
    title: "章节",
    color: null,
    metadata: {},
    frame_step: null,
    source: "manual",
    created_by: null,
    created_at: "2026-05-21T00:00:00Z",
    updated_at: null,
    ...overrides,
  };
}

function renderSidebar() {
  return render(
    <VideoChapterSidebar datasetItemId="item-1" frameIndex={0} maxFrame={100} canEdit={false} />,
  );
}

describe("VideoChapterSidebar frame_step / source display", () => {
  it("shows a sampled badge only for sampled chapters", () => {
    chaptersRef.current = [
      chapter({ id: "manual", title: "手动章", source: "manual" }),
      chapter({
        id: "sampled",
        title: "采样章",
        source: "sampled",
        start_frame: 20,
        end_frame: 30,
      }),
    ];
    const { getAllByTestId, queryAllByText } = renderSidebar();
    expect(getAllByTestId("video-chapter-row")).toHaveLength(2);
    expect(queryAllByText("采样")).toHaveLength(1);
  });

  it("renders frame_step when present", () => {
    chaptersRef.current = [chapter({ id: "stepped", title: "步长章", frame_step: 5 })];
    const { getByText } = renderSidebar();
    expect(getByText(/步长 5/)).toBeInTheDocument();
  });

  it("omits step text when frame_step is null", () => {
    chaptersRef.current = [chapter({ id: "nostep", frame_step: null })];
    const { queryByText } = renderSidebar();
    expect(queryByText(/步长/)).toBeNull();
  });
});

describe("VideoChapterSidebar timeline draft", () => {
  it("shows the arming hint when timeline draft is armed", () => {
    chaptersRef.current = [];
    const { getByTestId } = render(
      <VideoChapterSidebar
        datasetItemId="item-1"
        frameIndex={0}
        maxFrame={100}
        canEdit
        timelineDraftArmed
        onToggleTimelineDraft={() => {}}
      />,
    );
    expect(getByTestId("video-chapter-draft-hint")).toBeInTheDocument();
  });

  it("reports row hover and highlights the controlled hovered chapter", () => {
    chaptersRef.current = [chapter({ id: "ch1", title: "A" })];
    const onHoverChapter = vi.fn();
    const { getByTestId } = render(
      <VideoChapterSidebar
        datasetItemId="item-1"
        frameIndex={50}
        maxFrame={100}
        canEdit={false}
        hoveredChapterId="ch1"
        onHoverChapter={onHoverChapter}
      />,
    );
    const row = getByTestId("video-chapter-row");
    expect(row).toHaveAttribute("data-hovered", "true");
    fireEvent.mouseEnter(row);
    expect(onHoverChapter).toHaveBeenCalledWith("ch1");
    fireEvent.mouseLeave(row);
    expect(onHoverChapter).toHaveBeenCalledWith(null);
  });

  it("opens the create form prefilled from a timeline brush and consumes the draft once", () => {
    chaptersRef.current = [];
    const onConsumeDraftRange = vi.fn();
    const { getByTestId, getByDisplayValue } = render(
      <VideoChapterSidebar
        datasetItemId="item-1"
        frameIndex={0}
        maxFrame={100}
        canEdit
        draftRange={{ startFrame: 12, endFrame: 34 }}
        onConsumeDraftRange={onConsumeDraftRange}
      />,
    );
    expect(getByTestId("video-chapter-form")).toBeInTheDocument();
    expect(getByDisplayValue("12")).toBeInTheDocument();
    expect(getByDisplayValue("34")).toBeInTheDocument();
    expect(onConsumeDraftRange).toHaveBeenCalledTimes(1);
  });
});
