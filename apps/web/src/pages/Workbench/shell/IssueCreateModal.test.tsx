import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CreateFeedbackPayload, ListFeedbacksParams } from "@/api/feedbacks";
import { IssueCreateModal } from "./IssueCreateModal";

interface MutationCallbacks {
  onSuccess: () => void;
  onError: (error: Error) => void;
}
const requests: Array<{ payload: CreateFeedbackPayload; callbacks: MutationCallbacks }> = [];
const listSubscriptions: ListFeedbacksParams[] = [];
const mutate = vi.fn((payload: CreateFeedbackPayload, callbacks: MutationCallbacks) => {
  requests.push({ payload, callbacks });
});

vi.mock("@/hooks/useFeedbacks", () => ({
  useCreateFeedback: (params: ListFeedbacksParams) => {
    listSubscriptions.push(params);
    return { mutate, isPending: false };
  },
}));

type Props = ComponentProps<typeof IssueCreateModal>;

function setup(over: Partial<Props> = {}) {
  const onClose = vi.fn();
  const props: Props = {
    open: true,
    projectId: "P1",
    taskId: "T1",
    listParams: { project_id: "P1", task_id: "T1", kind: "issue" },
    prefilledAnchor: { x: 0.2, y: 0.4, frame: 3 },
    onClose,
    ...over,
  };
  return { ...render(<IssueCreateModal {...props} />), props, onClose };
}

function fillBody(body = "此处边界需要复核") {
  fireEvent.change(screen.getByPlaceholderText("描述问题位置 / 现象 / 期望行为"), {
    target: { value: body },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "提交" }));
}

describe("IssueCreateModal", () => {
  beforeEach(() => {
    requests.length = 0;
    listSubscriptions.length = 0;
    vi.clearAllMocks();
  });
  afterEach(cleanup);

  it("does not mount a form or mutation observer while closed", () => {
    setup({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(listSubscriptions).toHaveLength(0);
  });

  it("shows and submits the confirmed source frame F0", () => {
    setup({ prefilledAnchor: { x: 0.25, y: 0.75, frame: 0 } });
    const dialog = screen.getByRole("dialog");
    expect(dialog.hasAttribute("data-workbench-issue-create")).toBe(true);
    expect(dialog.getAttribute("data-state")).toBe("open");
    expect(screen.getByTestId("issue-create-frame").textContent).toBe("源帧 F 0");
    fillBody();
    submit();
    expect(requests[0].payload).toMatchObject({
      project_id: "P1",
      task_id: "T1",
      anchor_type: "pixel",
      anchor_position: { x: 0.25, y: 0.75, frame: 0 },
    });
  });

  it("freezes the opening anchor and does not expose editable coordinates", () => {
    const view = setup();
    view.rerender(
      <IssueCreateModal {...view.props} prefilledAnchor={{ x: 0.8, y: 0.9, frame: 17 }} />,
    );
    expect(screen.queryByPlaceholderText("x (0-1)")).toBeNull();
    expect(screen.queryByPlaceholderText("y (0-1)")).toBeNull();
    expect(screen.getByTestId("issue-create-frame").textContent).toBe("源帧 F 3");
    fillBody();
    submit();
    expect(requests[0].payload.anchor_position).toEqual({ x: 0.2, y: 0.4, frame: 3 });
  });

  it("copies the input anchor instead of retaining a mutable parent object", () => {
    const anchor = { x: 0.2, y: 0.4, frame: 3 };
    const view = setup({ prefilledAnchor: anchor });
    anchor.frame = 17;
    anchor.x = 0.8;
    view.rerender(<IssueCreateModal {...view.props} />);
    fillBody();
    submit();
    expect(requests[0].payload.anchor_position).toEqual({ x: 0.2, y: 0.4, frame: 3 });
  });

  it("keeps image pixel payloads free of video frame fields", () => {
    setup({ prefilledAnchor: { x: 0.2, y: 0.4 } });
    fillBody();
    submit();
    expect(screen.queryByTestId("issue-create-frame")).toBeNull();
    expect(requests[0].payload.anchor_position).toEqual({ x: 0.2, y: 0.4 });
  });

  it("uses an explicit task intent with a null anchor", () => {
    setup({ anchorMode: "task", prefilledAnchor: null });
    expect(screen.queryByPlaceholderText("x (0-1)")).toBeNull();
    expect(screen.queryByPlaceholderText("y (0-1)")).toBeNull();
    expect(screen.queryByTestId("issue-create-frame")).toBeNull();
    fillBody();
    submit();
    expect(requests[0].payload).toMatchObject({ anchor_type: "task", anchor_position: null });
  });

  it("keeps a task-only opening frameless even if later props offer pixel coordinates", () => {
    const view = setup({ anchorMode: "task", prefilledAnchor: null });
    expect(screen.queryByPlaceholderText("x (0-1)")).toBeNull();
    expect(screen.queryByPlaceholderText("y (0-1)")).toBeNull();
    view.rerender(
      <IssueCreateModal {...view.props} anchorMode="pixel" prefilledAnchor={{ x: 0.5, y: 0.5 }} />,
    );
    expect(screen.queryByPlaceholderText("x (0-1)")).toBeNull();
    expect(screen.queryByTestId("issue-create-frame")).toBeNull();
    fillBody();
    submit();
    expect(requests[0].payload).toMatchObject({ anchor_type: "task", anchor_position: null });
  });

  it("requires a confirmed point for pixel intent", () => {
    setup({ anchorMode: "pixel", prefilledAnchor: null });
    expect(screen.queryByPlaceholderText("x (0-1)")).toBeNull();
    expect(screen.queryByPlaceholderText("y (0-1)")).toBeNull();
    expect(screen.getByText("尚未确认画布点，请关闭后使用「在画布选点」。")).toBeTruthy();
    fillBody();
    submit();
    expect(requests).toHaveLength(0);
  });

  it("preserves form input and the same source frame after a failed mutation", () => {
    const view = setup();
    fireEvent.change(screen.getByPlaceholderText("一句话概括"), { target: { value: "检查边界" } });
    fireEvent.click(screen.getByRole("button", { name: "阻断" }));
    fillBody("保留这段详情");
    submit();
    view.rerender(
      <IssueCreateModal {...view.props} prefilledAnchor={{ x: 0.8, y: 0.9, frame: 17 }} />,
    );
    act(() => requests[0].callbacks.onError(new Error("暂时无法保存")));
    expect(screen.getByRole("alert").textContent).toBe("暂时无法保存");
    expect((screen.getByPlaceholderText("一句话概括") as HTMLInputElement).value).toBe("检查边界");
    expect(
      (screen.getByPlaceholderText("描述问题位置 / 现象 / 期望行为") as HTMLTextAreaElement).value,
    ).toBe("保留这段详情");
    submit();
    expect(requests[1].payload).toEqual(requests[0].payload);
    expect(requests[1].payload.anchor_position).toEqual({ x: 0.2, y: 0.4, frame: 3 });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(view.onClose).not.toHaveBeenCalled();
  });

  it("does not duplicate a submission while its result is pending", () => {
    setup();
    fillBody();
    const button = screen.getByRole("button", { name: "提交" });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "提交中…" }).hasAttribute("disabled")).toBe(true);
  });

  it("closes only its current session on successful submission", () => {
    const view = setup();
    fillBody();
    submit();
    act(() => requests[0].callbacks.onSuccess());
    expect(view.onClose).toHaveBeenCalledTimes(1);
  });

  it("cannot clear or close a reopened form when an older mutation succeeds", () => {
    const view = setup();
    fillBody("旧会话");
    submit();
    view.rerender(<IssueCreateModal {...view.props} open={false} />);
    view.rerender(
      <IssueCreateModal {...view.props} prefilledAnchor={{ x: 0.7, y: 0.8, frame: 17 }} />,
    );
    fillBody("新会话");
    act(() => requests[0].callbacks.onSuccess());
    expect(view.onClose).not.toHaveBeenCalled();
    expect(
      (screen.getByPlaceholderText("描述问题位置 / 现象 / 期望行为") as HTMLTextAreaElement).value,
    ).toBe("新会话");
    expect(screen.getByTestId("issue-create-frame").textContent).toBe("源帧 F 17");
    submit();
    expect(requests[1].payload.anchor_position).toEqual({ x: 0.7, y: 0.8, frame: 17 });
  });

  it("starts a clean task session without inheriting an older failure or pending state", () => {
    const view = setup();
    fillBody("T1 详情");
    submit();
    view.rerender(
      <IssueCreateModal
        {...view.props}
        projectId="P2"
        taskId="T2"
        listParams={{ project_id: "P2", task_id: "T2", kind: "issue" }}
        prefilledAnchor={{ x: 0.7, y: 0.8, frame: 17 }}
      />,
    );
    act(() => requests[0].callbacks.onError(new Error("T1 的旧错误")));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      (screen.getByPlaceholderText("描述问题位置 / 现象 / 期望行为") as HTMLTextAreaElement).value,
    ).toBe("");
    fillBody("T2 详情");
    submit();
    expect(requests[1].payload).toMatchObject({ project_id: "P2", task_id: "T2", body: "T2 详情" });
    expect(listSubscriptions[listSubscriptions.length - 1]).toEqual({
      project_id: "P2",
      task_id: "T2",
      kind: "issue",
    });
  });

  it("does not revive a mutation when task identity cycles A → B → A", () => {
    const view = setup();
    fillBody("第一次 A");
    submit();
    view.rerender(<IssueCreateModal {...view.props} taskId="T2" />);
    view.rerender(
      <IssueCreateModal {...view.props} prefilledAnchor={{ x: 0.7, y: 0.8, frame: 17 }} />,
    );
    fillBody("第二次 A");
    act(() => requests[0].callbacks.onSuccess());
    expect(view.onClose).not.toHaveBeenCalled();
    expect(
      (screen.getByPlaceholderText("描述问题位置 / 现象 / 期望行为") as HTMLTextAreaElement).value,
    ).toBe("第二次 A");
  });

  it("invalidates a submission as soon as cancel is clicked before parent rerender", () => {
    const view = setup();
    fillBody();
    submit();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    act(() => requests[0].callbacks.onSuccess());
    expect(view.onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape and ignores old callbacks after unmount", () => {
    const view = setup();
    fillBody();
    submit();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(view.onClose).toHaveBeenCalledTimes(1);
    view.unmount();
    act(() => requests[0].callbacks.onSuccess());
    expect(view.onClose).toHaveBeenCalledTimes(1);
  });

  it("freezes the object version, viewport and time window while submitting one source-frame range", () => {
    const anchor = {
      x: 0.3,
      y: 0.4,
      frame: 130,
      maxFrame: 179,
      annotationId: "A1",
      annotationLabel: "车辆",
      videoContext: {
        schema_version: 1 as const,
        annotation_version: 3,
        viewport: { center_x: 0.6, center_y: 0.7, zoom: 2 },
        timeline_window: { from: 100.5, to: 160.5 },
      },
    };
    const view = setup({ prefilledAnchor: anchor });
    anchor.videoContext.annotation_version = 4;
    anchor.videoContext.viewport.zoom = 5;
    view.rerender(<IssueCreateModal {...view.props} />);
    fireEvent.click(screen.getByTestId("issue-frame-range-enabled"));
    fireEvent.change(screen.getByTestId("issue-frame-range-from"), { target: { value: "120" } });
    fireEvent.change(screen.getByTestId("issue-frame-range-to"), { target: { value: "160" } });
    fillBody();
    submit();
    expect(requests).toHaveLength(1);
    expect(requests[0].payload).toMatchObject({
      annotation_id: "A1",
      anchor_position: {
        frame: 130,
        video_context: {
          schema_version: 1,
          annotation_version: 3,
          viewport: { center_x: 0.6, center_y: 0.7, zoom: 2 },
          timeline_window: { from: 100.5, to: 160.5 },
          frame_range: { from_frame: 120, to_frame: 160 },
        },
      },
    });
  });

  it("shows a short object identity and clears only the association", () => {
    setup({
      prefilledAnchor: {
        x: 0.2,
        y: 0.4,
        annotationId: "annotation-123456",
        annotationLabel: "车辆",
      },
    });
    expect(screen.getByTestId("issue-create-location-summary")).toHaveTextContent(
      "对象 车辆 · anno…3456",
    );
    fireEvent.click(screen.getByTestId("issue-clear-object"));
    expect(screen.getByTestId("issue-create-location-summary")).toHaveTextContent(
      "画布位置 x 0.200 · y 0.400",
    );
    expect(screen.queryByTestId("issue-clear-object")).toBeNull();
    fillBody();
    submit();
    expect(requests[0].payload.annotation_id).toBeUndefined();
    expect(requests[0].payload.anchor_position).toEqual({ x: 0.2, y: 0.4 });
  });

  it("clears video object metadata while preserving the source frame and view", () => {
    setup({
      prefilledAnchor: {
        x: 0.3,
        y: 0.4,
        frame: 7,
        annotationId: "annotation-123456",
        annotationLabel: "车辆",
        videoContext: {
          schema_version: 1,
          track_id: "track-1",
          annotation_version: 3,
          viewport: { center_x: 0.4, center_y: 0.5, zoom: 2 },
        },
      },
    });
    fireEvent.click(screen.getByTestId("issue-clear-object"));
    expect(screen.getByTestId("issue-context-object")).toHaveTextContent("未关联对象");
    fillBody();
    submit();
    expect(requests[0].payload.annotation_id).toBeUndefined();
    expect(requests[0].payload.anchor_position).toEqual({
      x: 0.3,
      y: 0.4,
      frame: 7,
      video_context: {
        schema_version: 1,
        viewport: { center_x: 0.4, center_y: 0.5, zoom: 2 },
      },
    });
  });

  it.each([
    ["131", "160"],
    ["120", "129"],
    ["120.5", "160"],
    ["120", "180"],
    ["", "160"],
  ])("rejects a range %s–%s outside source boundaries or excluding the anchor", (from, to) => {
    setup({
      prefilledAnchor: {
        x: 0.3,
        y: 0.4,
        frame: 130,
        maxFrame: 179,
        videoContext: { schema_version: 1 },
      },
    });
    fireEvent.click(screen.getByTestId("issue-frame-range-enabled"));
    fireEvent.change(screen.getByTestId("issue-frame-range-from"), { target: { value: from } });
    fireEvent.change(screen.getByTestId("issue-frame-range-to"), { target: { value: to } });
    fillBody();
    submit();
    expect(requests).toHaveLength(0);
    expect(screen.getByRole("alert").textContent).toContain("包含 F 130");
  });

  it("omits object and context for an explicit task-only issue", () => {
    setup({
      anchorMode: "task",
      prefilledAnchor: {
        x: 0.3,
        y: 0.4,
        frame: 130,
        annotationId: "A1",
        videoContext: { schema_version: 1, annotation_version: 3 },
      },
    });
    expect(screen.queryByTestId("issue-create-location-summary")).toBeNull();
    fillBody();
    submit();
    expect(requests[0].payload.annotation_id).toBeUndefined();
    expect(requests[0].payload.anchor_position).toBeNull();
    expect(requests[0].payload.anchor_type).toBe("task");
  });
});
