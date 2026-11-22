import { useCallback } from "react";
import type { Annotation, AnnotationResponse } from "@/types";
import type { AnnotationPayload } from "@/api/tasks";
import { canTranslateAnnotationGeometry, translateGeometry } from "./geometryTranslate";

interface UseClipboardArgs {
  userBoxes: Annotation[];
  selectedIds: string[];
  clipboard: Annotation[];
  setClipboard: (b: Annotation[]) => void;
  createAnnotation: (payload: AnnotationPayload) => Promise<AnnotationResponse>;
  pushBatch: (cmds: { kind: "create"; annotationId: string; payload: AnnotationPayload }[]) => void;
  setSelectedIds?: (ids: string[]) => void;
  imgW: number;
  imgH: number;
}

const PIXEL_OFFSET = 10;

export function useClipboard({
  userBoxes,
  selectedIds,
  clipboard,
  setClipboard,
  createAnnotation,
  pushBatch,
  setSelectedIds,
  imgW,
  imgH,
}: UseClipboardArgs) {
  const offX = imgW > 0 ? PIXEL_OFFSET / imgW : 0;
  const offY = imgH > 0 ? PIXEL_OFFSET / imgH : 0;

  const copyAnnotations = useCallback(
    (targets: Annotation[]) => {
      const supported = targets.filter(canTranslateAnnotationGeometry);
      if (supported.length === 0) return 0;
      setClipboard(supported);
      return supported.length;
    },
    [setClipboard],
  );

  const copySelection = useCallback(() => {
    if (selectedIds.length === 0) return 0;
    const targets = userBoxes.filter((b) => selectedIds.includes(b.id));
    return copyAnnotations(targets);
  }, [copyAnnotations, selectedIds, userBoxes]);

  const pasteFrom = useCallback(
    async (sources: Annotation[]) => {
      const supported = sources.filter(canTranslateAnnotationGeometry);
      if (supported.length === 0) return [];
      const cmds: { kind: "create"; annotationId: string; payload: AnnotationPayload }[] = [];
      const newIds: string[] = [];
      for (const b of supported) {
        const { geometry, annotationType } = translateGeometry(b, offX, offY);
        const payload: AnnotationPayload = {
          annotation_type: annotationType,
          class_name: b.cls,
          geometry,
          confidence: 1,
        };
        try {
          const created = await createAnnotation(payload);
          cmds.push({ kind: "create", annotationId: created.id, payload });
          newIds.push(created.id);
        } catch {
          /* 单条失败不阻塞其他 */
        }
      }
      if (cmds.length > 0) pushBatch(cmds);
      if (newIds.length > 0) setSelectedIds?.(newIds);
      return newIds;
    },
    [offX, offY, createAnnotation, pushBatch, setSelectedIds],
  );

  const paste = useCallback(() => pasteFrom(clipboard), [pasteFrom, clipboard]);

  const duplicateSelection = useCallback(() => {
    const targets = userBoxes.filter((b) => selectedIds.includes(b.id));
    return pasteFrom(targets);
  }, [userBoxes, selectedIds, pasteFrom]);

  return {
    copySelection,
    copyAnnotations,
    paste,
    duplicateSelection,
    hasClipboard: clipboard.some(canTranslateAnnotationGeometry),
  };
}
