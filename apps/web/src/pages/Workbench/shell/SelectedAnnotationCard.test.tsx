import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, fireEvent } from "@testing-library/react";
import { SelectedAnnotationCard } from "./SelectedAnnotationCard";
import { useState } from "react";
import { resolveVideoSelectionCardCollapsed } from "../state/useWorkbenchShellModel.helpers";

const position = { x: 100, y: 80, w: 340, h: 440 };

function SelectionWithRetainedTracker({
  trackerVisible,
  initialCollapsed = false,
}: {
  trackerVisible: boolean;
  initialCollapsed?: boolean;
}) {
  const [preferredCollapsed, setPreferredCollapsed] = useState(initialCollapsed);
  return (
    <>
      <output data-testid="preferred-collapse">{String(preferredCollapsed)}</output>
      <SelectedAnnotationCard
        title="car"
        position={position}
        onPositionChange={() => {}}
        collapsed={resolveVideoSelectionCardCollapsed(preferredCollapsed, true, trackerVisible)}
        onCollapse={() => setPreferredCollapsed(true)}
        onExpand={() => setPreferredCollapsed(false)}
      >
        <button type="button">编辑 Mask</button>
      </SelectedAnnotationCard>
    </>
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe("SelectedAnnotationCard", () => {
  it("restores expanded content when a retained tracker panel is hidden without changing the preference", () => {
    vi.useFakeTimers();
    const { getByTestId, getByRole, queryByRole, rerender } = render(
      <SelectionWithRetainedTracker trackerVisible />,
    );
    expect(queryByRole("button", { name: "编辑 Mask" })).toBeNull();
    expect(getByTestId("preferred-collapse")).toHaveTextContent("false");

    rerender(<SelectionWithRetainedTracker trackerVisible={false} />);
    expect(getByRole("button", { name: "编辑 Mask" })).toBeVisible();
    expect(getByTestId("preferred-collapse")).toHaveTextContent("false");

    rerender(<SelectionWithRetainedTracker trackerVisible />);
    act(() => vi.advanceTimersByTime(160));
    expect(queryByRole("button", { name: "编辑 Mask" })).toBeNull();
    expect(getByTestId("preferred-collapse")).toHaveTextContent("false");

    rerender(<SelectionWithRetainedTracker trackerVisible={false} />);
    expect(getByRole("button", { name: "编辑 Mask" })).toBeVisible();
  });

  it("preserves a manually collapsed preference across tracker visibility changes and allows expanding while hidden", () => {
    vi.useFakeTimers();
    const { getByTestId, getByRole, getByLabelText, queryByRole, rerender } = render(
      <SelectionWithRetainedTracker trackerVisible initialCollapsed />,
    );
    rerender(<SelectionWithRetainedTracker trackerVisible={false} initialCollapsed />);
    expect(queryByRole("button", { name: "编辑 Mask" })).toBeNull();
    expect(getByTestId("preferred-collapse")).toHaveTextContent("true");
    rerender(<SelectionWithRetainedTracker trackerVisible initialCollapsed />);
    rerender(<SelectionWithRetainedTracker trackerVisible={false} initialCollapsed />);
    expect(getByTestId("preferred-collapse")).toHaveTextContent("true");
    expect(queryByRole("button", { name: "编辑 Mask" })).toBeNull();

    fireEvent.click(getByLabelText("展开选中信息卡(可拖动)"));
    expect(getByRole("button", { name: "编辑 Mask" })).toBeVisible();
    expect(getByTestId("preferred-collapse")).toHaveTextContent("false");

    fireEvent.click(getByLabelText("收起浮窗"));
    act(() => vi.advanceTimersByTime(160));
    expect(getByTestId("preferred-collapse")).toHaveTextContent("true");
    rerender(<SelectionWithRetainedTracker trackerVisible initialCollapsed />);
    rerender(<SelectionWithRetainedTracker trackerVisible={false} initialCollapsed />);
    expect(queryByRole("button", { name: "编辑 Mask" })).toBeNull();
    expect(getByTestId("preferred-collapse")).toHaveTextContent("true");
  });

  it("展开态渲染标题与内容,点收起触发 onCollapse", () => {
    const onCollapse = vi.fn();
    const { getByText, getByLabelText } = render(
      <SelectedAnnotationCard
        title="car"
        position={position}
        onPositionChange={() => {}}
        collapsed={false}
        onCollapse={onCollapse}
        onExpand={() => {}}
      >
        <div>卡片内容</div>
      </SelectedAnnotationCard>,
    );
    expect(getByText("选中对象")).not.toBeNull();
    expect(getByText("car")).not.toBeNull();
    expect(getByText("卡片内容")).not.toBeNull();
    fireEvent.click(getByLabelText("收起浮窗"));
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("折叠态渲染信息胶囊(不渲染内容),点击触发 onExpand", () => {
    const onExpand = vi.fn();
    const { getByLabelText, getByText, queryByText } = render(
      <SelectedAnnotationCard
        title="car"
        position={position}
        onPositionChange={() => {}}
        collapsed
        onCollapse={() => {}}
        onExpand={onExpand}
      >
        <div>卡片内容</div>
      </SelectedAnnotationCard>,
    );
    const tab = getByLabelText("展开选中信息卡(可拖动)") as HTMLElement;
    expect(getByText("car")).not.toBeNull();
    expect(queryByText("卡片内容")).toBeNull();
    expect(tab.style.getPropertyValue("--selection-tab-x")).toBe("212px");
    expect(tab.style.getPropertyValue("--selection-tab-y")).toBe("280px");
    expect(tab.style.getPropertyValue("--selection-tab-w")).toBe("116px");
    fireEvent.click(tab);
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it("收起时先保留展开面板完成退场,再渲染信息胶囊", () => {
    vi.useFakeTimers();
    const props = {
      title: "car",
      position,
      onPositionChange: () => {},
      onCollapse: () => {},
      onExpand: () => {},
      children: <div>卡片内容</div>,
    };
    const { getByText, queryByText, rerender } = render(
      <SelectedAnnotationCard {...props} collapsed={false} />,
    );

    rerender(<SelectedAnnotationCard {...props} collapsed />);

    expect(getByText("卡片内容")).not.toBeNull();
    expect(queryByText("选中")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(160);
    });

    expect(getByText("car")).not.toBeNull();
    expect(queryByText("卡片内容")).toBeNull();
  });
});
