/**
 * v0.6.6 · CommentInput.serialize 单元测试。
 *
 * 覆盖 ROADMAP 列出的 4 类边界：
 *  - chip 紧邻 chip：两个 mention 直接相连，offset 不能错位
 *  - chip 在 block 元素首尾：div / p 包裹时换行注入正确
 *  - 普通文本 + chip 混合：base 路径
 *  - 仅文本（无 chip）：mentions 为空
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { renderCommentBody, serialize } from "../CommentInput";
import { CommentInput } from "../CommentInput";
import { commentsApi, type CommentCanvasDrawing } from "@/api/comments";
import {
  createDiscussionDraftStore,
  type DiscussionDraftStore,
} from "../../state/useDiscussionDraftStore";
import type { DiscussionPayload, DiscussionTarget } from "../../state/discussionTypes";

type MockCanvasProps = {
  open: boolean;
  onSave: (drawing: CommentCanvasDrawing | null) => void;
  onDraftChange?: (drawing: CommentCanvasDrawing | null) => void;
  initial?: CommentCanvasDrawing | null;
};

const canvasHarness = vi.hoisted(() => ({
  onSave: null as MockCanvasProps["onSave"] | null,
  onDraftChange: null as MockCanvasProps["onDraftChange"] | null,
  initial: null as MockCanvasProps["initial"] | null,
}));

vi.mock("@/components/CanvasDrawingEditor", () => ({
  CanvasDrawingEditor: ({ open, onSave, onDraftChange, initial }: MockCanvasProps) => {
    if (open) {
      canvasHarness.onSave = onSave;
      canvasHarness.onDraftChange = onDraftChange ?? null;
      canvasHarness.initial = initial ?? null;
    }
    return open ? (
      <button
        type="button"
        data-testid="mock-canvas-save"
        onClick={() =>
          onSave({
            shapes: [{ type: "line", points: [0, 0, 1, 1] }],
          } as CommentCanvasDrawing)
        }
      >
        保存批注
      </button>
    ) : null;
  },
}));

function makeRoot(html: string): HTMLElement {
  const div = document.createElement("div");
  div.innerHTML = html;
  return div;
}

describe("CommentInput.serialize", () => {
  it("纯文本无 chip → body 等于文本，mentions 空", () => {
    const root = makeRoot("hello world");
    const { body, mentions } = serialize(root);
    expect(body).toBe("hello world");
    expect(mentions).toEqual([]);
  });

  it("单个 chip + 后续文本 → mention offset/length 正确", () => {
    const root = makeRoot('<span data-mention-uid="u1" data-mention-name="alice">@alice</span> hi');
    const { body, mentions } = serialize(root);
    expect(body).toBe("@alice hi");
    expect(mentions).toEqual([{ userId: "u1", displayName: "alice", offset: 0, length: 6 }]);
  });

  it("chip 紧邻 chip（无中间文本） → 两个 mention 偏移连续", () => {
    const root = makeRoot(
      '<span data-mention-uid="u1" data-mention-name="alice">@alice</span>' +
        '<span data-mention-uid="u2" data-mention-name="bob">@bob</span>',
    );
    const { body, mentions } = serialize(root);
    expect(body).toBe("@alice@bob");
    expect(mentions).toEqual([
      { userId: "u1", displayName: "alice", offset: 0, length: 6 },
      { userId: "u2", displayName: "bob", offset: 6, length: 4 },
    ]);
  });

  it("chip 在 block 元素首尾 → 块间换行注入不污染 mention offset", () => {
    const root = makeRoot(
      '<div><span data-mention-uid="u1" data-mention-name="alice">@alice</span></div>' +
        "<div>line2</div>",
    );
    const { body, mentions } = serialize(root);
    expect(body).toBe("@alice\nline2");
    expect(mentions[0]).toEqual({
      userId: "u1",
      displayName: "alice",
      offset: 0,
      length: 6,
    });
  });

  it("BR 节点转换为换行", () => {
    const root = makeRoot("line1<br>line2");
    const { body } = serialize(root);
    expect(body).toBe("line1\nline2");
  });

  it("mention chip data-mention-name 缺失时回退到 textContent", () => {
    const root = makeRoot('<span data-mention-uid="u1">@fallback</span>');
    const { body, mentions } = serialize(root);
    // textContent = "@fallback"，name = "@fallback"，导出 text = "@@fallback"
    expect(mentions[0].userId).toBe("u1");
    expect(mentions[0].displayName).toBe("@fallback");
    expect(body.startsWith("@@fallback")).toBe(true);
  });

  it("keeps mention offsets aligned after trimming whitespace before emoji text", () => {
    const root = makeRoot(
      ' \t🙂 <span data-mention-uid="u1" data-mention-name="alice">@alice</span> ',
    );
    expect(serialize(root)).toEqual({
      body: "🙂 @alice",
      mentions: [{ userId: "u1", displayName: "alice", offset: 3, length: 6 }],
    });
  });

  it("renders stored mentions as chips in task comment history", () => {
    const view = render(
      <div>
        {renderCommentBody("@alice please check", [
          { userId: "u1", displayName: "alice", offset: 0, length: 6 },
        ])}
      </div>,
    );
    expect(view.getByText("@alice")).toBeInTheDocument();
    expect(view.getByText("@alice")).toHaveClass("text-brand");
  });
});

const annotationA: DiscussionTarget = {
  projectId: "p",
  taskId: "a",
  kind: "annotation",
  annotationId: "ann-a",
};
const annotationB: DiscussionTarget = {
  projectId: "p",
  taskId: "b",
  kind: "annotation",
  annotationId: "ann-b",
};
const textTask: DiscussionTarget = { projectId: "p", taskId: "a", kind: "task" };

function renderComposer(
  target: DiscussionTarget,
  onSubmit: (payload: unknown) => void | Promise<unknown>,
  extra: Partial<ComponentProps<typeof CommentInput>> = {},
) {
  const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
  return render(
    <CommentInput target={target} draftStore={store} members={[]} onSubmit={onSubmit} {...extra} />,
  );
}

function editor(container: HTMLElement): HTMLElement {
  return container.querySelector('[contenteditable="true"]') as HTMLElement;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => {
  canvasHarness.onSave = null;
  canvasHarness.onDraftChange = null;
  canvasHarness.initial = null;
  delete (Range.prototype as unknown as { getBoundingClientRect?: unknown }).getBoundingClientRect;
  vi.restoreAllMocks();
});

describe("CommentInput session composer", () => {
  it("restores independent annotation A/B/A drafts without rewriting on each edit", () => {
    const onSubmit = vi.fn();
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const input = editor(view.container);
    input.textContent = "draft A";
    fireEvent.input(input);
    view.rerender(
      <CommentInput target={annotationB} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputB = editor(view.container);
    inputB.textContent = "draft B";
    fireEvent.input(inputB);
    view.rerender(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    expect(editor(view.container).textContent).toBe("draft A");
  });

  it("rehydrates the same target when the authenticated owner changes", () => {
    const onSubmit = vi.fn();
    const oldStore = createDiscussionDraftStore({
      owner: { userId: "old-user", sessionId: "old-session" },
    });
    const newStore = createDiscussionDraftStore({
      owner: { userId: "new-user", sessionId: "new-session" },
    });
    oldStore.patchDraft(annotationA, { body: "old account" });
    newStore.patchDraft(annotationA, { body: "new account" });
    const view = render(
      <CommentInput target={annotationA} draftStore={oldStore} members={[]} onSubmit={onSubmit} />,
    );
    expect(editor(view.container).textContent).toBe("old account");

    view.rerender(
      <CommentInput target={annotationA} draftStore={newStore} members={[]} onSubmit={onSubmit} />,
    );
    expect(editor(view.container).textContent).toBe("new account");
  });

  it("rehydrates legacy annotation-only adapters when the annotation changes", () => {
    const onSubmit = vi.fn();
    const view = render(<CommentInput annotationId="ann-a" members={[]} onSubmit={onSubmit} />);
    const input = editor(view.container);
    input.textContent = "legacy A";
    fireEvent.input(input);

    view.rerender(<CommentInput annotationId="ann-b" members={[]} onSubmit={onSubmit} />);
    expect(editor(view.container).textContent).toBe("");
  });

  it("defers editor sync until IME composition ends", async () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput target={textTask} draftStore={store} members={[]} onSubmit={vi.fn()} />,
    );
    const input = editor(view.container);
    // 组合中：不同步草稿，避免 React/store 更新打断输入法。
    fireEvent.compositionStart(input);
    input.textContent = "中";
    fireEvent.input(input);
    expect(store.getDraft(textTask)?.body ?? "").toBe("");
    // 组合结束：补一次同步，拿到最终文本。
    fireEvent.compositionEnd(input);
    await waitFor(() => expect(store.getDraft(textTask)?.body).toBe("中"));
  });

  it("allows task mentions while keeping attachments and drawing disabled", () => {
    const onSubmit = vi.fn();
    const view = renderComposer(textTask, onSubmit);
    const input = editor(view.container);
    expect(view.container.querySelector('input[type="file"]')).toBeNull();
    expect(view.container.querySelector('button[title*="题图"]')).toBeNull();
    expect(input).toHaveAttribute("data-placeholder", "留言（@ 提及成员）...");
    input.textContent = "task note";
    fireEvent.input(input);
    fireEvent.click(view.getByRole("button", { name: /发送/ }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        body: "task note",
        mentions: [],
        attachments: [],
        canvas_drawing: null,
      }),
      expect.anything(),
    );
  });

  it("picking a mention with Enter inserts the chip without submitting the comment", () => {
    // jsdom 不实现 Range.getBoundingClientRect；@ 触发需要光标矩形。
    Object.defineProperty(Range.prototype, "getBoundingClientRect", {
      configurable: true,
      value: () => ({
        left: 10,
        top: 200,
        right: 20,
        bottom: 214,
        width: 10,
        height: 14,
        x: 10,
        y: 200,
        toJSON: () => ({}),
      }),
    });
    const onSubmit = vi.fn();
    const view = renderComposer(textTask, onSubmit, {
      members: [{ id: "u1", name: "Alice" }],
    });
    const input = editor(view.container);
    input.textContent = "hi @";
    const textNode = input.firstChild as Text;
    const range = document.createRange();
    range.setStart(textNode, textNode.length);
    range.collapse(true);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    fireEvent.input(input);

    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onSubmit).not.toHaveBeenCalled();
    expect(input.querySelector('[data-mention-uid="u1"]')).not.toBeNull();

    // 再次 Enter 才算真正发送。
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("offers popup drawing for an explicitly enabled task target", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput
        target={textTask}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="task-image"
      />,
    );

    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    fireEvent.click(view.getByTestId("mock-canvas-save"));
    expect(store.getDraft(textTask)?.canvas_drawing?.shapes).toHaveLength(1);
  });

  it("does not submit during IME composition and ignores rapid duplicate Enter/click triggers", async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const onSubmit = vi.fn(() => pending);
    const view = renderComposer(textTask, onSubmit);
    const input = editor(view.container);
    input.textContent = "中文";
    fireEvent.input(input);
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    fireEvent.click(view.getByRole("button", { name: /发送/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    release();
    await waitFor(() =>
      expect(view.container.querySelector("[contenteditable]")?.textContent).toBe(""),
    );
  });

  it("keeps A and B submit controls independent when A finishes first", async () => {
    const pendingA = deferred<void>();
    const pendingB = deferred<void>();
    const onSubmit = vi.fn((payload: DiscussionPayload) =>
      payload.body === "A" ? pendingA.promise : pendingB.promise,
    );
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputA = editor(view.container);
    inputA.textContent = "A";
    fireEvent.input(inputA);
    const send = () => view.getByRole("button", { name: /发送/ });
    expect(send()).toBeEnabled();
    fireEvent.click(send());
    expect(send()).toBeDisabled();

    view.rerender(
      <CommentInput target={annotationB} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputB = editor(view.container);
    inputB.textContent = "B";
    fireEvent.input(inputB);
    expect(send()).toBeEnabled();
    fireEvent.click(send());
    expect(send()).toBeDisabled();

    view.rerender(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    expect(send()).toBeDisabled();
    expect(editor(view.container).textContent).toBe("A");
    view.rerender(
      <CommentInput target={annotationB} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    pendingA.resolve();
    await waitFor(() => {
      expect(send()).toBeDisabled();
      expect(editor(view.container).textContent).toBe("B");
    });

    pendingB.resolve();
    await waitFor(() => {
      expect(send()).toBeEnabled();
      expect(editor(view.container).textContent).toBe("");
    });
  });

  it("keeps A pending when B completes first and blocks A on return", async () => {
    const pendingA = deferred<void>();
    const pendingB = deferred<void>();
    const onSubmit = vi.fn((payload: DiscussionPayload) =>
      payload.body === "A" ? pendingA.promise : pendingB.promise,
    );
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputA = editor(view.container);
    inputA.textContent = "A";
    fireEvent.input(inputA);
    const send = () => view.getByRole("button", { name: /发送/ });
    fireEvent.click(send());

    view.rerender(
      <CommentInput target={annotationB} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputB = editor(view.container);
    inputB.textContent = "B";
    fireEvent.input(inputB);
    expect(send()).toBeEnabled();
    fireEvent.click(send());

    pendingB.resolve();
    await waitFor(() => {
      expect(send()).toBeEnabled();
      expect(editor(view.container).textContent).toBe("");
    });
    view.rerender(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    expect(send()).toBeDisabled();
    expect(editor(view.container).textContent).toBe("A");

    pendingA.resolve();
    await waitFor(() => {
      expect(send()).toBeEnabled();
      expect(editor(view.container).textContent).toBe("");
    });
  });

  it("retains a failed draft and leaves newer content after an old request resolves", async () => {
    let reject!: (error: Error) => void;
    const failed = new Promise<void>((_, rejectPromise) => {
      reject = rejectPromise;
    });
    const onSubmit = vi.fn(() => failed);
    const view = renderComposer(annotationA, onSubmit);
    const input = editor(view.container);
    input.textContent = "keep me";
    fireEvent.input(input);
    fireEvent.click(view.getByRole("button", { name: /发送/ }));
    input.textContent = "new edit";
    fireEvent.input(input);
    reject(new Error("offline"));
    await waitFor(() => expect(view.getByRole("button", { name: "发送" })).toBeInTheDocument());
    expect(view.container.querySelector('[role="alert"]')).toBeNull();
    expect(editor(view.container).textContent).toBe("new edit");
  });

  it("does not clear the visible B editor when an A submission resolves after switching targets", async () => {
    let resolve!: () => void;
    const pending = new Promise<void>((done) => {
      resolve = done;
    });
    const onSubmit = vi.fn(() => pending);
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputA = editor(view.container);
    inputA.textContent = "A pending";
    fireEvent.input(inputA);
    fireEvent.click(view.getByRole("button", { name: /发送/ }));

    view.rerender(
      <CommentInput target={annotationB} draftStore={store} members={[]} onSubmit={onSubmit} />,
    );
    const inputB = editor(view.container);
    inputB.textContent = "B draft";
    fireEvent.input(inputB);
    resolve();
    await waitFor(() => expect(view.getByRole("button", { name: "发送" })).toBeInTheDocument());
    expect(editor(view.container).textContent).toBe("B draft");
    expect(store.getDraft(annotationA)?.body).toBe("");
  });

  it("routes a late live drawing result to its original target", async () => {
    const store: DiscussionDraftStore = createDiscussionDraftStore({
      owner: { userId: "u1", sessionId: "s1" },
    });
    const origin = store.makeOrigin(annotationA)!;
    const onConsume = vi.fn();
    const onSubmit = vi.fn();
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={onSubmit}
        liveCanvas={{ active: true, result: null, onStart: vi.fn(), onConsume }}
      />,
    );
    view.rerender(
      <CommentInput
        target={annotationB}
        draftStore={store}
        members={[]}
        onSubmit={onSubmit}
        liveCanvas={{
          active: false,
          result: { shapes: [{ type: "line", points: [0, 0, 1, 1] }] },
          resultId: "result-a",
          origin,
          onStart: vi.fn(),
          onConsume,
        }}
      />,
    );
    await waitFor(() =>
      expect(store.getDraft(annotationA)?.canvas_drawing?.shapes).toHaveLength(1),
    );
    expect(store.getDraft(annotationB)?.canvas_drawing).toBeNull();
    expect(onConsume).toHaveBeenCalledWith("result-a");
    view.unmount();
  });

  it("rejects a queued A modal save after switching to target B", () => {
    canvasHarness.onSave = null;
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const onSubmit = vi.fn();
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={onSubmit}
        enableCanvasDrawing
        backgroundUrl="image-a"
      />,
    );
    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    const saveA = canvasHarness.onSave as ((drawing: CommentCanvasDrawing | null) => void) | null;
    if (!saveA) throw new Error("canvas save callback was not mounted");

    view.rerender(
      <CommentInput
        target={annotationB}
        draftStore={store}
        members={[]}
        onSubmit={onSubmit}
        enableCanvasDrawing
        backgroundUrl="image-b"
      />,
    );
    saveA({ shapes: [{ type: "line", points: [0, 0, 1, 1] }] } as CommentCanvasDrawing);

    expect(store.getDraft(annotationA)?.canvas_drawing).toBeNull();
    expect(store.getDraft(annotationB)?.canvas_drawing).toBeNull();
  });

  it("keeps annotation-only popup adapters usable inside a discussion provider", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput
        annotationId="legacy-annotation"
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="legacy-image"
      />,
    );
    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    expect(view.getByTestId("mock-canvas-save")).toBeVisible();
    fireEvent.click(view.getByTestId("mock-canvas-save"));
    expect(view.getByRole("button", { name: "批注 · 1 条" })).toBeVisible();
  });

  it("autosaves popup drawing drafts and restores them after closing and reopening", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
      />,
    );

    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    const drawing = {
      shapes: [{ type: "line" as const, points: [0.1, 0.2, 0.8, 0.9] }],
    };
    canvasHarness.onDraftChange?.(drawing);
    expect(store.getDraft(annotationA)?.canvas_drawing).toEqual(drawing);

    // A target change closes the popup, but the store retains its composed
    // drawing. Returning and opening a fresh popup hydrates the saved shape.
    view.rerender(
      <CommentInput
        target={annotationB}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-b"
      />,
    );
    view.rerender(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
      />,
    );
    fireEvent.click(view.getByRole("button", { name: /批注/ }));
    expect(canvasHarness.initial).toEqual(drawing);
  });

  it("keeps popup and live drawing mutually exclusive in either direction", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const onStart = vi.fn();
    const onConsume = vi.fn();
    const liveCanvas = {
      active: false,
      result: null,
      onStart,
      onConsume,
    };
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
        liveCanvas={liveCanvas}
      />,
    );

    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    fireEvent.click(view.getByRole("button", { name: "在题图上绘制" }));
    expect(onStart).not.toHaveBeenCalled();

    view.unmount();
    canvasHarness.onSave = null;
    const liveView = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
        liveCanvas={{ ...liveCanvas, active: true }}
      />,
    );
    const popup = liveView.getByRole("button", { name: "弹窗批注" });
    expect(popup).toBeDisabled();
    expect(canvasHarness.onSave).toBeNull();
    liveView.unmount();
  });

  it("blocks rapid duplicate live starts before the host publishes active=true", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const onStart = vi.fn();
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
        liveCanvas={{ active: false, result: null, onStart, onConsume: vi.fn() }}
      />,
    );
    const live = view.getByRole("button", { name: "在题图上绘制" });
    fireEvent.click(live);
    fireEvent.click(live);
    expect(onStart).toHaveBeenCalledTimes(1);
    view.unmount();
  });

  it("releases a stale live guard when switching tasks with an unfinished drawing", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const onStart = vi.fn();
    const view = render(
      <CommentInput
        target={textTask}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
        liveCanvas={{ active: false, result: null, onStart, onConsume: vi.fn() }}
      />,
    );
    fireEvent.click(view.getByRole("button", { name: "在题图上绘制" }));
    expect(onStart).toHaveBeenCalledWith(null, expect.objectContaining({ target: textTask }));

    const taskB: DiscussionTarget = { projectId: "p", taskId: "b", kind: "task" };
    view.rerender(
      <CommentInput
        target={taskB}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-b"
        liveCanvas={{ active: false, result: null, onStart, onConsume: vi.fn() }}
      />,
    );

    fireEvent.click(view.getByRole("button", { name: "在题图上绘制" }));
    expect(onStart).toHaveBeenCalledTimes(2);
    expect(onStart.mock.calls[1][1]?.target).toEqual(taskB);
  });

  it("uses the opening video anchor for a popup draft after playback changes", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const anchorAtOpen = { kind: "video_frame" as const, frameIndex: 12, trackId: "track-a" };
    const anchorAfterPlayback = {
      kind: "video_frame" as const,
      frameIndex: 48,
      trackId: "track-b",
    };
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="frame-12"
        anchor={anchorAtOpen}
      />,
    );
    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    view.rerender(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="frame-48"
        anchor={anchorAfterPlayback}
      />,
    );
    canvasHarness.onDraftChange?.({
      shapes: [{ type: "line", points: [0, 0, 1, 1] }],
    });
    expect(store.getDraft(annotationA)?.anchor).toEqual(anchorAtOpen);
  });

  it("ignores a late popup draft callback from A after switching to B", () => {
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput
        target={annotationA}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-a"
      />,
    );
    fireEvent.click(view.getByRole("button", { name: "弹窗批注" }));
    const lateDraft = canvasHarness.onDraftChange;
    if (!lateDraft) throw new Error("canvas draft callback was not mounted");
    view.rerender(
      <CommentInput
        target={annotationB}
        draftStore={store}
        members={[]}
        onSubmit={vi.fn()}
        enableCanvasDrawing
        backgroundUrl="image-b"
      />,
    );
    lateDraft({ shapes: [{ type: "line", points: [0, 0, 1, 1] }] });
    expect(store.getDraft(annotationA)?.canvas_drawing).toBeNull();
    expect(store.getDraft(annotationB)?.canvas_drawing).toBeNull();
  });

  it("stops a multi-file upload before the next init after its owner expires", async () => {
    let current = true;
    const store = createDiscussionDraftStore({
      owner: { userId: "u1", sessionId: "s1" },
      isOwnerCurrent: () => current,
    });
    const init = vi
      .spyOn(commentsApi, "attachmentUploadInit")
      .mockResolvedValue({ expires_in: 60, storage_key: "k-1", upload_url: "/upload-1" });
    const upload = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      current = false;
      return { ok: true, status: 200 } as Response;
    });
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={vi.fn()} />,
    );
    const files = [
      new File(["a"], "a.png", { type: "image/png" }),
      new File(["b"], "b.png", { type: "image/png" }),
    ];
    fireEvent.change(view.container.querySelector('input[type="file"]')!, {
      target: { files },
    });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(init).toHaveBeenCalledTimes(1));
    current = true;
    expect(store.getDraft(annotationA)?.attachments).toEqual([]);
  });

  it("stops before PUT when the owner expires while upload init is pending", async () => {
    let current = true;
    const store = createDiscussionDraftStore({
      owner: { userId: "u1", sessionId: "s1" },
      isOwnerCurrent: () => current,
    });
    let releaseInit!: (value: {
      expires_in: number;
      storage_key: string;
      upload_url: string;
    }) => void;
    const initResult = new Promise<{
      expires_in: number;
      storage_key: string;
      upload_url: string;
    }>((resolve) => {
      releaseInit = resolve;
    });
    const init = vi.spyOn(commentsApi, "attachmentUploadInit").mockReturnValue(initResult);
    const upload = vi.spyOn(globalThis, "fetch");
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={vi.fn()} />,
    );
    const file = new File(["a"], "a.png", { type: "image/png" });
    fireEvent.change(view.container.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    await waitFor(() => expect(init).toHaveBeenCalledTimes(1));

    current = false;
    await act(async () => {
      releaseInit({ expires_in: 60, storage_key: "k-1", upload_url: "/upload-1" });
      await initResult;
    });
    expect(upload).not.toHaveBeenCalled();
    current = true;
    expect(store.getDraft(annotationA)?.attachments).toEqual([]);
  });

  it("scopes upload disabled state to its target while A is pending", async () => {
    const pendingInit = deferred<{
      expires_in: number;
      storage_key: string;
      upload_url: string;
    }>();
    const init = vi.spyOn(commentsApi, "attachmentUploadInit").mockReturnValue(pendingInit.promise);
    const upload = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    const store = createDiscussionDraftStore({ owner: { userId: "u1", sessionId: "s1" } });
    const view = render(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={vi.fn()} />,
    );
    const file = new File(["a"], "a.png", { type: "image/png" });
    const fileInput = () => view.container.querySelector('input[type="file"]')!;
    expect(fileInput()).toBeEnabled();
    fireEvent.change(fileInput(), { target: { files: [file] } });
    await waitFor(() => expect(init).toHaveBeenCalledTimes(1));
    expect(fileInput()).toBeDisabled();

    view.rerender(
      <CommentInput target={annotationB} draftStore={store} members={[]} onSubmit={vi.fn()} />,
    );
    expect(fileInput()).toBeEnabled();
    view.rerender(
      <CommentInput target={annotationA} draftStore={store} members={[]} onSubmit={vi.fn()} />,
    );
    expect(fileInput()).toBeDisabled();

    pendingInit.resolve({ expires_in: 60, storage_key: "k-1", upload_url: "/upload-1" });
    await waitFor(() => {
      expect(upload).toHaveBeenCalledTimes(1);
      expect(fileInput()).toBeEnabled();
    });
    expect(store.getDraft(annotationA)?.attachments).toHaveLength(1);
  });
});
