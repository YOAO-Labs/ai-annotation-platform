import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import type { useAcceptPrediction, useRejectPrediction } from "@/hooks/usePredictions";
import { isCurrentAuthOwner } from "@/stores/authStore";
import { aiBoxOnFrame } from "../../stage/aiBoxFrames";
import type { AiBox } from "../../state/transforms";
import type { useAnnotationHistory } from "../../state/useAnnotationHistory";
import type { useWorkbenchState } from "../../state/useWorkbenchState";

export type PredictionDecisionResult = {
  status: "success" | "failed" | "awaiting-class" | "cancelled" | "ignored";
};

interface Args {
  taskId: string | undefined;
  projectId?: string | undefined;
  videoSegmentId?: string | null;
  meUserId?: string | null;
  s: ReturnType<typeof useWorkbenchState>;
  aiBoxes: AiBox[];
  acceptedShapeKeys: ReadonlySet<string>;
  isLocked: boolean;
  accept: ReturnType<typeof useAcceptPrediction>["mutateAsync"];
  reject: ReturnType<typeof useRejectPrediction>["mutateAsync"];
  history: ReturnType<typeof useAnnotationHistory>;
  pushToast: (toast: { msg: string; sub?: string; kind: "success" | "warning" | "error" }) => void;
  recordRecentClass: (cls: string) => void;
  dismiss: (id: string) => void;
}

interface Decision {
  box: AiBox;
  kind: "accept" | "reject";
  attributes?: Record<string, unknown>;
  bulk?: boolean;
  pickerSeen?: boolean;
  selection: object;
  nextIds: string[];
  status: "pending" | "awaiting-class" | "success";
  promise?: Promise<PredictionDecisionResult>;
}

/** Own ordinary prediction writes, class retries and the selection they may advance. */
export function usePredictionDecisions(args: Args) {
  const { taskId, projectId, videoSegmentId, meUserId, s } = args;
  // A task may be revisited before its write or canonical query finishes. Include the
  // project and account so a retained task id cannot reuse another owner's decisions.
  const taskDecisions = useRef(new Map<string, Map<string, Decision>>());
  const ownerKey = JSON.stringify([
    taskId ?? null,
    projectId ?? null,
    videoSegmentId ?? null,
    meUserId ?? null,
  ]);
  const owner = useMemo(() => {
    let decisions = taskDecisions.current.get(ownerKey);
    if (!decisions) {
      decisions = new Map<string, Decision>();
      taskDecisions.current.set(ownerKey, decisions);
    }
    return { taskId, projectId, videoSegmentId, meUserId, active: true, decisions };
  }, [meUserId, ownerKey, projectId, taskId, videoSegmentId]);
  // Identity changes even when the user leaves and returns to the same selection/frame/task.
  const selection = useMemo(
    () => ({ owner, id: s.selectedId, frame: s.videoFrameIndex }),
    [owner, s.selectedId, s.videoFrameIndex],
  );
  const latest = useRef({ args, owner, selection });
  useLayoutEffect(() => {
    latest.current = { args, owner, selection };
    for (const [id, decision] of owner.decisions) {
      const ownsPicker =
        s.editingClass?.accept?.predictionId === decision.box.predictionId &&
        s.editingClass?.accept?.shapeIndex === decision.box.shapeIndex;
      if (decision.status === "awaiting-class" && ownsPicker) decision.pickerSeen = true;
      if (
        decision.status === "success" &&
        (args.acceptedShapeKeys.has(id) ||
          args.acceptedShapeKeys.has(`pred-${decision.box.predictionId}-*`))
      ) {
        // Canonical query data has consumed it; a later undo can make it reviewable again.
        owner.decisions.delete(id);
      }
      if (
        decision.status === "awaiting-class" &&
        (selection !== decision.selection || (decision.pickerSeen && !ownsPicker))
      ) {
        owner.decisions.delete(id);
        if (
          s.editingClass?.accept?.predictionId === decision.box.predictionId &&
          s.editingClass.accept.shapeIndex === decision.box.shapeIndex
        )
          s.setEditingClass(null);
      }
    }
  });
  useLayoutEffect(() => {
    owner.active = true;
    return () => {
      owner.active = false;
      const current = latest.current;
      const editing = current.args.s.editingClass?.accept;
      if (
        current.owner === owner &&
        editing &&
        [...owner.decisions.values()].some(
          (decision) =>
            decision.status === "awaiting-class" &&
            decision.box.predictionId === editing.predictionId &&
            decision.box.shapeIndex === editing.shapeIndex,
        )
      )
        current.args.s.setEditingClass(null);
      for (const [id, decision] of owner.decisions) {
        if (decision.status === "awaiting-class") owner.decisions.delete(id);
      }
    };
  }, [owner]);

  const isCurrentOwner = useCallback(
    () =>
      owner.active &&
      latest.current.owner === owner &&
      (!owner.meUserId || isCurrentAuthOwner(owner.meUserId)),
    [owner],
  );

  const run = useCallback(
    async (
      decision: Decision,
      kind: "accept" | "reject",
      overrideClassName?: string,
    ): Promise<PredictionDecisionResult> => {
      const currentTask = isCurrentOwner;
      const currentSelection = () =>
        currentTask() && latest.current.selection === decision.selection;
      const { box } = decision;
      try {
        if (kind === "accept") {
          const created = await args.accept({
            predictionId: box.predictionId,
            shapeIndex: box.shapeIndex,
            attributeOverrides: decision.attributes,
            overrideClassName,
          });
          decision.status = "success";
          if (!currentTask()) return { status: "cancelled" };
          args.history.push({
            kind: "acceptPrediction",
            predictionId: box.predictionId,
            createdAnnotationIds: created.map((ann) => ann.id),
          });
        } else {
          await args.reject({ predictionId: box.predictionId, shapeIndex: box.shapeIndex });
          decision.status = "success";
          if (!currentTask()) return { status: "cancelled" };
        }
        decision.status = "success";
        if (kind === "reject") args.dismiss(box.id);
        if (
          !decision.bulk &&
          currentSelection() &&
          s.selectedId === box.id &&
          aiBoxOnFrame(box, s.videoFrameIndex)
        ) {
          const now = latest.current.args;
          const nextId = now.s.workbenchConfig.common.autoAdvanceOnDecide
            ? (decision.nextIds.find(
                (id) => now.aiBoxes.some((b) => b.id === id) && !owner.decisions.has(id),
              ) ?? null)
            : null;
          now.s.setSelectedId(nextId);
        }
        if (!decision.bulk)
          args.pushToast({
            msg: kind === "accept" ? "已采纳 AI 标注" : "已忽略 AI 候选",
            kind: "success",
          });
        return { status: "success" };
      } catch (error) {
        if (!currentTask()) {
          owner.decisions.delete(box.id);
          return { status: "cancelled" };
        }
        if (
          !decision.bulk &&
          kind === "accept" &&
          (error as { status?: number } | null)?.status === 422 &&
          currentSelection()
        ) {
          decision.status = "awaiting-class";
          // Mutation notifications can render the old picker state before setEditingClass commits.
          decision.pickerSeen = false;
          s.setEditingClass({
            annotationId: "",
            geom: { x: box.x, y: box.y, w: box.w, h: box.h },
            currentClass: box.cls,
            anchor: box.geometry?.type.startsWith("video_")
              ? { left: Math.max(16, window.innerWidth - 340), top: 96 }
              : undefined,
            accept: {
              predictionId: box.predictionId,
              shapeIndex: box.shapeIndex,
              toolUnitId: box.tool_unit_id ?? undefined,
            },
          });
          args.pushToast({
            msg: "该类别不在项目标签集",
            sub: `请为模型类别「${box.cls}」选择对应的项目标签`,
            kind: "warning",
          });
          return { status: "awaiting-class" };
        }
        owner.decisions.delete(box.id);
        if (!decision.bulk)
          args.pushToast({
            msg: kind === "accept" ? "采纳失败" : "忽略失败",
            sub: error instanceof Error ? error.message : "请稍后重试",
            kind: "error",
          });
        return { status: "failed" };
      }
    },
    [args, isCurrentOwner, owner, s],
  );

  const decide = useCallback(
    (
      box: AiBox,
      kind: "accept" | "reject",
      attributes?: Record<string, unknown>,
      bulk = false,
    ): Promise<PredictionDecisionResult> => {
      const existing = owner.decisions.get(box.id);
      if (existing && existing.kind !== kind) return Promise.resolve({ status: "ignored" });
      if (existing)
        return (
          existing.promise ??
          Promise.resolve({
            status: existing.status === "awaiting-class" ? "awaiting-class" : "ignored",
          })
        );
      if (!isCurrentOwner() || !taskId || args.isLocked || !box.predictionId)
        return Promise.resolve({ status: "ignored" });
      const scoped = args.aiBoxes.filter((b) => bulk || aiBoxOnFrame(b, s.videoFrameIndex));
      const index = scoped.findIndex((b) => b.id === box.id);
      // Explicit all-frame list buttons retain their scope; hotkeys provide current-frame boxes.
      if (!args.aiBoxes.some((b) => b.id === box.id)) return Promise.resolve({ status: "ignored" });
      const decision: Decision = {
        box,
        kind,
        attributes,
        selection,
        bulk,
        nextIds:
          index < 0
            ? []
            : [...scoped.slice(index + 1), ...scoped.slice(0, index).reverse()].map((b) => b.id),
        status: "pending",
      };
      owner.decisions.set(box.id, decision);
      decision.promise = run(decision, kind);
      return decision.promise;
    },
    [args, isCurrentOwner, owner, run, s.videoFrameIndex, selection, taskId],
  );

  const acceptPrediction = useCallback(
    (box: AiBox, attributes?: Record<string, unknown>) => decide(box, "accept", attributes),
    [decide],
  );
  const rejectPrediction = useCallback((box: AiBox) => decide(box, "reject"), [decide]);
  const acceptAll = useCallback(
    async (boxes: AiBox[]) => {
      if (!isCurrentOwner()) return null;
      const results: PredictionDecisionResult[] = [];
      // These writes share one task's lock and annotation version.
      for (const box of boxes) {
        if (!isCurrentOwner()) return null;
        results.push(await decide(box, "accept", undefined, true));
      }
      return isCurrentOwner() ? results : null;
    },
    [decide, isCurrentOwner],
  );
  const commitClass = useCallback(
    (cls: string): Promise<PredictionDecisionResult> => {
      const editing = s.editingClass?.accept;
      const decision = [...owner.decisions.values()].find(
        (item) =>
          item.box.predictionId === editing?.predictionId &&
          item.box.shapeIndex === editing?.shapeIndex,
      );
      if (
        !decision ||
        decision.status !== "awaiting-class" ||
        !isCurrentOwner() ||
        latest.current.selection !== decision.selection ||
        args.isLocked
      ) {
        return Promise.resolve({ status: "ignored" });
      }
      decision.status = "pending";
      s.setEditingClass(null);
      s.setActiveClass(cls);
      args.recordRecentClass(cls);
      decision.promise = run(decision, "accept", cls);
      return decision.promise;
    },
    [args, isCurrentOwner, owner, run, s],
  );
  const cancelClass = useCallback(() => {
    const editing = s.editingClass?.accept;
    for (const [id, decision] of owner.decisions) {
      if (
        decision.status === "awaiting-class" &&
        decision.box.predictionId === editing?.predictionId &&
        decision.box.shapeIndex === editing?.shapeIndex
      )
        owner.decisions.delete(id);
    }
  }, [owner, s.editingClass]);
  return { acceptPrediction, rejectPrediction, acceptAll, commitClass, cancelClass };
}
