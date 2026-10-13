import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import type { ComponentProps } from "react";
import type { TaskDiscussionPage } from "@/api/discussion";
import {
  createDiscussionDraftStore,
  type DiscussionDraftStore,
} from "../state/useDiscussionDraftStore";

const mocks = vi.hoisted(() => {
  const taskQuery = {
    data: undefined as { pages: TaskDiscussionPage[]; pageParams: unknown[] } | undefined,
    isPending: false,
    isError: false,
    error: null as unknown,
    refetch: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
  const legacyQuery = {
    data: undefined as
      | { pages: Array<{ items: never[]; next_cursor: null }>; pageParams: unknown[] }
      | undefined,
    isPending: false,
    isError: false,
    error: null as unknown,
    refetch: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
  return {
    taskQuery,
    legacyQuery,
    useTaskDiscussion: vi.fn(() => taskQuery),
    useAnnotationCommentsInfinite: vi.fn(() => legacyQuery),
    createComment: { mutateAsync: vi.fn(), isPending: false },
    patchComment: { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false },
    deleteComment: { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false },
    createFeedback: { mutateAsync: vi.fn(), isPending: false },
    patchFeedback: { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false },
    deleteFeedback: { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false },
    attachmentDownloadUrl: vi.fn(),
    isCurrentAuthOwner: vi.fn(() => true),
    historyQuery: {
      data: undefined,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    },
    mentionCandidates: {
      data: [] as Array<{
        user_id: string;
        user_name: string;
        user_email?: string | null;
        kind: "member" | "owner" | "super_admin";
      }>,
    },
    store: null as DiscussionDraftStore | null,
    snapshot: null,
  };
});

vi.mock("@/hooks/useTaskDiscussion", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/useTaskDiscussion")>(
    "@/hooks/useTaskDiscussion",
  );
  return { ...actual, useTaskDiscussion: mocks.useTaskDiscussion };
});
vi.mock("@/hooks/useAnnotationComments", () => ({
  useAnnotationCommentsInfinite: mocks.useAnnotationCommentsInfinite,
  useCreateComment: () => mocks.createComment,
  usePatchComment: () => mocks.patchComment,
  useDeleteComment: () => mocks.deleteComment,
}));
vi.mock("@/hooks/useFeedbacks", () => ({
  useCreateFeedback: () => mocks.createFeedback,
  usePatchFeedback: () => mocks.patchFeedback,
  useDeleteFeedback: () => mocks.deleteFeedback,
}));
vi.mock("@/hooks/useProjects", () => ({
  useProjectMentionCandidates: () => mocks.mentionCandidates,
}));
vi.mock("@/api/comments", () => ({
  commentsApi: { attachmentDownloadUrl: mocks.attachmentDownloadUrl },
}));
vi.mock("@/stores/authStore", () => ({
  isCurrentAuthOwner: mocks.isCurrentAuthOwner,
}));
vi.mock("@/hooks/useAnnotationAuditHistory", () => ({
  useAnnotationAuditHistory: () => mocks.historyQuery,
  useTaskAuditHistory: () => mocks.historyQuery,
}));
vi.mock("../state/DiscussionDraftProvider", async () => {
  const { useSyncExternalStore } = await import("react");
  const subscribe = () => () => {};
  const getSnapshot = () => null;
  return {
    useDiscussionDraftStore: () => mocks.store,
    useDiscussionDraftSnapshot: () =>
      useSyncExternalStore(
        mocks.store?.subscribe ?? subscribe,
        mocks.store?.getSnapshot ?? getSnapshot,
      ),
  };
});
vi.mock("../state/useHoveredCommentStore", () => {
  const state = {
    setHover: vi.fn(),
    togglePin: vi.fn(),
    clearPin: vi.fn(),
    pinnedId: null,
    setComposing: vi.fn(),
  };
  return {
    useHoveredCommentStore: (selector: (value: typeof state) => unknown) => selector(state),
  };
});
vi.mock("./CommentInput", () => ({
  CommentInput: ({
    target,
    annotationId,
    taskId,
    targetAvailable,
    busy,
    enableCanvasDrawing,
    backgroundUrl,
    liveCanvas,
    onReturnToTask,
    members,
  }: {
    target?: { kind: string; annotationId?: string };
    annotationId?: string | null;
    taskId?: string | null;
    targetAvailable?: boolean;
    busy?: boolean;
    enableCanvasDrawing?: boolean;
    backgroundUrl?: string | null;
    liveCanvas?: unknown;
    onReturnToTask?: () => void;
    members?: Array<{ id: string; name: string; hint?: string }>;
  }) => (
    <div
      data-testid="mock-composer"
      data-target={target?.kind ?? (annotationId ? "annotation" : "none")}
      data-annotation-id={target?.annotationId ?? annotationId ?? ""}
      data-live-canvas={String(Boolean(liveCanvas))}
      data-task-id={taskId ?? ""}
      data-target-available={targetAvailable === undefined ? "unknown" : String(targetAvailable)}
      data-busy={String(Boolean(busy))}
      data-canvas-enabled={String(Boolean(enableCanvasDrawing))}
      data-background={backgroundUrl ?? ""}
      data-has-return={onReturnToTask ? "true" : "false"}
      data-members={(members ?? []).map((member) => member.id).join(",")}
      data-member-hints={(members ?? [])
        .map((member) => `${member.id}:${member.hint ?? ""}`)
        .join(",")}
    />
  ),
  renderCommentBody: (body: string) => body,
}));
vi.mock("@/components/CanvasDrawingEditor", () => ({
  CanvasDrawingPreview: () => <div data-testid="drawing-preview" />,
}));
vi.mock("@/components/ui/Icon", () => ({ Icon: () => <span aria-hidden="true" /> }));

import { CommentsPanel } from "./CommentsPanel";

it.each([false, true])(
  "returns to the task when selection is cleared (session store: %s)",
  (withStore) => {
    if (withStore) {
      mocks.store = createDiscussionDraftStore({
        owner: { sessionId: "session-a", userId: "user-a" },
      });
      mocks.store.patchDraft(
        {
          projectId: "project-a",
          taskId: "task-a",
          kind: "annotation",
          annotationId: "annotation-a",
        },
        { body: "keep annotation draft" },
      );
    }
    const props = {
      taskId: "task-a",
      projectId: "project-a",
      currentUserId: "user-a",
      annotationClassById: { "annotation-a": "car" },
      backgroundUrl: "/task-a.png",
      enableCanvasDrawing: true,
    };
    const view = renderPanel({ ...props, annotationId: "annotation-a" });
    fireEvent.change(screen.getByRole("combobox", { name: "发送目标" }), {
      target: { value: JSON.stringify(["project-a", "task-a", "annotation", "annotation-a"]) },
    });
    view.rerender(
      <MemoryRouter>
        <CommentsPanel {...props} annotationId={null} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "task");
    expect(screen.getByRole("combobox", { name: "发送目标" })).toHaveValue(
      JSON.stringify(["project-a", "task-a", "task", null]),
    );
    view.rerender(
      <MemoryRouter>
        <CommentsPanel {...props} annotationId="annotation-a" />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("mock-composer")).toHaveAttribute(
      "data-annotation-id",
      "annotation-a",
    );
    if (withStore) {
      expect(
        mocks.store?.getDraft({
          projectId: "project-a",
          taskId: "task-a",
          kind: "annotation",
          annotationId: "annotation-a",
        })?.body,
      ).toBe("keep annotation draft");
    }
  },
);

const annotationData = (id: string, annotationId = "annotation-a") => ({
  id,
  annotation_id: annotationId,
  author_id: "user-a",
  author_name: "Nia Okafor",
  body: `annotation ${id}`,
  is_resolved: false,
  is_active: true,
  mentions: [],
  attachments: [],
  canvas_drawing: null,
  anchor: null,
  created_at: "2026-09-11T10:00:00.000Z",
  updated_at: null,
});

const feedbackData = (id: string) => ({
  id,
  kind: "comment" as const,
  anchor_type: "task" as const,
  project_id: "project-a",
  task_id: "task-a",
  annotation_id: null,
  anchor_position: null,
  status: "open" as const,
  severity: null,
  title: null,
  body: `task ${id}`,
  author_id: "user-b",
  author_name: "Priya Mehta",
  attachments: [],
  canvas_drawing: null,
  thread_parent_id: null,
  is_active: true,
  resolved_at: null,
  resolved_by_id: null,
  created_at: "2026-09-11T09:00:00.000Z",
  updated_at: null,
});

function renderPanel(props: Partial<ComponentProps<typeof CommentsPanel>> = {}) {
  return render(
    <MemoryRouter>
      <CommentsPanel
        annotationId={null}
        taskId="task-a"
        projectId="project-a"
        currentUserId="user-a"
        {...props}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.taskQuery.data = {
    pages: [{ items: [], next_cursor: null, total: 0 }],
    pageParams: [undefined],
  };
  mocks.taskQuery.isPending = false;
  mocks.taskQuery.isError = false;
  mocks.taskQuery.error = null;
  mocks.taskQuery.hasNextPage = false;
  mocks.legacyQuery.data = undefined;
  mocks.mentionCandidates = { data: [] };
  mocks.store = null;
  mocks.snapshot = null;
  mocks.useTaskDiscussion.mockClear();
  mocks.useAnnotationCommentsInfinite.mockClear();
  mocks.taskQuery.refetch.mockClear();
  mocks.createFeedback.mutateAsync.mockReset();
  mocks.createComment.mutateAsync.mockReset();
  mocks.patchFeedback.mutate.mockReset();
  mocks.patchFeedback.mutateAsync.mockReset();
  mocks.patchComment.mutate.mockReset();
  mocks.patchComment.mutateAsync.mockReset();
  mocks.deleteFeedback.mutate.mockReset();
  mocks.deleteFeedback.mutateAsync.mockReset();
  mocks.deleteComment.mutate.mockReset();
  mocks.deleteComment.mutateAsync.mockReset();
  mocks.attachmentDownloadUrl.mockReset();
  mocks.isCurrentAuthOwner.mockReset();
  mocks.isCurrentAuthOwner.mockReturnValue(true);
  mocks.patchComment.mutateAsync.mockResolvedValue(undefined);
  mocks.deleteComment.mutateAsync.mockResolvedValue(undefined);
  mocks.patchFeedback.mutateAsync.mockResolvedValue(undefined);
  mocks.deleteFeedback.mutateAsync.mockResolvedValue(undefined);
  mocks.createComment.isPending = false;
  mocks.createFeedback.isPending = false;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("CommentsPanel discussion feed", () => {
  it("passes image task drawing capability and renders task drawing previews", () => {
    const taskDrawing = { shapes: [{ type: "line" as const, points: [0, 0, 1, 1] }] };
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "feedback",
              data: { ...feedbackData("task-drawing"), canvas_drawing: taskDrawing },
              actions: { edit: false, delete: false, change_status: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 1,
        },
      ],
      pageParams: [undefined],
    };
    renderPanel({
      enableCanvasDrawing: true,
      enableTaskCanvasDrawing: true,
      backgroundUrl: "/task-a.png",
    });

    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-canvas-enabled", "true");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-background", "/task-a.png");
    expect(screen.getByTestId("drawing-preview")).toBeInTheDocument();
  });

  it("labels owner, super admin and member @ mention candidates", () => {
    mocks.mentionCandidates = {
      data: [
        {
          user_id: "user-owner",
          user_name: "Owner Name",
          user_email: "owner@example.com",
          kind: "owner",
        },
        {
          user_id: "user-root",
          user_name: "Root Admin",
          user_email: "root@example.com",
          kind: "super_admin",
        },
        {
          user_id: "user-b",
          user_name: "Priya Mehta",
          user_email: "priya@example.com",
          kind: "member",
        },
      ],
    };
    renderPanel();

    const composer = screen.getByTestId("mock-composer");
    expect(composer).toHaveAttribute("data-members", "user-owner,user-root,user-b");
    expect(composer).toHaveAttribute(
      "data-member-hints",
      "user-owner:项目负责人,user-root:超级管理员,user-b:",
    );
  });

  it("admits a new verified comment focus before falling back from a cleared selection", () => {
    const props = {
      annotationId: "annotation-a",
      taskId: "task-a",
      projectId: "project-a",
      currentUserId: "user-a",
    };
    const view = renderPanel(props);
    fireEvent.change(screen.getByRole("combobox", { name: "评论阅读范围" }), {
      target: { value: "annotation" },
    });
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: annotationData("focused", "annotation-b"),
              actions: { edit: false, delete: false, change_status: false, reply: false },
            },
          ],
          total: 1,
          next_cursor: null,
        },
      ],
      pageParams: [null],
    };
    view.rerender(
      <MemoryRouter>
        <CommentsPanel
          {...props}
          annotationId={null}
          commentFocus={{
            requestId: "new-focus",
            annotationId: "annotation-b",
            commentId: "focused",
            annotationLabel: "car",
            canvasAvailable: false,
          }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("combobox", { name: "评论阅读范围" })).toHaveValue("annotation");
    expect(mocks.useTaskDiscussion).toHaveBeenLastCalledWith(
      "task-a",
      "annotation",
      "annotation-b",
      true,
      "project-a",
    );
    expect(
      document.querySelector('[data-comment-key="annotation_comment:focused"]'),
    ).toHaveAttribute("aria-current", "true");
  });

  it("opens an original annotation comment without changing the task composer or focusing an equal feedback id", () => {
    const actions = { edit: false, delete: false, change_status: false, reply: false };
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            { source: "feedback", data: feedbackData("same"), actions },
            { source: "annotation_comment", data: annotationData("same", "unloaded"), actions },
          ],
          next_cursor: null,
          total: 2,
        },
      ],
      pageParams: [undefined],
    };
    const onCommentFocusHandled = vi.fn();
    const props = {
      annotationId: null,
      taskId: "task-a",
      projectId: "project-a",
      currentUserId: "user-a",
      commentFocus: {
        requestId: "request",
        annotationId: "unloaded",
        commentId: "same",
        annotationLabel: "车辆",
        canvasAvailable: false,
      },
      onCommentFocusHandled,
    };
    const view = renderPanel(props);
    expect(mocks.useTaskDiscussion).toHaveBeenLastCalledWith(
      "task-a",
      "annotation",
      "unloaded",
      true,
      "project-a",
    );
    expect(screen.getByRole("combobox", { name: "评论阅读范围" })).toHaveValue("annotation");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "task");
    const row = document.querySelector('[data-comment-key="annotation_comment:same"]');
    expect(row).toHaveFocus();
    expect(row).toHaveAttribute("aria-current", "true");
    expect(document.querySelector('[data-comment-key="feedback:same"]')).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.getByText(/不会自动认领视频分段/)).toBeInTheDocument();
    expect(onCommentFocusHandled).toHaveBeenCalledOnce();
    expect(onCommentFocusHandled).toHaveBeenCalledWith("request");
    screen.getByRole("combobox", { name: "发送目标" }).focus();
    view.rerender(
      <MemoryRouter>
        <CommentsPanel {...props} commentFocus={null} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("combobox", { name: "发送目标" })).toHaveFocus();
    expect(onCommentFocusHandled).toHaveBeenCalledTimes(1);
    view.rerender(
      <MemoryRouter>
        <CommentsPanel {...props} taskId="task-b" commentFocus={null} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("combobox", { name: "评论阅读范围" })).toHaveValue("all");
    expect(screen.queryByText(/不会自动认领视频分段/)).toBeNull();
    expect(row).not.toHaveAttribute("aria-current");
  });

  it("无标注时默认读取全部讨论并提供任务纯文本 composer", () => {
    renderPanel();

    expect(screen.getByRole("combobox", { name: "评论阅读范围" })).toHaveValue("all");
    expect(screen.getByRole("option", { name: "本任务全部讨论" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "仅任务留言" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "当前标注" })).not.toBeInTheDocument();
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "task");
    expect(screen.getByText("暂无讨论")).toBeInTheDocument();
    expect(mocks.useAnnotationCommentsInfinite).toHaveBeenCalledWith(null);
    expect(mocks.useTaskDiscussion).toHaveBeenLastCalledWith(
      "task-a",
      "all",
      null,
      true,
      "project-a",
    );
  });

  it("阅读范围和发送目标彼此独立，自动路由后可回退到任务", () => {
    renderPanel({
      annotationId: "annotation-a",
      annotationClassById: { "annotation-a": "person" },
    });

    fireEvent.change(screen.getByRole("combobox", { name: "评论阅读范围" }), {
      target: { value: "annotation" },
    });
    expect(mocks.useTaskDiscussion).toHaveBeenLastCalledWith(
      "task-a",
      "annotation",
      "annotation-a",
      true,
      "project-a",
    );
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "annotation");

    fireEvent.change(screen.getByRole("combobox", { name: "发送目标" }), {
      target: { value: JSON.stringify(["project-a", "task-a", "task", null]) },
    });
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "task");
  });

  it("显式选择旧目标后，该标注移除时禁用发送并保留返回任务入口", () => {
    mocks.store = createDiscussionDraftStore({
      owner: { sessionId: "session-a", userId: "user-a" },
    });
    const view = renderPanel({
      annotationId: "annotation-b",
      annotationClassById: { "annotation-a": "person", "annotation-b": "car" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "发送目标" }), {
      target: { value: JSON.stringify(["project-a", "task-a", "annotation", "annotation-a"]) },
    });

    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "annotation");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-task-id", "task-a");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target-available", "true");

    view.rerender(
      <MemoryRouter>
        <CommentsPanel
          annotationId="annotation-b"
          taskId="task-a"
          projectId="project-a"
          currentUserId="user-a"
          annotationClassById={{ "annotation-b": "car" }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "annotation");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target-available", "false");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-has-return", "true");
  });

  it("lists same-class instances and follows selection while preserving task fallback across remounts", () => {
    mocks.store = createDiscussionDraftStore({
      owner: { sessionId: "session-a", userId: "user-a" },
    });
    const props = {
      taskId: "task-a",
      projectId: "project-a",
      annotationClassById: { "aaaaaaaa-1": "bus", "bbbbbbbb-2": "bus" },
      liveCanvas: { active: false, result: null, onStart: vi.fn(), onConsume: vi.fn() },
    };
    const view = renderPanel({ ...props, annotationId: "aaaaaaaa-1" });
    const targets = screen.getByRole("combobox", { name: "发送目标" });
    expect(
      within(targets).getByRole("option", { name: "当前标注 · bus · aaaaaaaa" }),
    ).toBeInTheDocument();
    expect(
      within(targets).getByRole("option", { name: "标注 · bus · bbbbbbbb" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-annotation-id", "aaaaaaaa-1");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-live-canvas", "true");
    fireEvent.change(targets, {
      target: { value: JSON.stringify(["project-a", "task-a", "task", null]) },
    });
    view.unmount();
    const returned = renderPanel({ ...props, annotationId: "aaaaaaaa-1" });
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-target", "task");
    returned.rerender(
      <MemoryRouter>
        <CommentsPanel {...props} annotationId="bbbbbbbb-2" />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-annotation-id", "bbbbbbbb-2");
    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-live-canvas", "true");
  });

  it("候选或多选 ID 不会触发 annotation endpoint，并给出范围回退说明", () => {
    const view = renderPanel({
      annotationId: "annotation-a",
      annotationClassById: { "annotation-a": "person" },
    });
    const scope = screen.getByRole("combobox", { name: "评论阅读范围" });
    fireEvent.change(scope, { target: { value: "annotation" } });

    view.rerender(
      <MemoryRouter>
        <CommentsPanel
          annotationId="prediction-candidate-1"
          taskId="task-a"
          projectId="project-a"
          currentUserId="user-a"
          annotationClassById={{}}
        />
      </MemoryRouter>,
    );

    expect(mocks.useAnnotationCommentsInfinite).toHaveBeenCalledWith(null);
    expect(screen.getByRole("combobox", { name: "评论阅读范围" })).toHaveValue("task");
    expect(screen.getByText("当前没有可用的已保存标注，已切换为仅任务留言。")).toBeInTheDocument();
    expect(
      within(screen.getByRole("combobox", { name: "评论阅读范围" })).queryByRole("option", {
        name: "当前标注",
      }),
    ).not.toBeInTheDocument();
    expect(mocks.useTaskDiscussion).toHaveBeenLastCalledWith(
      "task-a",
      "task",
      null,
      true,
      "project-a",
    );
  });

  it("按 source+id 保留混合来源，即使两个来源 UUID 相同", () => {
    const sameId = "same-id";
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: annotationData(sameId),
              actions: { edit: true, change_status: true, delete: true, reply: false },
            },
            {
              source: "feedback",
              data: feedbackData(sameId),
              actions: { edit: false, change_status: false, delete: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 2,
        },
      ],
      pageParams: [undefined],
    };
    renderPanel({ annotationClassById: { "annotation-a": "person" } });

    expect(screen.getByText("评论 (2)")).toBeInTheDocument();
    expect(screen.getAllByTestId("discussion-comment-row")).toHaveLength(2);
    expect(screen.getAllByTestId("discussion-source-chip").map((node) => node.textContent)).toEqual(
      ["标注评论", "任务留言"],
    );
    expect(screen.getAllByText("same-id", { exact: false })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "删除评论" })).toHaveLength(1);
  });

  it("附件操作使用行数据的 annotation_id，任务 feedback 附件明确不可下载", async () => {
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: {
                ...annotationData("comment-a", "annotation-row"),
                attachments: [
                  {
                    storageKey: "comment-attachments/annotation-row/a.txt",
                    fileName: "a.txt",
                    mimeType: "text/plain",
                    size: 4,
                  },
                ],
              },
              actions: { edit: false, change_status: false, delete: false, reply: false },
            },
            {
              source: "feedback",
              data: {
                ...feedbackData("feedback-a"),
                attachments: [{ storage_key: "task-feedback/a.txt", file_name: "a.txt", size: 4 }],
              },
              actions: { edit: false, change_status: false, delete: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 2,
        },
      ],
      pageParams: [undefined],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: vi.fn().mockResolvedValue(new Blob(["attachment"], { type: "text/plain" })),
    });
    const createObjectURL = vi.fn(() => "blob:comment-attachment");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mocks.attachmentDownloadUrl.mockResolvedValue({ download_url: "https://signed.example/a.txt" });
    renderPanel({ annotationId: "annotation-current" });

    const row = screen.getAllByTestId("discussion-comment-row")[0];
    fireEvent.click(within(row).getByRole("button", { name: /a\.txt/ }));
    await waitFor(() =>
      expect(mocks.attachmentDownloadUrl).toHaveBeenCalledWith(
        "annotation-row",
        "comment-attachments/annotation-row/a.txt",
      ),
    );
    expect(fetchMock).toHaveBeenCalledWith("https://signed.example/a.txt", {
      credentials: "omit",
    });
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:comment-attachment");
    expect(screen.getByText("a.txt（暂不支持下载）")).toBeInTheDocument();
  });

  it("附件签名地址失败时在对应卡片显示错误，并保留重试入口", async () => {
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: {
                ...annotationData("comment-a", "annotation-row"),
                attachments: [
                  {
                    storageKey: "comment-attachments/annotation-row/a.txt",
                    fileName: "a.txt",
                    mimeType: "text/plain",
                    size: 4,
                  },
                ],
              },
              actions: { edit: false, change_status: false, delete: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 1,
        },
      ],
      pageParams: [undefined],
    };
    const error = Object.assign(new Error("无权限"), { status: 403 });
    mocks.attachmentDownloadUrl.mockRejectedValueOnce(error);
    renderPanel({ annotationId: "annotation-current" });

    const button = screen.getByRole("button", { name: /a\.txt/ });
    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.getByTestId("discussion-attachment-error")).toHaveTextContent("403"),
    );
    expect(button).toBeEnabled();
  });

  it("签名 URL 返回 503 时在对应卡片显示错误且允许重试", async () => {
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: {
                ...annotationData("comment-a", "annotation-row"),
                attachments: [
                  {
                    storageKey: "comment-attachments/annotation-row/a.txt",
                    fileName: "a.txt",
                    mimeType: "text/plain",
                    size: 4,
                  },
                ],
              },
              actions: { edit: false, change_status: false, delete: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 1,
        },
      ],
      pageParams: [undefined],
    };
    mocks.attachmentDownloadUrl.mockResolvedValue({ download_url: "https://signed.example/a.txt" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503, blob: vi.fn() }));
    renderPanel({ annotationId: "annotation-current" });

    const button = screen.getByRole("button", { name: /a\.txt/ });
    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.getByTestId("discussion-attachment-error")).toHaveTextContent("503"),
    );
    expect(button).toBeEnabled();
  });

  it("账号会话在签名接口返回前变更时不再请求文件或触发浏览器下载", async () => {
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: {
                ...annotationData("comment-a", "annotation-row"),
                attachments: [
                  {
                    storageKey: "comment-attachments/annotation-row/a.txt",
                    fileName: "a.txt",
                    mimeType: "text/plain",
                    size: 4,
                  },
                ],
              },
              actions: { edit: false, change_status: false, delete: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 1,
        },
      ],
      pageParams: [undefined],
    };
    let resolveSigned: (value: { download_url: string }) => void = () => {};
    mocks.attachmentDownloadUrl.mockReturnValueOnce(
      new Promise<{ download_url: string }>((resolve) => {
        resolveSigned = resolve;
      }),
    );
    const fetchMock = vi.fn();
    const createObjectURL = vi.fn();
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL: vi.fn() });
    renderPanel({ annotationId: "annotation-current" });

    fireEvent.click(screen.getByRole("button", { name: /a\.txt/ }));
    await waitFor(() => expect(mocks.attachmentDownloadUrl).toHaveBeenCalledTimes(1));
    mocks.isCurrentAuthOwner.mockReturnValue(false);
    resolveSigned({ download_url: "https://signed.example/a.txt" });
    await waitFor(() => expect(fetchMock).not.toHaveBeenCalled());

    expect(createObjectURL).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("提供 session draft store 的 legacy annotation composer 不受全局 mutation pending 阻塞", () => {
    (mocks as { store: object | null }).store = {};
    mocks.createComment.isPending = true;

    renderPanel({
      annotationId: "annotation-a",
      taskId: null,
      annotationTaskId: "task-a",
      annotationClassById: { "annotation-a": "person" },
    });

    expect(screen.getByTestId("mock-composer")).toHaveAttribute("data-busy", "false");
  });

  it("服务端 actions 关闭时不渲染修改操作，并按来源路由状态/删除", async () => {
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "annotation_comment",
              data: annotationData("comment-a"),
              actions: { edit: false, change_status: true, delete: true, reply: false },
            },
            {
              source: "feedback",
              data: feedbackData("feedback-a"),
              actions: { edit: false, change_status: true, delete: true, reply: false },
            },
          ],
          next_cursor: null,
          total: 2,
        },
      ],
      pageParams: [undefined],
    };
    renderPanel();

    const rows = screen.getAllByTestId("discussion-comment-row");
    fireEvent.click(within(rows[0]).getByRole("button", { name: "标为已解决" }));
    await waitFor(() => expect(mocks.patchComment.mutateAsync).toHaveBeenCalled());
    fireEvent.click(within(rows[0]).getByRole("button", { name: "删除评论" }));
    fireEvent.click(within(rows[1]).getByRole("button", { name: "标为已解决" }));
    await waitFor(() => expect(mocks.patchFeedback.mutateAsync).toHaveBeenCalled());
    fireEvent.click(within(rows[1]).getByRole("button", { name: "删除评论" }));

    expect(mocks.patchComment.mutateAsync).toHaveBeenCalledWith({
      id: "comment-a",
      payload: { is_resolved: true },
      annotationId: "annotation-a",
      taskId: "task-a",
    });
    expect(mocks.deleteComment.mutateAsync).toHaveBeenCalledWith({
      id: "comment-a",
      annotationId: "annotation-a",
      taskId: "task-a",
    });
    expect(mocks.patchFeedback.mutateAsync).toHaveBeenCalledWith({
      id: "feedback-a",
      payload: { status: "resolved" },
    });
    expect(mocks.deleteFeedback.mutateAsync).toHaveBeenCalledWith("feedback-a");
  });

  it("状态操作失败时在对应卡片显示错误，而不是静默消失", async () => {
    mocks.patchFeedback.mutateAsync.mockRejectedValueOnce(new Error("权限不足"));
    mocks.taskQuery.data = {
      pages: [
        {
          items: [
            {
              source: "feedback",
              data: feedbackData("feedback-a"),
              actions: { edit: false, change_status: true, delete: false, reply: false },
            },
          ],
          next_cursor: null,
          total: 1,
        },
      ],
      pageParams: [undefined],
    };
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "标为已解决" }));
    await waitFor(() =>
      expect(screen.getByTestId("discussion-row-error")).toHaveTextContent("权限不足"),
    );
    expect(screen.getByRole("button", { name: "标为已解决" })).toBeEnabled();
  });

  it("错误状态提供重试且不伪装成空结果", () => {
    mocks.taskQuery.isError = true;
    mocks.taskQuery.error = new Error("offline");
    renderPanel();

    expect(screen.getByRole("alert")).toHaveTextContent("无法加载讨论");
    expect(screen.queryByText("暂无讨论")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.taskQuery.refetch).toHaveBeenCalledTimes(1);
  });
});
