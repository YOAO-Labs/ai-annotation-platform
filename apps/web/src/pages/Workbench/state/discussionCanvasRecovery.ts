import type { CommentCanvasDrawing } from "@/api/comments";
import type { DiscussionOrigin } from "./discussionTypes";

export const CANVAS_RECOVERY_TTL_MS = 5 * 60 * 1000;
const PREFIX = "canvas_draft:v2:";
const CURRENT_SCHEMA_VERSION = 3;

export interface CanvasRecoveryScope {
  userId: string;
  projectId: string;
  taskId: string;
}

export interface CanvasRecoveryRecord extends CanvasRecoveryScope {
  schemaVersion: 2 | 3;
  targetKind: "task" | "annotation";
  annotationId: string | null;
  shapes: NonNullable<CommentCanvasDrawing["shapes"]>;
  ts: number;
}

export function canvasRecoveryKey(scope: CanvasRecoveryScope, annotationId: string | null): string {
  return PREFIX + JSON.stringify([scope.userId, scope.projectId, scope.taskId, annotationId]);
}

function storageKeys(): string[] {
  return Array.from({ length: sessionStorage.length }, (_, index) =>
    sessionStorage.key(index),
  ).filter((key): key is string => key !== null);
}

function validShapes(value: unknown): value is CanvasRecoveryRecord["shapes"] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every(
      (shape) =>
        shape &&
        typeof shape === "object" &&
        ["line", "arrow", "rect", "ellipse"].includes(
          (shape as { type?: unknown }).type as string,
        ) &&
        Array.isArray((shape as { points?: unknown }).points) &&
        (shape as { points: unknown[] }).points.length >= 4 &&
        (shape as { points: unknown[] }).points.length % 2 === 0 &&
        (shape as { points: unknown[] }).points.every((point) => Number.isFinite(point)),
    )
  );
}

function validRecord(value: unknown): value is CanvasRecoveryRecord {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<CanvasRecoveryRecord> & { schemaVersion?: unknown };
  if (
    ![record.userId, record.projectId, record.taskId].every(
      (id) => typeof id === "string" && id.length > 0,
    ) ||
    !Number.isFinite(record.ts) ||
    !validShapes(record.shapes)
  )
    return false;
  if (record.schemaVersion === 2) {
    return typeof record.annotationId === "string" && record.annotationId.length > 0;
  }
  return (
    record.schemaVersion === CURRENT_SCHEMA_VERSION &&
    (record.targetKind === "task" || record.targetKind === "annotation") &&
    (record.targetKind === "task"
      ? record.annotationId === null
      : typeof record.annotationId === "string" && record.annotationId.length > 0)
  );
}

function normalizeRecord(value: CanvasRecoveryRecord): CanvasRecoveryRecord {
  return value.schemaVersion === 2 ? { ...value, targetKind: "annotation" } : value;
}

export function readCanvasDraftRecovery(scope: CanvasRecoveryScope): CanvasRecoveryRecord[] {
  try {
    const legacyKey = `canvas_draft:${scope.taskId}`;
    const legacy = sessionStorage.getItem(legacyKey);
    if (legacy) {
      try {
        const value = JSON.parse(legacy) as { ts?: number };
        if (typeof value.ts === "number" && Date.now() - value.ts > CANVAS_RECOVERY_TTL_MS) {
          sessionStorage.removeItem(legacyKey);
        }
      } catch {
        /* Leave unknown legacy content untouched. */
      }
    }
    const records: CanvasRecoveryRecord[] = [];
    for (const key of storageKeys()) {
      if (!key.startsWith(PREFIX)) continue;
      let value: unknown;
      try {
        value = JSON.parse(sessionStorage.getItem(key) ?? "null");
      } catch {
        continue;
      }
      if (!validRecord(value)) continue;
      const record = normalizeRecord(value);
      if (canvasRecoveryKey(record, record.annotationId) !== key) continue;
      if (
        record.userId !== scope.userId ||
        record.projectId !== scope.projectId ||
        record.taskId !== scope.taskId
      )
        continue;
      const age = Date.now() - record.ts;
      if (age > CANVAS_RECOVERY_TTL_MS || age < 0) {
        sessionStorage.removeItem(key);
        continue;
      }
      records.push(record);
    }
    return records.sort((a, b) => b.ts - a.ts);
  } catch {
    return [];
  }
}

export function writeCanvasDraftRecovery(
  origin: DiscussionOrigin,
  drawing: CommentCanvasDrawing,
): void {
  if (origin.target.kind !== "annotation" && origin.target.kind !== "task") return;
  const scope = {
    userId: origin.owner.userId,
    projectId: origin.target.projectId,
    taskId: origin.target.taskId,
  };
  const targetKind = origin.target.kind;
  const annotationId = targetKind === "annotation" ? origin.target.annotationId : null;
  const record: CanvasRecoveryRecord = {
    ...scope,
    schemaVersion: CURRENT_SCHEMA_VERSION,
    targetKind,
    annotationId,
    shapes: drawing.shapes ?? [],
    ts: Date.now(),
  };
  try {
    const key = canvasRecoveryKey(scope, annotationId);
    if (!record.shapes.length) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(record));
  } catch {
    /* In-memory drafts remain available when storage is blocked/full. */
  }
}

export function clearCanvasDraftRecovery(origin: DiscussionOrigin): void {
  if (origin.target.kind !== "annotation" && origin.target.kind !== "task") return;
  try {
    sessionStorage.removeItem(
      canvasRecoveryKey(
        {
          userId: origin.owner.userId,
          projectId: origin.target.projectId,
          taskId: origin.target.taskId,
        },
        origin.target.kind === "annotation" ? origin.target.annotationId : null,
      ),
    );
  } catch {
    /* Storage may be disabled. */
  }
}

export function clearUserCanvasDraftRecovery(userId: string): void {
  try {
    for (const key of storageKeys()) {
      if (!key.startsWith(PREFIX)) continue;
      try {
        const tuple: unknown = JSON.parse(key.slice(PREFIX.length));
        if (Array.isArray(tuple) && tuple.length === 4 && tuple[0] === userId)
          sessionStorage.removeItem(key);
      } catch {
        /* Do not guess the owner of malformed keys. */
      }
    }
  } catch {
    /* Storage may be disabled. */
  }
}
