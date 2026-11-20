// 第 3 批 · 从 ThreeDWorkbench 抽出的 PSR 数值字段防抖落库管线。
// 改 PSR 数值面板 → parsePsrForm 校验 → 250ms 防抖 → PATCH 几何 + 入 history。
// 这是 PSR 编辑里唯一单一职责、单消费者(handleField)且不碰共享 form/setForm 的部分:
// 完整 usePsrEditor 因 form/setForm 被 gizmo handler / 一键操作 / 浮窗 commit / 渲染等
// 8 处共享(假边界)而无法干净切分,按计划 §7 "缩小范围" 只抽此管线。逐字搬运,行为零变化。
//
// 与 B 类其余 3D 簇不同:本管线无 Three.js/WebGL 依赖,可 fake timers 单测。
import { useCallback, useEffect, useRef, useState } from "react";
import type { AnnotationResponse } from "@/types";
import type { useUpdateAnnotation } from "@/hooks/useTasks";
import {
  geometryConvention,
  parsePsrForm,
  psrFormToGeometry,
  type PsrField,
} from "./ThreeDWorkbench.helpers";
import type { LidarAxisConvention } from "./geometry/axisConvention";
import type { useThreeDHistory } from "./useThreeDHistory";

interface UsePsrPatchPipelineParams {
  selectedId: string | null;
  selectedAnn: AnnotationResponse | null;
  axisConvention: LidarAxisConvention;
  updateAnnotation: ReturnType<typeof useUpdateAnnotation>;
  history: ReturnType<typeof useThreeDHistory>;
  form?: Record<PsrField, string> | null;
}

export function usePsrPatchPipeline({
  selectedId,
  selectedAnn,
  axisConvention,
  updateAnnotation,
  history,
  form,
}: UsePsrPatchPipelineParams) {
  const patchTimer = useRef<number | null>(null);
  // 已通过校验、尚未发出的防抖提交;flush 时同步执行,避免 unmount / 切 task 丢编辑。
  const pendingCommitRef = useRef<(() => void) | null>(null);
  const [hasPendingPatch, setHasPendingPatch] = useState(false);
  const [invalidDraft, setInvalidDraft] = useState(false);

  // 有未发出的防抖 PATCH 就立即执行一次,再清定时器(幂等:执行后置空 ref)。
  const flushPatch = useCallback(() => {
    if (patchTimer.current) {
      window.clearTimeout(patchTimer.current);
      patchTimer.current = null;
    }
    const commit = pendingCommitRef.current;
    pendingCommitRef.current = null;
    setHasPendingPatch(false);
    commit?.();
  }, []);

  // 卸载或切换选中对象前 flush:防止 250ms 防抖窗口内切 task / 关锁,把 pending 的
  // 几何 PATCH + history 一并丢弃(无任何提示的静默数据丢失)。
  useEffect(() => {
    setInvalidDraft(false);
    return flushPatch;
  }, [selectedId, flushPatch]);

  // 全部字段解析有效(尺寸>0)时防抖 PATCH;有空 / 非法字段则暂不提交(等用户输完)。
  const schedulePatch = useCallback(
    (f: Record<PsrField, string>) => {
      if (!selectedId) return;
      const { values: v, valid } = parsePsrForm(f);
      // Invalid input also supersedes the queued valid value; cleanup must not
      // commit geometry that the form no longer shows.
      if (patchTimer.current) window.clearTimeout(patchTimer.current);
      patchTimer.current = null;
      pendingCommitRef.current = null;
      setHasPendingPatch(valid);
      setInvalidDraft(!valid);
      if (!valid) return;
      // 把提交逻辑存进 ref,使 250ms 定时器与 flush(unmount/切换)走同一条路径,不重复落库。
      const commit = () => {
        const geometry = psrFormToGeometry(
          v,
          geometryConvention(selectedAnn?.geometry, axisConvention),
        );
        updateAnnotation.mutate({ annotationId: selectedId, payload: { geometry } });
        if (selectedAnn?.geometry?.type === "box_3d") {
          history.push({
            kind: "update",
            annotationId: selectedId,
            before: { geometry: selectedAnn.geometry },
            after: { geometry },
          });
        }
      };
      pendingCommitRef.current = commit;
      patchTimer.current = window.setTimeout(() => {
        patchTimer.current = null;
        pendingCommitRef.current = null;
        setHasPendingPatch(false);
        commit();
      }, 250);
    },
    [selectedId, updateAnnotation, selectedAnn?.geometry, axisConvention, history],
  );

  return {
    schedulePatch,
    hasPendingPatch,
    hasInvalidDraft: form === undefined ? invalidDraft : !!form && !parsePsrForm(form).valid,
    isSaving: updateAnnotation.isPending,
    saveError: updateAnnotation.error,
  };
}
