import type {
  AnnotationCommentAnchor,
  CommentAttachment,
  CommentCanvasDrawing,
  CommentMention,
} from "@/api/comments";

export interface DiscussionActions {
  edit: boolean;
  change_status: boolean;
  delete: boolean;
  reply: boolean;
}

export type DiscussionReadScope = "all" | "task" | "annotation";
export type DiscussionTarget = {
  projectId: string;
  taskId: string;
} & (
  | { kind: "task" }
  | { kind: "annotation"; annotationId: string }
  | { kind: "issue"; rootIssueId: string }
);

export interface DiscussionSessionOwner {
  sessionId: string;
  userId: string;
}

export interface DiscussionPayload {
  body: string;
  mentions: CommentMention[];
  attachments: CommentAttachment[];
  canvas_drawing: CommentCanvasDrawing | null;
  anchor?: AnnotationCommentAnchor | null;
}

export interface DiscussionOrigin {
  owner: DiscussionSessionOwner;
  target: DiscussionTarget;
  requestId: string;
}

export function discussionTargetKey(target: DiscussionTarget): string {
  return JSON.stringify([
    target.projectId,
    target.taskId,
    target.kind,
    target.kind === "annotation"
      ? target.annotationId
      : target.kind === "issue"
        ? target.rootIssueId
        : null,
  ]);
}

export const discussionKeys = {
  task: (taskId: string) => ["task-discussion", taskId] as const,
  feed: (taskId: string, scope: DiscussionReadScope, annotationId?: string | null, limit = 50) =>
    ["task-discussion", taskId, scope, annotationId ?? null, limit] as const,
  feedbacks: (projectId: string) => ["feedbacks", projectId] as const,
  thread: (rootIssueId: string) => ["feedback-thread", rootIssueId] as const,
};
